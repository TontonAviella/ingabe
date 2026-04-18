"""Ingestion scheduler — APScheduler AsyncIOScheduler per-source cron.

Runs inside the FastAPI process (co-located with the brain hook loop).
On startup, loads every active row from brain_sources that has a
schedule_cron and registers a cron job. On fire, the job dispatches to
the fetcher matching fetcher_type, writes every FetchedContent via
normalizer.write_page, and records success/failure back onto brain_sources.

Phase 0 scope:
  - fetcher_type=html only (PDF/API fetchers land in Phase 1)
  - access_scope defaults to public (partner_internal gated on the Phase 1
    YAML merge + secrets mount — build_source_config() already refuses to
    instantiate partner_internal without partner_id)
  - single replica — APScheduler runs in-process. Multi-replica
    coordination via Postgres advisory locks lives in Phase 2 when we
    scale past one app container.

Why co-located instead of a separate crawler container: Phase 0 volumes
are small (T2/T3 sources, daily cadence). Splitting the container
doubles the deploy surface for ~zero payoff. Phase 1 moves OCR-heavy
fetchers to a dedicated mundi-crawler container where the daily budget
can be isolated from user-facing traffic.
"""

from __future__ import annotations

import logging
import os
from datetime import datetime, timezone
from typing import Optional

import asyncpg
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger

from src.database.pool import get_async_db_connection
from src.services.brain_ingestion import normalizer, registry
from src.services.brain_ingestion.feature_flags import partner_internal_enabled
from src.services.brain_ingestion.html_fetcher import HTMLFetcher
from src.services.brain_ingestion.models import FetchedContent
from src.services.brain_service import BrainService

logger = logging.getLogger(__name__)


SYSTEM_OWNER_UUID = os.environ.get(
    "BRAIN_INGEST_OWNER_UUID",
    "00000000-0000-0000-0000-000000000000",
)

_FETCHER_REGISTRY = {
    "html": HTMLFetcher,
}

_scheduler: Optional[AsyncIOScheduler] = None


async def _open_admin_conn() -> asyncpg.Connection:
    """Fresh DB connection with empty app.user_id — worker bypass path.

    Mirrors brain_hook_processor.run_hook_processor_once. See also the
    mundiuser/BYPASSRLS caveat in project memory: until the prod role is
    downgraded, this empty-user_id handshake is belt-and-suspenders; the
    role already bypasses. The handshake stays correct once the role is
    fixed.
    """
    from src.database.pool import _build_postgres_url

    conn = await asyncpg.connect(_build_postgres_url())
    await conn.execute("SELECT set_config('app.user_id', '', false)")
    return conn


