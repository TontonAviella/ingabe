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
            risk_factors_json="",
            exposure_geojson="",
            max_hexes=1,
        )
    )

    assert result["status"] == "error"
    assert "above max_hexes" in result["error"]
