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
}

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
    band1_href: str, band2_href: str, bbox: list[float], index_name: str
) -> Optional[Dict[str, Any]]:
    import rasterio
    from rasterio.env import Env as RasterioEnv
    from rasterio.windows import from_bounds

    with RasterioEnv(**_GDAL_ENV):
        with rasterio.open(band1_href) as src1, rasterio.open(band2_href) as src2:
            window1 = from_bounds(*bbox, transform=src1.transform)
            window2 = from_bounds(*bbox, transform=src2.transform)

            max_size = 1024
            w1_width = min(int(window1.width), max_size)
            w1_height = min(int(window1.height), max_size)
            w2_width = min(int(window2.width), max_size)
            w2_height = min(int(window2.height), max_size)

            b1 = src1.read(
                1, window=window1, out_shape=(w1_height, w1_width)
            ).astype(np.float32)
            b2 = src2.read(
                1, window=window2, out_shape=(w2_height, w2_width)
            ).astype(np.float32)

            min_h = min(b1.shape[0], b2.shape[0])
            min_w = min(b1.shape[1], b2.shape[1])
            b1 = b1[:min_h, :min_w]
            b2 = b2[:min_h, :min_w]

            denom = b1 + b2
            if index_name == "ndvi":
                index_arr = np.where(denom != 0, (b2 - b1) / denom, 0.0)
            else:
                index_arr = np.where(denom != 0, (b1 - b2) / denom, 0.0)

            valid = (index_arr >= -1.0) & (index_arr <= 1.0) & np.isfinite(index_arr)
            valid &= (b1 > 0) | (b2 > 0)
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

    band1_href = _resolve_band_href(assets, formula["band1"])
    band2_href = _resolve_band_href(assets, formula["band2"])

    if not band1_href or not band2_href:
        return {
            "status": "error",
            "error": f"Scene missing required bands for {index_name.upper()}: need {formula['band1']} and {formula['band2']}",
        }

    stats = await loop.run_in_executor(
        None,
        lambda: _compute_stats_from_cogs(band1_href, band2_href, bbox, index_name),
    )

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
    else:
        response["statistics"] = None
        response["warning"] = "No valid pixels found for statistics computation"

    return response
