import asyncio
import json
import uuid
from typing import Any

from pydantic import BaseModel, Field

from src.routes.websocket import kue_ephemeral_action
from src.services.open_buildings import (
    OpenBuildingsExposureInput,
    analyze_open_buildings_exposure as analyze_open_buildings_exposure_service,
)
from src.services.rain_impact import parse_bbox
from src.tools.pyd import IngabeToolCallMetaArgs


class AnalyzeOpenBuildingsExposureArgs(BaseModel):
    location_label: str = Field(
        ...,
        description="Human-readable area name, e.g. 'Cyampirita housing area' or 'Kigali flood corridor'.",
    )
    bbox: str = Field(
        ...,
        description="Area to analyze as 'west,south,east,north' in WGS84.",
    )
    h3_resolution: int = Field(
        ...,
        description="H3 resolution. Use 8 for district/town overview, 9 for neighborhood, 10+ for small local AOIs.",
    )
    min_confidence: float = Field(
        ...,
        description="Minimum Open Buildings confidence to include. Use 0.75 normally, 0.85-0.90 for high precision.",
    )
    buildings_geojson: str = Field(
        ...,
        description="Optional GeoJSON FeatureCollection of building footprints. Pass empty string if unavailable.",
    )
    open_buildings_csv: str = Field(
        ...,
        description="Optional small Open Buildings CSV text with latitude,longitude,area_in_meters,confidence,geometry,full_plus_code. Pass empty string if unavailable.",
    )
    risk_factors_json: str = Field(
        ...,
        description="Optional JSON object with rainfall_mm_24h, flood_depth_m, slope_degrees, drainage_deficit, runoff_index, imperviousness, or empty string.",
    )
    max_buildings: int = Field(
        ...,
        description="Safety cap for live building features parsed from provided data. Use 5000 or lower for live Sage calls.",
    )
    max_hexes: int = Field(
        ...,
        description="Safety cap for generated H3 cells. Use 5000 for normal live work; lower for large bboxes.",
    )
    include_ingest_plan: bool = Field(
        ...,
        description="Whether to include the Open Buildings ingest/cache plan and selected tile URLs in the response.",
    )
    fetch_tile_metadata: bool = Field(
        ...,
        description="Whether to fetch public Open Buildings tile metadata to select candidate tile URLs for this bbox. Does not download large tile CSVs.",
    )
    render_map: bool = Field(
        ...,
        description="Whether to render the H3 building exposure layer on the map.",
    )
    render_3d: bool = Field(
        ...,
        description="Whether to extrude risk/building exposure cells in 3D.",
    )


async def analyze_open_buildings_exposure(
    args: AnalyzeOpenBuildingsExposureArgs,
    meta: IngabeToolCallMetaArgs,
) -> dict[str, Any]:
    """Analyze building/housing exposure using Google Open Buildings footprints and render it as an H3 map.

    Use when the user asks for exact building footprints, counts, settlement
    exposure, flood/rain impact on housing, city/infrastructure exposure, or
    how Open Buildings combines with TESSERA/H3. For houses/buildings visible
    inside an uploaded drone/orthophoto raster, use create_raster_h3_context_layer
    first so the answer is based on the local pixels already on the map; use
    Open Buildings afterward as confirmation or for exact exposure counts. If
    cached building footprints are not available, call with empty building inputs
    and include_ingest_plan=true only when that external footprint evidence is
    actually needed. This tool does not download massive Open Buildings CSV
    tiles in the live response path; production should ingest/cache them first.
    """

    bbox = parse_bbox(args.bbox)
    result = analyze_open_buildings_exposure_service(
        OpenBuildingsExposureInput(
            location_label=args.location_label,
            bbox=bbox,
            h3_resolution=args.h3_resolution,
            min_confidence=args.min_confidence,
            buildings_geojson=args.buildings_geojson,
            open_buildings_csv=args.open_buildings_csv,
            risk_factors_json=args.risk_factors_json,
            max_buildings=args.max_buildings,
            max_hexes=args.max_hexes,
            include_ingest_plan=args.include_ingest_plan,
            fetch_tile_metadata=args.fetch_tile_metadata,
        )
    )

    if args.render_map and result.get("status") == "success":
        source_id = f"sage-open-buildings-h3-{uuid.uuid4().hex[:8]}"
        style = {
            "color_property": "risk_score",
            "stops": [
                {"max": 40, "color": "#16a34a"},
                {"max": 60, "color": "#eab308"},
                {"max": 80, "color": "#f97316"},
                {"max": 101, "color": "#dc2626"},
            ],
            "fill_opacity": 0.6,
            "stroke_color": "#111827",
            "stroke_width": 1.2,
            "extrude_3d": args.render_3d,
            "extrusion_property": "risk_score",
            "extrusion_scale": 45,
        }
        async with kue_ephemeral_action(
            meta.conversation_id,
            f"Rendering Open Buildings exposure: {args.location_label}",
            bounds=bbox,
        ) as payload:
            payload.updates["add_geojson_layer"] = {
                "source_id": source_id,
                "geojson": result["geojson"],
                "name": f"Building Exposure - {args.location_label}",
                "bounds": bbox,
                "style_hint": "open_buildings_h3_exposure",
                "style": style,
            }
            await asyncio.sleep(0.2)
        result["engines"]["render"]["source_id"] = source_id
        result["engines"]["render"]["rendered"] = True
    elif result.get("status") == "success":
        result["engines"]["render"]["rendered"] = False

    if result.get("status") == "success":
        geojson = result["geojson"]
        building_geojson = result["building_exposure_geojson"]
        result["geojson"] = json.dumps(geojson)
        result["building_exposure_geojson"] = json.dumps(building_geojson)
        result["geojson_feature_count"] = len(geojson.get("features", []))

    return result
