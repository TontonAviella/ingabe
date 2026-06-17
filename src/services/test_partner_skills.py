"""Tests for partner skill allowlist filter.

Pure-logic tests for filter_tools_payload — no DB needed. The DB-backed
fetch_allowed_skills is also exercised end-to-end against the real
partner_skills table to catch SQL/schema drift.
"""

import uuid

import asyncpg
import pytest

from src.database.pool import _build_postgres_url
from src.services.partner_skills import (
    fetch_allowed_skills,
    filter_tools_payload,
    grant_skill,
    revoke_skill,
)


# ---------------------------------------------------------------------------
# filter_tools_payload — pure-logic
# ---------------------------------------------------------------------------

def _tool(name: str) -> dict:
    return {"type": "function", "function": {"name": name, "parameters": {}}}


def test_filter_none_returns_unchanged_list():
    payload = [_tool("a"), _tool("b"), _tool("c")]
    out = filter_tools_payload(payload, None)
    assert out == payload
    # New list, not the same reference (so caller mutations don't propagate)
    assert out is not payload


def test_filter_drops_non_allowed():
    payload = [_tool("a"), _tool("b"), _tool("c")]
    out = filter_tools_payload(payload, {"a", "c"})
    names = [t["function"]["name"] for t in out]
    assert names == ["a", "c"]


def test_filter_empty_set_drops_everything():
    payload = [_tool("a"), _tool("b")]
    out = filter_tools_payload(payload, set())
    assert out == []


def test_filter_drops_malformed_entries_when_allowlist_active():
    """An allowlisted payload must drop entries it cannot identify by name."""
    payload = [_tool("a"), {"type": "function", "function": {}}, {"type": "other"}]
    out = filter_tools_payload(payload, {"a"})
    assert out == [_tool("a")]


def test_filter_handles_iterable_input():
    """filter_tools_payload accepts any Iterable[dict], not just list."""
    def gen():
        yield _tool("a")
        yield _tool("b")
    out = filter_tools_payload(gen(), {"b"})
    assert [t["function"]["name"] for t in out] == ["b"]


# ---------------------------------------------------------------------------
# fetch_allowed_skills — None branches with a fake conn
# ---------------------------------------------------------------------------

class _FakeConn:
    def __init__(self, rows):
        self._rows = rows
        self.called_with = None

    async def fetch(self, query, *args):
        self.called_with = (query, args)
        return self._rows


@pytest.mark.asyncio
async def test_fetch_none_for_empty_partner_id():
    conn = _FakeConn(rows=[{"skill_name": "x"}])
    assert await fetch_allowed_skills(conn, None) is None
    assert await fetch_allowed_skills(conn, "") is None
    # Empty/None short-circuits — no SQL was issued
    assert conn.called_with is None


@pytest.mark.asyncio
async def test_fetch_none_when_zero_rows():
    conn = _FakeConn(rows=[])
    assert await fetch_allowed_skills(conn, "partner-1") is None
    assert conn.called_with is not None  # SQL was issued


@pytest.mark.asyncio
async def test_fetch_returns_set_from_rows():
    conn = _FakeConn(rows=[
        {"skill_name": "render_map_snapshot"},
        {"skill_name": "get_field_health"},
    ])
    out = await fetch_allowed_skills(conn, "partner-1")
    assert out == {"render_map_snapshot", "get_field_health"}


# ---------------------------------------------------------------------------
# DB-backed integration: grant_skill → fetch → revoke roundtrip
# ---------------------------------------------------------------------------

@pytest.mark.postgres
@pytest.mark.asyncio
async def test_grant_fetch_revoke_roundtrip():
    """Full lifecycle against the real partner_skills table.

    Catches SQL/schema drift (column names, ON CONFLICT target, RLS policy)
    that the fake-conn unit tests can't see.
    """
    partner_id = f"test-partner-{uuid.uuid4()}"
    url = _build_postgres_url()
    c = await asyncpg.connect(url)
    try:
        # Initially unrestricted (zero rows)
        assert await fetch_allowed_skills(c, partner_id) is None

        # Grant two skills
        await grant_skill(c, partner_id, "render_map_snapshot", note="initial")
        await grant_skill(c, partner_id, "get_field_health")
        allowed = await fetch_allowed_skills(c, partner_id)
        assert allowed == {"render_map_snapshot", "get_field_health"}

        # Idempotent re-grant: still 2 rows, note preserved when not provided
        await grant_skill(c, partner_id, "render_map_snapshot")
        allowed = await fetch_allowed_skills(c, partner_id)
        assert allowed == {"render_map_snapshot", "get_field_health"}

        # Revoke one: soft-disable, row stays for audit, not returned
        await revoke_skill(c, partner_id, "get_field_health")
        allowed = await fetch_allowed_skills(c, partner_id)
        assert allowed == {"render_map_snapshot"}

        # Row count check: revoked row was disabled, not deleted
        row_count = await c.fetchval(
            "SELECT count(*) FROM partner_skills WHERE partner_id = $1",
            partner_id,
        )
        assert row_count == 2

        # Re-grant the revoked skill: it flips back to enabled
        await grant_skill(c, partner_id, "get_field_health", note="re-enabled")
        allowed = await fetch_allowed_skills(c, partner_id)
        assert allowed == {"render_map_snapshot", "get_field_health"}
    finally:
        await c.execute(
            "DELETE FROM partner_skills WHERE partner_id = $1", partner_id
        )
        await c.close()