async def _run_source_job(source_id: str) -> None:
    """Fire one ingest cycle for `source_id`. Called by APScheduler.

    Connection discipline:
      - `lock_conn` is a dedicated session held for the lifetime of the job.
        It holds the session-scoped advisory lock and is used only for the
        bookkeeping reads/writes (config lookup, record_fetch_*). It does
        NOT participate in per-item writes, so slow HTTP fetches can't tie
        up a connection needed by the writer pool.
      - Per-item writes inside `_PersistingFetcher.on_item` acquire a fresh
        pool connection, wrap the three normalizer writes in a transaction,
        and release. Pool pressure is bounded by write rate, not by fetch
        latency.
    """
    log = logger.getChild(source_id)
    lock_conn = await _open_admin_conn()
    lock_key = f"brain_ingest:{source_id}"
    lock_held = False
    try:
        # Prod runs uvicorn with --workers 6, so each worker has its own
        # APScheduler. Without a lock, one cron tick fires 6 concurrent
        # fetches. pg_try_advisory_lock is non-blocking and session-scoped:
        # first worker to acquire runs the job, the rest exit immediately.
        # Same mechanism covers future multi-container scale-out.
        lock_held = await lock_conn.fetchval(
            "SELECT pg_try_advisory_lock(hashtextextended($1, 0))",
            lock_key,
        )
        if not lock_held:
            log.info("source_skipped_lock_held")
            return

        row = await lock_conn.fetchrow(
            "SELECT source_id, url, fetcher_type, tier, schedule_cron, status "
            "FROM brain_sources WHERE source_id = $1 AND status = 'active'",
            source_id,
        )
        if not row:
            log.info("source_skipped_not_active")
            return

        fetcher_type = row["fetcher_type"]
        fetcher_cls = _FETCHER_REGISTRY.get(fetcher_type)
        if fetcher_cls is None:
            log.warning(
                "unknown_fetcher_type",
                extra={"fetcher_type": fetcher_type},
            )
            return

        try:
            source_cfg = registry.build_source_config(dict(row))
        except ValueError as e:
            log.error("source_config_rejected", extra={"error": str(e)})
            await registry.record_fetch_failure(
                lock_conn, source_id, datetime.now(timezone.utc), str(e),
            )
            return

        # Partner-internal sources require the BRAIN_PARTNER_INTERNAL_ENABLED
        # flag. Off by default until Phase 0 hard gates land (child-table RLS,
        # session GUC wiring, scheduler/CLI audit). A partner-internal fetch
        # that runs before RLS is verified could persist rows that leak via
        # Sage retrieval — same flag gates the write and retrieval paths.
        if source_cfg.access_scope == "partner_internal" and not partner_internal_enabled():
            log.info(
                "source_skipped_partner_internal_flag_off",
                extra={"source_id": source_id, "fetcher_type": fetcher_type},
            )
            return

        brain = BrainService()

        class _PersistingFetcher(fetcher_cls):  # type: ignore[valid-type,misc]
            async def on_item(self, item: FetchedContent) -> None:
                # Fresh pool connection per write. user_id="" matches the
                # admin handshake: empty app.user_id means "system writer",
                # bypassing RLS policies that key off session user.
                async with get_async_db_connection(user_id="") as write_conn:
                    await normalizer.write_page(
                        write_conn,
                        brain,
                        item,
                        owner_uuid=SYSTEM_OWNER_UUID,
                    )

        fetcher = _PersistingFetcher(source_cfg)
        result = await fetcher.run()

        if result.error:
            await registry.record_fetch_failure(
                lock_conn, source_id, result.finished_at, result.error,
            )
        elif result.items_failed > 0 and result.items_fetched == 0:
            # Every item crashed but the outer loop survived. Without this
            # branch the scheduler records success and operators see a green
            # last_success while every fetch is actually failing. For
            # continuous ops we'd rather surface the failure on last_error.
            await registry.record_fetch_failure(
                lock_conn,
                source_id,
                result.finished_at,
                f"all {result.items_failed} items failed "
                "(see fetch_item_failed logs)",
            )
        else:
            await registry.record_fetch_success(
                lock_conn, source_id, result.finished_at,
            )

        log.info(
            "source_fetch_complete",
            extra={
                "items_fetched": result.items_fetched,
                "items_failed": result.items_failed,
                "items_skipped": result.items_skipped_unchanged,
                "duration_sec": (
                    result.finished_at - result.started_at
                ).total_seconds(),
            },
        )
    except Exception as e:
        log.exception("source_job_crashed")
        try:
            await registry.record_fetch_failure(
                lock_conn,
                source_id,
                datetime.now(timezone.utc),
                repr(e)[:500],
            )
        except Exception:
            log.exception("fetch_failure_record_also_failed")
    finally:
        # Release the advisory lock explicitly. Relying on session teardown
        # (conn.close()) is unsafe: lifespan shutdown(wait=False) can cancel
        # mid-run, and future PgBouncer transaction-pooling would strand the
        # backend session, keeping the lock indefinitely. A stuck lock means
        # the source silently stops ingesting with no error trail.
        if lock_held:
            try:
                await lock_conn.execute(
                    "SELECT pg_advisory_unlock(hashtextextended($1, 0))",
                    lock_key,
                )
            except Exception:
                log.exception("advisory_lock_release_failed")
        await lock_conn.close()


async def start_ingestion_scheduler() -> Optional[AsyncIOScheduler]:
    """Load active sources and register one cron job per source.

    Safe to call on app startup even if the brain_sources migration
    hasn't run yet — any exception during bootstrap returns None so the
    main app keeps starting. Re-invocation while a scheduler is already
    running is a no-op (returns the existing instance).
    """
    global _scheduler
    if _scheduler is not None:
        logger.warning("scheduler_already_running")
        return _scheduler

    try:
        conn = await _open_admin_conn()
    except Exception:
        logger.exception("ingestion_scheduler_conn_failed")
        return None

    try:
        rows = await conn.fetch(
            "SELECT source_id, schedule_cron FROM brain_sources "
            "WHERE status = 'active' AND schedule_cron IS NOT NULL"
        )
    except Exception:
        logger.exception("ingestion_scheduler_startup_skipped")
        return None
    finally:
        await conn.close()

    scheduler = AsyncIOScheduler(timezone="UTC")
    registered = 0
    for row in rows:
        source_id = row["source_id"]
        cron = row["schedule_cron"]
        try:
            trigger = CronTrigger.from_crontab(cron, timezone="UTC")
        except Exception:
            logger.warning(
                "invalid_schedule_cron_skipped",
                extra={"source_id": source_id, "schedule_cron": cron},
            )
            continue
        scheduler.add_job(
            _run_source_job,
            trigger=trigger,
            args=[source_id],
            id=f"brain_ingest:{source_id}",
            replace_existing=True,
            max_instances=1,
            coalesce=True,
        )
        registered += 1

    scheduler.start()
    _scheduler = scheduler
    logger.info(
        "ingestion_scheduler_started",
        extra={"sources_registered": registered},
    )
    return scheduler


async def shutdown_ingestion_scheduler() -> None:
    """Cancel scheduled jobs and release the scheduler.

    Called from the FastAPI lifespan teardown. wait=False so shutdown
    doesn't block app exit on an in-flight fetch; the job body catches
    cancellation and the admin conn is closed in its own finally.
    """
    global _scheduler
    if _scheduler is None:
        return
    _scheduler.shutdown(wait=False)
    _scheduler = None
    logger.info("ingestion_scheduler_stopped")
