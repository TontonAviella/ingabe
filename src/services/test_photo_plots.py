"""Which maps a reader can choose as a photo's plots (needs the test database)."""

import json

import pytest

from src.services import photo_plots
from src.structures import async_conn
from src.utils import generate_id

BOUNDS = [30.42, -1.70, 30.44, -1.68]


async def _add_layer(conn, map_id: str, owner, name: str, kind: str, metadata: dict) -> str:
    layer_id = generate_id(prefix="L")
    await conn.execute(
        """
        INSERT INTO map_layers (layer_id, owner_uuid, name, type, metadata, bounds, geometry_type, feature_count,
                                source_map_id, created_on, last_edited)
        VALUES ($1, $2, $3, $4, $5, $6, $7, 3, $8, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)
        """,
        layer_id, owner, name, kind, json.dumps(metadata), BOUNDS, "multipolygon" if kind == "vector" else None, map_id,
    )
    await conn.execute("UPDATE user_mundiai_maps SET layers = array_append(COALESCE(layers, '{}'), $1) WHERE id = $2", layer_id, map_id)
    return layer_id


@pytest.mark.anyio
async def test_only_the_readers_own_maps_can_be_a_photos_plots(auth_client):
    created = await auth_client.post("/api/maps/create", json={"title": "Plot map choices"})
    assert created.status_code == 200
    map_id = created.json()["id"]

    async with async_conn("test_photo_plots") as conn:
        owner = (await conn.fetchrow("SELECT owner_uuid FROM user_mundiai_maps WHERE id = $1", map_id))["owner_uuid"]
        photo = await _add_layer(conn, map_id, owner, "Photo", "raster", {})
        own = await _add_layer(conn, map_id, owner, "Cooperative blocks", "vector", {})
        hexagons = await _add_layer(conn, map_id, owner, "Raster Context - Photo", "vector",
                                    {"source": "sage_raster_h3_context", "analysis_kind": "raster_h3_context"})
        outlines = await _add_layer(conn, map_id, owner, "Roofs", "vector",
                                    {"source": "sage_raster_object_candidates", "analysis_kind": "raster_object_candidates"})
        maps = await photo_plots.plot_maps(conn, photo, BOUNDS)

    offered = {plot_map.layer_id for plot_map, _ in maps}
    assert own in offered
    assert not offered & {hexagons, outlines}

    # A photo already set to the hexagons goes back to the plots Ingabe found.
    chosen = await photo_plots.plots_from_chosen_map(None, "", photo, {photo_plots.PLOT_MAP_KEY: hexagons}, "", "", maps)
    assert chosen == (None, None, None)
