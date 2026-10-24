"""Sage and AI-model health counters for Prometheus (`/metrics`), kept in Redis.

Counted where they happen (src/routes/message_routes.py): every Sage turn and how it ended, how long it took, every
tool call and whether it finished, and every error from the AI model by kind. Redis holds them so they are shared by
all app workers and survive restarts (Prometheus counters must only go up). Counting never raises: a Redis outage
loses counts, not turns.

Also exported: the date the AI model's paid credit ends (SAGE_LLM_CREDIT_EXPIRES, YYYY-MM-DD in .env), so an alert
can warn before Sage stops answering.

Alerts on these live in monitoring/alerts.yml; the Sage dashboard in monitoring/grafana/dashboards/sage.json.
"""

from __future__ import annotations

import asyncio
import logging
import os
from datetime import datetime, timezone
from typing import Any, Optional

from src.dependencies.redis_client import get_async_redis

logger = logging.getLogger(__name__)

_KEY = "health_metrics:v1"  # one Redis hash: field "name|label=value,..." -> number
TURN_BUCKETS = (5.0, 15.0, 30.0, 60.0, 120.0, 300.0)  # seconds; a turn over 2 minutes is slow
REDIS_LIMIT_S = 0.5  # a Redis that hangs must not hold up a turn: past this the count is dropped

# Shown as 0 until first counted. Prometheus's increase() needs a sample before the first event, or the first
# credit refusal (or hanging turn) after a deploy or a Redis restart would never alert.
_ZERO_FIELDS = (
    [f"sage_turns_total|outcome={o}" for o in ("completed", "failed", "cancelled")]
    + [f"sage_turns_closed_unfinished_total|source={s}" for s in ("turn_end", "stop")]
    + [f"sage_llm_calls_total|outcome={o}" for o in ("ok", "error")]
    + [f"sage_llm_errors_total|kind={k},stage={st}"
       for k in ("credit", "auth", "rate_limit", "overloaded", "upstream_5xx", "context", "bad_request", "other")
       for st in ("attempt", "final")]
    + [f"frontend_map_events_total|event={e}"
       for e in ("map_load", "map_error", "tile_error", "deck_ready", "deck_error", "webgl_lost")]
)

_HELP = {
    "sage_turns_total": ("counter", "Sage turns by how they ended (completed, failed, stopped)"),
    "sage_turn_duration_seconds": ("histogram", "Time from a person's message to Sage's last word"),
    "sage_turns_closed_unfinished_total": ("counter", "Sage turns left hanging and closed for the person"),
    "sage_tool_calls_total": ("counter", "Sage tool calls by tool and outcome (ok, error, timeout)"),
    "sage_llm_calls_total": ("counter", "Calls to the AI model by outcome"),
    "sage_llm_errors_total": ("counter", "AI model errors by kind; stage=attempt is one try, stage=final ended the turn"),
    "frontend_map_events_total": ("counter", "Maps in people's browsers: loaded, failed, tile errors, deck.gl, lost graphics"),
}


def llm_error_kind(err: Any) -> str:
    """What kind of AI-model error this is, from its HTTP status and message (OpenAI-compatible SDK errors)."""
    status = getattr(err, "status_code", None)
    text = str(err).lower()
    if status == 402 or "credit balance" in text or "insufficient credit" in text or "billing" in text:
        return "credit"
    if status in (401, 403) or "authentication" in text or "invalid x-api-key" in text:
        return "auth"
    if status == 429 or "rate limit" in text:
        return "rate_limit"
    if status == 529 or "overloaded" in text:
        return "overloaded"
    if (status or 0) >= 500 or "provider returned error" in text:
        return "upstream_5xx"
    if "context length" in text or "context_length_exceeded" in text or "maximum context" in text or "context window" in text:
        return "context"
    if status == 400:
        return "bad_request"
    return "other"


def _field(name: str, labels: Optional[dict[str, str]] = None) -> str:
    inner = ",".join(f"{k}={v}" for k, v in sorted((labels or {}).items()))
    return f"{name}|{inner}"


