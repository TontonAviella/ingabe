import uuid
from contextlib import asynccontextmanager
from datetime import datetime, timezone

import pytest
from botocore.exceptions import ClientError
from fastapi.responses import Response
from PIL import Image

import src.database.pool as pool
from src.routes import project_routes


class FakeBaseMapProvider:
    def __init__(self, preview_path: str):
        self._preview_path = preview_path

    def get_default_preview_path(self) -> str:
        return self._preview_path


class FakeS3:
    async def get_object(self, **_kwargs):
        raise ClientError({"Error": {"Code": "NoSuchKey"}}, "GetObject")

    async def put_object(self, **_kwargs):
        raise AssertionError("invalid renderer output should not be cached")


class FakeConn:
    async def fetchrow(self, query: str, *_args):
        if "user_mundiai_projects" in query:
            return {
                "id": "Ptest123456",
                "owner_uuid": uuid.uuid4(),
                "editor_uuids": [],
                "viewer_uuids": [],
                "link_accessible": False,
                "title": "Preview Test",
                "maps": ["Mtest123456"],
                "map_diff_messages": [],
                "created_on": datetime.now(timezone.utc),
                "soft_deleted_at": None,
            }

        if "user_mundiai_maps" in query:
            return {"layers": ["layer_1"]}

        raise AssertionError(f"unexpected query: {query}")


@asynccontextmanager
async def fake_connection():
    yield FakeConn()


@pytest.mark.anyio
async def test_social_preview_falls_back_when_renderer_returns_non_image(
    monkeypatch,
    tmp_path,
):
    fallback_path = tmp_path / "default.webp"
    Image.new("RGB", (1, 1), color=(0, 0, 0)).save(fallback_path, "WEBP")

    async def fake_get_s3_client():
        return FakeS3()

    async def fake_style(*_args, **_kwargs):
        return "{}"

    async def fake_render(*_args, **_kwargs):
        return Response(content=b"not an image", media_type="application/json"), {}

    monkeypatch.setenv("S3_BUCKET", "test-bucket")
    monkeypatch.setattr(pool, "get_async_read_connection", fake_connection)
    monkeypatch.setattr(project_routes, "get_async_db_connection", fake_connection)
    monkeypatch.setattr(project_routes, "get_async_s3_client", fake_get_s3_client)
    monkeypatch.setattr(project_routes, "get_map_style_internal", fake_style)
    monkeypatch.setattr(project_routes, "render_map_internal", fake_render)

    response = await project_routes.get_project_social_preview(
        "Ptest123456",
        base_map_provider=FakeBaseMapProvider(str(fallback_path)),
    )

    assert response.status_code == 200
    assert response.media_type == "image/webp"
    assert response.body == fallback_path.read_bytes()


def test_default_social_preview_returns_503_when_file_is_missing(tmp_path):
    missing_path = tmp_path / "missing.webp"

    response = project_routes._default_social_preview_response(
        FakeBaseMapProvider(str(missing_path)),
    )

    assert response.status_code == 503
    assert response.media_type == "image/webp"
    assert response.body == b""


# A drone orthophoto near Cyampirita, Rwanda, in a magenta the satellite basemap never shows.
DRONE_BOUNDS = (30.4175, -1.7009, 30.4315, -1.6929)
DRONE_RGB = (230, 20, 200)


def _write_drone_ortho(path) -> None:
    import numpy as np
    import rasterio
    from rasterio.transform import from_bounds

    width, height = 280, 160
    bands = np.zeros((4, height, width), dtype=np.uint8)
    for i, value in enumerate((*DRONE_RGB, 255)):
        bands[i] = value
    with rasterio.open(
        path, "w", driver="GTiff", width=width, height=height, count=4, dtype="uint8",
        crs="EPSG:4326", transform=from_bounds(*DRONE_BOUNDS, width, height),
    ) as dst:
        dst.write(bands)


@pytest.mark.anyio
@pytest.mark.timeout(150)
async def test_social_preview_shows_the_drone_raster_centred(auth_client, tmp_path):
    """2026-10-08: every project card showed the default basemap: the renderer could not fetch the
    raster's relative /api/layer tile URL and the whole render failed with a RenderError."""
    import io

    created = await auth_client.post("/api/maps/create", json={"title": "Drone preview"})
    assert created.status_code == 200, created.text
    project_id = created.json()["project_id"]

    ortho = tmp_path / "drone_ortho.tif"
    _write_drone_ortho(ortho)
    with open(ortho, "rb") as f:
        uploaded = await auth_client.post(
            f"/api/maps/{created.json()['id']}/layers",
            files={"file": ("drone_ortho.tif", f, "image/tiff")},
            data={"layer_name": "Drone ortho"},
        )
    assert uploaded.status_code == 200, uploaded.text
    layer_id, map_id = uploaded.json()["id"], uploaded.json()["dag_child_map_id"]

    # Build the optimized COG the preview reads (a background task, finished when the call returns).
    cog = await auth_client.post(f"/api/maps/{map_id}/layers/{layer_id}/generate-cog")
    assert cog.status_code == 200, cog.text
    render_status = (await auth_client.get(f"/api/layer/{layer_id}/render-status")).json()
    assert render_status.get("status") == "ready", render_status

    response = await auth_client.get(f"/api/projects/{project_id}/social.webp")
    assert response.status_code == 200
    assert response.headers["content-type"] == "image/webp"
    preview = Image.open(io.BytesIO(response.content)).convert("RGB")
    assert preview.size == (1200, 630)
    centre = preview.getpixel((600, 315))
    assert all(abs(got - want) <= 12 for got, want in zip(centre, DRONE_RGB)), centre


