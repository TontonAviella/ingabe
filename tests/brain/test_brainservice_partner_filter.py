"""BrainService application-layer partner filter tests (P1-4).

Validates that _PARTNER_FILTER is applied to ALL BrainService read methods,
not just search. Each method is tested with cross-partner data: partner A's
pages must be invisible when queried from partner B's session.

This is defense-in-depth on top of RLS. If RLS fails (e.g. BYPASSRLS
accidentally re-granted), these application-layer filters are the last gate.
"""

import uuid
from datetime import date as date_type

import asyncpg
import pytest
import pytest_asyncio

from src.database.pool import _build_postgres_url
from src.services.brain_service import BrainService, PageInput, TimelineInput

pytestmark = pytest.mark.asyncio(loop_scope="module")

PARTNER_A = str(uuid.uuid4())
PARTNER_B = str(uuid.uuid4())
USER_A = str(uuid.uuid4())
USER_B = str(uuid.uuid4())
RUN_TAG = uuid.uuid4().hex[:8]


async def _set_gucs(conn, user_id: str, partner_id: str):
    await conn.execute("SELECT set_config('app.user_id', $1, false)", user_id)
    await conn.execute("SELECT set_config('app.partner_id', $1, false)", partner_id)
    await conn.execute("SELECT set_config('app.role', '', false)")


async def _clear_gucs(conn):
    await conn.execute("SELECT set_config('app.user_id', '', false)")
    await conn.execute("SELECT set_config('app.partner_id', '', false)")
    await conn.execute("SELECT set_config('app.role', '', false)")


@pytest_asyncio.fixture(scope="module", loop_scope="module")
async def seeded():
    """Seed one partner_internal page for partner A and one public page."""
    from src.database.migrate import run_migrations
    await run_migrations()

    conn = await asyncpg.connect(_build_postgres_url())
    brain = BrainService()

    slug_a = f"bsf-a-internal-{RUN_TAG}"
    slug_pub = f"bsf-public-{RUN_TAG}"

    # Seed as no-user (bypass RLS for seeding)
    await _clear_gucs(conn)

    await brain.put_page(
        conn, slug_a,
        PageInput(
            type="source_document",
            title=f"Partner A Secret {RUN_TAG}",
            compiled_truth="Confidential insurance data for cooperative Gabiro.",
            frontmatter={"source_type": "partner_upload"},
        ),
        owner_uuid=USER_A,
    )
    await conn.execute(
        """
        UPDATE brain_pages
        SET access_scope = 'partner_internal', partner_id = $2::uuid
        WHERE slug = $1
        """,
        slug_a, PARTNER_A,
    )

    await brain.put_page(
        conn, slug_pub,
        PageInput(
            type="field",
            title=f"Public Knowledge {RUN_TAG}",
            compiled_truth="Rwanda has two rainy seasons.",
            frontmatter={},
        ),
        owner_uuid=USER_A,
    )
    await conn.execute(
        "UPDATE brain_pages SET access_scope = 'public' WHERE slug = $1",
        slug_pub,
    )

    # Add timeline entry to partner A's page (for get_timeline test)
    await _set_gucs(conn, USER_A, PARTNER_A)
    await brain.add_timeline_entry(
        conn, slug_a,
        TimelineInput(
            date=date_type(2026, 4, 21),
            summary="Initial upload",
            source="partner_upload",
        ),
        owner_uuid=USER_A,
    )

    yield {
        "conn": conn,
        "brain": brain,
        "slug_a": slug_a,
        "slug_pub": slug_pub,
    }

    await _clear_gucs(conn)
    await conn.execute(
        "DELETE FROM brain_pages WHERE slug = ANY($1::text[])",
        [slug_a, slug_pub],
    )
    await conn.close()


# ---------------------------------------------------------------------------
# get_page
# ---------------------------------------------------------------------------

@pytest.mark.postgres
async def test_get_page_blocked_cross_partner(seeded):
    conn, brain = seeded["conn"], seeded["brain"]
    await _set_gucs(conn, USER_B, PARTNER_B)

    page = await brain.get_page(conn, seeded["slug_a"])
    assert page is None, "get_page leaked partner A's page to partner B"


@pytest.mark.postgres
async def test_get_page_visible_to_owner(seeded):
    conn, brain = seeded["conn"], seeded["brain"]
    await _set_gucs(conn, USER_A, PARTNER_A)

    page = await brain.get_page(conn, seeded["slug_a"])
    assert page is not None, "Partner A can't see their own page"


@pytest.mark.postgres
async def test_get_page_public_visible_to_all(seeded):
    conn, brain = seeded["conn"], seeded["brain"]
    await _set_gucs(conn, USER_B, PARTNER_B)

    page = await brain.get_page(conn, seeded["slug_pub"])
    assert page is not None, "Public page invisible to partner B"


# ---------------------------------------------------------------------------
# list_pages
# ---------------------------------------------------------------------------

@pytest.mark.postgres
async def test_list_pages_excludes_other_partner(seeded):
    conn, brain = seeded["conn"], seeded["brain"]
    await _set_gucs(conn, USER_B, PARTNER_B)

    pages = await brain.list_pages(conn, limit=500)
    slugs = [p.slug for p in pages]
    assert seeded["slug_a"] not in slugs, "list_pages leaked partner A's page"


@pytest.mark.postgres
async def test_list_pages_includes_own_and_public(seeded):
    conn, brain = seeded["conn"], seeded["brain"]
    await _set_gucs(conn, USER_A, PARTNER_A)

    pages = await brain.list_pages(conn, limit=500)
    slugs = [p.slug for p in pages]
    assert seeded["slug_a"] in slugs, "Own partner page missing from list"
    assert seeded["slug_pub"] in slugs, "Public page missing from list"


