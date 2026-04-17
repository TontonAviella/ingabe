"""Concurrency + rate + cost guards for ingestion.

Implements the P4 fix from the plan eng review: OCR is unbounded-cost by
default (pdf text extraction calls OCR for scanned pages, which hits a
paid API). A single pathological PDF batch can burn the daily budget in
minutes. Guardrails below, in layered order of strictness:

  1. Concurrency semaphore — hard ceiling of 4 simultaneous OCR jobs.
     Prevents CPU/memory blow-up on the app container.
  2. Token-bucket rate limit — 500 requests/hour global, per-source cap
     40% of daily quota. Smooths bursts.
  3. Redis daily counter — 5000 OCR pages/day global cost ceiling. Hard
     stop when exceeded; fetcher raises DailyBudgetExceeded.
  4. Queue-depth alert — fires at 2000 pending jobs; ops gets paged
     instead of silently backing up.
  5. Pre-enqueue cost projection — estimate_cost(n_pages) is called before
     submit; jobs projected to breach the daily ceiling are rejected up
     front rather than started and aborted mid-way.

Redis keys (all TTL to midnight UTC):
  brain_ingest:ocr_pages:<YYYYMMDD>           global counter
  brain_ingest:src:<source_id>:ocr:<YYYYMMDD> per-source counter
  brain_ingest:queue_depth                    gauge (updated by scheduler)
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass
from datetime import datetime, timezone

logger = logging.getLogger(__name__)


OCR_MAX_CONCURRENCY = 4
OCR_DAILY_PAGE_CEILING = 5000
OCR_HOURLY_REQUEST_CEILING = 500
PER_SOURCE_DAILY_FRACTION = 0.40  # any one source caps at 40% of daily
QUEUE_DEPTH_ALERT = 2000

# Cost model. OpenAI vision OCR pricing as of 2026-04 — adjust via env
# if the provider changes. Intentionally coarse; this is a ceiling
# projection, not an invoice estimator.
OCR_COST_PER_PAGE_USD = 0.005


class DailyBudgetExceeded(Exception):
    """Raised when an ingestion op would push past OCR_DAILY_PAGE_CEILING."""


class PerSourceCapExceeded(Exception):
    """Raised when a single source tries to use >40% of daily ceiling."""


@dataclass
class RateBucket:
    """Simple token bucket, not thread-safe — protected by ocr_semaphore."""
    capacity: int
    tokens: float
    refill_per_sec: float
    last_refill: float

    @classmethod
    def new(cls, capacity: int, window_seconds: int) -> "RateBucket":
        return cls(
            capacity=capacity,
            tokens=float(capacity),
            refill_per_sec=capacity / window_seconds,
            last_refill=time.monotonic(),
        )

    def try_acquire(self, cost: float = 1.0) -> bool:
        now = time.monotonic()
        elapsed = now - self.last_refill
        self.tokens = min(
            self.capacity,
            self.tokens + elapsed * self.refill_per_sec,
        )
        self.last_refill = now
        if self.tokens >= cost:
            self.tokens -= cost
            return True
        return False


# Process-local singletons. One app container = one set of budgets. Global
# coordination across multiple app replicas happens via Redis counters
# below; the semaphore and in-memory bucket are per-replica.
ocr_semaphore = asyncio.Semaphore(OCR_MAX_CONCURRENCY)
_hourly_bucket = RateBucket.new(OCR_HOURLY_REQUEST_CEILING, 3600)


def estimate_cost_usd(n_pages: int) -> float:
    return n_pages * OCR_COST_PER_PAGE_USD


def _day_key(prefix: str, source_id: str | None = None) -> str:
    day = datetime.now(timezone.utc).strftime("%Y%m%d")
    if source_id:
        return f"brain_ingest:src:{source_id}:{prefix}:{day}"
    return f"brain_ingest:{prefix}:{day}"


def _seconds_until_midnight_utc() -> int:
    now = datetime.now(timezone.utc)
    tomorrow = now.replace(hour=0, minute=0, second=0, microsecond=0)
    tomorrow = tomorrow.replace(day=tomorrow.day)
    # If now > midnight, add a day
    if tomorrow <= now:
        return int(((now.replace(hour=23, minute=59, second=59) - now).total_seconds())) + 1
    return int((tomorrow - now).total_seconds())


async def reserve_ocr_budget(
    redis_client,
    source_id: str,
    n_pages: int,
) -> None:
    """Reserve n_pages against the global + per-source daily OCR budgets.

    Raises DailyBudgetExceeded or PerSourceCapExceeded BEFORE doing any work,
    so the caller can skip submitting the job. The reservation is atomic
    via INCRBY — if it overshoots, we decrement back and raise.
    """
    global_key = _day_key("ocr_pages")
    src_key = _day_key("ocr", source_id=source_id)
    ttl = _seconds_until_midnight_utc()
    per_source_cap = int(OCR_DAILY_PAGE_CEILING * PER_SOURCE_DAILY_FRACTION)

    new_global = await redis_client.incrby(global_key, n_pages)
    await redis_client.expire(global_key, ttl)
    if new_global > OCR_DAILY_PAGE_CEILING:
        await redis_client.decrby(global_key, n_pages)
        raise DailyBudgetExceeded(
            f"global OCR ceiling {OCR_DAILY_PAGE_CEILING}/day would be "
            f"exceeded (projected {new_global}, requested {n_pages})"
        )

    new_src = await redis_client.incrby(src_key, n_pages)
    await redis_client.expire(src_key, ttl)
    if new_src > per_source_cap:
        await redis_client.decrby(src_key, n_pages)
        await redis_client.decrby(global_key, n_pages)
        raise PerSourceCapExceeded(
            f"source {source_id} would exceed 40% daily cap "
            f"({per_source_cap}); projected {new_src}, requested {n_pages}"
        )


def rate_limit_acquire() -> bool:
    """Try to consume 1 token from the hourly bucket. Non-blocking.

    Caller decides whether to queue, skip, or raise. We do not block here
    because an ingest worker blocked on rate limit holds its semaphore slot
    and starves other fetchers.
    """
    return _hourly_bucket.try_acquire(1.0)


def queue_depth_alert(current_depth: int) -> bool:
    """Return True if depth crossed the alert threshold. Caller emits.

    Deliberately does not page directly — keeps this module pure. The
    scheduler wraps this with the actual alerting channel.
    """
    return current_depth >= QUEUE_DEPTH_ALERT
