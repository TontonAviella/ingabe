import asyncio
import logging
import uuid
from typing import Any, Dict

from pydantic import BaseModel, Field

from src.routes.websocket import kue_ephemeral_action
from src.services.stac_service import STACService
from src.tools.pyd import IngabeToolCallMetaArgs

logger = logging.getLogger(__name__)


class DisplaySatelliteLayerArgs(BaseModel):
    bbox: str = Field(
        ...,
        description="Bounding box as 'west,south,east,north' in WGS84 coordinates, e.g. '29.44,-1.72,29.68,-1.50'",
    )
    date_from: str = Field(
        ...,
        description="Start date in ISO 8601 format, e.g. '2025-01-01'",
    )
    date_to: str = Field(
        ...,
        description="End date in ISO 8601 format, e.g. '2025-01-31'",
    )
    layer_name: str = Field(
        ...,
        description="Display name for the map layer, e.g. 'Musanze TCI Jan 2025'",
    )


def _build_cog_tile_url(
    visual_href: str,
    expression: str = "visual",
    nir_href: str = "",
    green_href: str = "",
    swir_href: str = "",
) -> str:
    base = "/api/cog-tiles/{z}/{x}/{y}.png"
    params = [f"url={visual_href}", f"expression={expression}"]
    if nir_href:
        params.append(f"nir_url={nir_href}")
    if green_href:
        params.append(f"green_url={green_href}")
    if swir_href:
        params.append(f"swir_url={swir_href}")
    return f"{base}?{'&'.join(params)}"


async def display_satellite_layer(
    args: DisplaySatelliteLayerArgs, meta: IngabeToolCallMetaArgs
) -> Dict[str, Any]:
    """Display satellite imagery (true color) on the map for a specific area and date range. Searches Earth Search for the best Sentinel-2 scene and adds it as a visible tile layer. Use this when the user wants to see satellite imagery on the map."""
    try:
        bbox = [float(x.strip()) for x in args.bbox.split(",")]
        if len(bbox) != 4:
            return {"status": "error", "error": "bbox must have 4 values: west,south,east,north"}
    except ValueError:
        return {"status": "error", "error": "bbox values must be numbers"}

    datetime_range = f"{args.date_from}/{args.date_to}"

    stac = STACService("earth_search")
    loop = asyncio.get_running_loop()
    result = await loop.run_in_executor(
        None,
        lambda: stac.search_imagery(
            bbox=bbox,
            datetime_range=datetime_range,
            max_cloud_cover=30.0,
            limit=5,
        ),
    )

    items = result.get("items", [])
    if not items:
        return {
            "status": "error",
            "error": f"No Sentinel-2 scenes found for {datetime_range} with <30% cloud cover",
        }

    best = min(items, key=lambda x: x.get("cloud_cover", 100) or 100)

    assets = best.get("assets", {})
    visual_href = ""
    for key in ("visual", "thumbnail"):
        if key in assets:
            visual_href = assets[key]["href"]
            break

    if not visual_href:
        red_href = assets.get("red", assets.get("B04", {})).get("href", "")
        if not red_href:
            return {"status": "error", "error": "Scene has no visual or red band asset"}
        visual_href = red_href

    tile_url = _build_cog_tile_url(visual_href, expression="visual")
    source_id = f"sage-tci-{uuid.uuid4().hex[:8]}"

    async with kue_ephemeral_action(
        meta.conversation_id,
        f"Adding satellite layer: {args.layer_name}",
        bounds=bbox,
    ) as payload:
        payload.updates["add_tile_layer"] = {
            "source_id": source_id,
            "tiles": [tile_url],
            "tileSize": 256,
            "maxzoom": 14,
            "name": args.layer_name,
            "bounds": bbox,
        }
        await asyncio.sleep(0.3)

    return {
        "status": "displayed",
        "layer_name": args.layer_name,
        "source_id": source_id,
        "scene_id": best.get("id"),
        "scene_date": best.get("datetime"),
        "cloud_cover": best.get("cloud_cover"),
        "bbox": bbox,
    }
