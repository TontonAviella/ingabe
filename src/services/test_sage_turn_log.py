"""Sage turn log: each turn's own record (built by the flight recorder with Langfuse off) and the Health page's
tables made from records."""
from __future__ import annotations

import json

import pytest

from src.services import sage_flight_recorder as fr
from src.services import sage_turn_log as log


@pytest.fixture
def saved(monkeypatch: pytest.MonkeyPatch) -> list[dict]:
    out: list[dict] = []
    monkeypatch.setattr(fr, "_tracer", None)
    monkeypatch.delenv("LANGFUSE_PUBLIC_KEY", raising=False)
    monkeypatch.setattr(log, "save_soon", lambda record: out.append(record))
    return out


def test_a_turn_is_recorded_without_langfuse(saved: list[dict]) -> None:
    tools = [{"type": "function", "function": {"name": "get_forecast"}}]
    with fr.sage_turn_trace(session_id="42", user_id="u1", metadata={"map_id": "M1", "partner_id": "P1"}) as turn:
        assert not turn.enabled
        turn.routing(user_text="rain in Huye?", reason="agri", categories=["agriculture"], small_talk=False,
                     tools=tools, shortlist="embed", model="m1")
        turn.generation(model="m1", messages=[], tools=tools, step=0).end(output="x")
        turn.tool_started("c1", "get_forecast", {"district": "Huye"})
        turn.tool_finished("c1", json.dumps({"status": "error", "error": "upstream timeout"}))
        turn.tool_started("c2", "get_forecast", {})
        turn.tool_finished("c2", json.dumps({"status": "error", "harness_layer": "H2", "error": "Missing district"}))
        turn.tool_started("c3", "get_forecast", {"district": "Huye"})
        turn.tool_finished("c3", json.dumps({"status": "error", "error": "stopped", "stopped_after_seconds": 120}))
        bad = turn.generation(model="m1", messages=[], tools=tools, step=1)
        bad.end(level="ERROR", status="429 too many requests")

    (rec,) = saved
    assert rec["map_id"] == "M1" and rec["partner_id"] == "P1" and rec["conversation_id"] == "42"
    assert rec["outcome"] == "completed" and rec["model"] == "m1"
    assert rec["routing"] == {"reason": "agri", "categories": ["agriculture"], "tool_count": 1, "shortlist": "embed"}
    kinds = [(s["kind"], s["name"], s["status"], s.get("rule")) for s in rec["steps"]]
    assert kinds == [("model", "m1", "ok", None), ("tool", "get_forecast", "error", None),
                     ("tool", "get_forecast", "error", "arguments"), ("tool", "get_forecast", "error", "time_limit"),
                     ("model", "m1", "error", None)]
    assert rec["steps"][1]["error"] == "upstream timeout"
    assert rec["steps"][4]["error"] == "429 too many requests"
    assert all(s["seconds"] is not None for s in rec["steps"])
    assert {"tool_error", "arguments", "time_limit"} <= set(rec["rules"])
    assert "rain in Huye" not in json.dumps(rec)  # no message text is kept


def test_a_crash_ends_open_steps_and_fails_the_turn(saved: list[dict]) -> None:
    with pytest.raises(RuntimeError):
        with fr.sage_turn_trace(session_id="1", user_id="u") as turn:
            turn.tool_started("c1", "slow_tool", {})
            raise RuntimeError("boom")
    (rec,) = saved
    assert rec["outcome"] == "failed" and rec["error"] == "RuntimeError: boom"
    assert rec["steps"][0]["status"] == "unfinished"
    assert "unfinished_step" in rec["rules"]


def test_rules_are_read_from_the_marks_in_tool_results() -> None:
    assert log.rule_of_tool_result({"harness_layer": "H4"}) == "repeat_blocked"
    assert log.rule_of_tool_result({"error_kind": "wrong_industry"}) == "wrong_industry"
    assert log.rule_of_tool_result({"stopped_after_seconds": 120}) == "time_limit"
    assert log.rule_of_tool_result({"status": "error", "error": "x"}) is None
    assert log.rule_of_tool_result("not a dict") is None
    assert log.rule_info("result_checked:no_data")[0] == "Known failure explained: no_data"
    assert log.rule_info("made_up") == ("made_up", "")


def _rec(at: str, steps: list[dict], rules: list[str], outcome: str = "completed") -> dict:
    return {"started_at": at, "seconds": 9.0, "outcome": outcome, "map_id": "M", "steps": steps, "rules": rules}


def test_summary_tables_per_tool_rule_and_turn() -> None:
    records = [  # newest first, as read from the table
        _rec("2026-10-25T12:00:00+00:00", [
            {"kind": "model", "name": "m", "seconds": 3.0, "status": "ok"},
            {"kind": "tool", "name": "ndvi", "seconds": 2.0, "status": "error", "error": "no scenes"},
            {"kind": "tool", "name": "ndvi", "seconds": 120.0, "status": "error", "error": "stopped",
             "rule": "time_limit"},
        ], ["tool_error", "time_limit"]),
        _rec("2026-10-25T11:00:00+00:00", [
            {"kind": "tool", "name": "ndvi", "seconds": 4.0, "status": "ok"},
            {"kind": "tool", "name": "rain", "seconds": 1.0, "status": "ok"},
        ], ["tool_error"]),
    ]
    out = log.summarize(records)
    ndvi = next(t for t in out["tools"] if t["tool"] == "ndvi")
    assert (ndvi["calls"], ndvi["ok"], ndvi["failed"], ndvi["time_limit"]) == (3, 1, 1, 1)
    assert ndvi["median_s"] == 3.0  # the stopped call's 120 s is not a run time
    assert ndvi["last_error"] == "no scenes" and ndvi["last_error_at"] == "2026-10-25T12:00:00+00:00"
    assert out["tools"][0]["tool"] == "ndvi"  # problems first
    rules = {r["rule"]: r for r in out["rules"]}
    assert rules["tool_error"]["turns"] == 2 and rules["time_limit"]["turns"] == 1
    assert rules["time_limit"]["name"] == "Tool time limit"
    first = out["turns"][0]
    assert first["model_s"] == 3.0 and first["tools_s"] == 122.0
    assert first["steps"] == "AI model ✓ 3.0 s → ndvi ✗ 2.0 s → ndvi ✗ 120.0 s (Tool time limit)"
    assert first["error"] == "ndvi: no scenes; ndvi: stopped"


@pytest.mark.anyio
async def test_a_saved_turn_comes_back_in_the_summary() -> None:
    import uuid
    from datetime import datetime, timezone

    from src.database.pool import get_async_db_connection

    tool = f"tool_{uuid.uuid4().hex[:8]}"
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    record = _rec(now, [{"kind": "tool", "name": tool, "seconds": 1.5, "status": "error", "error": "no data",
                         "rule": "arguments"}], ["arguments", "tool_error"])
    record["conversation_id"] = tool
    await log.save(record)
    try:
        out = await log.recent_summary(days=1)
        row = next(t for t in out["tools"] if t["tool"] == tool)
        assert (row["calls"], row["refused"]) == (1, 1)
        assert any(t["conversation_id"] == tool for t in out["turns"])
    finally:
        async with get_async_db_connection() as conn:
            await conn.execute("DELETE FROM sage_turn_log WHERE conversation_id = $1", tool)
