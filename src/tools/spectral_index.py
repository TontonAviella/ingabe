import asyncio
import logging
import uuid
from typing import Any, Dict, Optional

import numpy as np
from pydantic import BaseModel, Field

from src.routes.websocket import kue_ephemeral_action
from src.services.stac_service import STACService
from src.tools.pyd import IngabeToolCallMetaArgs

logger = logging.getLogger(__name__)

_INDEX_FORMULAS = {
    "ndvi": {"band1": "red", "band2": "nir", "label": "NDVI (vegetation)"},
    "ndwi": {"band1": "green", "band2": "nir", "label": "NDWI (water)"},
    "nbr": {"band1": "nir", "band2": "swir22", "label": "NBR (burn severity)"},
}

_BAND_ASSET_KEYS = {
    "red": ["red", "B04"],
    "green": ["green", "B03"],
    "nir": ["nir", "B08"],
    "swir22": ["swir22", "B12"],
    "scl": ["scl", "SCL"],
}

# Below this share of clear ground the numbers describe too little of the area to stand without a warning.
MOSTLY_CLEAR = 0.5

_GDAL_ENV = {
    "GDAL_HTTP_MERGE_CONSECUTIVE_RANGES": "YES",
    "GDAL_DISABLE_READDIR_ON_OPEN": "EMPTY_DIR",
    "CPL_VSIL_CURL_ALLOWED_EXTENSIONS": ".tif,.tiff",
    "GDAL_HTTP_MAX_RETRY": "3",
    "GDAL_HTTP_RETRY_DELAY": "1",
}


class ComputeSpectralIndexArgs(BaseModel):
    bbox: str = Field(
        ...,
        description="Bounding box as 'west,south,east,north' in WGS84 coordinates",
    )
    index: str = Field(
        ...,
        description="Spectral index to compute: 'ndvi' (vegetation health), 'ndwi' (water/flooding), or 'nbr' (burn severity)",
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
        description="Display name for the map layer, e.g. 'NDVI Musanze Jan 2025'",
    )


def _resolve_band_href(assets: dict, role: str) -> Optional[str]:
    for key in _BAND_ASSET_KEYS.get(role, []):
        if key in assets:
            return assets[key].get("href")
    return None


def _compute_stats_from_cogs(
    band1_href: str, band2_href: str, bbox: list[float], index_name: str, scl_href: Optional[str] = None
) -> Optional[Dict[str, Any]]:
    """Index statistics over bbox (west, south, east, north in latitude and longitude) from two band files.

    Each band file keeps its own grid in metres (UTM), so the box is moved onto that grid before any
    pixel is read. Until 2026-11-23 the latitude/longitude box was laid straight onto the metre grid,
    which pointed outside every scene: no place in Rwanda ever got numbers (five of five tried). Both
    bands are read onto the first band's pixel grid, so a 20 m band lines up with a 10 m one. Only the
    part of the box inside the scene is read.

    With the scene's class map (scl_href), cloud, cloud shadow, thin cloud, snow and empty pixels are
    left out (stac_service.SCL_UNUSABLE) and ``clear_share`` says how much of the box was left. The
    class map misses haze: a pass it called fully clear read NDVI 0.25 with half the usual spread
    (Cyampirita, 2026-10-23). None when the box has no usable pixel.
    """
    import rasterio
    from rasterio.enums import Resampling
    from rasterio.env import Env as RasterioEnv
    from rasterio.errors import WindowError
    from rasterio.warp import transform_bounds
    from rasterio.windows import Window, from_bounds

    from src.services.stac_service import SCL_UNUSABLE

    def over_box(src):
        """The part of the box inside this band file, as a window on its grid; None when they do not meet."""
        window = from_bounds(*transform_bounds("EPSG:4326", src.crs, *bbox), transform=src.transform)
        try:
            window = window.intersection(Window(0, 0, src.width, src.height))
        except WindowError:
            return None
        return window if window.width > 0 and window.height > 0 else None

    with RasterioEnv(**_GDAL_ENV):
        with rasterio.open(band1_href) as src1, rasterio.open(band2_href) as src2:
            window1, window2 = over_box(src1), over_box(src2)
            if window1 is None or window2 is None:
                return None
            max_size = 1024
            shape = (
                min(max(int(round(window1.height)), 1), max_size),
                min(max(int(round(window1.width)), 1), max_size),
            )
            b1 = src1.read(1, window=window1, out_shape=shape).astype(np.float32)
            b2 = src2.read(1, window=window2, out_shape=shape).astype(np.float32)
            scl = None
            if scl_href is not None:
                with rasterio.open(scl_href) as scl_src:
                    scl_window = over_box(scl_src)
                    if scl_window is not None:
                        scl = scl_src.read(1, window=scl_window, out_shape=shape, resampling=Resampling.nearest)

    denom = b1 + b2
    safe = np.where(denom != 0, denom, 1.0)
    if index_name == "ndvi":
        index_arr = np.where(denom != 0, (b2 - b1) / safe, 0.0)
    else:
        index_arr = np.where(denom != 0, (b1 - b2) / safe, 0.0)

    valid = (index_arr >= -1.0) & (index_arr <= 1.0) & np.isfinite(index_arr)
    valid &= (b1 > 0) | (b2 > 0)
    in_scene = int(np.count_nonzero(valid))
    if scl is not None:
        valid &= ~np.isin(scl, list(SCL_UNUSABLE))
    vals = index_arr[valid]

    if len(vals) == 0:
        return None

    return {
        "mean": round(float(np.mean(vals)), 4),
        "std": round(float(np.std(vals)), 4),
        "min": round(float(np.min(vals)), 4),
        "max": round(float(np.max(vals)), 4),
        "median": round(float(np.median(vals)), 4),
        "valid_pixels": int(len(vals)),
        "cloud_masked": scl is not None,
        "clear_share": round(len(vals) / in_scene, 2) if scl is not None else None,
    }


