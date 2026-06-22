import json

import pytest

from src.dependencies.pydantic_tools import get_pydantic_tool_calls
from src.services.rain_impact import RainImpactInput, analyze_expected_rain_impact, parse_bbox
from src.tools.pyd import tool_from
from src.tools.rain_impact import AnalyzeExpectedRainImpactArgs, analyze_expected_rain_impact as rain_tool


def test_parse_bbox_validates_order():
    assert parse_bbox("29.0,-2.0,30.0,-1.0") == [29.0, -2.0, 30.0, -1.0]
    with pytest.raises(ValueError):
        parse_bbox("30,-2,29,-1")


def test_analyze_expected_rain_impact_generates_scored_mesh():
    result = analyze_expected_rain_impact(
        RainImpactInput(
            location_label="Bugesera lowlands",
            bbox=[29.9, -2.3, 30.2, -2.0],
            rainfall_mm_24h=95,
            rainfall_mm_72h=180,
            soil_saturation="saturated",
            crop_stage="flowering",
            forecast_summary="Heavy rain expected overnight.",
            exposure_geojson="",
        )
    )

    assert result["status"] == "success"
    assert result["summary"]["feature_count"] == 16
    assert result["summary"]["highest_risk_level"] in {"high", "severe"}
    feature = result["geojson"]["features"][0]
    assert "risk_score" in feature["properties"]
    assert "expected_impact" in feature["properties"]
    assert result["map"]["style_hint"] == "rain_impact_risk"
    assert result["map"]["height_property"] == "risk_score"


def test_analyze_expected_rain_impact_scores_exposure_geojson():
    exposure = {
        "type": "FeatureCollection",
        "features": [
            {
                "type": "Feature",
                "geometry": {
                    "type": "Polygon",
                    "coordinates": [[
                        [29.9, -2.3],
                        [30.0, -2.3],
                        [30.0, -2.2],
                        [29.9, -2.2],
                        [29.9, -2.3],
                    ]],
                },
                "properties": {"asset": "storage"},
            }
        ],
    }
    result = analyze_expected_rain_impact(
        RainImpactInput(
            location_label="Store",
            bbox=[29.9, -2.3, 30.0, -2.2],
            rainfall_mm_24h=35,
            rainfall_mm_72h=70,
            soil_saturation="normal",
            crop_stage="storage",
            forecast_summary="Rain showers.",
            exposure_geojson=json.dumps(exposure),
        )
    )

    assert result["summary"]["feature_count"] == 1
    props = result["geojson"]["features"][0]["properties"]
    assert props["asset"] == "storage"
    assert props["risk_level"] in {"low", "moderate", "high", "severe"}


def test_rain_impact_tool_is_registered_with_strict_schema():
    registry = get_pydantic_tool_calls()
    assert "analyze_expected_rain_impact" in registry
    schema = tool_from(rain_tool, AnalyzeExpectedRainImpactArgs)
    params = schema["function"]["parameters"]
    assert "render_3d" in params["required"]
    assert params["additionalProperties"] is False
