from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from types import SimpleNamespace

import pytest

import src.tools.raster_object_candidates as raster_tool
from src.services.raster_object_candidates import RasterObjectCandidateInput
from src.tools.raster_object_candidates import (
    AnalyzeRasterObjectCandidatesArgs,
    _exception_message,
    _timeout_result,
    analyze_raster_object_candidates,
)


def test_timeout_exception_message_is_plain() -> None:
    assert _exception_message(TimeoutError()) == "TimeoutError"


def test_raster_timeout_result_does_not_render_blank() -> None:
    result = _timeout_result(
        RasterObjectCandidateInput(
            raster_url="file:///tmp/example.tif",
            layer_id="Lsource",
            layer_name="Cyampirita_Orthophoto",
            bounds_wgs84=None,
            target_classes=["building"],
            max_candidates=500,
            max_sample_pixels=1_200_000,
            min_area_m2=8,
            max_area_m2=1500,
            confidence_threshold=0.5,
            engine_preference="rasterio_numpy",
        ),
        requested_engine="rasterio_numpy",
        timeout_seconds=90,
    )

    assert result["status"] == "error"
    assert "timed out after 90s" in result["error"]
    assert result["summary"]["candidate_count"] == 0
    assert result["summary"]["screening_model"] == "timeout_before_result"
    assert result["engines"]["selection"]["used"] == "timeout_before_result"


@pytest.mark.anyio
async def test_live_timeout_skips_persistence_and_rendering(monkeypatch) -> None:
    row = {
        "layer_id": "Lsource",
        "name": "Cyampirita_Orthophoto",
        "type": "raster",
        "s3_key": "uploads/source.tif",
        "bounds": [29.7, -2.45, 29.95, -2.2],
        "metadata": {},
        "owner_uuid": "00000000-0000-0000-0000-000000000001",
    }

    class FakeConnection:
        async def fetchrow(self, *_args):
            return row

    @asynccontextmanager
    async def fake_read_connection():
        yield FakeConnection()

    class FakeS3:
        async def generate_presigned_url(self, *_args, **_kwargs):
            return "https://isdasoil.s3.amazonaws.com/source.tif"

    async def fake_get_s3_client():
        return FakeS3()

    async def time_out(*_args, **_kwargs):
        raise asyncio.TimeoutError

    persistence_called = False

    async def fail_persistence(**_kwargs):
        nonlocal persistence_called
        persistence_called = True
        raise AssertionError("timeout results must not be persisted")

    monkeypatch.setattr("src.structures.get_async_read_connection", fake_read_connection)
    monkeypatch.setattr("src.utils.get_async_s3_client", fake_get_s3_client)
    monkeypatch.setattr("src.utils.get_bucket_name", lambda: "test-bucket")
    monkeypatch.setattr(raster_tool, "_run_service_with_timeout", time_out)
    monkeypatch.setattr(raster_tool, "_timeout_seconds_for_engine", lambda _engine: 5.0)
    monkeypatch.setattr(raster_tool, "persist_raster_object_candidate_layer", fail_persistence)

    result = await analyze_raster_object_candidates(
        AnalyzeRasterObjectCandidatesArgs(
            layer_id="Lsource",
            target_classes=["building"],
            max_candidates=100,
            max_sample_pixels=100_000,
            min_area_m2=8,
            max_area_m2=1_500,
            confidence_threshold=0.65,
            engine_preference="fastsam",
            render_map=True,
        ),
        SimpleNamespace(
            user_uuid=row["owner_uuid"],
            map_id="Mmap",
            project_id="Pproject",
            conversation_id="Cconversation",
        ),
    )

    assert result["status"] == "error"
    assert result["summary"]["count_semantics"] == "not_available_timeout"
    assert persistence_called is False
