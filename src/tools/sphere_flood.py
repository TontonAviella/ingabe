import asyncio
import json
import uuid
from typing import Any

from pydantic import BaseModel, Field

from src.routes.websocket import kue_ephemeral_action
from src.services.forge3d_adapter import build_forge3d_impact_layer
from src.services.rain_impact import parse_bbox
from src.services.sphere_flood import SphereFloodInput, analyze_sphere_flood_impact as run_sphere_flood
from src.tools.pyd import IngabeToolCallMetaArgs


class AnalyzeSphereFloodImpactArgs(BaseModel):
    exposure_geojson: str = Field(
        ...,
        description=(
            "GeoJSON Feature or FeatureCollection for exposed assets/buildings/farm infrastructure. "
            "Properties can override occupancy_type, area_m2, building_value_usd, content_value_usd, "
            "inventory_value_usd, first_floor_height_ft, foundation_type, and number_stories."
        ),
    )
    bbox: str = Field(
        ...,
        description="Map bounds to zoom/render as 'west,south,east,north' in WGS84.",
    )
    flood_depth_m: float = Field(
        ...,
        description="Expected flood water depth at asset locations in meters. Sphere will convert to feet internally.",
    )
    flood_type: str = Field(
        ...,
        description="Sphere/HAZUS flood type: R for riverine or C for coastal. Use R for Rwanda unless explicitly coastal.",
    )
    default_occupancy: str = Field(
        ...,
        description="HAZUS occupancy for assets missing one. Use AGR1 for agricultural buildings/storage.",
    )
    default_building_value_usd: float = Field(
        ...,
        description="Default replacement value per asset/building in USD if properties do not provide building_value_usd.",
    )
    default_content_value_usd: float = Field(
        ...,
        description="Default contents value per asset/building in USD if properties do not provide content_value_usd.",
    )
    default_area_m2: float = Field(
        ...,
        description="Default building footprint/area in square meters if properties do not provide area_m2 or area_sqft.",
    )
    default_first_floor_height_m: float = Field(
        ...,
        description="Default first-floor elevation above ground in meters.",
    )
    render_map: bool = Field(
        ...,
        description="Whether to immediately render the damage/loss map for the user.",
    )
    render_3d: bool = Field(
        ...,
        description="Whether map rendering should extrude damage severity as height.",
    )
    use_forge3d: bool = Field(
        ...,
        description="Whether to build a Forge3D BuildingLayer summary from the result when forge3d is installed.",
    )


async def analyze_sphere_flood_impact(
    args: AnalyzeSphereFloodImpactArgs,
    meta: IngabeToolCallMetaArgs,
) -> dict[str, Any]:
    """Run Sphere HAZUS-style flood damage/loss analysis for exposed assets.

    Use this when the agent has a flood-depth estimate/raster-derived value and
    wants asset damage, loss, debris, or restoration estimates. This is NOT a
    rain forecast tool; call get_forecast or another hydrology/flood model first
    to derive expected flood depth, then call this tool with exposure GeoJSON.
    """
    bbox = parse_bbox(args.bbox)
    result = run_sphere_flood(
        SphereFloodInput(
            exposure_geojson=args.exposure_geojson,
            flood_depth_m=args.flood_depth_m,
            flood_type=args.flood_type,
            default_occupancy=args.default_occupancy,
            default_building_value_usd=args.default_building_value_usd,
            default_content_value_usd=args.default_content_value_usd,
            default_area_m2=args.default_area_m2,
            default_first_floor_height_m=args.default_first_floor_height_m,
        )
    )

    if result.get("status") == "success" and args.use_forge3d:
        result["forge3d"] = build_forge3d_impact_layer(
            result["geojson"],
            height_property=result["map"]["height_property"],
            height_scale=result["map"]["height_scale"],
        )

    if args.render_map and result.get("status") == "success":
        source_id = f"sage-sphere-flood-{uuid.uuid4().hex[:8]}"
        style = {
            "color_property": "risk_score",
            "stops": [
                {"max": 20, "color": "#2ecc71"},
                {"max": 45, "color": "#f1c40f"},
                {"max": 70, "color": "#e67e22"},
                {"max": 101, "color": "#c0392b"},
            ],
            "fill_opacity": 0.64,
            "stroke_color": "#111827",
            "stroke_width": 1.5,
            "extrude_3d": args.render_3d,
            "extrusion_property": result["map"]["height_property"],
            "extrusion_scale": result["map"]["height_scale"],
        }
        async with kue_ephemeral_action(
            meta.conversation_id,
            "Rendering Sphere flood damage map",
            bounds=bbox,
        ) as payload:
            payload.updates["add_geojson_layer"] = {
                "source_id": source_id,
                "geojson": result["geojson"],
                "name": "Sphere Flood Damage",
                "bounds": bbox,
                "style_hint": result["map"]["style_hint"],
                "style": style,
            }
            await asyncio.sleep(0.2)
        result["map"]["source_id"] = source_id
        result["map"]["rendered"] = True
    elif "map" in result:
        result["map"]["rendered"] = False

    if isinstance(result.get("geojson"), dict):
        result["geojson"] = json.dumps(result["geojson"])
    return result
