"""Unit tests for ingestion concurrency/budget guards.

No DB or network needed. These cover the P4 eng-review fix: OCR rate
limits and daily budget must reject work BEFORE it starts, not after.
"""

from __future__ import annotations

import asyncio

import pytest

from src.services.brain_ingestion.concurrency import (
    DailyBudgetExceeded,
    OCR_DAILY_PAGE_CEILING,
    OCR_HOURLY_REQUEST_CEILING,
    OCR_MAX_CONCURRENCY,
    PER_SOURCE_DAILY_FRACTION,
    PerSourceCapExceeded,
    RateBucket,
    estimate_cost_usd,
    ocr_semaphore,
    queue_depth_alert,
    rate_limit_acquire,
    reserve_ocr_budget,
)

# Only the budget-reservation tests are async; sync tests don't need the mark.
_asyncio = pytest.mark.asyncio(loop_scope="session")


class _FakeRedis:
    """Just enough for reserve_ocr_budget's INCRBY / DECRBY / EXPIRE."""

    def __init__(self) -> None:
        self.values: dict[str, int] = {}

    async def incrby(self, key: str, n: int) -> int:
        self.values[key] = self.values.get(key, 0) + n
        return self.values[key]

    async def decrby(self, key: str, n: int) -> int:
        self.values[key] = self.values.get(key, 0) - n
        return self.values[key]

    async def expire(self, key: str, ttl: int) -> None:
        return None


def test_cost_estimate_monotonic():
    assert estimate_cost_usd(0) == 0
    assert estimate_cost_usd(100) > 0
    assert estimate_cost_usd(500) > estimate_cost_usd(100)


def test_ocr_semaphore_is_hard_capped():
    assert ocr_semaphore._value == OCR_MAX_CONCURRENCY  # type: ignore[attr-defined]
    assert OCR_MAX_CONCURRENCY == 4


def test_queue_depth_alert_threshold():
    assert queue_depth_alert(0) is False
    assert queue_depth_alert(1999) is False
    assert queue_depth_alert(2000) is True
    assert queue_depth_alert(10000) is True


def test_rate_bucket_refills_over_time():
    b = RateBucket.new(capacity=10, window_seconds=1)
    # Drain
    for _ in range(10):
        assert b.try_acquire(1.0)
    assert b.try_acquire(1.0) is False

    # Force refill by fast-forwarding the clock via last_refill rewind.
    b.last_refill -= 1.0
    assert b.try_acquire(1.0) is True


@_asyncio
async def test_reserve_budget_blocks_over_global_ceiling():
    r = _FakeRedis()
    # Fill the global ceiling by spreading across enough sources to avoid
    # hitting the per-source cap (40% = 2000 pages). Three sources at
    # 2000+2000+1000 = 5000 = ceiling.
    per_source_cap = int(OCR_DAILY_PAGE_CEILING * PER_SOURCE_DAILY_FRACTION)
    remaining = OCR_DAILY_PAGE_CEILING
    src_idx = 0
    while remaining > 0:
        chunk = min(per_source_cap, remaining)
        await reserve_ocr_budget(r, f"src-{src_idx}", chunk)
        remaining -= chunk
        src_idx += 1
    # One more page must fail — global ceiling is full.
    with pytest.raises(DailyBudgetExceeded):
        await reserve_ocr_budget(r, "src-last", 1)


@_asyncio
async def test_reserve_budget_blocks_over_per_source_cap():
    r = _FakeRedis()
    per_source_cap = int(OCR_DAILY_PAGE_CEILING * PER_SOURCE_DAILY_FRACTION)
    # Reserve up to the per-source cap.
    await reserve_ocr_budget(r, "src-greedy", per_source_cap)
    # One more page from the same source must fail.
    with pytest.raises(PerSourceCapExceeded):
        await reserve_ocr_budget(r, "src-greedy", 1)
    # Another source can still make progress — global headroom remains.
    await reserve_ocr_budget(r, "src-other", 10)


@_asyncio
async def test_reserve_budget_decrements_on_overshoot():
    """If a reservation fails, the counter must be rolled back so retries
    (e.g. next minute) see the real remaining headroom, not a phantom
    consumption.
    """
    r = _FakeRedis()
    # Fill global to 10 under ceiling, spread across sources so no one
    # source exceeds the 40% per-source cap.
    per_source_cap = int(OCR_DAILY_PAGE_CEILING * PER_SOURCE_DAILY_FRACTION)
    target = OCR_DAILY_PAGE_CEILING - 10
    src_idx = 0
    while target > 0:
        chunk = min(per_source_cap, target)
        await reserve_ocr_budget(r, f"src-fill-{src_idx}", chunk)
        target -= chunk
        src_idx += 1

    # A 100-page burst would overshoot global (only 10 pages headroom).
    with pytest.raises(DailyBudgetExceeded):
        await reserve_ocr_budget(r, "src-z", 100)

    # After the failed reservation, a 5-page reservation should succeed
    # (global still has 10 pages headroom — rollback worked).
    await reserve_ocr_budget(r, "src-z", 5)
