"""Pages about layers elsewhere cannot crowd the current map's layers out of the viewport.

On 2026-10-07 every Brain page had the same updated_at (a migration's side
effect), and twenty copies of the Cyampirita orthophoto uploaded to other maps
filled the viewport query's LIMIT; the copy on the user's map was then dropped
by the packet's layer filter, so Sage lost the one page about the photo it was
asked about. The layer filter now runs in SQL, before the LIMIT.
"""

import uuid

import asyncpg
import pytest
import pytest_asyncio

from src.database.pool import _build_postgres_url
from src.services.brain_context import build_brain_context_packet
from src.services.brain_service import BrainService, PageInput

pytestmark = pytest.mark.asyncio(loop_scope="module")

RUN_TAG = uuid.uuid4().hex[:8]
USER = str(uuid.uuid4())
VIEWPORT = (30.41, -1.71, 30.44, -1.69)
IN_VIEWPORT = '{"type":"Point","coordinates":[30.42,-1.70]}'

ON_MAP = f"Lon{RUN_TAG}"
ON_MAP_UNDERSCORE = f"L_{RUN_TAG}"  # `_` must not act as a LIKE wildcard
OFF_MAP = [f"Loff{i}{RUN_TAG}" for i in range(10)]
LOOKALIKE = f"Lx{RUN_TAG}"  # matches layer-l_<tag>-f% only if `_` is a wildcard

SLUG_ON_MAP_RASTER = f"raster-{ON_MAP.lower()}"
SLUG_ON_MAP_FEATURE = f"layer-{ON_MAP_UNDERSCORE.lower()}-f0"
SLUG_LOOKALIKE_FEATURE = f"layer-{LOOKALIKE.lower()}-f0"
SLUG_NOTE = f"bbox-note-{RUN_TAG}"
OFF_MAP_SLUGS = [f"raster-{layer.lower()}" for layer in OFF_MAP]


async def _put(conn, brain, slug, title):
    await brain.put_page(
        conn, slug,
        PageInput(type="field", title=title, compiled_truth=f"{title}.", geom_geojson=IN_VIEWPORT),
        owner_uuid=USER,
    )


@pytest_asyncio.fixture(scope="module", loop_scope="module")
async def seeded():
    conn = await asyncpg.connect(_build_postgres_url())
    brain = BrainService()
    await conn.execute("SELECT set_config('app.user_id', '', false)")
    # Oldest first: the pages that must survive are the oldest ones.
    await _put(conn, brain, SLUG_ON_MAP_RASTER, "Cyampirita_Orthophoto on this map")
    await _put(conn, brain, SLUG_ON_MAP_FEATURE, "Plot 1 on this map")
    await _put(conn, brain, SLUG_NOTE, "Field visit note")
    await _put(conn, brain, SLUG_LOOKALIKE_FEATURE, "Plot on another map")
    for slug in OFF_MAP_SLUGS:
        await _put(conn, brain, slug, "Cyampirita_Orthophoto on another map")
    await conn.execute("SELECT set_config('app.user_id', $1, false)", USER)
    await conn.execute("SELECT set_config('app.partner_id', '', false)")

    yield {"conn": conn, "brain": brain}

    await conn.execute("SELECT set_config('app.user_id', '', false)")
    await conn.execute(
        "DELETE FROM brain_pages WHERE slug = ANY($1::text[])",
        [SLUG_ON_MAP_RASTER, SLUG_ON_MAP_FEATURE, SLUG_NOTE, SLUG_LOOKALIKE_FEATURE, *OFF_MAP_SLUGS],
    )
    await conn.close()


@pytest.mark.postgres
async def test_without_layer_ids_newer_pages_elsewhere_fill_the_limit(seeded):
    """The failure mode: an unfiltered query returns only the newest pages."""
    pages = await seeded["brain"].get_pages_in_bbox(seeded["conn"], VIEWPORT, limit=8)

    assert SLUG_ON_MAP_RASTER not in {p.slug for p in pages}


@pytest.mark.postgres
async def test_layer_ids_keep_this_maps_layer_pages_and_other_pages(seeded):
    pages = await seeded["brain"].get_pages_in_bbox(
        seeded["conn"], VIEWPORT, limit=8, layer_ids=[ON_MAP, ON_MAP_UNDERSCORE],
    )
    slugs = {p.slug for p in pages}

    assert {SLUG_ON_MAP_RASTER, SLUG_ON_MAP_FEATURE, SLUG_NOTE} <= slugs
    assert not slugs & set(OFF_MAP_SLUGS)
    assert SLUG_LOOKALIKE_FEATURE not in slugs


@pytest.mark.postgres
async def test_empty_layer_ids_keep_only_pages_that_are_not_about_a_layer(seeded):
    pages = await seeded["brain"].get_pages_in_bbox(seeded["conn"], VIEWPORT, limit=8, layer_ids=[])

    assert {p.slug for p in pages} >= {SLUG_NOTE}
    assert not any(p.slug.startswith(("raster-", "layer-")) for p in pages)


@pytest.mark.postgres
async def test_packet_keeps_the_orthophoto_on_this_map_despite_newer_copies_elsewhere(seeded):
    packet = await build_brain_context_packet(
        seeded["conn"], seeded["brain"],
        query_text="How is cassava doing around this photo?",
        viewport_bounds=VIEWPORT,
        visible_layer_ids=[ON_MAP, ON_MAP_UNDERSCORE],
    )

    assert packet is not None
    assert f"slug={SLUG_ON_MAP_RASTER}" in packet
    assert f"slug={SLUG_ON_MAP_FEATURE}" in packet
    for slug in OFF_MAP_SLUGS:
        assert slug not in packet
