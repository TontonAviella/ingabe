"""Which maps a reader can choose as a photo's plots (needs the test database)."""

import json

import pytest

from src.database.rwanda_reader import RWANDA_INTERNAL_CONNECTION_NAME
from src.services import photo_plots
from src.structures import async_conn
from src.utils import generate_id

BOUNDS = [30.42, -1.70, 30.44, -1.68]


async def _add_connection(conn, project_id: str, owner, name: str) -> str:
    connection_id = generate_id(prefix="C")
    await conn.execute(
        "INSERT INTO project_postgres_connections (id, project_id, user_id, connection_uri, connection_name) VALUES ($1, $2, $3, $4, $5)",
        connection_id, project_id, owner, "postgresql://example.invalid/db", name,
    )
    return connection_id


async def _add_layer(conn, map_id: str, owner, name: str, kind: str, metadata: dict, connection_id: str | None = None) -> str:
    layer_id = generate_id(prefix="L")
    await conn.execute(
        """
        INSERT INTO map_layers (layer_id, owner_uuid, name, type, metadata, bounds, geometry_type, feature_count,
                                source_map_id, postgis_connection_id, postgis_query, created_on, last_edited)
        VALUES ($1, $2, $3, $4, $5, $6, $7, 3, $8, $9, $10, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)
        """,
        layer_id, owner, name, kind, json.dumps(metadata), BOUNDS, None if kind == "raster" else "multipolygon", map_id,
        connection_id, "SELECT 1" if connection_id else None,
    )
    await conn.execute("UPDATE user_mundiai_maps SET layers = array_append(COALESCE(layers, '{}'), $1) WHERE id = $2", layer_id, map_id)
    return layer_id


@pytest.mark.anyio
async def test_only_the_readers_own_maps_can_be_a_photos_plots(auth_client):
    created = await auth_client.post("/api/maps/create", json={"title": "Plot map choices"})
    assert created.status_code == 200
    map_id = created.json()["id"]

    async with async_conn("test_photo_plots") as conn:
        row = await conn.fetchrow("SELECT owner_uuid, project_id FROM user_mundiai_maps WHERE id = $1", map_id)
        owner, project_id = row["owner_uuid"], row["project_id"]
        photo = await _add_layer(conn, map_id, owner, "Photo", "raster", {})
        own = await _add_layer(conn, map_id, owner, "Cooperative blocks", "vector", {})
        hexagons = await _add_layer(conn, map_id, owner, "Raster Context - Photo", "vector",
                                    {"source": "sage_raster_h3_context", "analysis_kind": "raster_h3_context"})
        outlines = await _add_layer(conn, map_id, owner, "Roofs", "vector",
                                    {"source": "sage_raster_object_candidates", "analysis_kind": "raster_object_candidates"})
        platform = await _add_connection(conn, project_id, owner, RWANDA_INTERNAL_CONNECTION_NAME)
        boundaries = await _add_layer(conn, map_id, owner, "Sector boundaries", "postgis", {"fast_admin_boundary": True}, platform)
        indices = await _add_layer(conn, map_id, owner, "Sector Agri Indices", "postgis", {"deckgl_3d": True}, platform)
        readers_db = await _add_connection(conn, project_id, owner, "Cooperative database")
        own_postgis = await _add_layer(conn, map_id, owner, "Cooperative plots", "postgis", {}, readers_db)
        maps = await photo_plots.plot_maps(conn, photo, BOUNDS)

    offered = {plot_map.layer_id for plot_map, _ in maps}
    assert offered == {own, own_postgis}
    assert not offered & {hexagons, outlines, boundaries, indices}

    # A photo already set to the hexagons goes back to the plots Ingabe found.
    chosen = await photo_plots.plots_from_chosen_map(None, "", photo, {photo_plots.PLOT_MAP_KEY: hexagons}, "", "", maps)
    assert chosen == (None, None, None)
