"""Satellite tile proxy — serves tiles via Sentinel Hub Process API.

Keeps OAuth2 credentials server-side and caches tiles in Redis to reduce
Processing Unit (PU) consumption. Follows the same pattern as
worldcover_router.py.

Endpoint: GET /api/satellite/{z}/{x}/{y}.png
"""

import asyncio
import io
import logging
from datetime import date, timedelta
from functools import lru_cache

from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import Response

from src.circuit_breaker import sentinel_hub_cb
from src.services.sentinel_hub_tiles import (
    fetch_tile,
    is_configured,
    search_catalog,
    tile_bbox_3857,
)
from src.tile_cache import tile_cache

logger = logging.getLogger(__name__)

satellite_router = APIRouter(prefix="/api", tags=["Satellite"])

# Limit concurrent requests to Sentinel Hub to avoid rate-limiting
_SH_SEMAPHORE = asyncio.Semaphore(16)


@lru_cache(maxsize=1)
def _transparent_tile() -> bytes:
    """512x512 transparent PNG — must match tileSize in basemap source config."""
    from PIL import Image

    buf = io.BytesIO()
    Image.new("RGBA", (512, 512), (0, 0, 0, 0)).save(buf, format="PNG")
    return buf.getvalue()


_TILE_HEADERS = {
    "Cache-Control": "public, max-age=604800",
    "Access-Control-Allow-Origin": "*",
    "Access-Control-Allow-Methods": "GET, OPTIONS",
    "Access-Control-Allow-Headers": "Range, Content-Type",
}


@satellite_router.get(
    "/satellite/{z}/{x}/{y}.png",
    operation_id="get_satellite_tile",
    summary="Sentinel Hub satellite imagery tile",
    description=(
        "Proxies XYZ raster tiles from Sentinel Hub Process API. "
        "Supports Sentinel-2, PlanetScope, and SkySat collections. "
        "Tiles are cached in Redis for 1 hour."
    ),
)
async def get_satellite_tile(
    z: int,
    x: int,
    y: int,
    layer: str = Query("TRUE-COLOR", description="Visualization (TRUE-COLOR, NDVI, FALSE-COLOR, NDRE)"),
    collection: str = Query("sentinel-2-l2a", description="Data collection (sentinel-2-l2a, planetscope, skysat)"),
    date_from: str = Query("", description="Start date ISO (e.g. 2025-05-01)"),
    date_to: str = Query("", description="End date ISO (e.g. 2025-05-31)"),
    maxcc: int = Query(100, ge=0, le=100, description="Max cloud coverage %"),
    hd: bool = Query(True, description="HD mode: 512px tiles with BICUBIC upsampling"),
    mosaic: str = Query("leastCC", description="Mosaicking order: leastCC (clearest) or mostRecent (newest)"),
):
    if not is_configured():
        raise HTTPException(
            status_code=503,
            detail="Sentinel Hub credentials not configured",
        )

    if z < 0 or z > 18 or x < 0 or y < 0 or x >= (1 << z) or y >= (1 << z):
        raise HTTPException(status_code=400, detail="Invalid tile coordinates")

    tile_size = 512 if hd else 256

    # Default to last 60 days — gives enough scenes to find clear imagery
    # even during Rwanda's rainy season (Oct-May)
    if not date_from or not date_to:
        today = date.today()
        date_to = today.isoformat()
        date_from = (today - timedelta(days=60)).isoformat()

    if not sentinel_hub_cb.can_execute():
        return Response(
            content=_transparent_tile(),
            media_type="image/png",
            headers=_TILE_HEADERS,
        )

    # Build cache key (include hd flag to separate 256/512 caches)
    cache_key = f"sat-{collection}-{layer}-{date_from}-{date_to}-cc{maxcc}-{'hd' if hd else 'sd'}-{mosaic}"

    # Check Redis cache
    cached = await tile_cache.get(cache_key, z, x, y, fmt="sat")
    if cached is not None:
        return Response(content=cached, media_type="image/png", headers=_TILE_HEADERS)

    # Cache miss — fetch from Sentinel Hub Process API
    bbox = tile_bbox_3857(z, x, y)

    try:
        async with _SH_SEMAPHORE:
            png_bytes = await fetch_tile(
                collection=collection,
                layer=layer,
                bbox=bbox,
                date_from=date_from,
                date_to=date_to,
                maxcc=maxcc,
                width=tile_size,
                height=tile_size,
                mosaic=mosaic,
            )

        if png_bytes is None:
            sentinel_hub_cb.record_failure()
            return Response(
                content=_transparent_tile(),
                media_type="image/png",
                headers=_TILE_HEADERS,
            )

        sentinel_hub_cb.record_success()

    except Exception:
        logger.exception("Sentinel Hub tile fetch failed z=%d x=%d y=%d", z, x, y)
        sentinel_hub_cb.record_failure()
        return Response(
            content=_transparent_tile(),
            media_type="image/png",
            headers=_TILE_HEADERS,
        )

    # Empty or very small response — treat as no data
    if len(png_bytes) < 100:
        empty = _transparent_tile()
        await tile_cache.put(cache_key, z, x, y, empty, fmt="sat")
        return Response(content=empty, media_type="image/png", headers=_TILE_HEADERS)

    await tile_cache.put(cache_key, z, x, y, png_bytes, fmt="sat")
    return Response(content=png_bytes, media_type="image/png", headers=_TILE_HEADERS)


@satellite_router.get(
    "/satellite/scene-info",
    operation_id="get_satellite_scene_info",
    summary="Scene metadata for visible satellite imagery",
    description=(
        "Queries Sentinel Hub Catalog for the scene currently displayed "
        "on the map (least cloudy in the date range). Returns acquisition "
        "date, cloud cover %, and date range."
    ),
)
async def get_satellite_scene_info(
    west: float = Query(..., description="West bound (longitude)"),
    south: float = Query(..., description="South bound (latitude)"),
    east: float = Query(..., description="East bound (longitude)"),
    north: float = Query(..., description="North bound (latitude)"),
    collection: str = Query("sentinel-2-l2a"),
    date_from: str = Query(""),
    date_to: str = Query(""),
    mosaic: str = Query("leastCC", description="Mosaicking order: leastCC (clearest) or mostRecent (newest)"),
):
    if not is_configured():
        raise HTTPException(status_code=503, detail="Sentinel Hub not configured")

    # Default to same 60-day window used for tiles
    if not date_from or not date_to:
        today = date.today()
        date_to = today.isoformat()
        date_from = (today - timedelta(days=60)).isoformat()

    scenes = await search_catalog(
        bbox_wgs84=(west, south, east, north),
        collection=collection,
        date_from=date_from,
        date_to=date_to,
    )

    if not scenes:
        return {
            "scene_date": None,
            "cloud_cover": None,
            "date_from": date_from,
            "date_to": date_to,
            "scenes_available": 0,
            "mosaic": mosaic,
        }

    # Sort based on mosaic mode: leastCC → by cloud cover, mostRecent → by date
    if mosaic == "mostRecent":
        scenes.sort(key=lambda s: s.get("datetime", ""), reverse=True)

    best = scenes[0]
    return {
        "scene_date": best["datetime"],
        "cloud_cover": best["cloud_cover"],
        "date_from": date_from,
        "date_to": date_to,
        "scenes_available": len(scenes),
        "mosaic": mosaic,
    }
