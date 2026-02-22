import io
import json
import logging
from typing import Optional

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


# ── In-memory geometry cache ─────────────────────────────────────────────
# Admin boundary geometries are fetched once from PostGIS per session
# and cached in memory (they never change at runtime).
_geom_cache: dict[str, dict] = {}


async def _get_admin_geometry(
    district: Optional[str] = None,
    sector: Optional[str] = None,
    cell: Optional[str] = None,
) -> Optional[dict]:
    """Fetch GeoJSON geometry for an admin boundary from PostGIS.

    Returns cached result if available.  Priority: cell > sector > district.
    """
    cache_key = f"cell:{cell}" if cell else f"sector:{sector}" if sector else f"district:{district}"
    if cache_key in _geom_cache:
        return _geom_cache[cache_key]

    try:
        from src.structures import get_async_db_connection

        async with get_async_db_connection() as conn:
            if cell:
                row = await conn.fetchrow(
                    "SELECT ST_AsGeoJSON(geom)::text FROM rwanda_cell_boundaries WHERE LOWER(cell_name) = LOWER($1) LIMIT 1",
                    cell,
                )
            elif sector:
                row = await conn.fetchrow(
                    "SELECT ST_AsGeoJSON(geom)::text FROM rwanda_sector_boundaries WHERE LOWER(sector_name) = LOWER($1) LIMIT 1",
                    sector,
                )
            else:
                row = await conn.fetchrow(
                    "SELECT ST_AsGeoJSON(geom)::text FROM rwanda_district_boundaries WHERE LOWER(district) = LOWER($1) LIMIT 1",
                    district,
                )

            if row and row[0]:
                geom = json.loads(row[0])
                _geom_cache[cache_key] = geom
                return geom
    except Exception as e:
        logger.warning("Admin geometry lookup failed for %s: %s", cache_key, e)

    return None


@worldcover_router.get(
    "/worldcover/{z}/{x}/{y}.png",
    operation_id="get_worldcover_tile",
    summary="ESRI 10m Annual Land Cover 2024 tile",
    description="Serves XYZ raster tiles from ESRI / Impact Observatory 10m Annual LULC 2024. "
    "mode=all shows all 9 land cover classes. mode=cropland highlights cropland only. "
    "Pass district/sector/cell to clip the tile to an admin boundary.",
)
async def get_worldcover_tile(
    z: int,
    x: int,
    y: int,
    mode: str = Query("all", pattern="^(all|cropland)$"),
    district: Optional[str] = Query(None, description="Clip to Rwanda district boundary"),
    sector: Optional[str] = Query(None, description="Clip to Rwanda sector boundary"),
    cell: Optional[str] = Query(None, description="Clip to Rwanda cell boundary"),
    bbox: Optional[str] = Query(None, description="Clip to bounding box: west,south,east,north in EPSG:4326"),
):
    if z < 0 or z > 16 or x < 0 or y < 0 or x >= (1 << z) or y >= (1 << z):
        raise HTTPException(status_code=400, detail="Invalid tile coordinates")

    # Build cache key including admin filter or bbox
    admin_suffix = ""
    if cell:
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
    if district or sector or cell:
        clip_geometry = await _get_admin_geometry(
            district=district, sector=sector, cell=cell,
        )
        # If admin name not found, return transparent tile (not an error —
        # the LLM might have misspelled it)
        if clip_geometry is None:
            logger.warning(
                "Admin boundary not found: district=%s sector=%s cell=%s",
                district, sector, cell,
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

    # Render from remote COG
    try:
        png_bytes = render_tile(x, y, z, mode=mode, clip_geometry=clip_geometry)
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
