"""What a reader saw of a photo's question cards, so the next deck can change with them (Roger, 2026-10-25).

- A new set each day: with no seed asked for, the deck's seed is the day's (daily_seed, the day in DAY_ZONE), the
  same all day for one reader and photo, another tomorrow; "Other questions" asks for another seed.
- What changed: each card's status when the reader first saw it, moved on only when they open it. A card whose answer
  has improved to ready or partly ready since then is marked new and comes first, until they open it.
- What they opened: a card opened in the last day gives way to others.

Kept in Redis per reader and photo (or inspection), for MEMORY_DAYS. Losing it only loses this ordering, never an
answer, so every read and write here is best effort.
"""

from __future__ import annotations

import logging
import time
import zlib
from datetime import date, datetime
from typing import Any, Optional
from zoneinfo import ZoneInfo

from src.services.card_status import (
    CANT_TELL,
    LEARN,
    NEEDS_CAMERA,
    NEEDS_FLIGHT,
    PARTLY,
    READY,
    TO_BUILD,
    WORKING,
)

logger = logging.getLogger(__name__)

MEMORY_DAYS = 60
SEEN_HOURS = 24  # a card opened this recently gives way to others
DAY_ZONE = ZoneInfo("Africa/Kigali")  # where Ingabe's day starts (the server runs on UTC)
# How far along an answer is; a card is new only when it moved up to partly or ready.
_RANK = {TO_BUILD: 0, LEARN: 0, CANT_TELL: 1, NEEDS_FLIGHT: 1, NEEDS_CAMERA: 1, WORKING: 2, PARTLY: 3, READY: 4}


def daily_seed(reader: str, target: str, today: Optional[date] = None) -> int:
    """A seed that is the same all day for one reader and photo, and different the next day (never 0)."""
    day = (today or datetime.now(DAY_ZONE).date()).isoformat()
    return 1 + zlib.crc32(f"{day}:{reader}:{target}".encode()) % 999_999


def _key(reader: str, target: str) -> str:
    return f"cards:{reader}:{target}"


async def recall(redis: Any, reader: str, target: str) -> tuple[frozenset[str], dict[str, str]]:
    """(cards opened in the last SEEN_HOURS, each card's status when the reader last saw the deck)."""
    if redis is None:
        return frozenset(), {}
    try:
        raw = await redis.hgetall(_key(reader, target))
    except Exception:  # noqa: BLE001 - best effort: the deck is only ordered without it
        logger.warning("card memory: could not read %s", target, exc_info=True)
        return frozenset(), {}
    fields = {(k.decode() if isinstance(k, bytes) else k): (v.decode() if isinstance(v, bytes) else v)
              for k, v in (raw or {}).items()}
    now = time.time()
    seen = frozenset(k[5:] for k, v in fields.items()
                     if k.startswith("seen:") and now - float(v) < SEEN_HOURS * 3600)
    statuses = {k[7:]: v for k, v in fields.items() if k.startswith("status:")}
    return seen, statuses


def changed(before: dict[str, str], now: dict[str, str]) -> frozenset[str]:
    """Cards whose answer improved to partly or ready since the reader's baseline (none without one): a card that got
    worse, or stayed, is not new."""
    return frozenset(card for card, status in now.items()
                     if status in (PARTLY, READY) and card in before
                     and _RANK.get(status, 0) > _RANK.get(before[card], 0))


async def remember_deck(redis: Any, reader: str, target: str, statuses: dict[str, str]) -> None:
    """Take each card's status as the baseline where it has none yet (a card's baseline moves on only when the
    reader opens it, so its "new" mark stays until then)."""
    if redis is None or not statuses:
        return
    try:
        key = _key(reader, target)
        for card, status in statuses.items():
            await redis.hsetnx(key, f"status:{card}", status)
        await redis.expire(key, MEMORY_DAYS * 86400)
    except Exception:  # noqa: BLE001 - best effort: the deck is only ordered without it
        logger.warning("card memory: could not write %s", target, exc_info=True)


async def remember_opened(redis: Any, reader: str, target: str, card: str, status: Optional[str] = None) -> None:
    """The reader opened a card: it gives way for a day, and its answer as they saw it becomes its baseline."""
    mapping = {f"seen:{card}": str(time.time())}
    if status:
        mapping[f"status:{card}"] = status
    await _write(redis, reader, target, mapping)


async def _write(redis: Any, reader: str, target: str, mapping: dict[str, str]) -> None:
    if redis is None or not mapping:
        return
    try:
        key = _key(reader, target)
        await redis.hset(key, mapping=mapping)
        await redis.expire(key, MEMORY_DAYS * 86400)
    except Exception:  # noqa: BLE001 - best effort: the deck is only ordered without it
        logger.warning("card memory: could not write %s", target, exc_info=True)
