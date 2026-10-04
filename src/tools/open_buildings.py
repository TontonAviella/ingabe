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
from src.tools.geojson_transport import geojson_layer_update
from src.tools.h3_layer_render import compact_h3_geojson, render_h3_risk_layer
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
    exposure, flood/rain impact on housing, city/infrastructure exposure, houses
    from a basemap/satellite background, or how Open Buildings combines with
    TESSERA/H3. For houses/buildings visible inside an uploaded drone/orthophoto
    raster, this is the footprint confirmation path; do not use raster H3 cells
    as a substitute for building counts. If cached building footprints are not
    available, call with empty building inputs and include_ingest_plan=true only
    when that external footprint evidence is actually needed. This tool does not
    download massive Open Buildings CSV tiles in the live response path;
    production should ingest/cache them first.
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

    if result.get("status") == "success":
        persisted_layer = None
        if args.render_map:
            persisted_layer = await render_h3_risk_layer(
                result,
                meta=meta,
                layer_name=f"Building Exposure - {args.location_label}",
                render_3d=args.render_3d,
                bounds=bbox,
                analysis_kind="open_buildings_h3_exposure",
                extrusion_scale=45,
                style_hint="open_buildings_h3_exposure",
            )
        else:
            result["engines"]["render"]["rendered"] = False
        compact_h3_geojson(result, persisted_layer)
        result["building_exposure_geojson"] = json.dumps(result["building_exposure_geojson"])

    return result
