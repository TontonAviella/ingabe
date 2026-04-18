"""Registry — brain_sources CRUD + config merge.

The brain_sources table stores operational state (status, last_success,
last_error). Declarative config (crawl_depth, rate limits, access_scope,
partner_id, license) lives in YAML so engineers can diff it in git.

For partner_internal sources, the YAML path points to a secrets mount
that is NOT checked into the public repo — contents include the
partner_id that needs to flow to RLS. The loader refuses to instantiate
a partner_internal SourceConfig without a resolved partner_id.

Phase 0 lands the DB-only path. Phase 1 adds the YAML merge and the
secrets mount wiring for the first partner.
"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Iterable, Optional

import asyncpg

from src.services.brain_ingestion.models import SourceConfig

logger = logging.getLogger(__name__)


async def upsert_source_row(
    conn: asyncpg.Connection,
    *,
    source_id: str,
    url: str,
    fetcher_type: str,
    tier: str,
    schedule_cron: Optional[str] = None,
    status: str = "active",
) -> None:
    """Register or update a source's operational row. Admin only (RLS)."""
    await conn.execute(
        """
        INSERT INTO brain_sources
            (source_id, url, fetcher_type, tier, schedule_cron, status)
        VALUES ($1, $2, $3, $4, $5, $6)
        ON CONFLICT (source_id) DO UPDATE SET
            url           = EXCLUDED.url,
            fetcher_type  = EXCLUDED.fetcher_type,
            tier          = EXCLUDED.tier,
            schedule_cron = EXCLUDED.schedule_cron,
            status        = EXCLUDED.status,
            updated_at    = now()
        """,
        source_id, url, fetcher_type, tier, schedule_cron, status,
    )


async def record_fetch_success(
    conn: asyncpg.Connection, source_id: str, at: datetime
) -> None:
    await conn.execute(
        """
        UPDATE brain_sources
        SET last_success = $2, last_error = NULL, updated_at = now()
        WHERE source_id = $1
        """,
        source_id, at,
    )


async def record_fetch_failure(
    conn: asyncpg.Connection,
    source_id: str,
    at: datetime,
    error: str,
    *,
    mark_broken: bool = False,
) -> None:
    """Record an error. If mark_broken, status transitions to 'broken' so
    the scheduler stops picking this source until an operator unpauses it.
    """
    new_status = "broken" if mark_broken else None
    await conn.execute(
        """
        UPDATE brain_sources
        SET last_error = $2,
            updated_at = now(),
            status     = COALESCE($3, status)
        WHERE source_id = $1
        """,
        source_id, error[:2000], new_status,
    )


async def list_active_sources(
    conn: asyncpg.Connection,
    *,
    tier: Optional[str] = None,
) -> Iterable[dict]:
    rows = await conn.fetch(
        """
        SELECT source_id, url, fetcher_type, tier, schedule_cron,
               status, last_success, last_error, last_tos_check
        FROM brain_sources
        WHERE status = 'active'
          AND ($1::text IS NULL OR tier = $1)
        ORDER BY tier, source_id
        """,
        tier,
    )
    return [dict(r) for r in rows]


def build_source_config(
    db_row: dict,
    *,
    access_scope: str = "public",
    partner_id: Optional[str] = None,
    license: Optional[str] = None,
    language: str = "en",
    max_requests_per_hour: int = 60,
    max_requests_per_day: int = 500,
    ocr_enabled: bool = False,
) -> SourceConfig:
    """Merge a DB row with caller-supplied declarative config.

    Guards against the footgun of partner_internal scope with no
    partner_id — refuses to build rather than letting the RLS write path
    catch it later.
    """
    if access_scope == "partner_internal" and not partner_id:
        raise ValueError(
            f"source {db_row['source_id']}: access_scope=partner_internal "
            "requires partner_id. Refusing to build SourceConfig."
        )
    return SourceConfig(
        source_id=db_row["source_id"],
        name=db_row["source_id"],
        tier=db_row["tier"],
        status=db_row["status"],
        seed_url=db_row["url"],
        schedule_cron=db_row.get("schedule_cron"),
        access_scope=access_scope,
        partner_id=partner_id,
        license=license,
        language=language,
        max_requests_per_hour=max_requests_per_hour,
        max_requests_per_day=max_requests_per_day,
        ocr_enabled=ocr_enabled,
    )
