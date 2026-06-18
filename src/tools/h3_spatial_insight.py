import asyncio
import json
import logging
import uuid
from typing import Any

from pydantic import BaseModel, Field

from src.routes.websocket import kue_ephemeral_action
from src.services.h3_spatial_insight import (
    H3SpatialInsightInput,
    create_h3_spatial_insight as create_h3_spatial_insight_service,
)
from src.services.h3_layer_persistence import persist_h3_spatial_insight_layer
from src.services.rain_impact import parse_bbox
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
    risk_factors_json so the same H3 layer becomes Whitebox-backed. The normal
    render path persists PMTiles for the browser and GeoParquet metadata for
    analytics; inline GeoJSON is only a small fallback preview path.
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
    if args.render_map and result.get("status") == "success":
        engines = result.setdefault("engines", {})
        render_engine = engines.setdefault("render", {})
        transport = engines.setdefault("transport", {})
        try:
            persisted_layer = await persist_h3_spatial_insight_layer(
                result=result,
                user_uuid=meta.user_uuid,
                map_id=meta.map_id,
                project_id=meta.project_id,
                layer_name=f"Spatial Risk - {args.location_label}",
                render_3d=args.render_3d,
            )
        except Exception as exc:
            logger.warning("H3 layer persistence failed; falling back to inline preview: %s", exc, exc_info=True)

        if persisted_layer:
            async with kue_ephemeral_action(
                meta.conversation_id,
                f"Saving spatial risk layer: {args.location_label}",
                layer_id=persisted_layer.layer_id,
                update_style_json=True,
                bounds=persisted_layer.bounds or bbox,
            ) as payload:
                payload.updates["h3_layer_persisted"] = {
                    "layer_id": persisted_layer.layer_id,
                    "name": f"Spatial Risk - {args.location_label}",
                    "pmtiles": True,
                    "geoparquet": bool(persisted_layer.geoparquet_key),
                    "feature_count": persisted_layer.feature_count,
                }
                await asyncio.sleep(0.2)
            render_engine["layer_id"] = persisted_layer.layer_id
            render_engine["rendered"] = True
            transport["current"] = "pmtiles_vector_layer"
            transport["browser"] = "PMTiles/MVT"
            transport["analytics_cache"] = (
                "GeoParquet" if persisted_layer.geoparquet_key else "pending"
            )
            result["layer_id"] = persisted_layer.layer_id
            result["pmtiles_key"] = persisted_layer.pmtiles_key
            result["geoparquet_key"] = persisted_layer.geoparquet_key
        else:
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
                f"Rendering spatial risk preview: {args.location_label}",
                bounds=bbox,
            ) as payload:
                payload.updates["add_geojson_layer"] = {
                    "source_id": source_id,
                    "geojson": result["geojson"],
                    "name": f"Spatial Risk - {args.location_label}",
                    "bounds": bbox,
                    "style_hint": render_engine.get("style_hint", "h3_spatial_insight_risk"),
                    "style": style,
                }
                await asyncio.sleep(0.2)
            render_engine["source_id"] = source_id
            render_engine["rendered"] = True
            transport["current"] = "inline_geojson_preview_fallback"
    elif result.get("status") == "success":
        engines = result.setdefault("engines", {})
        render_engine = engines.setdefault("render", {})
        render_engine["rendered"] = False

    if result.get("status") == "success":
        geojson = result["geojson"]
        result["geojson_feature_count"] = len(geojson.get("features", []))
        if persisted_layer:
            result["geojson"] = (
                "omitted from tool response; persisted as PMTiles/MVT layer "
                f"{persisted_layer.layer_id}"
            )
        else:
            result["geojson"] = json.dumps(geojson)

    return result
