import asyncio
import logging
import uuid
from typing import Any, Dict
from urllib.parse import quote

from pydantic import BaseModel, Field

from src.routes.websocket import kue_ephemeral_action
from src.services.stac_service import STACService
from src.tools.pyd import IngabeToolCallMetaArgs

logger = logging.getLogger(__name__)


# Style presets — maps a domain-meaningful style_hint to a (colormap, rescale, expression)
# triple that cog_tile_router knows how to render. The convention is: callers pass a
# style_hint that names what they're displaying ("soil_nitrogen", "ndvi", "drought_severity"),
# and the tool resolves it here. Adding a new domain layer = one row in this dict.
STYLE_PRESETS: Dict[str, Dict[str, Any]] = {
    # Spectral indices (already supported by cog_tile_router as expression modes)
    "ndvi":               {"expression": "ndvi", "colormap": "rdylgn",   "rescale": "-0.2,0.9"},
    "ndwi":               {"expression": "ndwi", "colormap": "rdbu_r",   "rescale": "-0.5,0.8"},
    "nbr":                {"expression": "nbr",  "colormap": "rdylgn",   "rescale": "-0.5,0.8"},
    "visual":             {"expression": "visual", "colormap": "",       "rescale": ""},
    # Soil chemistry — back-transformed values in real units (g/kg, ppm, pH)
    "soil_nitrogen":      {"expression": "single_band", "colormap": "ylgn",     "rescale": "0,5"},     # g/kg
    "soil_phosphorus":    {"expression": "single_band", "colormap": "ylorrd",   "rescale": "0,30"},    # ppm
    "soil_potassium":     {"expression": "single_band", "colormap": "ylgnbu",   "rescale": "0,300"},   # ppm
    "soil_ph":            {"expression": "single_band", "colormap": "rdbu",     "rescale": "4,8"},
    "soil_organic_carbon":{"expression": "single_band", "colormap": "ylorbr",   "rescale": "0,40"},    # g/kg
    "soil_clay":          {"expression": "single_band", "colormap": "ylorbr",   "rescale": "0,80"},    # %
    "soil_sand":          {"expression": "single_band", "colormap": "ylorbr_r", "rescale": "0,80"},    # %
    # Vegetation/anomaly z-scores (-3 stress → +3 healthy)
    "anomaly_zscore":     {"expression": "single_band", "colormap": "rdbu",     "rescale": "-3,3"},
    # Drought severity (VCI 0=worst → 1=normal, or DroughtCondCat 0..4)
    "drought_severity":   {"expression": "single_band", "colormap": "reds",     "rescale": "0,4"},
    # Soil moisture (volumetric water content 0..0.5 m³/m³)
    "soil_moisture":      {"expression": "single_band", "colormap": "blues",    "rescale": "0,0.5"},
    # Evapotranspiration (mm/day, 0..10)
    "evapotranspiration": {"expression": "single_band", "colormap": "viridis",  "rescale": "0,10"},
    # Temperature (degrees C, -10..40 covers Rwanda comfortably)
    "temperature":        {"expression": "single_band", "colormap": "rdylbu_r", "rescale": "-10,40"},
    # Rainfall accumulation (mm, 0..500 covers a Rwandan growing season)
    "rainfall":           {"expression": "single_band", "colormap": "blues",    "rescale": "0,500"},
}


