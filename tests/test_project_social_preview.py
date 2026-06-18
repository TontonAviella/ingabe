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
