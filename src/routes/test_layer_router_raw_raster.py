import asyncio
from contextlib import asynccontextmanager

from src.database.models import LAYER_TYPE_RASTER
from src.routes import layer_router


def test_render_status_ready_raw_before_cog_when_rasterd_configured(monkeypatch):
    class FakeConn:
        async def fetchrow(self, _query, _layer_id):
            return {
                "layer_id": "LrawRaster01",
                "type": LAYER_TYPE_RASTER,
                "metadata": {"band_count": 4, "cog_status": "generating"},
                "bounds": [29.0, -2.0, 29.1, -1.9],
                "s3_key": "uploads/user/project/LrawRaster01.tif",
            }

    @asynccontextmanager
    async def fake_async_conn(*_args, **_kwargs):
        yield FakeConn()

    monkeypatch.setattr(layer_router, "async_conn", fake_async_conn)
    monkeypatch.setenv("RASTER_TILE_ENGINE_URL", "http://rasterd:8877")

    status = asyncio.run(layer_router.get_layer_render_status("LrawRaster01"))

    assert status["ready"] is True
    assert status["status"] == "ready_raw"
    assert status["optimized_ready"] is False
    assert status["tile_url"] == "/api/layer/LrawRaster01/{z}/{x}/{y}.png"


def test_raster_engine_tile_allows_raw_non_webmercator(monkeypatch):
    calls = []

    class FakeResponse:
        status_code = 200
        content = b"png-bytes"

    class FakeClient:
        async def get(self, url, params):
            calls.append((url, params))
            return FakeResponse()

    monkeypatch.setattr(layer_router, "_get_raster_engine_url", lambda: "http://rasterd:8877")
    monkeypatch.setattr(layer_router, "_get_raster_engine_client", lambda: FakeClient())

    tile = asyncio.run(
        layer_router._try_raster_engine_tile(
            layer_id="LrawRaster01",
            asset_url="/vsis3/mundi-uploads/uploads/user/project/LrawRaster01.tif",
            metadata={"band_count": 4, "original_srid": 32735},
            z=20,
            x=608976,
            y=535146,
        )
    )

    assert tile == b"png-bytes"
    assert calls == [
        (
            "http://rasterd:8877/tiles/20/608976/535146.png",
            {
                "url": "/vsis3/mundi-uploads/uploads/user/project/LrawRaster01.tif",
                "layer_id": "LrawRaster01",
                "bands": "1,2,3",
            },
        )
    ]


def test_raw_raster_status_not_ready_for_python_colormap_layers(monkeypatch):
    monkeypatch.setenv("RASTER_TILE_ENGINE_URL", "http://rasterd:8877")

    assert layer_router._raw_raster_tiles_available(
        "uploads/user/project/LndviRaster.tif",
        {"band_count": 1, "raster_value_stats_b1": {"min": 0, "max": 1}},
        [29.0, -2.0, 29.1, -1.9],
    ) is False