async def compute_spectral_index(
    args: ComputeSpectralIndexArgs, meta: IngabeToolCallMetaArgs
) -> Dict[str, Any]:
    """Compute a spectral index (NDVI, NDWI, or NBR) over an area and display it on the map with a colormap. Returns area statistics (mean, std, min, max). Use this for vegetation health analysis, water/flood detection, or burn severity mapping."""
    index_name = args.index.lower()
    if index_name not in _INDEX_FORMULAS:
        return {"status": "error", "error": f"Unknown index '{args.index}'. Use: ndvi, ndwi, nbr"}

    try:
        bbox = [float(x.strip()) for x in args.bbox.split(",")]
        if len(bbox) != 4:
            return {"status": "error", "error": "bbox must have 4 values: west,south,east,north"}
    except ValueError:
        return {"status": "error", "error": "bbox values must be numbers"}

    datetime_range = f"{args.date_from}/{args.date_to}"
    formula = _INDEX_FORMULAS[index_name]

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
        logger.exception("STAC search failed for compute_spectral_index")
        return {"status": "error", "error": f"Satellite imagery search failed: {e}"}
    if "error" in result:
        # A failed search is not "no scenes found" (2026-10-07).
        logger.warning("STAC search failed for compute_spectral_index: %s", result["error"])
        return {"status": "error", "error": f"Satellite imagery search failed: {result['error']}"}

    items = result.get("items", [])
    if not items:
        return {
            "status": "error",
            "error": f"No Sentinel-2 scenes found for {datetime_range} with <30% cloud cover",
        }

    best = min(items, key=lambda x: x.get("cloud_cover", 100) or 100)
    assets = best.get("assets", {})

    band1_href = _resolve_band_href(assets, formula["band1"])
    band2_href = _resolve_band_href(assets, formula["band2"])

    if not band1_href or not band2_href:
        return {
            "status": "error",
            "error": f"Scene missing required bands for {index_name.upper()}: need {formula['band1']} and {formula['band2']}",
        }

    scl_href = _resolve_band_href(assets, "scl")
    try:
        stats = await loop.run_in_executor(
            None,
            lambda: _compute_stats_from_cogs(band1_href, band2_href, bbox, index_name, scl_href),
        )
    except Exception as e:
        logger.warning("Stats computation failed for %s: %s", index_name, e)
        stats = None

    from src.tools.display_layer import _build_cog_tile_url

    if index_name == "ndvi":
        tile_url = _build_cog_tile_url(band1_href, expression="ndvi", nir_href=band2_href)
    elif index_name == "ndwi":
        tile_url = _build_cog_tile_url(band2_href, expression="ndwi", nir_href=band2_href, green_href=band1_href)
    elif index_name == "nbr":
        tile_url = _build_cog_tile_url(band1_href, expression="nbr", nir_href=band1_href, swir_href=band2_href)
    else:
        tile_url = _build_cog_tile_url(band1_href, expression=index_name, nir_href=band2_href)

    source_id = f"sage-{index_name}-{uuid.uuid4().hex[:8]}"

    async with kue_ephemeral_action(
        meta.conversation_id,
        f"Computing {index_name.upper()}: {args.layer_name}",
        bounds=bbox,
    ) as payload:
        payload.updates["add_tile_layer"] = {
            "source_id": source_id,
            "tiles": [tile_url],
            "tileSize": 256,
            "maxzoom": 14,
            "name": args.layer_name,
            "bounds": bbox,
            "index": index_name,
        }
        await asyncio.sleep(0.3)

    response: Dict[str, Any] = {
        "status": "displayed",
        "index": index_name.upper(),
        "layer_name": args.layer_name,
        "source_id": source_id,
        "scene_id": best.get("id"),
        "scene_date": best.get("datetime"),
        "cloud_cover": best.get("cloud_cover"),
        "bbox": bbox,
    }

    if stats:
        response["statistics"] = stats
        clear = stats.get("clear_share")
        if clear is not None and clear < MOSTLY_CLEAR:
            response["warning"] = (
                f"Only {clear:.0%} of this area was clear of cloud in this scene; "
                "the statistics describe that part only, and the map layer still shows the cloud."
            )
    else:
        response["statistics"] = None
        response["warning"] = "No clear ground in this area in this scene, so there are no statistics."

    return response
