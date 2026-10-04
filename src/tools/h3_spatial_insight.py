import asyncio
import json
import logging
import uuid
from typing import Any

from pydantic import BaseModel, Field

from src.services.h3_risk_classes import inline_style_stops, legend
from src.routes.websocket import kue_ephemeral_action
from src.services.h3_spatial_insight import (
    H3SpatialInsightInput,
    create_h3_spatial_insight as create_h3_spatial_insight_service,
)
from src.services.h3_layer_persistence import persist_h3_spatial_insight_layer
from src.services.rain_impact import parse_bbox
from src.tools.geojson_transport import geojson_layer_update
from src.tools.h3_layer_render import compact_h3_geojson, render_h3_risk_layer
from src.tools.pyd import IngabeToolCallMetaArgs

logger = logging.getLogger(__name__)


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
        description=(
            "Requested H3 resolution (8 town, 9 neighbourhood/farm, 10 small area). The tool uses the size "
            "the evidence supports: 7 (~5 km2) when only area-wide risk factors are given, 8-10 when "
            "buildings, roads, farms or assets are counted per hexagon."
        ),
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
            "JSON object with observed/modelled risk factors, or empty string if unknown. Do not put guesses, "
            "domain labels, map names, or basemap descriptions here. Useful keys: "
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

    Use when the user wants interactive spatial risk cells, priority zones, or a
    map-first analysis that mixes drone/satellite/basemap context with real
    evidence: buildings, roads, farms, drainage, assets, terrain, rain, flood,
    or environmental metrics. Do not use this from basemap imagery alone. This
    tool creates an internal H3 cell layer and scores cells only from provided
    factors/exposure geometry. When Whitebox terrain/hydrology outputs exist,
    pass their per-area metrics through risk_factors_json so the same layer
    becomes Whitebox-backed. The normal render path persists PMTiles for the
    browser and GeoParquet metadata for analytics; inline GeoJSON is only a
    small fallback preview path.
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

    persisted_layer = None
    if result.get("status") == "success":
        if args.render_map:
            render_engine = result.setdefault("engines", {}).setdefault("render", {})
            persisted_layer = await render_h3_risk_layer(
                result,
                meta=meta,
                layer_name=f"Spatial Risk - {args.location_label}",
                render_3d=args.render_3d,
                bounds=bbox,
                analysis_kind="h3_spatial_insight",
                extrusion_scale=render_engine.get("height_scale", 50),
                style_hint=render_engine.get("style_hint", "h3_spatial_insight_risk"),
            )
        else:
            result.setdefault("engines", {}).setdefault("render", {})["rendered"] = False
        compact_h3_geojson(result, persisted_layer)

    return result
