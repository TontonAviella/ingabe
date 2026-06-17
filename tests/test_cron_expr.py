"""Tests for src.cron.cron_expr — the 5-field cron evaluator used by sage_alerts.

next_after(now) is the only thing sage_alerts actually calls; everything else
is parsing. We pin both:

  - parsing semantics (`*`, `*/N`, `A-B`, `A,B,C`, validation errors)
  - next_after semantics (strict > now, DOM/DOW AND vs OR vs *, UTC handling,
    field rollovers across minute/hour/day/month/year boundaries)

These match the cases sage_alerts will hit in production (Monday 06:00 UTC
BK Insurance digest, every-N-minutes test fires, etc.).
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from src.cron.cron_expr import CronExpr, next_fire_after


# ---------- _parse_field via CronExpr ----------


def test_star_expands_to_full_range() -> None:
    c = CronExpr("* * * * *")
    assert c.minute == set(range(0, 60))
    assert c.hour == set(range(0, 24))
    assert c.dom == set(range(1, 32))
    assert c.month == set(range(1, 13))
    assert c.dow == set(range(0, 7))


def test_literal_int() -> None:
    c = CronExpr("30 6 * * *")
    assert c.minute == {30}
    assert c.hour == {6}


def test_step_with_star_base() -> None:
    c = CronExpr("*/15 * * * *")
    assert c.minute == {0, 15, 30, 45}


def test_step_with_range_base() -> None:
    c = CronExpr("0-30/10 * * * *")
    assert c.minute == {0, 10, 20, 30}


def test_range() -> None:
    c = CronExpr("0 9-17 * * *")
    assert c.hour == set(range(9, 18))


def test_comma_list() -> None:
    c = CronExpr("0,15,30,45 * * * *")
    assert c.minute == {0, 15, 30, 45}


def test_combined_list_and_range() -> None:
    c = CronExpr("0 8,12,17-19 * * *")
    assert c.hour == {8, 12, 17, 18, 19}


# ---------- parse errors ----------


def test_wrong_field_count() -> None:
    with pytest.raises(ValueError, match="5 space-separated"):
        CronExpr("* * * *")


def test_minute_out_of_range() -> None:
    with pytest.raises(ValueError):
        CronExpr("60 * * * *")


def test_hour_out_of_range() -> None:
    with pytest.raises(ValueError):
        CronExpr("0 24 * * *")


def test_reverse_range_rejected() -> None:
    with pytest.raises(ValueError):
        CronExpr("0 17-9 * * *")


def test_zero_step_rejected() -> None:
    with pytest.raises(ValueError):
        CronExpr("*/0 * * * *")


# ---------- next_after: basic ----------


def test_next_after_requires_tz_aware() -> None:
    c = CronExpr("0 * * * *")
    with pytest.raises(ValueError, match="timezone-aware"):
        c.next_after(datetime(2026, 5, 12, 10, 30, 0))


def test_next_after_strictly_after_now() -> None:
    # If `now` matches, we must NOT return now — next match is the next hour.
    c = CronExpr("0 * * * *")
    now = datetime(2026, 5, 12, 10, 0, 0, tzinfo=timezone.utc)
    nxt = c.next_after(now)
    assert nxt == datetime(2026, 5, 12, 11, 0, tzinfo=timezone.utc)


def test_next_after_seconds_truncated() -> None:
    c = CronExpr("*/15 * * * *")
    now = datetime(2026, 5, 12, 10, 0, 30, tzinfo=timezone.utc)
    # 10:00:30 → next is 10:15:00, not 10:00:00
    assert c.next_after(now) == datetime(2026, 5, 12, 10, 15, tzinfo=timezone.utc)


def test_next_after_minute_rollover_to_next_hour() -> None:
    c = CronExpr("5 * * * *")
    now = datetime(2026, 5, 12, 10, 30, tzinfo=timezone.utc)
    assert c.next_after(now) == datetime(2026, 5, 12, 11, 5, tzinfo=timezone.utc)


def test_next_after_hour_rollover_to_next_day() -> None:
    c = CronExpr("0 6 * * *")
    now = datetime(2026, 5, 12, 7, 0, tzinfo=timezone.utc)
    assert c.next_after(now) == datetime(2026, 5, 13, 6, 0, tzinfo=timezone.utc)


def test_next_after_day_rollover_to_next_month() -> None:
    c = CronExpr("0 0 1 * *")  # first of each month at 00:00 UTC
    now = datetime(2026, 5, 12, 23, 30, tzinfo=timezone.utc)
    assert c.next_after(now) == datetime(2026, 6, 1, 0, 0, tzinfo=timezone.utc)


def test_next_after_year_rollover() -> None:
    c = CronExpr("0 0 1 1 *")  # Jan 1 00:00 UTC
    now = datetime(2026, 6, 1, 12, 0, tzinfo=timezone.utc)
    assert c.next_after(now) == datetime(2027, 1, 1, 0, 0, tzinfo=timezone.utc)


# ---------- next_after: dow/dom semantics ----------


def test_dow_only_matches_monday() -> None:
    # Cron DOW: 0=Sun..6=Sat → Monday=1
    c = CronExpr("0 6 * * 1")
    # 2026-05-12 is a Tuesday; next Monday 06:00 is 2026-05-18
    now = datetime(2026, 5, 12, 10, 0, tzinfo=timezone.utc)
    assert c.next_after(now) == datetime(2026, 5, 18, 6, 0, tzinfo=timezone.utc)


def test_dow_only_when_today_is_monday_morning() -> None:
    c = CronExpr("0 6 * * 1")
    # 2026-05-11 is a Monday. Now=05:00 → fire 06:00 same day.
    now = datetime(2026, 5, 11, 5, 0, tzinfo=timezone.utc)
    assert c.next_after(now) == datetime(2026, 5, 11, 6, 0, tzinfo=timezone.utc)


def test_dom_only() -> None:
    c = CronExpr("0 0 15 * *")  # 15th of every month
    now = datetime(2026, 5, 12, 10, 0, tzinfo=timezone.utc)
    assert c.next_after(now) == datetime(2026, 5, 15, 0, 0, tzinfo=timezone.utc)


def test_both_dom_and_dow_restricted_uses_OR() -> None:
    # Standard cron quirk: when BOTH dom and dow are restricted, it's an OR.
    # "Fire on the 1st OR on a Monday" — used rarely but must match the doc.
    c = CronExpr("0 0 1 * 1")
    # 2026-05-12 Tue 10:00 → next match is Mon 2026-05-18 (closer than the 1st of June)
    now = datetime(2026, 5, 12, 10, 0, tzinfo=timezone.utc)
    nxt = c.next_after(now)
    assert nxt == datetime(2026, 5, 18, 0, 0, tzinfo=timezone.utc)


def test_both_dom_and_dow_unrestricted_matches_every_day() -> None:
    c = CronExpr("0 12 * * *")
    now = datetime(2026, 5, 12, 13, 0, tzinfo=timezone.utc)
    # next is tomorrow noon
    assert c.next_after(now) == datetime(2026, 5, 13, 12, 0, tzinfo=timezone.utc)


# ---------- next_after: timezone normalization ----------


def test_non_utc_input_is_normalized_to_utc() -> None:
    from datetime import timedelta, timezone as tz

    c = CronExpr("0 * * * *")
    # 11:30 in UTC+2 == 09:30 UTC. Next fire is 10:00 UTC.
    plus2 = tz(timedelta(hours=2))
    now = datetime(2026, 5, 12, 11, 30, tzinfo=plus2)
    assert c.next_after(now) == datetime(2026, 5, 12, 10, 0, tzinfo=timezone.utc)


# ---------- module-level next_fire_after ----------


def test_module_helper_equivalent_to_class_method() -> None:
    now = datetime(2026, 5, 12, 10, 0, tzinfo=timezone.utc)
    expr = "*/30 * * * *"
    assert next_fire_after(expr, now) == CronExpr(expr).next_after(now)
