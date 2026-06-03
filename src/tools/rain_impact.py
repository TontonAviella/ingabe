import asyncio
import json
import uuid
from typing import Any

from pydantic import BaseModel, Field

from src.routes.websocket import kue_ephemeral_action
from src.services.rain_impact import (
    RainImpactInput,
    analyze_expected_rain_impact as analyze_expected_rain_impact_service,
    parse_bbox,
)
from src.tools.pyd import IngabeToolCallMetaArgs


class AnalyzeExpectedRainImpactArgs(BaseModel):
    location_label: str = Field(
        ...,
        description="Human-readable area name, e.g. 'Bugesera lowlands' or 'Cyampirita farms'.",
    )
    bbox: str = Field(
        ...,
        description="Area to analyze as 'west,south,east,north' in WGS84.",
    )
    rainfall_mm_24h: float = Field(
        ...,
        description="Expected rainfall total over the next 24 hours in millimeters.",
    )
    rainfall_mm_72h: float = Field(
        ...,
        description="Expected rainfall total over the next 72 hours in millimeters.",
    )
    soil_saturation: str = Field(
        ...,
        description="Current soil wetness class: dry, normal, wet, saturated, low, medium, or high.",
    )
    crop_stage: str = Field(
        ...,
        description="Crop stage: planting, germination, vegetative, flowering, grain_fill, harvest, storage, or unknown.",
    )
    forecast_summary: str = Field(
        ...,
        description="Short text from the weather forecast tool explaining timing/intensity. Use empty string if unknown.",
    )
    exposure_geojson: str = Field(
        ...,
        description=(
            "Optional GeoJSON Feature or FeatureCollection string for farms, cells, roads, or assets to score. "
            "Use an empty string to generate a coarse risk mesh over bbox."
        ),
    )
    render_map: bool = Field(
        ...,
        description="Whether to immediately render the impact map for the user. Usually true.",
    )
    render_3d: bool = Field(
        ...,
        description="Whether to extrude risk_score as a 3D height on the map. Usually true for impact overviews.",
    )


async def analyze_expected_rain_impact(
    args: AnalyzeExpectedRainImpactArgs,
    meta: IngabeToolCallMetaArgs,
) -> dict[str, Any]:
    """Estimate expected agricultural impacts from forecast rainfall and render a risk map.

    Use after a weather/forecast tool gives expected rainfall. This tool answers:
    "What kind of rain are we expecting, what will likely happen to farms/assets,
    and where is the impact highest?" It returns a GeoJSON FeatureCollection with
    risk_score, risk_level, and expected_impact fields. When render_map is true it
    paints the layer on the map; when render_3d is true the map uses 3D extrusion
    where taller polygons mean higher expected impact.
    """
    bbox = parse_bbox(args.bbox)
    result = analyze_expected_rain_impact_service(
        RainImpactInput(
            location_label=args.location_label,
            bbox=bbox,
            rainfall_mm_24h=args.rainfall_mm_24h,
            rainfall_mm_72h=args.rainfall_mm_72h,
            soil_saturation=args.soil_saturation,
            crop_stage=args.crop_stage,
            forecast_summary=args.forecast_summary,
            exposure_geojson=args.exposure_geojson,
        )
    )

    if args.render_map and result.get("status") == "success":
        source_id = f"sage-rain-impact-{uuid.uuid4().hex[:8]}"
        style = {
            "color_property": "risk_score",
            "stops": [
                {"max": 35, "color": "#2ecc71"},
                {"max": 55, "color": "#f1c40f"},
                {"max": 75, "color": "#e67e22"},
                {"max": 101, "color": "#c0392b"},
            ],
            "fill_opacity": 0.62,
            "stroke_color": "#1f2937",
            "stroke_width": 1.5,
            "extrude_3d": args.render_3d,
            "extrusion_property": "risk_score",
            "extrusion_scale": result["map"]["height_scale"],
        }
        async with kue_ephemeral_action(
            meta.conversation_id,
            f"Rendering rain impact map: {args.location_label}",
            bounds=bbox,
        ) as payload:
            payload.updates["add_geojson_layer"] = {
                "source_id": source_id,
                "geojson": result["geojson"],
                "name": f"Expected Rain Impact - {args.location_label}",
                "bounds": bbox,
                "style_hint": result["map"]["style_hint"],
                "style": style,
            }
            await asyncio.sleep(0.2)
        result["map"]["source_id"] = source_id
        result["map"]["rendered"] = True
    else:
        result["map"]["rendered"] = False

    result["geojson"] = json.dumps(result["geojson"])
    return result
