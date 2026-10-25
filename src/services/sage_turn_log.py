"""Sage turn log: one row per Sage turn saying what Sage did, for the Ingabe Health page.

The flight recorder (src/services/sage_flight_recorder.py) builds each turn's record as the turn runs: the steps in
order (each model call and tool call, how long it took, whether it worked and the error), the rules that stepped in,
and how the turn ended. This module owns the rules' names and meanings, saves records (table sage_turn_log, kept
RETENTION_DAYS) and turns them into the Health page's tables: per tool, per rule, and the latest turns.

No message text is kept: tools, timings, statuses, errors and ids only. Saving never raises into a turn.
"""

from __future__ import annotations

import asyncio
import json
import logging
import statistics
from collections import Counter, defaultdict
from datetime import datetime
from typing import Any, Mapping, Optional

from src.structures import async_conn

logger = logging.getLogger(__name__)

RETENTION_DAYS = 90
MAX_STEPS = 60  # per turn; a turn has at most 25 model steps (message_routes), each with a few tools
_ERROR_CHARS = 300

# Every rule that can step into a Sage turn: (name, what it does, in plain words). A record lists rule ids; the page
# shows the name and meaning. Ids ending in ":" are prefixes (the part after it names the case).
RULES: dict[str, tuple[str, str]] = {
    "time_limit": ("Tool time limit", "A tool that runs past its time limit is stopped; Sage answers with what the "
                   "other tools found and says which part is missing."),
    "arguments": ("Tool arguments checked", "A tool call with missing, wrong or unknown arguments is sent back to Sage "
                  "to correct before it runs (Life-Harness H2)."),
    "repeat_blocked": ("Repeated call blocked", "The same tool call with the same arguments three times in a row is "
                       "refused; Sage must use the result it has or ask (Life-Harness H4)."),
    "wrong_industry": ("Other industry's tool refused", "A tool for another industry (for example tower tools in a "
                       "farm project) or an unknown tool name is refused."),
    "result_checked:": ("Known failure explained", "A tool failure Ingabe recognises is given the facts to fix it "
                        "before Sage sees it (the part after the colon names the failure)."),
    "abdication": ("Answered without tools", "Sage answered in words where a tool was needed."),
    "guard_fired": ("Asked to use the tool", "After answering without tools, Sage was asked once to call the tool "
                    "(abdication guard)."),
    "guard_recovered": ("Used the tool when asked", "After the abdication guard, Sage did call a tool."),
    "step_limit": ("Step limit reached", "A turn may take at most 25 model steps; this one reached the last."),
    "fallback_model": ("Backup model answered", "The main AI model failed and a backup model answered."),
    "rate_limited": ("AI model busy", "The AI model refused with 'too many requests'."),
    "llm_error": ("AI model error", "The AI model returned an error on this turn."),
    "tool_error": ("A tool failed", "At least one tool returned an error."),
    "unfinished_step": ("Turn ended mid-step", "The turn ended while a model or tool step was still running."),
    "cancelled": ("Stopped by the person", "The person pressed Stop."),
    "fast_path:": ("Answered without the AI model", "A fixed route answered (the part after the colon names it)."),
    "shortlist_bm25_fallback": ("Tool list by keywords only", "Choosing which tools to offer could not use "
                                "meaning (embeddings), only keywords, so a needed tool may not have been offered."),
    "small_talk": ("Small talk", "A greeting or chat; no tools offered."),
    "shortlist:": ("Tool list method", "How the tools offered to Sage were chosen (the part after the colon)."),
    "steps_truncated": ("Long turn", f"The turn had more than {MAX_STEPS} steps; only the first were recorded."),
}


def rule_info(rule: str) -> tuple[str, str]:
    """(name, meaning) of a rule id, including prefixed ids such as result_checked:<kind>."""
    if rule in RULES:
        return RULES[rule]
    head, _, case = rule.partition(":")
    name, meaning = RULES.get(f"{head}:", (rule, ""))
    return (f"{name}: {case}" if case else name), meaning


def rule_of_tool_result(result: Any) -> Optional[str]:
    """Which rule produced a tool result instead of (or after) running the tool, from the marks the rules put in it
    (life_harness: harness_layer; industry.tool_refusal: error_kind; the tool time limit: stopped_after_seconds)."""
    if not isinstance(result, Mapping):
        return None
    if result.get("stopped_after_seconds") is not None:
        return "time_limit"
    if result.get("error_kind") == "wrong_industry":
        return "wrong_industry"
    return {"H2": "arguments", "H4": "repeat_blocked"}.get(str(result.get("harness_layer") or ""))


def clip_error(text: Any) -> Optional[str]:
    if text is None:
        return None
    text = " ".join(str(text).split())
    return text if len(text) <= _ERROR_CHARS else text[: _ERROR_CHARS - 1] + "…"


async def save(record: Mapping[str, Any]) -> None:
    """Store one turn and drop turns older than RETENTION_DAYS. Logs and returns on any failure."""
    try:
        async with async_conn("sage_turn_log.save") as conn:
            await conn.execute(
                """
                INSERT INTO sage_turn_log (started_at, seconds, outcome, map_id, conversation_id, partner_id, rules,
                                           record)
                VALUES ($1, $2, $3, $4, $5, $6, $7, $8::jsonb)
                """,
                datetime.fromisoformat(record["started_at"]), float(record["seconds"]), record["outcome"],
                record.get("map_id"), record.get("conversation_id"), record.get("partner_id"),
                list(record.get("rules") or []), json.dumps(record, default=str),
            )
            await conn.execute(
                f"DELETE FROM sage_turn_log WHERE started_at < now() - interval '{RETENTION_DAYS} days'")
    except Exception:
        logger.warning("sage turn log: could not save a turn", exc_info=True)


