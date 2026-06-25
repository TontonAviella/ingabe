from __future__ import annotations

from src.tools.raster_object_candidates import (
    _annotate_timeout_fallback,
    _exception_message,
    _should_fallback_after_timeout,
)


def test_deep_pass_timeout_fallback_annotation_is_user_visible() -> None:
    result = {
        "status": "success",
        "summary": {"performance_note": "Existing note."},
        "engines": {},
    }

    _annotate_timeout_fallback(
        result,
        requested_engine="terramind_geolibre",
        timeout_seconds=60,
    )

    summary = result["summary"]
    assert (
        "deep semantic image pass timed out after 60s"
        in summary["deep_pass_fallback_reason"]
    )
    assert "used the quick raster marker" in summary["performance_note"]
    assert (
        result["engines"]["deep_pass_timeout_fallback"]["requested"]
        == "terramind_geolibre"
    )
    assert (
        result["engines"]["deep_pass_timeout_fallback"]["used"]
        == "rasterio_numpy_candidate_extractor_v2"
    )


def test_samgeo_timeout_errors_do_not_render_blank() -> None:
    assert _exception_message(TimeoutError()) == "TimeoutError"
    assert _should_fallback_after_timeout("segment-geospatial") is True
    assert _should_fallback_after_timeout("terramind_geolibre") is True
    assert _should_fallback_after_timeout("rasterio_numpy") is False
