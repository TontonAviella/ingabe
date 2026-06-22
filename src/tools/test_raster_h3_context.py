from __future__ import annotations

from src.tools.raster_h3_context import (
    CreateRasterH3ContextLayerArgs,
    RasterCellStats,
    _capture_raster_h3_telemetry,
    _cell_features,
    _target_shape,
    _zoom_resolution_map,
)
from src.tools.pyd import IngabeToolCallMetaArgs


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
    assert "not individual house or road marks" in props["evidence_basis"]
    assert "inspect the marked area" in props["likely_issue"]


def test_drone_is_source_modality_not_agriculture_domain() -> None:
    features, _scores = _cell_features(
        {
            "8a6ad81a6877fff": RasterCellStats(
                count=25,
                grvi_sum=1.0,
                grvi_sq_sum=0.08,
                brightness_sum=8.0,
            )
        },
        domain="drone",
        analysis_goal="screen uploaded orthophoto",
        h3_resolution=10,
        zoom_min=0,
        zoom_max=15,
    )

    props = features[0]["properties"]
    assert props["domain"] == "mixed"
    assert props["score_kind"] == "mixed_visual_attention_proxy"
    assert "crop condition" not in props["recommended_action"]


def test_zoom_resolution_map_refines_at_higher_zoom() -> None:
    zoom_map = _zoom_resolution_map([10, 11, 12])

    assert zoom_map == [
        {"h3_resolution": 10, "minzoom": 0, "maxzoom": 15},
        {"h3_resolution": 11, "minzoom": 15, "maxzoom": 17},
        {"h3_resolution": 12, "minzoom": 17, "maxzoom": 24},
    ]


def test_raster_h3_telemetry_includes_render_correlation(monkeypatch) -> None:
    captured: dict = {}

    def fake_capture_backend_event(event, *, distinct_id, properties):
        captured["event"] = event
        captured["distinct_id"] = distinct_id
        captured["properties"] = properties
        return True

    from src.services import posthog_analytics

    monkeypatch.setattr(
        posthog_analytics,
        "capture_backend_event",
        fake_capture_backend_event,
    )

    _capture_raster_h3_telemetry(
        {
            "status": "success",
            "layer_id": "Lrendered",
            "pmtiles_key": "pmtiles/user/project/Lrendered.pmtiles",
            "geoparquet_key": "geoparquet/user/project/Lrendered.parquet",
            "pmtiles_maxzoom": 20,
            "summary": {
                "cell_count": 4221,
                "elapsed_ms": 10303.6,
                "h3_resolutions": [10, 11, 12],
                "adaptive_resolution": True,
                "resolution_count": 3,
            },
        },
        args=CreateRasterH3ContextLayerArgs(
            layer_id="Lsource",
            domain="mixed",
            analysis_goal="screen orthophoto context",
            h3_resolution=10,
            max_hexes=5000,
            max_sample_pixels=60000,
            render_map=True,
            render_3d=False,
        ),
        meta=IngabeToolCallMetaArgs(
            user_uuid="user-1",
            conversation_id=3332,
            map_id="Mmap",
            project_id="Pproject",
            session=None,
        ),
        persisted=True,
    )

    props = captured["properties"]
    assert captured["event"] == "backend_raster_h3_context_completed"
    assert props["map_id"] == "Mmap"
    assert props["project_id"] == "Pproject"
    assert props["conversation_id"] == 3332
    assert props["layer_id"] == "Lsource"
    assert props["rendered_layer_id"] == "Lrendered"
    assert props["pmtiles_maxzoom"] == 20
    assert props["pmtiles_present"] is True
    assert props["geoparquet_present"] is True
