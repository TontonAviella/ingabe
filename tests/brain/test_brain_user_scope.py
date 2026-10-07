"""Sage's memory packet holds only Brain pages in the user's own scope.

On 2026-10-07 the packet for a question about a drone photo carried seven test
pages written by other (random) owners: RLS shows every page with no
access_scope to everyone ("NULL is public"), and the packet padded an empty
result with the newest of those. These tests run the real SQL:
BrainService.slugs_in_user_scope, and build_brain_context_packet end to end.
"""

import uuid

import asyncpg
import pytest
import pytest_asyncio

from src.database.pool import _build_postgres_url
from src.services.brain_context import build_brain_context_packet
from src.services.brain_service import BrainService, ChunkInput, PageInput

pytestmark = pytest.mark.asyncio(loop_scope="module")

RUN_TAG = uuid.uuid4().hex[:8]
USER = str(uuid.uuid4())
STRANGER = str(uuid.uuid4())
PARTNER = str(uuid.uuid4())
OTHER_PARTNER = str(uuid.uuid4())

# A layer on the user's map that a teammate uploaded: its Brain page belongs
# to the teammate but is about what the user is looking at.
LAYER_ID = f"L{RUN_TAG}Ab"
VIEWPORT = (30.41, -1.71, 30.44, -1.69)
IN_VIEWPORT = '{"type":"Point","coordinates":[30.42,-1.70]}'
ORTHO_FOOTPRINT = (
    '{"type":"Polygon","coordinates":[[[30.4175,-1.7009],[30.4315,-1.7009],'
    '[30.4315,-1.6929],[30.4175,-1.6929],[30.4175,-1.7009]]]}'
)


def _slug(name: str) -> str:
    return f"scope-{name}-{RUN_TAG}"


# slug -> (owner, put_page kwargs, in the user's scope?)
PAGES = {
    _slug("mine"): (USER, {}, True),
    _slug("shared-view"): (STRANGER, {"viewer_uuids": [USER]}, True),
    _slug("shared-edit"): (STRANGER, {"editor_uuids": [USER]}, True),
    _slug("public"): (STRANGER, {"access_scope": "public"}, True),
    _slug("my-partner"): (STRANGER, {"access_scope": "partner_internal", "partner_id": PARTNER}, True),
    _slug("stranger"): (STRANGER, {}, False),
    _slug("other-partner"): (STRANGER, {"access_scope": "partner_internal", "partner_id": OTHER_PARTNER}, False),
    _slug("mundi-only"): (STRANGER, {"access_scope": "mundi_only"}, False),
}
STRANGER_TEST_FIELD = _slug("test-field")
ORTHO_SLUG = f"raster-{LAYER_ID.lower()}"


async def _as(conn: asyncpg.Connection, user_id: str, partner_id: str) -> None:
    await conn.execute("SELECT set_config('app.user_id', $1, false)", user_id)
    await conn.execute("SELECT set_config('app.partner_id', $1, false)", partner_id)
    await conn.execute("SELECT set_config('app.role', '', false)")


@pytest_asyncio.fixture(scope="module", loop_scope="module")
async def seeded():
    conn = await asyncpg.connect(_build_postgres_url())
    brain = BrainService()
    await _as(conn, "", "")  # seed with no user: RLS lets the seeding through

    for slug, (owner, kwargs, _) in PAGES.items():
        await brain.put_page(
            conn, slug,
            PageInput(type="field", title=slug, compiled_truth=f"Scope fixture {slug}."),
            owner_uuid=owner, **kwargs,
        )
    # The leftover test page: someone else's, no access_scope, matching the
    # question's words and sitting in the viewport.
    await brain.put_page(
        conn, STRANGER_TEST_FIELD,
        PageInput(
            type="field", title="Gasabo Test Field",
            compiled_truth="A 2-hectare cassava field in Gatsibo district.",
            geom_geojson=IN_VIEWPORT,
        ),
        owner_uuid=STRANGER,
    )
    await brain.upsert_chunks(conn, STRANGER_TEST_FIELD, [
        ChunkInput(chunk_index=0, chunk_text="A 2-hectare cassava field in Gatsibo district."),
    ])
    await brain.put_page(
        conn, ORTHO_SLUG,
        PageInput(
            type="field", title="Raster: Cyampirita_Orthophoto",
            compiled_truth="Raster layer: Cyampirita_Orthophoto. Bands: 4.",
            geom_geojson=ORTHO_FOOTPRINT,
        ),
        owner_uuid=STRANGER,
    )

    yield {"conn": conn, "brain": brain}

    await _as(conn, "", "")
    await conn.execute(
        "DELETE FROM brain_pages WHERE slug = ANY($1::text[])",
        [*PAGES, STRANGER_TEST_FIELD, ORTHO_SLUG],
    )
    await conn.close()


@pytest.mark.postgres
async def test_slugs_in_user_scope_leaves_out_other_owners_unscoped_pages(seeded):
    conn, brain = seeded["conn"], seeded["brain"]
    await _as(conn, USER, PARTNER)

    in_scope = await brain.slugs_in_user_scope(conn, [*PAGES, "no-such-page"])

    assert in_scope == {slug for slug, (_, _, expected) in PAGES.items() if expected}


@pytest.mark.postgres
async def test_slugs_in_user_scope_of_nothing_is_nothing(seeded):
    conn, brain = seeded["conn"], seeded["brain"]
    await _as(conn, USER, PARTNER)

    assert await brain.slugs_in_user_scope(conn, []) == set()


@pytest.mark.postgres
async def test_packet_skips_a_strangers_test_page_that_matches_the_question(seeded):
    conn, brain = seeded["conn"], seeded["brain"]
    await _as(conn, USER, PARTNER)

    packet = await build_brain_context_packet(
        conn, brain,
        query_text="cassava field Gatsibo",
        viewport_bounds=VIEWPORT,
        visible_layer_ids=[LAYER_ID, "LQmvuX9mQavb"],
    )

    assert packet is not None
    assert f"slug={ORTHO_SLUG}" in packet  # on the map, so in scope
    assert STRANGER_TEST_FIELD not in packet
    assert "source=recent" not in packet


@pytest.mark.postgres
async def test_packet_is_empty_when_only_strangers_pages_match(seeded):
    conn, brain = seeded["conn"], seeded["brain"]
    await _as(conn, USER, PARTNER)

    packet = await build_brain_context_packet(
        conn, brain,
        query_text="cassava field Gatsibo",
        viewport_bounds=VIEWPORT,
        visible_layer_ids=[],
    )

    assert packet is None
