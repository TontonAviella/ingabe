"""BrainService application-layer partner filter tests (P1-4).

Validates that PAGE_SCOPE_FILTER is applied to ALL BrainService read methods,
not just search. Each method is tested with cross-partner data: partner A's
pages must be invisible when queried from partner B's session.

This is defense-in-depth on top of RLS. If RLS fails (e.g. BYPASSRLS
accidentally re-granted), these application-layer filters are the last gate.

The same readers must keep a user's private page (put_page's default since
2026-10-07, when a page with no scope still counted as public) away from
every other user, including colleagues at the owner's partner, while its
owner, the users it is shared with and workers still read it.
"""

import uuid
from datetime import date as date_type

import asyncpg
import pytest
import pytest_asyncio

from src.database.pool import _build_postgres_url
from src.services.brain_service import (
    BrainService,
    ChunkInput,
    PageInput,
    TimelineInput,
)

pytestmark = pytest.mark.asyncio(loop_scope="module")

PARTNER_A = str(uuid.uuid4())
PARTNER_B = str(uuid.uuid4())
USER_A = str(uuid.uuid4())
USER_B = str(uuid.uuid4())
RUN_TAG = uuid.uuid4().hex[:8]

# A drone orthophoto page as brain_hook_processor writes it: no scope given.
PRIVATE_WORD = f"cyampirita{RUN_TAG}"
PRIVATE_GEOM = '{"type":"Point","coordinates":[30.42,-1.70]}'
PRIVATE_BBOX = (30.41, -1.71, 30.44, -1.69)
# A unit vector only this module's chunk points along (nomic-embed-text: 768).
PRIVATE_EMBEDDING = [0.0] * 768
PRIVATE_EMBEDDING[int(RUN_TAG, 16) % 768] = 1.0


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


# ---------------------------------------------------------------------------
# Private pages (put_page's default scope)
# ---------------------------------------------------------------------------


@pytest_asyncio.fixture(scope="module", loop_scope="module")
async def private_pages(seeded):
    """USER_A's orthophoto page and USER_B's page shared with USER_A, both
    written by a worker without naming a scope."""
    conn, brain = seeded["conn"], seeded["brain"]
    slug_mine = f"bsf-a-private-{RUN_TAG}"
    slug_shared = f"bsf-b-private-shared-{RUN_TAG}"

    await _clear_gucs(conn)
    await brain.put_page(
        conn, slug_mine,
        PageInput(
            type="field",
            title=f"Raster: {PRIVATE_WORD}",
            compiled_truth=f"Raster layer: {PRIVATE_WORD} orthophoto. Bands: 4.",
            geom_geojson=PRIVATE_GEOM,
        ),
        owner_uuid=USER_A,
    )
    await brain.upsert_chunks(conn, slug_mine, [
        ChunkInput(
            chunk_index=0,
            chunk_text=f"Raster layer: {PRIVATE_WORD} orthophoto.",
            embedding=PRIVATE_EMBEDDING,
            model="nomic-embed-text",
        ),
    ])
    await brain.add_timeline_entry(
        conn, slug_mine,
        TimelineInput(date=date_type(2026, 10, 7), summary="Raster uploaded"),
        owner_uuid=USER_A,
    )
    await brain.put_page(
        conn, slug_shared,
        PageInput(
            type="field",
            title=f"Shared {PRIVATE_WORD}",
            compiled_truth=f"Shared field notes {PRIVATE_WORD}.",
        ),
        owner_uuid=USER_B,
        viewer_uuids=[USER_A],
    )

    yield {"mine": slug_mine, "shared": slug_shared}

    await _clear_gucs(conn)
    await conn.execute(
        "DELETE FROM brain_pages WHERE slug = ANY($1::text[])",
        [slug_mine, slug_shared],
    )


