"""Tests for BRAIN_PARTNER_INTERNAL_ENABLED feature flag.

Covers the helper plus the two write-path gate sites and the retrieval
isolation layer. The flag gates the write path only; retrieval uses the
app.partner_id GUC and _PARTNER_FILTER for defense-in-depth isolation.

Write-path gates (flag-controlled):
  1. scheduler._run_source_job — skips partner_internal sources when off
  2. normalizer.write_page — refuses partner_internal writes when off

Retrieval isolation (GUC-controlled, tested here for completeness):
  3. brain_service.search_keyword — filters via app.partner_id GUC
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone

import asyncpg
import pytest
import pytest_asyncio

from src.database.pool import _build_postgres_url
from src.services.brain_ingestion import normalizer
from src.services.brain_ingestion.feature_flags import partner_internal_enabled
from src.services.brain_ingestion.models import FetchedContent
from src.services.brain_service import BrainService, PageInput

# Session-scoped loop required so the async flag_test_conn fixture and the
# async tests share a loop — asyncpg attaches futures to a specific loop and
# will refuse cross-loop reuse. Applies to sync helper tests too, but that
# only generates a cosmetic PytestWarning; it doesn't affect correctness.
pytestmark = pytest.mark.asyncio(loop_scope="module")


# ---------------------------------------------------------------------------
# Helper unit tests (no DB)
# ---------------------------------------------------------------------------


def test_flag_default_off(monkeypatch):
    """Default env missing → False. Fail-safe default."""
    monkeypatch.delenv("BRAIN_PARTNER_INTERNAL_ENABLED", raising=False)
    assert partner_internal_enabled() is False


def test_flag_true_on(monkeypatch):
    """Literal 'true' enables."""
    monkeypatch.setenv("BRAIN_PARTNER_INTERNAL_ENABLED", "true")
    assert partner_internal_enabled() is True


def test_flag_case_insensitive(monkeypatch):
    """'TRUE', 'True', etc. all enable — operators shouldn't guess casing."""
    for v in ("TRUE", "True", "tRuE"):
        monkeypatch.setenv("BRAIN_PARTNER_INTERNAL_ENABLED", v)
        assert partner_internal_enabled() is True, f"expected True for {v!r}"


def test_flag_other_values_off(monkeypatch):
    """Anything that isn't 'true' → False. No surprise truthiness."""
    for v in ("false", "0", "1", "yes", "on", "enabled", ""):
        monkeypatch.setenv("BRAIN_PARTNER_INTERNAL_ENABLED", v)
        assert partner_internal_enabled() is False, f"expected False for {v!r}"


# ---------------------------------------------------------------------------
# write_page gate — unit (no DB, guard fires before conn is touched)
# ---------------------------------------------------------------------------


def _partner_item() -> FetchedContent:
    return FetchedContent(
        source_id="test-partner-src",
        url="https://partner.example.com/doc",
        fetched_at=datetime.now(timezone.utc),
        content_type="text/html",
        status_code=200,
        raw_bytes_len=42,
        text="private partner content",
        tier="T1",
        access_scope="partner_internal",
        partner_id=str(uuid.uuid4()),
    )


async def test_write_page_refuses_partner_internal_when_flag_off(monkeypatch):
    """write_page must raise before touching the connection when flag is off.

    This is the belt-and-suspenders layer: scheduler skips partner sources,
    but admin CLIs / replay jobs can also call write_page directly, and the
    write layer has to refuse independently.
    """
    monkeypatch.delenv("BRAIN_PARTNER_INTERNAL_ENABLED", raising=False)
    item = _partner_item()
    with pytest.raises(ValueError, match="BRAIN_PARTNER_INTERNAL_ENABLED"):
        # conn=None, brain=None: guard raises before either is used.
        await normalizer.write_page(None, None, item, owner_uuid=str(uuid.uuid4()))


async def test_write_page_still_requires_partner_id_when_flag_on(monkeypatch):
    """The partner_id guard remains even when the flag is on. The flag is an
    additional gate, not a replacement for the tenant-id requirement.
    """
    monkeypatch.setenv("BRAIN_PARTNER_INTERNAL_ENABLED", "true")
    item = _partner_item()
    item = item.model_copy(update={"partner_id": None})
    with pytest.raises(ValueError, match="partner_id"):
        await normalizer.write_page(None, None, item, owner_uuid=str(uuid.uuid4()))