def _redis():
    return get_async_redis()


async def count(name: str, labels: Optional[dict[str, str]] = None, by: int = 1) -> None:
    """Add `by` to a counter. Never raises."""
    try:
        r = _redis()
        if r is not None:
            await asyncio.wait_for(r.hincrby(_KEY, _field(name, labels), by), REDIS_LIMIT_S)
    except Exception:  # noqa: BLE001 - counting must never break or hold up a turn
        logger.debug("health metric %s not counted", name, exc_info=True)


async def observe(name: str, seconds: float, labels: Optional[dict[str, str]] = None) -> None:
    """Record one duration in a histogram with TURN_BUCKETS. Never raises."""
    try:
        r = _redis()
        if r is None:
            return
        pipe = r.pipeline(transaction=False)
        for le in TURN_BUCKETS:
            if seconds <= le:
                pipe.hincrby(_KEY, _field(f"{name}_bucket", {**(labels or {}), "le": str(le)}), 1)
        pipe.hincrby(_KEY, _field(f"{name}_bucket", {**(labels or {}), "le": "+Inf"}), 1)
        pipe.hincrbyfloat(_KEY, _field(f"{name}_sum", labels), seconds)
        pipe.hincrby(_KEY, _field(f"{name}_count", labels), 1)
        await asyncio.wait_for(pipe.execute(), REDIS_LIMIT_S)
    except Exception:  # noqa: BLE001
        logger.debug("health metric %s not observed", name, exc_info=True)


def _credit_expiry_line() -> list[str]:
    raw = os.environ.get("SAGE_LLM_CREDIT_EXPIRES", "").strip()
    if not raw:
        return []
    try:
        ends = datetime.strptime(raw, "%Y-%m-%d").replace(tzinfo=timezone.utc)
    except ValueError:
        logger.warning("SAGE_LLM_CREDIT_EXPIRES=%r is not YYYY-MM-DD; credit end not exported", raw)
        return []
    return [
        "# HELP sage_llm_credit_expires_timestamp_seconds When the AI model's paid credit ends (from .env)",
        "# TYPE sage_llm_credit_expires_timestamp_seconds gauge",
        f"sage_llm_credit_expires_timestamp_seconds {int(ends.timestamp())}",
    ]


def render(fields: dict[str, str]) -> list[str]:
    """Prometheus text lines for the stored counters (`fields` as read from the Redis hash)."""
    by_family: dict[str, list[str]] = {}
    fields = {**{f: "0" for f in _ZERO_FIELDS}, **fields}
    for field, value in sorted(fields.items()):
        name, _, inner = field.partition("|")
        family = next((f for f in _HELP if name == f or name.startswith(f + "_")), name)
        labels = ",".join(f'{k}="{v}"' for k, _, v in (p.partition("=") for p in inner.split(",") if p))
        by_family.setdefault(family, []).append(f"{name}{{{labels}}} {value}" if labels else f"{name} {value}")
    out: list[str] = []
    for family, lines in by_family.items():
        kind, text = _HELP.get(family, ("untyped", family))
        out += [f"# HELP {family} {text}", f"# TYPE {family} {kind}", *lines]
    return out


async def metric_lines() -> list[str]:
    """All lines for /metrics: the stored counters and the credit end date."""
    lines: list[str] = []
    try:
        r = _redis()
        if r is not None:
            raw = await asyncio.wait_for(r.hgetall(_KEY), 2 * REDIS_LIMIT_S)
            lines = render({(k.decode() if isinstance(k, bytes) else k): (v.decode() if isinstance(v, bytes) else v)
                            for k, v in raw.items()})
    except Exception:  # noqa: BLE001 - /metrics still answers with what it has
        logger.warning("health metrics could not be read from Redis", exc_info=True)
        lines = ["# HELP sage_metrics_unavailable 1 when the Sage counters could not be read",
                 "# TYPE sage_metrics_unavailable gauge", "sage_metrics_unavailable 1"]
    return lines + _credit_expiry_line()