async def _slugs_each_reader_returns(brain, conn, slug: str) -> dict[str, bool]:
    """Whether each BrainService reader returns the page (or its rows)."""
    keyword = await brain.search_keyword(conn, PRIVATE_WORD, limit=50)
    vector = await brain.search_vector(conn, PRIVATE_EMBEDDING, limit=50)
    hybrid = await brain.search_hybrid(conn, PRIVATE_WORD, embedding=PRIVATE_EMBEDDING, limit=50)
    listed = await brain.list_pages(conn, limit=500)
    in_bbox = await brain.get_pages_in_bbox(conn, PRIVATE_BBOX, limit=500)
    return {
        "get_page": await brain.get_page(conn, slug) is not None,
        "list_pages": slug in {p.slug for p in listed},
        "search_keyword": slug in {r.slug for r in keyword},
        "search_vector": slug in {r.slug for r in vector},
        "search_hybrid": slug in {r.slug for r in hybrid},
        "get_pages_in_bbox": slug in {p.slug for p in in_bbox},
        "get_chunks": bool(await brain.get_chunks(conn, slug)),
        "get_timeline": bool(await brain.get_timeline(conn, slug)),
        "slugs_in_user_scope": slug in await brain.slugs_in_user_scope(conn, [slug]),
    }


@pytest.mark.postgres
async def test_put_page_without_a_scope_makes_a_private_page(seeded, private_pages):
    conn = seeded["conn"]
    await _clear_gucs(conn)

    scope = await conn.fetchval(
        "SELECT access_scope FROM brain_pages WHERE slug = $1", private_pages["mine"]
    )
    assert scope == "private"


@pytest.mark.postgres
async def test_put_page_without_a_scope_keeps_an_existing_scope(seeded):
    """Re-writing a shared page without naming a scope must not make it private
    (the ingestion normalizer and partner uploads re-put before re-scoping)."""
    conn, brain = seeded["conn"], seeded["brain"]
    await _clear_gucs(conn)

    for slug in (seeded["slug_pub"], seeded["slug_a"]):
        before = await conn.fetchrow(
            "SELECT type, title, compiled_truth, access_scope FROM brain_pages WHERE slug = $1",
            slug,
        )
        # Same content, so the other tests' searches still find it.
        await brain.put_page(
            conn, slug,
            PageInput(type=before["type"], title=before["title"],
                      compiled_truth=before["compiled_truth"]),
            owner_uuid=USER_A,
        )
        after = await conn.fetchval("SELECT access_scope FROM brain_pages WHERE slug = $1", slug)
        assert after == before["access_scope"], slug


@pytest.mark.postgres
@pytest.mark.parametrize(
    "partner",
    [PARTNER_B, PARTNER_A, ""],
    ids=["other-partner", "owners-partner", "no-partner"],
)
async def test_private_page_hidden_from_another_user_by_every_reader(
    seeded, private_pages, partner
):
    conn, brain = seeded["conn"], seeded["brain"]
    await _set_gucs(conn, USER_B, partner)

    returned = await _slugs_each_reader_returns(brain, conn, private_pages["mine"])

    assert not any(returned.values()), f"another user read a private page through {returned}"


@pytest.mark.postgres
async def test_private_page_visible_to_its_owner_by_every_reader(seeded, private_pages):
    conn, brain = seeded["conn"], seeded["brain"]
    await _set_gucs(conn, USER_A, PARTNER_A)

    returned = await _slugs_each_reader_returns(brain, conn, private_pages["mine"])

    assert all(returned.values()), f"the owner lost their private page in {returned}"


@pytest.mark.postgres
async def test_private_page_shared_with_a_viewer_reaches_them(seeded, private_pages):
    conn, brain = seeded["conn"], seeded["brain"]
    await _set_gucs(conn, USER_A, PARTNER_A)

    assert await brain.get_page(conn, private_pages["shared"]) is not None
    found = await brain.search_keyword(conn, PRIVATE_WORD, limit=50)
    assert private_pages["shared"] in {r.slug for r in found}


@pytest.mark.postgres
async def test_worker_session_still_reads_private_pages(seeded, private_pages):
    """The hook processor and the embeddings backfill run with an empty
    app.user_id and must still resolve every user's private pages."""
    conn, brain = seeded["conn"], seeded["brain"]
    await _clear_gucs(conn)

    assert await brain.get_page(conn, private_pages["mine"]) is not None
    assert private_pages["mine"] in {
        p.slug for p in await brain.get_pages_in_bbox(conn, PRIVATE_BBOX, limit=500)
    }
    # A worker is not a user: the per-user scope check leaves private pages out.
    assert await brain.slugs_in_user_scope(conn, [private_pages["mine"]]) == set()