# ---------------------------------------------------------------------------
# Sage retrieval gate — integration (DB required)
# ---------------------------------------------------------------------------


@pytest_asyncio.fixture(scope="function", loop_scope="module")
async def flag_test_conn(_migrations_done):
    """Fresh asyncpg connection seeded with one public + one partner page.

    Function-scoped so each test gets a clean slate on the rows we insert —
    the existing session-scoped brain_conn fixture in test_brain_service.py
    accumulates state across tests and the partner rows would contaminate
    other tests.

    Depends on `_migrations_done` (session-scoped, defined in conftest.py)
    so the brain_pages table exists. Without this, xdist workers that don't
    happen to instantiate sync_client first hit `relation "brain_pages" does
    not exist`.
    """
    owner = str(uuid.uuid4())
    partner = str(uuid.uuid4())
    c = await asyncpg.connect(_build_postgres_url())
    await c.execute("SELECT set_config('app.user_id', $1, false)", owner)

    brain = BrainService()
    public_slug = f"flagtest-public-{uuid.uuid4().hex[:8]}"
    partner_slug = f"flagtest-partner-{uuid.uuid4().hex[:8]}"
    await brain.put_page(
        c, public_slug,
        PageInput(
            type="field", title="Public Retrieval Test",
            compiled_truth="unique_retrieval_marker_xyz public content",
        ),
        owner_uuid=owner,
    )
    await brain.put_page(
        c, partner_slug,
        PageInput(
            type="field", title="Partner Retrieval Test",
            compiled_truth="unique_retrieval_marker_xyz partner content",
        ),
        owner_uuid=owner,
    )
    await c.execute(
        "UPDATE brain_pages SET access_scope = 'partner_internal', partner_id = $2::uuid "
        "WHERE slug = $1",
        partner_slug, partner,
    )

    yield {
        "conn": c, "public_slug": public_slug,
        "partner_slug": partner_slug, "partner_id": partner,
    }

    await c.execute(
        "DELETE FROM brain_pages WHERE slug = ANY($1::text[])",
        [public_slug, partner_slug],
    )
    await c.close()


@pytest.mark.postgres
async def test_search_keyword_hides_partner_internal_without_partner_guc(flag_test_conn):
    """No app.partner_id GUC: keyword search must NOT surface partner_internal rows."""
    await flag_test_conn["conn"].execute("RESET app.partner_id")
    brain = BrainService()
    results = await brain.search_keyword(
        flag_test_conn["conn"], "unique_retrieval_marker_xyz",
    )
    slugs = {r.slug for r in results}
    assert flag_test_conn["public_slug"] in slugs
    assert flag_test_conn["partner_slug"] not in slugs


@pytest.mark.postgres
async def test_search_keyword_shows_partner_internal_with_matching_guc(flag_test_conn):
    """app.partner_id matches: keyword search surfaces that partner's rows."""
    conn = flag_test_conn["conn"]
    await conn.execute(
        "SELECT set_config('app.partner_id', $1, false)",
        flag_test_conn["partner_id"],
    )
    brain = BrainService()
    results = await brain.search_keyword(conn, "unique_retrieval_marker_xyz")
    slugs = {r.slug for r in results}
    assert flag_test_conn["public_slug"] in slugs
    assert flag_test_conn["partner_slug"] in slugs
    await conn.execute("RESET app.partner_id")


@pytest.mark.postgres
async def test_search_keyword_hides_partner_internal_with_wrong_guc(flag_test_conn):
    """app.partner_id set but wrong value: must NOT see other partner's rows."""
    conn = flag_test_conn["conn"]
    await conn.execute(
        "SELECT set_config('app.partner_id', $1, false)",
        str(uuid.uuid4()),
    )
    brain = BrainService()
    results = await brain.search_keyword(conn, "unique_retrieval_marker_xyz")
    slugs = {r.slug for r in results}
    assert flag_test_conn["public_slug"] in slugs
    assert flag_test_conn["partner_slug"] not in slugs
    await conn.execute("RESET app.partner_id")