class DisplayLayerArgs(BaseModel):
    asset_url: str = Field(
        ...,
        description=(
            "Public HTTPS URL of a Cloud-Optimized GeoTIFF (COG). Examples: "
            "'https://isdasoil.s3.amazonaws.com/soil_data/nitrogen_total/nitrogen_total.tif', "
            "'https://earth-search.aws.element84.com/...../B04.tif'. "
            "GeoJSON URLs and S3 protocol URLs are not yet supported in this version."
        ),
    )
    title: str = Field(
        ...,
        description="Human-readable layer name shown in the layers panel, e.g. 'Soil Nitrogen — Cyampirita'",
    )
    style_hint: str = Field(
        ...,
        description=(
            "Style preset that picks the colormap and value range. One of: "
            "ndvi, ndwi, nbr, visual, soil_nitrogen, soil_phosphorus, soil_potassium, "
            "soil_ph, soil_organic_carbon, soil_clay, soil_sand, anomaly_zscore, "
            "drought_severity, soil_moisture, evapotranspiration, temperature, rainfall."
        ),
    )
    bbox: str = Field(
        ...,
        description="Bounding box of the area to display, as 'west,south,east,north' in WGS84. Used for auto-zoom.",
    )
    band_index: int = Field(
        ...,
        description="1-based band index for single-band rasters (use 1 for most soil COGs and z-score rasters).",
    )


async def display_layer(
    args: DisplayLayerArgs, meta: IngabeToolCallMetaArgs
) -> Dict[str, Any]:
    """Display any public Cloud-Optimized GeoTIFF on the map with a styled colormap.

    This is the GENERIC display tool. Use it after computing or identifying a spatial
    raster you want the user to SEE. Pair it with analytical tools that return a URL:
    1. Call the analytical tool (e.g. get_soil_properties returns iSDAsoil COG URL).
    2. Call display_layer with that URL + a style_hint that names the domain.

    The frontend renders the layer immediately and adds it to the user's layer panel
    so they can toggle it on/off.

    Style hints map to colormaps and value ranges defined in STYLE_PRESETS. Pick the
    one that matches the data's domain — soil_nitrogen for N maps, ndvi for NDVI,
    drought_severity for VCI, etc.
    """
    try:
        bbox = [float(x.strip()) for x in args.bbox.split(",")]
        if len(bbox) != 4:
            return {"status": "error", "error": "bbox must have 4 values: west,south,east,north"}
    except ValueError:
        return {"status": "error", "error": "bbox values must be numbers"}

    preset = STYLE_PRESETS.get(args.style_hint)
    if preset is None:
        return {
            "status": "error",
            "error": f"Unknown style_hint '{args.style_hint}'. Valid: {sorted(STYLE_PRESETS.keys())}",
        }

    # Build the cog-tiles URL with the right query params for this style preset.
    # The frontend will use this template; MapLibre fills {z}/{x}/{y} per tile.
    params = [f"url={quote(args.asset_url, safe='')}", f"expression={preset['expression']}"]
    if preset["expression"] == "single_band":
        params.append(f"colormap={preset['colormap']}")
        params.append(f"rescale={preset['rescale']}")
        params.append(f"band_index={args.band_index}")

    tile_url = "/api/cog-tiles/{z}/{x}/{y}.png?" + "&".join(params)
    source_id = f"sage-display-{uuid.uuid4().hex[:8]}"

    async with kue_ephemeral_action(
        meta.conversation_id,
        f"Adding layer: {args.title}",
        bounds=bbox,
    ) as payload:
        payload.updates["add_tile_layer"] = {
            "source_id": source_id,
            "tiles": [tile_url],
            "tileSize": 256,
            "maxzoom": 14,
            "name": args.title,
            "bounds": bbox,
            "style_hint": args.style_hint,
        }
        await asyncio.sleep(0.3)

    return {
        "status": "displayed",
        "source_id": source_id,
        "title": args.title,
        "style_hint": args.style_hint,
        "asset_url": args.asset_url,
        "bbox": bbox,
        "tile_template": tile_url,
    }


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
    try:
        result = await loop.run_in_executor(
            None,
            lambda: stac.search_imagery(
                bbox=bbox,
                datetime_range=datetime_range,
                max_cloud_cover=30.0,
                limit=5,
            ),
        )
    except Exception as e:
        logger.exception("STAC search failed for display_satellite_layer")
        return {"status": "error", "error": f"Satellite imagery search failed: {e}"}

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