_pending: set[asyncio.Task] = set()


def save_soon(record: Mapping[str, Any]) -> None:
    """Save in the background on the running event loop (the turn does not wait); nothing when there is none."""
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return
    task = loop.create_task(save(record))
    _pending.add(task)
    task.add_done_callback(_pending.discard)


def _pct(values: list[float], q: float) -> Optional[float]:
    if not values:
        return None
    if len(values) == 1:
        return round(values[0], 1)
    return round(statistics.quantiles(values, n=100, method="inclusive")[int(q) - 1], 1)


def summarize(records: list[Mapping[str, Any]], *, recent: int = 60) -> dict[str, list[dict[str, Any]]]:
    """The Health page's Sage tables from turn records (newest first):
    - tools: per tool, calls and how they ended (ok, failed, stopped by the time limit, refused by a rule), median and
      95th-percentile seconds, and the latest error with when it happened;
    - rules: per rule, how often it stepped in (turns), its meaning, and the latest turn it did;
    - turns: the latest turns, each with its steps in order as one line, time in the model vs in tools, rules, error."""
    tools: dict[str, dict[str, Any]] = defaultdict(lambda: {"calls": 0, "ok": 0, "failed": 0, "time_limit": 0,
                                                            "refused": 0, "seconds": [], "last_error": None,
                                                            "last_error_at": None})
    rule_turns: Counter[str] = Counter()
    rule_last: dict[str, Mapping[str, Any]] = {}
    turns = []
    for rec in records:
        at = rec.get("started_at")
        for step in rec.get("steps") or []:
            if step.get("kind") != "tool":
                continue
            t = tools[step.get("name") or "?"]
            t["calls"] += 1
            rule = step.get("rule")
            if rule == "time_limit":
                t["time_limit"] += 1
            elif rule:
                t["refused"] += 1
            elif step.get("status") == "ok":
                t["ok"] += 1
            else:
                t["failed"] += 1
            if not rule and step.get("seconds") is not None:
                t["seconds"].append(float(step["seconds"]))
            if step.get("error") and t["last_error_at"] is None:  # records come newest first
                t["last_error"], t["last_error_at"] = step["error"], at
        for rule in set(rec.get("rules") or []):
            rule_turns[rule] += 1
            rule_last.setdefault(rule, rec)
        if len(turns) < recent:
            turns.append(_turn_row(rec))

    tool_rows = []
    for name, t in tools.items():
        tool_rows.append({
            "tool": name, "calls": t["calls"], "ok": t["ok"], "failed": t["failed"], "time_limit": t["time_limit"],
            "refused": t["refused"], "failed_share": round((t["failed"] + t["time_limit"]) / t["calls"], 3),
            "median_s": _pct(t["seconds"], 50), "p95_s": _pct(t["seconds"], 95),
            "last_error": t["last_error"], "last_error_at": t["last_error_at"],
        })
    tool_rows.sort(key=lambda r: (-(r["failed"] + r["time_limit"] + r["refused"]), -r["calls"], r["tool"]))
    rule_rows = []
    for rule, n in rule_turns.most_common():
        name, meaning = rule_info(rule)
        last = rule_last[rule]
        rule_rows.append({"rule": rule, "name": name, "meaning": meaning, "turns": n,
                          "last_at": last.get("started_at"), "last_map_id": last.get("map_id")})
    return {"tools": tool_rows, "rules": rule_rows, "turns": turns}


def _turn_row(rec: Mapping[str, Any]) -> dict[str, Any]:
    parts, model_s, tool_s, errors = [], 0.0, 0.0, []
    for step in rec.get("steps") or []:
        secs = step.get("seconds")
        mark = "✓" if step.get("status") == "ok" else "✗"
        label = step.get("name") or "?"
        if step.get("kind") == "model":
            model_s += secs or 0
            label = "AI model"
        else:
            tool_s += secs or 0
        why = f" ({rule_info(step['rule'])[0]})" if step.get("rule") else ""
        parts.append(f"{label} {mark}{'' if secs is None else f' {secs:.1f} s'}{why}")
        if step.get("error"):
            errors.append(f"{label}: {step['error']}")
    routing = rec.get("routing") or {}
    return {
        "started_at": rec.get("started_at"), "outcome": rec.get("outcome"), "seconds": rec.get("seconds"),
        "model_s": round(model_s, 1), "tools_s": round(tool_s, 1),
        "offered_tools": routing.get("tool_count"), "routing": routing.get("reason"),
        "fast_path": rec.get("fast_path"), "model": rec.get("model"),
        "steps": " → ".join(parts) or "(no steps)",
        "rules": ", ".join(rule_info(r)[0] for r in rec.get("rules") or []),
        "error": rec.get("error") or ("; ".join(errors[:3]) if errors else None),
        "map_id": rec.get("map_id"), "project_id": rec.get("project_id"),
        "conversation_id": rec.get("conversation_id"),
    }


async def recent_summary(days: int = 7) -> dict[str, Any]:
    """summarize() over the last `days` days of turns, read from the table."""
    async with async_conn("sage_turn_log.recent") as conn:
        # The map's project, so the page can open the conversation (maps are read here as message_routes does).
        rows = await conn.fetch(
            """
            SELECT t.record::text AS record, m.project_id
            FROM sage_turn_log t LEFT JOIN user_mundiai_maps m ON m.id = t.map_id
            WHERE t.started_at > now() - make_interval(days => $1)
            ORDER BY t.started_at DESC LIMIT 5000
            """, days)
    records = [{**json.loads(r["record"]), "project_id": r["project_id"]} for r in rows]
    out: dict[str, Any] = summarize(records)
    out["turn_count"] = len(records)
    out["days"] = days
    return out
