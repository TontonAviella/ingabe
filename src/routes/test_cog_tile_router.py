import json
from contextlib import asynccontextmanager

import pytest
from fastapi import HTTPException

from src.routes import cog_tile_router as router


@pytest.fixture
def anyio_backend():
    return "asyncio"


def test_local_layer_id_accepts_only_safe_layer_refs():
    assert router._local_layer_id("mundi-layer:L9r2NA2L7sye") == "L9r2NA2L7sye"
    assert router._local_layer_id("/api/layer/L9r2NA2L7sye.cog.tif") == "L9r2NA2L7sye"
    assert router._local_layer_id("http://172.19.0.4:9000/test-bucket/cog.tif") is None
    assert router._local_layer_id("mundi-layer:http://172.19.0.4") is None


@pytest.mark.anyio
async def test_untrusted_remote_cog_url_is_blocked_before_rendering():
    with pytest.raises(HTTPException) as exc:
        await router._resolve_cog_asset(
            "https://attacker.example/test-bucket/cog.tif",
            request=object(),
        )
    assert exc.value.status_code == 400
    assert "Remote COG host is not trusted" in str(exc.value.detail)


@pytest.mark.anyio
async def test_known_remote_cog_url_resolves_without_authentication():
    asset_url = (
        "https://sentinel-cogs.s3.us-west-2.amazonaws.com/"
        "sentinel-s2-l2a-cogs/35/M/RT/scene/TCI.tif"
    )

    assert await router._resolve_cog_asset(asset_url, request=object()) == (
        asset_url,
        asset_url,
    )


@pytest.mark.anyio
async def test_mundi_layer_ref_resolves_server_side_without_private_url_validation(
    monkeypatch,
):
    calls = {"validated": False}

    class FakeSession:
        def get_user_id(self):
            return "user-1"

    async def fake_verify_session_required(_request):
        return FakeSession()

    class FakeConn:
        async def fetchrow(self, _query, _layer_id):
            return {
                "layer_id": "L9r2NA2L7sye",
                "owner_uuid": "user-1",
                "metadata": json.dumps({"cog_key": "cog/layer/L9r2NA2L7sye.cog.tif"}),
                "s3_key": "uploads/L9r2NA2L7sye.tif",
            }

    @asynccontextmanager
    async def fake_async_read_conn(*_args, **_kwargs):
        yield FakeConn()

    class FakeS3:
        async def generate_presigned_url(self, *_args, **_kwargs):
            return "http://minio:9000/test-bucket/cog/layer/L9r2NA2L7sye.cog.tif?sig=1"

    async def fake_get_async_s3_client(*_args, **_kwargs):
        return FakeS3()

    async def fake_s3_op(awaitable, *_args, **_kwargs):
        return await awaitable

    def fail_validate_remote_cog_url(*_args, **_kwargs):
        calls["validated"] = True
        raise AssertionError(
            "local layer references must not use remote URL validation"
        )

    monkeypatch.setattr(router, "verify_session_required", fake_verify_session_required)
    monkeypatch.setattr(router, "async_read_conn", fake_async_read_conn)
    monkeypatch.setattr(router, "get_async_s3_client", fake_get_async_s3_client)
    monkeypatch.setattr(router, "get_bucket_name", lambda: "test-bucket")
    monkeypatch.setattr(router, "s3_op", fake_s3_op)
    monkeypatch.setattr(router, "validate_remote_cog_url", fail_validate_remote_cog_url)
    router._LOCAL_COG_URL_CACHE.clear()

    resolved_url, cache_identity = await router._resolve_cog_asset(
        "mundi-layer:L9r2NA2L7sye",
        request=object(),
    )

    assert calls["validated"] is False
    assert resolved_url.startswith("http://minio:9000/")
    assert cache_identity == "mundi-layer:L9r2NA2L7sye:cog/layer/L9r2NA2L7sye.cog.tif"