# ---------------------------------------------------------------------------
# search_keyword
# ---------------------------------------------------------------------------

@pytest.mark.postgres
async def test_search_keyword_blocked_cross_partner(seeded):
    conn, brain = seeded["conn"], seeded["brain"]
    await _set_gucs(conn, USER_B, PARTNER_B)

    results = await brain.search_keyword(conn, "Gabiro", limit=50)
    slugs = [r.slug for r in results]
    assert seeded["slug_a"] not in slugs, "search_keyword leaked partner A's page"


@pytest.mark.postgres
async def test_search_keyword_finds_own(seeded):
    conn, brain = seeded["conn"], seeded["brain"]
    await _set_gucs(conn, USER_A, PARTNER_A)

    results = await brain.search_keyword(conn, "Gabiro", limit=50)
    slugs = [r.slug for r in results]
    assert seeded["slug_a"] in slugs, "Partner A can't find their own page via keyword search"


# ---------------------------------------------------------------------------
# get_chunks
# ---------------------------------------------------------------------------

@pytest.mark.postgres
async def test_get_chunks_blocked_cross_partner(seeded):
    conn, brain = seeded["conn"], seeded["brain"]
    await _set_gucs(conn, USER_B, PARTNER_B)

    chunks = await brain.get_chunks(conn, seeded["slug_a"])
    assert len(chunks) == 0, "get_chunks leaked partner A's data"


# ---------------------------------------------------------------------------
# get_timeline
# ---------------------------------------------------------------------------

@pytest.mark.postgres
async def test_get_timeline_blocked_cross_partner(seeded):
    conn, brain = seeded["conn"], seeded["brain"]
    await _set_gucs(conn, USER_B, PARTNER_B)

    entries = await brain.get_timeline(conn, seeded["slug_a"])
    assert len(entries) == 0, "get_timeline leaked partner A's entries"


# ---------------------------------------------------------------------------
# get_links / get_backlinks
# ---------------------------------------------------------------------------

@pytest.mark.postgres
async def test_get_links_blocked_cross_partner(seeded):
    conn, brain = seeded["conn"], seeded["brain"]
    await _set_gucs(conn, USER_B, PARTNER_B)

    links = await brain.get_links(conn, seeded["slug_a"])
    assert len(links) == 0, "get_links leaked partner A's links"


@pytest.mark.postgres
async def test_get_backlinks_blocked_cross_partner(seeded):
    conn, brain = seeded["conn"], seeded["brain"]
    await _set_gucs(conn, USER_B, PARTNER_B)

    links = await brain.get_backlinks(conn, seeded["slug_a"])
    assert len(links) == 0, "get_backlinks leaked partner A's backlinks"


# ---------------------------------------------------------------------------
# get_tags
# ---------------------------------------------------------------------------

@pytest.mark.postgres
async def test_get_tags_blocked_cross_partner(seeded):
    conn, brain = seeded["conn"], seeded["brain"]
    await _set_gucs(conn, USER_B, PARTNER_B)

    tags = await brain.get_tags(conn, seeded["slug_a"])
    assert len(tags) == 0, "get_tags leaked partner A's tags"


# ---------------------------------------------------------------------------
# get_raw_data
# ---------------------------------------------------------------------------

@pytest.mark.postgres
async def test_get_raw_data_blocked_cross_partner(seeded):
    conn, brain = seeded["conn"], seeded["brain"]
    await _set_gucs(conn, USER_B, PARTNER_B)

    data = await brain.get_raw_data(conn, seeded["slug_a"])
    assert len(data) == 0, "get_raw_data leaked partner A's raw data"


# ---------------------------------------------------------------------------
# get_versions
# ---------------------------------------------------------------------------

@pytest.mark.postgres
async def test_get_versions_blocked_cross_partner(seeded):
    conn, brain = seeded["conn"], seeded["brain"]
    await _set_gucs(conn, USER_B, PARTNER_B)

    versions = await brain.get_versions(conn, seeded["slug_a"])
    assert len(versions) == 0, "get_versions leaked partner A's versions"


# ---------------------------------------------------------------------------
# get_stats — aggregate leak test
# ---------------------------------------------------------------------------

@pytest.mark.postgres
async def test_get_stats_excludes_other_partner_counts(seeded):
    """Aggregate counts must not include other partners' data."""
    conn, brain = seeded["conn"], seeded["brain"]

    # Get stats as partner A (owns 1 partner_internal page)
    await _set_gucs(conn, USER_A, PARTNER_A)
    stats_a = await brain.get_stats(conn)

    # Get stats as partner B (owns 0 pages)
    await _set_gucs(conn, USER_B, PARTNER_B)
    stats_b = await brain.get_stats(conn)

    # Partner B's total should be less than or equal to A's
    # (B sees only public, A sees public + their own)
    assert stats_b.get("total_pages", 0) <= stats_a.get("total_pages", 0), (
        "Partner B sees more pages than A in stats. Aggregate leak."
    )


# ---------------------------------------------------------------------------
# get_health — aggregate leak test
# ---------------------------------------------------------------------------

@pytest.mark.postgres
async def test_get_health_excludes_other_partner_counts(seeded):
    conn, brain = seeded["conn"], seeded["brain"]

    await _set_gucs(conn, USER_A, PARTNER_A)
    health_a = await brain.get_health(conn)

    await _set_gucs(conn, USER_B, PARTNER_B)
    health_b = await brain.get_health(conn)

    a_pages = health_a.get("total_pages", 0)
    b_pages = health_b.get("total_pages", 0)
    assert b_pages <= a_pages, (
        "Partner B sees more pages than A in health check. Aggregate leak."
    )
