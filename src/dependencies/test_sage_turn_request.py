"""Sage's per-turn request: tool catalog snapshot and routing plan."""
from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import pytest

from src.dependencies.pydantic_tools import get_pydantic_tool_calls
from src.dependencies.sage_routing import FAST_PATH_TOOLS, SMALL_TALK_SYSTEM_PROMPT
from src.dependencies.sage_turn_request import build_sage_tools_payload, plan_sage_turn
from src.dependencies import sage_turn_request

CATALOG = Path(__file__).resolve().parents[2] / "evals" / "sage_routing" / "tool_catalog.json"


def _names(tools: list[dict]) -> list[str]:
    return [t["function"]["name"] for t in tools]


def _eval_runner():
    path = Path(__file__).resolve().parents[2] / "scripts" / "eval_sage_routing.py"
    spec = importlib.util.spec_from_file_location("eval_sage_routing", path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module  # @dataclass resolves its module via sys.modules
    spec.loader.exec_module(module)
    return module


def test_tool_catalog_matches_eval_snapshot() -> None:
    """The routing eval's labels and labelling rules use this snapshot.

    If this fails, a tool was added, renamed, removed, or its coordinate /
    geometry parameters changed: update the labels in evals/sage_routing/cases,
    then run `python scripts/eval_sage_routing.py catalog --write`.
    """
    snapshot = json.loads(CATALOG.read_text())
    assert _eval_runner().live_tool_catalog() == snapshot
    assert sorted(FAST_PATH_TOOLS) == snapshot["fast_path_tools"]


def test_layer_enum_is_kept_only_when_layers_exist() -> None:
    def layer_param(tools: list[dict]) -> dict:
        tool = next(t for t in tools if t["function"]["name"] == "add_layer_to_map")
        return tool["function"]["parameters"]["properties"]["layer_id"]

    calls = get_pydantic_tool_calls()
    assert "enum" not in layer_param(build_sage_tools_payload(calls, {}))
    assert layer_param(build_sage_tools_payload(calls, {"L1": "Field"}))["enum"] == ["L1"]


def test_small_talk_sends_no_tools_and_skips_the_full_prompt() -> None:
    called = []
    plan = plan_sage_turn("hi", [{"role": "user", "content": "hi"}],
                          [{"function": {"name": "x"}}], lambda: called.append(1) or "FULL")
    assert plan.tools == [] and plan.tool_choice is None
    assert plan.system_prompt == SMALL_TALK_SYSTEM_PROMPT
    assert called == []


def test_domain_turn_gets_full_prompt_and_auto_tool_choice() -> None:
    tools = build_sage_tools_payload(get_pydantic_tool_calls(), {})
    text = "what is the NDVI of my fields in Huye"
    plan = plan_sage_turn(text, [{"role": "user", "content": text}], tools, lambda: "FULL PROMPT")
    assert plan.system_prompt.startswith("FULL PROMPT")
    assert plan.tools and plan.tool_choice == "auto"
    assert set(_names(plan.tools)) <= set(_names(tools))


# --- per-turn tool shortlist ------------------------------------------------

def test_tool_shortlist_k_parses_strictly(monkeypatch) -> None:
    from src.dependencies.sage_turn_request import tool_shortlist_k

    monkeypatch.delenv("SAGE_TOOL_SHORTLIST_K", raising=False)
    assert tool_shortlist_k() == 0
    monkeypatch.setenv("SAGE_TOOL_SHORTLIST_K", "15")
    assert tool_shortlist_k() == 15
    for bad in ("ten", "-1"):
        monkeypatch.setenv("SAGE_TOOL_SHORTLIST_K", bad)
        with pytest.raises(ValueError):
            tool_shortlist_k()


@pytest.mark.asyncio
async def test_apply_tool_shortlist_ranks_the_full_catalog_and_skips_small_talk() -> None:
    from src.dependencies.sage_turn_request import apply_tool_shortlist

    tools = build_sage_tools_payload(get_pydantic_tool_calls(), {})
    text = "NDVI by sector in Huye"
    plan = plan_sage_turn(text, [{"role": "user", "content": text}], tools, lambda: "P")
    shortlisted = await apply_tool_shortlist(plan, text, [], tools, k=10, embed=None)
    names = _names(shortlisted.tools)
    assert len(names) == 10 and shortlisted.shortlist == "bm25"
    # The category filter drops this tool for this request; the shortlist keeps it.
    assert "get_cell_ndvi_stats" in names and "get_cell_ndvi_stats" not in _names(plan.tools)

    small_talk = plan_sage_turn("hi", [{"role": "user", "content": "hi"}], tools, lambda: "P")
    assert await apply_tool_shortlist(small_talk, "hi", [], tools, k=10, embed=None) is small_talk


def test_bm25_shortlist_recall_on_eval_cases_stays_high() -> None:
    """Regression floor, offline and deterministic (BM25 only, no embeddings):
    for eval cases that reach the model, the top-15 shortlist must contain an
    accepted first tool in >= 97% of them (99.5% when written)."""
    from evals.sage_routing import scoring
    from src.dependencies.sage_routing import FAST_PATH_TOOLS, build_fast_tool_call
    from src.services.sage_tool_shortlist import shortlist_tools

    eval_dir = Path(__file__).resolve().parents[2] / "evals" / "sage_routing"
    catalog = json.loads((eval_dir / "tool_catalog.json").read_text())
    tools = build_sage_tools_payload(get_pydantic_tool_calls(), {})
    hits = total = 0
    for path in sorted((eval_dir / "cases").glob("*.jsonl")):
        for line in path.read_text().splitlines():
            if not line.strip():
                continue
            case = scoring.effective_case(json.loads(line), catalog)
            accepted = set(case["expect"].get("any_of") or [])
            fast = build_fast_tool_call(case["text"])
            if not accepted or (fast and fast.tool_name in FAST_PATH_TOOLS):
                continue
            history = case.get("history") or []
            plan = plan_sage_turn(case["text"], history + [{"role": "user", "content": case["text"]}],
                                  tools, lambda: "P")
            if not plan.tools:
                continue
            total += 1
            hits += bool(accepted & set(_names(shortlist_tools(case["text"], history, tools, k=15))))
    assert total > 150
    assert hits / total >= 0.97, f"{hits}/{total}"


# --- abdication guard ------------------------------------------------------

def _plan_for(text: str):
    tools = build_sage_tools_payload(get_pydantic_tool_calls(), {})
    return plan_sage_turn(text, [{"role": "user", "content": text}], tools, lambda: "P")


@pytest.mark.parametrize(
    ("text", "reply", "has_tools", "expected"),
    [
        ("will it rain in Musanze next week?", "I can check the forecast for Musanze for you.", False, True),
        ("will it rain in Musanze next week?", "", False, True),
        ("will it rain in Musanze next week?", "Calling the forecast.", True, False),   # tool called
        ("hi", "Hello! How can I help?", False, False),                                # small talk
        ("what is NDVI?", "NDVI is a vegetation index...", False, False),              # explanation
        ("explain the payout trigger", "A payout trigger is ...", False, False),
        ("create management zones for this field", "Which field should I use?", False, False),  # clarify
    ],
)
def test_is_abdication(text: str, reply: str, has_tools: bool, expected: bool) -> None:
    from src.dependencies.sage_turn_request import is_abdication

    assert is_abdication(_plan_for(text), text, reply, has_tools) is expected


def test_abdication_guard_flag(monkeypatch) -> None:
    from src.dependencies.sage_turn_request import abdication_guard_enabled

    monkeypatch.delenv("SAGE_ABDICATION_GUARD", raising=False)
    assert abdication_guard_enabled() is False
    monkeypatch.setenv("SAGE_ABDICATION_GUARD", "1")
    assert abdication_guard_enabled() is True


@pytest.mark.asyncio
async def test_guard_tools_are_a_short_ranked_list() -> None:
    from src.dependencies.sage_turn_request import GUARD_TOOL_COUNT, guard_tools

    tools = build_sage_tools_payload(get_pydantic_tool_calls(), {})
    picked = await guard_tools("will it rain in Musanze next week?", [], tools, embed=None)
    assert len(picked) == GUARD_TOOL_COUNT
    assert "get_forecast" in _names(picked)


class _RateLimitError(Exception):
    def __init__(self, message: str, *, status: int = 429, body=None, headers=None) -> None:
        super().__init__(message)
        self.status_code = status
        self.body = body
        self.response = type("R", (), {"headers": headers or {}})()


def test_rate_limit_retry_after_reads_openrouter_reset_from_the_body() -> None:
    import time as _time

    reset_ms = str(int((_time.time() + 4) * 1000))
    err = _RateLimitError(
        "Rate limit exceeded: free-models-per-min.",
        body={"metadata": {"headers": {"X-RateLimit-Limit": "20", "X-RateLimit-Reset": reset_ms}}},
    )
    wait = sage_turn_request.rate_limit_retry_after(err)
    assert wait is not None and 2.5 <= wait <= 4.5


def test_rate_limit_retry_after_prefers_retry_after_and_caps_it() -> None:
    assert sage_turn_request.rate_limit_retry_after(
        _RateLimitError("too many", headers={"Retry-After": "3"})) == 3.0
    assert sage_turn_request.rate_limit_retry_after(
        _RateLimitError("too many", headers={"retry-after": "600"})) == 15.0
    assert sage_turn_request.rate_limit_retry_after(_RateLimitError("too many")) == 5.0


def test_rate_limit_retry_after_ignores_daily_caps_and_other_errors() -> None:
    assert sage_turn_request.rate_limit_retry_after(
        _RateLimitError("Rate limit exceeded: free-models-per-day.")) is None
    assert sage_turn_request.rate_limit_retry_after(_RateLimitError("boom", status=500)) is None
    assert sage_turn_request.rate_limit_retry_after(ValueError("nope")) is None


def test_rate_limit_user_message_explains_daily_and_minute_limits() -> None:
    daily = sage_turn_request.rate_limit_user_message(
        _RateLimitError("Rate limit exceeded: free-models-per-day-high-balance."))
    assert daily and "02:00 Kigali" in daily
    minute = sage_turn_request.rate_limit_user_message(
        _RateLimitError("Rate limit exceeded: free-models-per-min."))
    assert minute and "in a minute" in minute
    assert sage_turn_request.rate_limit_user_message(_RateLimitError("boom", status=500)) is None
