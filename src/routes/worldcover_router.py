import io
import logging
from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import Response
from PIL import Image

from src.tile_cache import tile_cache
from src.worldcover import render_tile

logger = logging.getLogger(__name__)

worldcover_router = APIRouter(prefix="/api", tags=["WorldCover"])

_EMPTY_PNG: bytes | None = None


def _transparent_tile() -> bytes:
    global _EMPTY_PNG
    if _EMPTY_PNG is None:
        buf = io.BytesIO()
        Image.new("RGBA", (256, 256), (0, 0, 0, 0)).save(buf, format="PNG")
        _EMPTY_PNG = buf.getvalue()
    return _EMPTY_PNG


_TILE_HEADERS = {
    "Cache-Control": "public, max-age=86400",
    "Access-Control-Allow-Origin": "*",
    "Access-Control-Allow-Methods": "GET, OPTIONS",
    "Access-Control-Allow-Headers": "Range, Content-Type",
}


@worldcover_router.get(
    "/worldcover/{z}/{x}/{y}.png",
    operation_id="get_worldcover_tile",
    summary="ESA WorldCover 2021 land cover tile",
    description="Serves XYZ raster tiles from ESA WorldCover 2021 v200 (10m resolution). "
    "mode=all shows all 11 land cover classes. mode=cropland highlights cropland only.",
)
async def get_worldcover_tile(
    z: int,
    x: int,
    y: int,
    mode: str = Query("all", pattern="^(all|cropland)$"),
):
    if z < 0 or z > 16 or x < 0 or y < 0 or x >= (1 << z) or y >= (1 << z):
        raise HTTPException(status_code=400, detail="Invalid tile coordinates")

    # WorldCover is 10m resolution — beyond zoom 14 it's just upscaling pixels
    # Allow up to 16 for UX but no real detail gain past ~14
    cache_key = f"wc-{mode}"

    # Check Redis cache
    cached = await tile_cache.get(cache_key, z, x, y)
    if cached is not None:
        return Response(content=cached, media_type="image/png", headers=_TILE_HEADERS)

    # Render from remote COG
    try:
        png_bytes = render_tile(x, y, z, mode=mode)
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
