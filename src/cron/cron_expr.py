"""Minimal 5-field cron expression evaluator.

Format: `minute hour day-of-month month day-of-week` (all UTC).
Each field accepts:
- `*` (any)
- literal int (e.g., `30`)
- step `*/N` (every N)
- range `A-B`
- list `A,B,C`

Day-of-week: 0=Sun..6=Sat. Day-of-month and day-of-week are OR'd when both
are restricted. If only one is restricted, that restricted field controls the
match. This matches the standard cron behavior used by this evaluator.

Why inline instead of `croniter`: this worker needs only `next_fire_after(now)`,
the expressions we use are simple, and adding a dependency for ~50 LOC isn't
worth it.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Iterable, Set


_RANGES = {
    "minute": (0, 59),
    "hour": (0, 23),
    "dom": (1, 31),
    "month": (1, 12),
    "dow": (0, 6),
}


def _parse_field(text: str, lo: int, hi: int) -> Set[int]:
    text = text.strip()
    if text == "*":
        return set(range(lo, hi + 1))
    out: Set[int] = set()
    for part in text.split(","):
        step = 1
        if "/" in part:
            base, step_s = part.split("/", 1)
            step = int(step_s)
            part = base
        if part == "*":
            start, end = lo, hi
        elif "-" in part:
            a, b = part.split("-", 1)
            start, end = int(a), int(b)
        else:
            start = end = int(part)
        if start < lo or end > hi or start > end or step < 1:
            raise ValueError(f"invalid cron field part: {part}")
        out.update(range(start, end + 1, step))
    if not out:
        raise ValueError("cron field produced empty set")
    return out


class CronExpr:
    """Parsed 5-field cron expression. UTC only."""

    def __init__(self, expr: str) -> None:
        parts = expr.split()
        if len(parts) != 5:
            raise ValueError(
                f"cron expr must have 5 space-separated fields, got {len(parts)}: {expr!r}"
            )
        self.expr = expr
        self.minute = _parse_field(parts[0], *_RANGES["minute"])
        self.hour = _parse_field(parts[1], *_RANGES["hour"])
        self.dom = _parse_field(parts[2], *_RANGES["dom"])
        self.month = _parse_field(parts[3], *_RANGES["month"])
        self.dow = _parse_field(parts[4], *_RANGES["dow"])
        self._dom_restricted = parts[2].strip() != "*"
        self._dow_restricted = parts[4].strip() != "*"

    def _day_match(self, when: datetime) -> bool:
        # Python's weekday(): Mon=0..Sun=6. Cron: Sun=0..Sat=6.
        cron_dow = (when.weekday() + 1) % 7
        dom_ok = when.day in self.dom
        dow_ok = cron_dow in self.dow
        if self._dom_restricted and self._dow_restricted:
            return dom_ok or dow_ok  # standard cron OR when both restricted
        if self._dom_restricted:
            return dom_ok
        if self._dow_restricted:
            return dow_ok
        return True  # both are *

    def next_after(self, now: datetime) -> datetime:
        """Smallest datetime strictly greater than `now` that matches this expr.

        `now` must be timezone-aware. Returns a UTC datetime.
        """
        if now.tzinfo is None:
            raise ValueError("now must be timezone-aware")
        when = now.astimezone(timezone.utc).replace(second=0, microsecond=0) + timedelta(minutes=1)
        # Cap search at ~4 years to avoid runaway on impossible expressions.
        for _ in range(60 * 24 * 366 * 4):
            if (
                when.month in self.month
                and self._day_match(when)
                and when.hour in self.hour
                and when.minute in self.minute
            ):
                return when
            when += timedelta(minutes=1)
        raise ValueError(f"cron expr {self.expr!r} produced no fire time within 4 years")


def next_fire_after(expr: str, now: datetime) -> datetime:
    return CronExpr(expr).next_after(now)
