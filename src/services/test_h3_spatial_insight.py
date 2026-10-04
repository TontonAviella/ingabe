import json

from src.services.h3_spatial_insight import (
    H3SpatialInsightInput,
    create_h3_spatial_insight,
)


def test_create_h3_spatial_insight_generates_risk_cells():
    result = create_h3_spatial_insight(
        H3SpatialInsightInput(
            location_label="Test settlement",
            bbox=[30.0, -2.0, 30.02, -1.98],
            h3_resolution=9,
            domain="housing",
            analysis_goal="screen drainage risk around buildings",
            risk_factors_json=json.dumps(
                {
                    "rainfall_mm_24h": 70,
                    "slope_degrees": 12,
                    "imperviousness": 0.4,
                }
            ),
            exposure_geojson=json.dumps(
                {
                    "type": "Feature",
                    "geometry": {
                        "type": "Point",
                        "coordinates": [30.01, -1.99],
                    },
                    "properties": {"kind": "building"},
                }
            ),
            max_hexes=5000,
        )
    )

    assert result["status"] == "success"
    assert result["summary"]["cell_count"] > 0
    assert result["summary"]["domain"] == "housing"
    assert result["engines"]["grid"]["name"] == "H3"
    feature = result["geojson"]["features"][0]
    assert "h3_index" in feature["properties"]
    assert "risk_score" in feature["properties"]
    assert "recommended_action" in feature["properties"]


def test_create_h3_spatial_insight_respects_max_hexes():
    result = create_h3_spatial_insight(
        H3SpatialInsightInput(
            location_label="Too detailed",
            bbox=[30.0, -2.0, 30.2, -1.8],
            h3_resolution=11,
            domain="mixed",
            analysis_goal="test safety cap",
            risk_factors_json=json.dumps({"rainfall_mm_24h": 10}),
            exposure_geojson="",
            max_hexes=1,
        )
    )

    assert result["status"] == "error"
    assert "above max_hexes" in result["error"]


def test_create_h3_spatial_insight_does_not_invent_spatial_variation_from_area_factor():
    result = create_h3_spatial_insight(
        H3SpatialInsightInput(
            location_label="Area factor only",
            bbox=[30.0, -2.0, 30.02, -1.98],
            h3_resolution=9,
            domain="housing",
            analysis_goal="screen rain risk without local exposure",
            risk_factors_json=json.dumps({"rainfall_mm_24h": 70}),
            exposure_geojson="",
            max_hexes=5000,
        )
    )

    assert result["status"] == "success"
    scores = {
        feature["properties"]["risk_score"]
        for feature in result["geojson"]["features"]
    }
    assert len(scores) == 1
    assert result["summary"]["confidence"] == "low"


def test_area_wide_factors_get_large_hexagons_whatever_was_requested():
    from src.services.h3_spatial_insight import AREA_WIDE_RESOLUTION, effective_resolution
    resolution, reason = effective_resolution(10, has_exposure=False)
    assert resolution == AREA_WIDE_RESOLUTION
    assert "one value for the whole area" in reason


def test_counted_exposure_keeps_building_scale_hexagons():
    from src.services.h3_spatial_insight import effective_resolution
    assert effective_resolution(9, has_exposure=True)[0] == 9
    assert effective_resolution(12, has_exposure=True)[0] == 10
    assert effective_resolution(6, has_exposure=True)[0] == 8
