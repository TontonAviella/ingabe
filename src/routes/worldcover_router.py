import io
import logging
from functools import lru_cache
from typing import Optional

from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import Response

from src.services.admin_boundaries import lookup_admin_geometry
from src.tile_cache import tile_cache
from src.worldcover import render_tile

logger = logging.getLogger(__name__)

worldcover_router = APIRouter(prefix="/api", tags=["WorldCover"])


@lru_cache(maxsize=1)
def _transparent_tile() -> bytes:
    from PIL import Image
    buf = io.BytesIO()
    Image.new("RGBA", (256, 256), (0, 0, 0, 0)).save(buf, format="PNG")
    return buf.getvalue()


_TILE_HEADERS = {
    "Cache-Control": "public, max-age=86400",
    "Access-Control-Allow-Origin": "*",
    "Access-Control-Allow-Methods": "GET, OPTIONS",
    "Access-Control-Allow-Headers": "Range, Content-Type",
}


@worldcover_router.get(
    "/worldcover/{z}/{x}/{y}.png",
    operation_id="get_worldcover_tile",
    summary="ESRI 10m Annual Land Cover 2024 tile",
    description="Serves XYZ raster tiles from ESRI / Impact Observatory 10m Annual LULC 2024. "
    "mode=all shows all 9 land cover classes. mode=cropland highlights cropland only. "
    "Pass district/sector/cell/village to clip the tile to an admin boundary.",
)
async def get_worldcover_tile(
    z: int,
    x: int,
    y: int,
    mode: str = Query("all", pattern="^(all|cropland)$"),
    district: Optional[str] = Query(None, description="Clip to Rwanda district boundary"),
    sector: Optional[str] = Query(None, description="Clip to Rwanda sector boundary"),
    cell: Optional[str] = Query(None, description="Clip to Rwanda cell boundary"),
    village: Optional[str] = Query(None, description="Clip to Rwanda village boundary"),
    bbox: Optional[str] = Query(None, description="Clip to bounding box: west,south,east,north in EPSG:4326"),
):
    if z < 0 or z > 16 or x < 0 or y < 0 or x >= (1 << z) or y >= (1 << z):
        raise HTTPException(status_code=400, detail="Invalid tile coordinates")

    # Build cache key including admin filter or bbox
    admin_suffix = ""
    if village:
        admin_suffix = f"-village:{village.lower()}"
    elif cell:
        admin_suffix = f"-cell:{cell.lower()}"
    elif sector:
        admin_suffix = f"-sector:{sector.lower()}"
    elif district:
        admin_suffix = f"-district:{district.lower()}"
    elif bbox:
        admin_suffix = f"-bbox:{bbox}"

    cache_key = f"wc-{mode}{admin_suffix}"

    # Check Redis cache
    cached = await tile_cache.get(cache_key, z, x, y)
    if cached is not None:
        return Response(content=cached, media_type="image/png", headers=_TILE_HEADERS)

    # Fetch clip geometry if admin filter or bbox specified
    clip_geometry = None
    if district or sector or cell or village:
        clip_geometry = await lookup_admin_geometry(
            district=district, sector=sector, cell=cell, village=village,
        )
        # If admin name not found, return transparent tile (not an error —
        # the LLM might have misspelled it)
        if clip_geometry is None:
            logger.warning(
                "Admin boundary not found: district=%s sector=%s cell=%s village=%s",
                district, sector, cell, village,
            )
    elif bbox:
        try:
            west, south, east, north = [float(v) for v in bbox.split(",")]
            clip_geometry = {
                "type": "Polygon",
                "coordinates": [[
                    [west, south],
                    [east, south],
                    [east, north],
                    [west, north],
                    [west, south],
                ]],
            }
        except (ValueError, TypeError):
            logger.warning("Invalid bbox parameter: %s", bbox)

    # Render from remote COG (blocking I/O — run off event loop)
    try:
        import asyncio
        png_bytes = await asyncio.to_thread(render_tile, x, y, z, mode=mode, clip_geometry=clip_geometry)
    except Exception:
        logger.exception("WorldCover tile render failed z=%d x=%d y=%d", z, x, y)
        return Response(content=_transparent_tile(), media_type="image/png", headers=_TILE_HEADERS)

    if png_bytes is None:
        # Tile outside WorldCover extent — cache the empty tile
        empty = _transparent_tile()
        await tile_cache.put(cache_key, z, x, y, empty)
        return Response(content=empty, media_type="image/png", headers=_TILE_HEADERS)

    await tile_cache.put(cache_key, z, x, y, png_bytes)
    return Response(content=png_bytes, media_type="image/png", headers=_TILE_HEADERS)