# A field boundary next to Cyampirita, filled with a teal the satellite basemap never shows.
FIELD_RGB = (20, 230, 200)


@pytest.mark.anyio
@pytest.mark.timeout(150)
async def test_social_preview_draws_a_postgis_layer_from_its_pmtiles(auth_client):
    """PostGIS layers have only live /api/layer/<id>/{z}/{x}/{y}.mvt tiles, which the renderer
    cannot fetch; the preview builds the layer's PMTiles from its query and draws those."""
    import io
    import json
    import os
    from urllib.parse import quote

    from src.structures import get_async_db_connection
    from src.utils import generate_id

    run_tag = uuid.uuid4().hex[:8]
    table = f"social_preview_fields_{run_tag}"
    created = await auth_client.post("/api/maps/create", json={"title": "PostGIS preview"})
    assert created.status_code == 200, created.text
    project_id, map_id = created.json()["project_id"], created.json()["id"]
    connection_id, layer_id, style_id = generate_id(prefix="C"), generate_id(prefix="L"), generate_id(prefix="S")
    west, south, east, north = DRONE_BOUNDS
    # The app's own database, as Sage's internal Rwanda connection reaches it.
    uri = (
        f"postgresql://{quote(os.environ.get('POSTGRES_USER', 'mundiuser'), safe='')}"
        f":{quote(os.environ.get('POSTGRES_PASSWORD', 'changeme'), safe='')}"
        f"@{os.environ.get('POSTGRES_HOST', 'postgresdb')}:{os.environ.get('POSTGRES_PORT', '5432')}"
        f"/{quote(os.environ['POSTGRES_DB'], safe='')}?sslmode=disable"
    )
    style = [
        {"id": f"{layer_id}-fill", "type": "fill", "source": layer_id, "source-layer": "reprojectedfgb",
         "paint": {"fill-color": "#%02x%02x%02x" % FIELD_RGB, "fill-opacity": 1}},
    ]

    async with get_async_db_connection() as conn:
        owner = str(await conn.fetchval("SELECT owner_uuid FROM user_mundiai_maps WHERE id = $1", map_id))
        await conn.execute(f"CREATE TABLE {table} (id serial PRIMARY KEY, name text, geom geometry(MultiPolygon, 4326))")
        try:
            await conn.execute(
                f"INSERT INTO {table} (name, geom) VALUES ('field', ST_Multi(ST_MakeEnvelope($1, $2, $3, $4, 4326)))",
                west, south, east, north,
            )
            await conn.execute(
                "INSERT INTO project_postgres_connections (id, project_id, user_id, connection_uri, connection_name)"
                " VALUES ($1, $2, $3, $4, 'preview test')",
                connection_id, project_id, owner, uri,
            )
            await conn.execute(
                """
                INSERT INTO map_layers
                (layer_id, owner_uuid, name, type, postgis_connection_id, postgis_query, metadata,
                 feature_count, bounds, geometry_type, source_map_id, created_on, last_edited,
                 postgis_attribute_column_list)
                VALUES ($1, $2, 'Field', 'postgis', $3, $4, '{}', 1, $5, 'multipolygon', $6,
                        CURRENT_TIMESTAMP, CURRENT_TIMESTAMP, ARRAY['name'])
                """,
                layer_id, owner, connection_id, f"SELECT id, name, geom FROM {table}",
                [west, south, east, north], map_id,
            )
            await conn.execute(
                "INSERT INTO layer_styles (style_id, layer_id, style_json, created_by) VALUES ($1, $2, $3, $4)",
                style_id, layer_id, json.dumps(style), owner,
            )
            await conn.execute(
                "INSERT INTO map_layer_styles (map_id, layer_id, style_id) VALUES ($1, $2, $3)",
                map_id, layer_id, style_id,
            )
            await conn.execute(
                "UPDATE user_mundiai_maps SET layers = array_append(COALESCE(layers, '{}'), $1) WHERE id = $2",
                layer_id, map_id,
            )

            response = await auth_client.get(f"/api/projects/{project_id}/social.webp")
            metadata = await conn.fetchval("SELECT metadata FROM map_layers WHERE layer_id = $1", layer_id)
        finally:
            await conn.execute(f"DROP TABLE {table}")

    assert response.status_code == 200
    preview = Image.open(io.BytesIO(response.content)).convert("RGB")
    centre = preview.getpixel((600, 315))
    assert all(abs(got - want) <= 12 for got, want in zip(centre, FIELD_RGB)), centre
    if isinstance(metadata, str):
        metadata = json.loads(metadata)
    assert metadata.get("pmtiles_key"), "the PMTiles built for the preview are recorded for reuse"
