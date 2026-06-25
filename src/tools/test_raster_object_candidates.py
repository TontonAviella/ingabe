from __future__ import annotations

from src.services.raster_object_candidates import RasterObjectCandidateInput
from src.tools.raster_object_candidates import (
    _exception_message,
    _timeout_result,
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
