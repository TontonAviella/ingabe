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


def _square(west: float, south: float) -> dict:
    return {"type": "Polygon", "coordinates": [[[west, south], [west + 0.001, south], [west + 0.001, south + 0.001],
                                                [west, south + 0.001], [west, south]]]}


@pytest.mark.anyio
async def test_outlined_fields_are_the_plots_until_the_reader_chooses_otherwise(monkeypatch):
    from src.services import drone_plots, field_taps
    from src.services.test_farm_records import _FakeS3

    s3, metadata = _FakeS3(), {"cog_key": "photo.tif"}
    monkeypatch.setenv("FIELD_TAPS", "1")
    assert await photo_plots.outlined_fields(s3, "b", "layer-1", metadata) is None  # nothing kept: nothing to choose

    rows = [{"id": "a", "number": 3, "geometry": _square(30.42, -1.70)},
            {"id": "b", "number": 7, "geometry": _square(30.43, -1.69)}]
    s3.objects[field_taps._kept_key("photo.tif", "layer-1")] = json.dumps({"outlines": rows, "next_number": 8}).encode()
    source, version = await photo_plots.outlined_fields(s3, "b", "layer-1", metadata)
    assert (source.layer_id, source.name, source.shapes) == (photo_plots.FIELDS, photo_plots.FIELDS_NAME, 2)
    own_map = (drone_plots.PlotMap(layer_id="Lmap", name="Farm map", shapes=4), "Lmap:v1")
    maps = [(source, version), own_map]

    # nothing chosen: the fields; a chosen map wins; "found" goes back to Ingabe's plots; a map that is gone, too
    assert photo_plots.chosen_source(metadata, maps) == photo_plots.FIELDS
    assert photo_plots.chosen_source({**metadata, photo_plots.PLOT_MAP_KEY: "Lmap"}, maps) == "Lmap"
    assert photo_plots.chosen_source({**metadata, photo_plots.PLOT_MAP_KEY: photo_plots.FOUND}, maps) is None
    assert photo_plots.chosen_source({**metadata, photo_plots.PLOT_MAP_KEY: "Lgone"}, maps) is None
    assert photo_plots.chosen_source(metadata, [own_map]) is None

    # each field is a plot under its own field number
    plots = await photo_plots._field_plots(s3, "b", "layer-1", metadata)
    assert [p.number for p in plots] == [3, 7] and plots[0].geometry.bounds[0] == pytest.approx(30.42)

    # the version follows the fields: moving a line or removing a field makes a new plot set
    rows[1]["geometry"] = _square(30.431, -1.69)
    s3.objects[field_taps._kept_key("photo.tif", "layer-1")] = json.dumps({"outlines": rows, "next_number": 8}).encode()
    assert (await photo_plots.outlined_fields(s3, "b", "layer-1", metadata))[1] != version

    monkeypatch.delenv("FIELD_TAPS")
    assert await photo_plots.outlined_fields(s3, "b", "layer-1", metadata) is None  # switch off: the fields are not a source


def test_checks_stay_with_outlined_fields_but_not_with_plots_found_again():
    import dataclasses

    from src.services import drone_plots, field_checks

    found = drone_plots.PlotSet(geojson={"type": "FeatureCollection", "features": []}, found_at="2026-11-13T08:00:00+00:00",
                                seconds=1.0, source="found", read_m_per_px=0.1)
    later = dataclasses.replace(found, found_at="2026-11-13T09:00:00+00:00")
    # plots Ingabe found again are new polygons under old numbers: their checks must not carry over
    assert field_checks.checks_set_id("photo.tif", found) != field_checks.checks_set_id("photo.tif", later)
    assert field_checks.checks_set_id("photo.tif", found) == field_checks.plot_set_id("photo.tif", found)
    # a kept field never changes and its number is never reused: its checks stay when other fields come and go
    fields = dataclasses.replace(found, source=drone_plots.OUTLINED_FIELDS_NAME)
    fields_later = dataclasses.replace(later, source=drone_plots.OUTLINED_FIELDS_NAME)
    assert (field_checks.checks_set_id("photo.tif", fields, "layer-1")
            == field_checks.checks_set_id("photo.tif", fields_later, "layer-1"))
    assert field_checks.checks_set_id("photo.tif", fields, "layer-1") != field_checks.checks_set_id("other.tif", fields, "layer-1")
    # the fields belong to one photo layer: a copy of the photo has other fields under the same numbers
    assert field_checks.checks_set_id("photo.tif", fields, "layer-1") != field_checks.checks_set_id("photo.tif", fields, "layer-2")
    with pytest.raises(ValueError, match="belong to a photo layer"):
        field_checks.checks_set_id("photo.tif", fields)
    # everything measured from pixels (bare spots, fingerprints) still follows the exact version of the plots
    assert field_checks.plot_set_id("photo.tif", fields) != field_checks.plot_set_id("photo.tif", fields_later)
