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
