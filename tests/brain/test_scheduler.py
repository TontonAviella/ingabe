"""Scheduler unit tests.

These don't boot the real APScheduler — they exercise _run_source_job
directly with an in-container DB, to prove the dispatch/persist/record
loop works end-to-end for Phase 0 (html fetcher only).

Strategy: monkeypatch HTMLFetcher.fetch_one so no network call happens,
then inspect the brain_pages row and the brain_sources.last_success
column after one synthetic fetch cycle.
"""

from __future__ import annotations

from datetime import datetime, timezone

import asyncpg
import pytest

from src.services.brain_ingestion import scheduler as sched
from src.services.brain_ingestion.models import FetchedContent

pytestmark = pytest.mark.asyncio(loop_scope="function")


_TEST_SOURCE_ID = "test-sched-src"
_TEST_URL = "https://example.test/doc"


async def _reset_source(conn: asyncpg.Connection) -> None:
    await conn.execute(
        "DELETE FROM brain_pages WHERE source_id = $1", _TEST_SOURCE_ID,
    )
    await conn.execute(
        "DELETE FROM brain_sources WHERE source_id = $1", _TEST_SOURCE_ID,
    )
    await conn.execute(
        """
        INSERT INTO brain_sources
          (source_id, url, fetcher_type, tier, schedule_cron, status)
        VALUES ($1, $2, 'html', 'T3', '0 3 * * *', 'active')
        """,
        _TEST_SOURCE_ID, _TEST_URL,
    )


def _fake_fetched() -> FetchedContent:
    return FetchedContent(
        source_id=_TEST_SOURCE_ID,
        url=_TEST_URL,
        fetched_at=datetime.now(timezone.utc),
        content_type="text/html",
        status_code=200,
        raw_bytes_len=42,
        text="Hello Kigali. Rwanda agriculture test content.",
        markdown=None,
        etag=None,
        last_modified=None,
        content_hash="deadbeef" * 8,
        title="Test doc",
        language="en",
        tier="T3",
        access_scope="public",
        partner_id=None,
        license=None,
    )


async def test_run_source_job_persists_and_records_success(monkeypatch):
    """Full Phase 0 loop: HTML fetch → write_page → last_success stamped."""
    from src.services.brain_ingestion.html_fetcher import HTMLFetcher
    from src.database.pool import _build_postgres_url

    async def _fake_fetch_one(self, url):
        return _fake_fetched()

    monkeypatch.setattr(HTMLFetcher, "fetch_one", _fake_fetch_one)

    admin = await asyncpg.connect(_build_postgres_url())
    await admin.execute("SELECT set_config('app.user_id', '', false)")
    try:
        await _reset_source(admin)

        await sched._run_source_job(_TEST_SOURCE_ID)

        # Row was persisted.
        page = await admin.fetchrow(
            "SELECT slug, title, compiled_truth, source_id, access_scope "
            "FROM brain_pages WHERE source_id = $1",
            _TEST_SOURCE_ID,
        )
        assert page is not None, "brain_pages row not written"
        assert page["title"] == "Test doc"
        assert "Rwanda agriculture" in page["compiled_truth"]
        assert page["access_scope"] == "public"

        # last_success stamped, last_error cleared.
        src = await admin.fetchrow(
            "SELECT last_success, last_error FROM brain_sources "
            "WHERE source_id = $1",
            _TEST_SOURCE_ID,
        )
        assert src["last_success"] is not None
        assert src["last_error"] is None

        # Cleanup.
        await _reset_source(admin)
        await admin.execute(
            "DELETE FROM brain_sources WHERE source_id = $1",
            _TEST_SOURCE_ID,
        )
    finally:
        await admin.close()


async def test_run_source_job_records_failure_on_fetch_error(monkeypatch):
    """When fetch_one raises, brain_sources.last_error is populated."""
    from src.services.brain_ingestion.html_fetcher import HTMLFetcher
    from src.database.pool import _build_postgres_url

    async def _boom(self, url):
        raise RuntimeError("simulated upstream failure")

    monkeypatch.setattr(HTMLFetcher, "fetch_one", _boom)

    admin = await asyncpg.connect(_build_postgres_url())
    await admin.execute("SELECT set_config('app.user_id', '', false)")
    try:
        await _reset_source(admin)

        await sched._run_source_job(_TEST_SOURCE_ID)

        src = await admin.fetchrow(
            "SELECT last_success, last_error FROM brain_sources "
            "WHERE source_id = $1",
            _TEST_SOURCE_ID,
        )
        # When every per-item fetch crashes and nothing gets fetched the
        # scheduler must record a run-level failure. Anything else hides
        # broken sources behind a green last_success timestamp.
        assert src is not None
        assert src["last_error"] is not None, (
            "all-items-failed run must stamp last_error"
        )
        assert "items failed" in src["last_error"]
        assert src["last_success"] is None

        await admin.execute(
            "DELETE FROM brain_sources WHERE source_id = $1",
            _TEST_SOURCE_ID,
        )
    finally:
        await admin.close()


