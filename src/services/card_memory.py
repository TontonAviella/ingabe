"""What a reader saw of a photo's question cards, so the next deck can change with them (Roger, 2026-10-25).

- A new set each day: with no seed asked for, the deck's seed is the day's (daily_seed), the same all day for one
  reader and photo, another tomorrow; "Other questions" asks for another seed.
- What changed: the status of each card the last time the reader saw the deck. A card whose answer became ready (or
  partly ready) since then is marked new and comes first.
- What they opened: a card opened in the last day gives way to others.

Kept in Redis per reader and photo (or inspection), for MEMORY_DAYS. Losing it only loses this ordering, never an
answer, so every read and write here is best effort.
"""

from __future__ import annotations

import logging
import time
import zlib
from datetime import date
from typing import Any, Optional

from src.services.card_status import PARTLY, READY

logger = logging.getLogger(__name__)

MEMORY_DAYS = 60
SEEN_HOURS = 24  # a card opened this recently gives way to others
_IMPROVED = (READY, PARTLY)


def daily_seed(reader: str, target: str, today: Optional[date] = None) -> int:
    """A seed that is the same all day for one reader and photo, and different the next day (never 0)."""
    day = (today or date.today()).isoformat()
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
    """Cards whose answer became ready or partly ready since the reader last saw the deck (none on a first look)."""
    if not before:
        return frozenset()
    return frozenset(card for card, status in now.items()
                     if status in _IMPROVED and before.get(card) is not None and before[card] != status)


async def remember_deck(redis: Any, reader: str, target: str, statuses: dict[str, str]) -> None:
    await _write(redis, reader, target, {f"status:{card}": status for card, status in statuses.items()})


async def remember_opened(redis: Any, reader: str, target: str, card: str) -> None:
    await _write(redis, reader, target, {f"seen:{card}": str(time.time())})


async def _write(redis: Any, reader: str, target: str, mapping: dict[str, str]) -> None:
    if redis is None or not mapping:
        return
    try:
        key = _key(reader, target)
        await redis.hset(key, mapping=mapping)
        await redis.expire(key, MEMORY_DAYS * 86400)
    except Exception:  # noqa: BLE001 - best effort: the deck is only ordered without it
        logger.warning("card memory: could not write %s", target, exc_info=True)
