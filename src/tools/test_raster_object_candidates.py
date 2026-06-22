from __future__ import annotations

from src.tools.raster_object_candidates import (
    _annotate_timeout_fallback,
    _exception_message,
    _should_fallback_after_timeout,
)


def test_samgeo_timeout_fallback_annotation_is_user_visible() -> None:
    result = {
        "status": "success",
        "summary": {"performance_note": "Existing note."},
        "engines": {},
    }

    _annotate_timeout_fallback(
        result,
        requested_engine="samgeo",
        timeout_seconds=60,
    )

    summary = result["summary"]
    assert "SamGeo timed out after 60s" in summary["samgeo_fallback_reason"]
    assert "used the rasterio/numpy candidate extractor" in summary["performance_note"]
    assert result["engines"]["samgeo_timeout_fallback"]["requested"] == "samgeo"
    assert (
        result["engines"]["samgeo_timeout_fallback"]["used"]
        == "rasterio_numpy_candidate_extractor_v1"
    )


def test_samgeo_timeout_errors_do_not_render_blank() -> None:
    assert _exception_message(TimeoutError()) == "TimeoutError"
    assert _should_fallback_after_timeout("segment-geospatial") is True
    assert _should_fallback_after_timeout("rasterio_numpy") is False
