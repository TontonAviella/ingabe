from __future__ import annotations

from src.tools.raster_h3_context import (
    RasterCellStats,
    _cell_features,
    _target_shape,
    _zoom_resolution_map,
)


def test_target_shape_caps_large_raster_sampling() -> None:
    height, width = _target_shape(25_480, 36_458, 20_000)

    assert height * width <= 20_000
    assert height > 0
    assert width > 0


def test_housing_context_is_visual_proxy_not_building_detection() -> None:
    features, scores = _cell_features(
        {
            "8a6ad81a6877fff": RasterCellStats(
                count=25,
                grvi_sum=-1.0,
                grvi_sq_sum=0.04,
                brightness_sum=15.0,
            )
        },
        domain="housing",
        analysis_goal="screen housing context",
        h3_resolution=10,
        zoom_min=0,
        zoom_max=15,
    )

    props = features[0]["properties"]
    assert scores[0] > 0
    assert props["domain"] == "housing"
    assert props["confidence"] == "low"
    assert "not confirmed building/road detection" in props["evidence_basis"]
    assert "verify with buildings" in props["likely_issue"]


def test_zoom_resolution_map_refines_at_higher_zoom() -> None:
    zoom_map = _zoom_resolution_map([10, 11, 12])

    assert zoom_map == [
        {"h3_resolution": 10, "minzoom": 0, "maxzoom": 15},
        {"h3_resolution": 11, "minzoom": 15, "maxzoom": 17},
        {"h3_resolution": 12, "minzoom": 17, "maxzoom": 24},
    ]
