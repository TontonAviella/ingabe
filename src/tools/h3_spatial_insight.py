import asyncio
import json
import uuid
from typing import Any

from pydantic import BaseModel, Field

from src.routes.websocket import kue_ephemeral_action
from src.services.h3_spatial_insight import (
    H3SpatialInsightInput,
    create_h3_spatial_insight as create_h3_spatial_insight_service,
)
from src.services.rain_impact import parse_bbox
from src.tools.pyd import IngabeToolCallMetaArgs


class CreateH3SpatialInsightLayerArgs(BaseModel):
    location_label: str = Field(
        ...,
        description="Human-readable area name, e.g. 'Cyampirita settlement edge' or 'Kigali wetland corridor'.",
    )
    bbox: str = Field(
        ...,
        description="Area to analyze as 'west,south,east,north' in WGS84.",
    )
    h3_resolution: int = Field(
        ...,
        description="H3 resolution. Use 8 for town/city overview, 9 for local neighborhood/farm/drone overview, 10+ only for small areas.",
    )
    domain: str = Field(
        ...,
        description="Insight domain: housing, infrastructure, environment, drone, agriculture, or mixed.",
    )
    analysis_goal: str = Field(
        ...,
        description="Short natural-language goal, e.g. 'find drainage risk around housing' or 'screen road washout risk'.",
    )
    risk_factors_json: str = Field(
        ...,
        description=(
            "JSON object with known risk factors, or empty string if unknown. Useful keys: "
            "rainfall_mm_24h, rainfall_mm_72h, flood_depth_m, slope_degrees, imperviousness, "
            "drainage_deficit, runoff_index, wetness_index, pollution_index, heat_index_c, ndvi_stress, soil_saturation."
        ),
    )
    exposure_geojson: str = Field(
        ...,
        description="Optional GeoJSON Feature/FeatureCollection for buildings, roads, drains, assets, farms, or drone-detected objects. Pass empty string if unavailable.",
    )
    max_hexes: int = Field(
        ...,
        description="Safety cap for generated H3 cells. Use 5000 for normal live work; lower it for large bboxes.",
    )
    render_map: bool = Field(
        ...,
        description="Whether to immediately render the H3 insight layer on the map. Usually true.",
    )
    render_3d: bool = Field(
        ...,
        description="Whether to extrude risk_score in 3D. Use true for overview/risk maps.",
    )


async def create_h3_spatial_insight_layer(
    args: CreateH3SpatialInsightLayerArgs,
    meta: IngabeToolCallMetaArgs,
) -> dict[str, Any]:
    """Create an interactive H3 spatial insight layer for city, housing, infrastructure, environment, drone, or farm analysis.

    Use when the user wants an H3/hex map, interactive spatial risk cells, or a
    map-first analysis that mixes drone/satellite/basemap context with buildings,
    roads, farms, drainage, or environmental exposure. This V1 creates the H3
    cell layer and scores cells from provided factors/exposure geometry. When
    Whitebox terrain/hydrology outputs exist, pass their per-area metrics through
    risk_factors_json so the same H3 layer becomes Whitebox-backed. Keep
    max_hexes bounded because V1 renders a live inline GeoJSON layer; large or
    reusable outputs should be tiled/cached as MVT/PMTiles in the next path.
    """

    bbox = parse_bbox(args.bbox)
    result = create_h3_spatial_insight_service(
        H3SpatialInsightInput(
            location_label=args.location_label,
            bbox=bbox,
            h3_resolution=args.h3_resolution,
            domain=args.domain,
            analysis_goal=args.analysis_goal,
            risk_factors_json=args.risk_factors_json,
            exposure_geojson=args.exposure_geojson,
            max_hexes=args.max_hexes,
        )
    )

    if args.render_map and result.get("status") == "success":
        engines = result.setdefault("engines", {})
        render_engine = engines.setdefault("render", {})
        source_id = f"sage-h3-insight-{uuid.uuid4().hex[:8]}"
        style = {
            "color_property": "risk_score",
            "stops": [
                {"max": 40, "color": "#22c55e"},
                {"max": 60, "color": "#facc15"},
                {"max": 80, "color": "#f97316"},
                {"max": 101, "color": "#dc2626"},
            ],
            "fill_opacity": 0.58,
            "stroke_color": "#111827",
            "stroke_width": 1.2,
            "extrude_3d": args.render_3d,
            "extrusion_property": "risk_score",
            "extrusion_scale": render_engine.get("height_scale", 50),
        }
        async with kue_ephemeral_action(
            meta.conversation_id,
            f"Rendering H3 insight layer: {args.location_label}",
            bounds=bbox,
        ) as payload:
            payload.updates["add_geojson_layer"] = {
                "source_id": source_id,
                "geojson": result["geojson"],
                "name": f"H3 Insight - {args.location_label}",
                "bounds": bbox,
                "style_hint": render_engine.get("style_hint", "h3_spatial_insight_risk"),
                "style": style,
            }
            await asyncio.sleep(0.2)
        render_engine["source_id"] = source_id
        render_engine["rendered"] = True
    elif result.get("status") == "success":
        engines = result.setdefault("engines", {})
        render_engine = engines.setdefault("render", {})
        render_engine["rendered"] = False

    if result.get("status") == "success":
        geojson = result["geojson"]
        result["geojson"] = json.dumps(geojson)
        result["geojson_feature_count"] = len(geojson.get("features", []))

    return result