async def test_run_source_job_skips_inactive_source():
    """Paused sources must not run even if someone triggers the job."""
    from src.database.pool import _build_postgres_url

    admin = await asyncpg.connect(_build_postgres_url())
    await admin.execute("SELECT set_config('app.user_id', '', false)")
    try:
        # Clear brain_pages first too — _TEST_SOURCE_ID is shared across tests
        # in this file, and pytest-xdist may leave rows from a prior worker's
        # test_run_source_job_inserts_pages run. Without this, the n==0 assertion
        # below races with whatever order xdist picks.
        await admin.execute(
            "DELETE FROM brain_pages WHERE source_id = $1",
            _TEST_SOURCE_ID,
        )
        await admin.execute(
            "DELETE FROM brain_sources WHERE source_id = $1",
            _TEST_SOURCE_ID,
        )
        await admin.execute(
            """
            INSERT INTO brain_sources
              (source_id, url, fetcher_type, tier, status)
            VALUES ($1, $2, 'html', 'T3', 'paused')
            """,
            _TEST_SOURCE_ID, _TEST_URL,
        )

        # Should return without touching brain_pages.
        await sched._run_source_job(_TEST_SOURCE_ID)

        n = await admin.fetchval(
            "SELECT count(*) FROM brain_pages WHERE source_id = $1",
            _TEST_SOURCE_ID,
        )
        assert n == 0

        await admin.execute(
            "DELETE FROM brain_sources WHERE source_id = $1",
            _TEST_SOURCE_ID,
        )
    finally:
        await admin.close()


async def test_run_source_job_rejects_unknown_fetcher_type():
    """fetcher_type values not in _FETCHER_REGISTRY must not crash the job."""
    from src.database.pool import _build_postgres_url

    admin = await asyncpg.connect(_build_postgres_url())
    await admin.execute("SELECT set_config('app.user_id', '', false)")
    try:
        await admin.execute(
            "DELETE FROM brain_sources WHERE source_id = $1",
            _TEST_SOURCE_ID,
        )
        await admin.execute(
            """
            INSERT INTO brain_sources
              (source_id, url, fetcher_type, tier, status)
            VALUES ($1, $2, 'pdf', 'T3', 'active')
            """,
            _TEST_SOURCE_ID, _TEST_URL,
        )

        # Phase 0 only registers html. pdf must be a no-op, not a crash.
        await sched._run_source_job(_TEST_SOURCE_ID)

        n = await admin.fetchval(
            "SELECT count(*) FROM brain_pages WHERE source_id = $1",
            _TEST_SOURCE_ID,
        )
        assert n == 0

        await admin.execute(
            "DELETE FROM brain_sources WHERE source_id = $1",
            _TEST_SOURCE_ID,
        )
    finally:
        await admin.close()


async def test_run_source_job_skips_when_lock_held(monkeypatch):
    """When another worker holds the advisory lock, the job must no-op.

    Simulates the uvicorn --workers 6 case: worker A's scheduler is
    already fetching, worker B's scheduler tick must not duplicate the
    fetch. This is the multi-worker coordination guarantee.
    """
    from src.services.brain_ingestion.html_fetcher import HTMLFetcher
    from src.database.pool import _build_postgres_url

    fetch_calls = {"n": 0}

    async def _counting_fetch(self, url):
        fetch_calls["n"] += 1
        return _fake_fetched()

    monkeypatch.setattr(HTMLFetcher, "fetch_one", _counting_fetch)

    admin = await asyncpg.connect(_build_postgres_url())
    await admin.execute("SELECT set_config('app.user_id', '', false)")

    # Second connection simulates the other worker holding the lock.
    holder = await asyncpg.connect(_build_postgres_url())
    await holder.execute("SELECT set_config('app.user_id', '', false)")

    try:
        await _reset_source(admin)

        lock_key = f"brain_ingest:{_TEST_SOURCE_ID}"
        got = await holder.fetchval(
            "SELECT pg_try_advisory_lock(hashtextextended($1, 0))",
            lock_key,
        )
        assert got is True, "holder failed to acquire lock"

        await sched._run_source_job(_TEST_SOURCE_ID)

        assert fetch_calls["n"] == 0, (
            "fetcher ran despite lock being held by another session"
        )
        n = await admin.fetchval(
            "SELECT count(*) FROM brain_pages WHERE source_id = $1",
            _TEST_SOURCE_ID,
        )
        assert n == 0

        await holder.execute(
            "SELECT pg_advisory_unlock(hashtextextended($1, 0))",
            lock_key,
        )
        await sched._run_source_job(_TEST_SOURCE_ID)
        assert fetch_calls["n"] == 1

        await _reset_source(admin)
        await admin.execute(
            "DELETE FROM brain_sources WHERE source_id = $1",
            _TEST_SOURCE_ID,
        )
    finally:
        await holder.close()
        await admin.close()
