"""The hook loop scans for stale embeddings only when Brain pages changed."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from src.services import brain_embeddings as be

T0 = datetime(2026, 10, 3, tzinfo=timezone.utc)


class FakeConn:
    def __init__(self) -> None:
        self.latest = T0

    async def fetchval(self, query: str, *args):
        assert "max(updated_at)" in query
        return self.latest


@pytest.fixture
def scans(monkeypatch: pytest.MonkeyPatch):
    calls: list[int] = []
    found = {"n": 0}

    async def fake_embed_all_stale(conn, brain, limit=50):
        calls.append(limit)
        return {"embedded": found["n"], "skipped": 0, "errors": 0}

    monkeypatch.setattr(be, "embed_all_stale", fake_embed_all_stale)
    monkeypatch.setattr(be, "_stale_scan_covered_through", None)
    return calls, found


async def _tick(conn: FakeConn, hooks: int = 0, limit: int = 10) -> dict:
    return await be.embed_stale_if_pages_changed(conn, None, limit=limit, hooks_processed=hooks)


@pytest.mark.asyncio
async def test_first_tick_scans_then_unchanged_ticks_skip(scans) -> None:
    calls, _ = scans
    conn = FakeConn()
    await _tick(conn)
    assert len(calls) == 1
    result = await _tick(conn)
    assert result["unchanged"] is True
    assert len(calls) == 1


@pytest.mark.asyncio
async def test_a_newer_page_triggers_a_scan(scans) -> None:
    calls, _ = scans
    conn = FakeConn()
    await _tick(conn)
    conn.latest = T0 + timedelta(seconds=5)
    await _tick(conn)
    assert len(calls) == 2


@pytest.mark.asyncio
async def test_processed_hooks_always_scan(scans) -> None:
    calls, _ = scans
    conn = FakeConn()
    await _tick(conn)
    await _tick(conn, hooks=3)
    assert len(calls) == 2


@pytest.mark.asyncio
async def test_a_full_batch_keeps_scanning_until_the_backlog_drains(scans) -> None:
    calls, found = scans
    conn = FakeConn()
    found["n"] = 10  # the batch filled its limit: more stale pages may remain
    await _tick(conn, limit=10)
    await _tick(conn, limit=10)
    found["n"] = 3  # short batch: caught up
    await _tick(conn, limit=10)
    await _tick(conn, limit=10)
    assert len(calls) == 3
