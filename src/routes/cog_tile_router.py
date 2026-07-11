"""COG tile endpoint for Earth Search satellite imagery.

Serves XYZ raster tiles from public Cloud Optimized GeoTIFFs (COGs) hosted on
Earth Search (AWS S3). No credentials needed. Uses rio-tiler for tile rendering,
same pattern as layer_router.py.

Supports spectral indices (NDVI, NDWI, NBR) via server-side band math.

Endpoint: GET /api/cog-tiles/{z}/{x}/{y}.png
"""

import asyncio
import hashlib
import io
import json
import logging
import os
import re
import time
from functools import lru_cache

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import Response

from src.dependencies.session import verify_session_required
from src.services.remote_cog_url import RemoteCogUrlError, validate_remote_cog_url
from src.structures import async_read_conn
from src.tile_cache import tile_cache
from src.utils import get_async_s3_client, get_bucket_name, s3_op

logger = logging.getLogger(__name__)

cog_tile_router = APIRouter(prefix="/api", tags=["COG Tiles"])

_COG_TILE_SEMAPHORE = asyncio.Semaphore(
    int(os.environ.get("COG_TILE_CONCURRENCY", "12"))
)

_rio_tiler_loaded = False
_Reader = None
_cmap = None
_TileOutsideBounds = None
_Image = None
_LOCAL_LAYER_REF_RE = re.compile(r"^mundi-layer:([A-Za-z0-9_-]{1,80})$")
_LOCAL_LAYER_API_RE = re.compile(r"^/api/layer/([A-Za-z0-9_-]{1,80})\.cog\.tif$")
_LOCAL_COG_URL_CACHE: dict[str, tuple[float, str, str]] = {}
_LOCAL_COG_URL_TTL = 120.0


def _ensure_rio_tiler():
    global _rio_tiler_loaded, _Reader, _cmap, _TileOutsideBounds, _Image
    if _rio_tiler_loaded:
        return
    from PIL import Image as _PILImage
    from rio_tiler.io import Reader as _RioReader
    from rio_tiler.colormap import cmap as _rio_cmap
    from rio_tiler.errors import TileOutsideBounds as _RioTileOOB

    _Image = _PILImage
    _Reader = _RioReader
    _cmap = _rio_cmap
    _TileOutsideBounds = _RioTileOOB
    _rio_tiler_loaded = True


@lru_cache(maxsize=1)
def _transparent_tile() -> bytes:
    from PIL import Image

    buf = io.BytesIO()
    Image.new("RGBA", (256, 256), (0, 0, 0, 0)).save(buf, format="PNG")
    return buf.getvalue()


INDEX_COLORMAPS = {
    "ndvi": "rdylgn",
    "ndwi": "rdbu_r",
    "nbr": "rdylgn",
}

INDEX_RESCALE = {
    "ndvi": (-0.2, 0.9),
    "ndwi": (-0.5, 0.8),
    "nbr": (-0.5, 0.8),
}

_TILE_HEADERS = {
    "Cache-Control": "public, max-age=86400",
    "Access-Control-Allow-Origin": "*",
    "Access-Control-Allow-Methods": "GET, OPTIONS",
    "Access-Control-Allow-Headers": "Range, Content-Type",
}


def _cache_key(url_hash: str, expression: str) -> str:
    return f"cog:{url_hash}:{expression}"


def _local_layer_id(asset_url: str) -> str | None:
    """Return layer id for trusted local raster references.

    Browser-facing Sage tools must not receive raw MinIO presigned URLs because
    Docker hostnames resolve to private IPs. A `mundi-layer:<id>` reference keeps
    the tile URL same-origin; this route resolves it server-side after auth.
    """
    match = _LOCAL_LAYER_REF_RE.match(asset_url)
    if match:
        return match.group(1)
    match = _LOCAL_LAYER_API_RE.match(asset_url)
    if match:
        return match.group(1)
    return None


async def _resolve_cog_asset(asset_url: str, request: Request) -> tuple[str, str]:
    layer_id = _local_layer_id(asset_url)
    if not layer_id:
        try:
            validated_url = validate_remote_cog_url(asset_url)
        except RemoteCogUrlError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return validated_url, validated_url

    session = await verify_session_required(request)
    user_id = session.get_user_id()
    async with async_read_conn("cog_tile_local_layer", user_id=user_id) as conn:
        layer_row = await conn.fetchrow(
            """
            SELECT layer_id, owner_uuid, metadata, s3_key
            FROM map_layers
            WHERE layer_id = $1
            """,
            layer_id,
        )
        if not layer_row:
            raise HTTPException(status_code=404, detail=f"Layer {layer_id} not found")

        if str(layer_row["owner_uuid"]) != user_id:
            project_row = await conn.fetchrow(
                """
                SELECT p.owner_uuid, p.editor_uuids, p.viewer_uuids
                FROM user_mundiai_projects p
                JOIN user_mundiai_maps m ON m.project_id = p.id
                WHERE $1 = ANY(m.layers)
                  AND p.soft_deleted_at IS NULL
                  AND m.soft_deleted_at IS NULL
                LIMIT 1
                """,
                layer_id,
            )
            editors = project_row["editor_uuids"] if project_row else []
            viewers = project_row["viewer_uuids"] if project_row else []
            can_access = bool(
                project_row
                and (
                    str(project_row["owner_uuid"]) == user_id
                    or user_id in [str(u) for u in (editors or []) + (viewers or [])]
                )
            )
            if not can_access:
                raise HTTPException(status_code=404, detail=f"Layer {layer_id} not found")

    metadata = layer_row["metadata"] or {}
    if isinstance(metadata, str):
        metadata = json.loads(metadata)
    cog_key = metadata.get("cog_key") if isinstance(metadata, dict) else None
    if not cog_key:
        raise HTTPException(
            status_code=409,
            detail=f"Layer {layer_id} does not have an optimized COG yet",
        )

    now = time.monotonic()
    cached = _LOCAL_COG_URL_CACHE.get(cog_key)
    if cached and (now - cached[0]) < _LOCAL_COG_URL_TTL:
        return cached[1], cached[2]

    bucket = get_bucket_name()
    s3_client = await get_async_s3_client(signature_version="s3v4")
    resolved_url = await s3_op(
        s3_client.generate_presigned_url(
            "get_object",
            Params={"Bucket": bucket, "Key": cog_key},
            ExpiresIn=180,
        ),
        "presigned URL",
        f"local display layer {layer_id}",
    )
    cache_identity = f"mundi-layer:{layer_id}:{cog_key}"
    _LOCAL_COG_URL_CACHE[cog_key] = (now, resolved_url, cache_identity)
    return resolved_url, cache_identity


@lru_cache(maxsize=8)
def _colormap_lut(cm_name: str):
    import numpy as np
    _ensure_rio_tiler()
    cm = _cmap.get(cm_name)
    return np.array([cm[i] for i in range(256)], dtype=np.uint8)


@cog_tile_router.get(
    "/cog-tiles/{z}/{x}/{y}.png",
    operation_id="get_cog_tile",
    summary="Render a tile from a public COG (Earth Search)",
)
async def get_cog_tile(
    z: int,
    x: int,
    y: int,
    request: Request,
    url: str = Query(..., description="COG URL (Earth Search S3 or any public COG)"),
    expression: str = Query(
        "visual",
        description="Rendering mode: visual (RGB), ndvi, ndwi, nbr, single_band",
        pattern="^(visual|ndvi|ndwi|nbr|single_band)$",
    ),
    nir_url: str = Query("", description="NIR band COG URL (B08) for index computation"),
    green_url: str = Query("", description="Green band COG URL (B03) for NDWI"),
    swir_url: str = Query("", description="SWIR2 band COG URL (B12) for NBR"),
    colormap: str = Query("", description="rio-tiler colormap name for single_band mode (e.g. viridis, RdYlGn, YlGn)"),
    rescale: str = Query("", description="Min,max range for single_band mode, e.g. '0,5' or '4,8'"),
    band_index: int = Query(1, ge=1, le=8, description="Band index for single_band mode (1-based, default 1)"),
):
    if z < 0 or z > 18 or x < 0 or y < 0 or x >= (1 << z) or y >= (1 << z):
        raise HTTPException(status_code=400, detail="Invalid tile coordinates")

    url, url_identity = await _resolve_cog_asset(url, request)
    if nir_url:
        nir_url, nir_identity = await _resolve_cog_asset(nir_url, request)
    else:
        nir_identity = ""
    if green_url:
        green_url, green_identity = await _resolve_cog_asset(green_url, request)
    else:
        green_identity = ""
    if swir_url:
        swir_url, swir_identity = await _resolve_cog_asset(swir_url, request)
    else:
        swir_identity = ""

    url_hash = hashlib.sha256(
        f"{url_identity}:{nir_identity}:{green_identity}:{swir_identity}:{colormap}:{rescale}:{band_index}".encode()
    ).hexdigest()[:16]
    cache_id = _cache_key(url_hash, expression)

    cached = await tile_cache.get(cache_id, z, x, y, fmt="cog")
    if cached is not None:
        return Response(content=cached, media_type="image/png", headers=_TILE_HEADERS)

    _ensure_rio_tiler()
    import numpy as np

    def _render_tile() -> bytes:
        import rasterio
        from rasterio.env import Env as RasterioEnv

        gdal_env = {
            "GDAL_HTTP_MERGE_CONSECUTIVE_RANGES": "YES",
            "GDAL_DISABLE_READDIR_ON_OPEN": "EMPTY_DIR",
            "CPL_VSIL_CURL_ALLOWED_EXTENSIONS": ".tif,.tiff",
            "GDAL_HTTP_MAX_RETRY": "3",
            "GDAL_HTTP_RETRY_DELAY": "1",
        }

        with RasterioEnv(**gdal_env):
            if expression == "visual":
                with _Reader(url) as src:
                    img = src.tile(x, y, z, tilesize=256)
                    return img.render(img_format="PNG")

            elif expression in ("ndvi", "ndwi", "nbr"):
                if expression == "ndvi":
                    band1_url = url  # red (B04)
                    band2_url = nir_url  # NIR (B08)
                    if not band2_url:
                        raise ValueError("nir_url required for NDVI")
                elif expression == "ndwi":
                    band1_url = green_url or url  # green (B03)
                    band2_url = nir_url  # NIR (B08)
                    if not band2_url:
                        raise ValueError("nir_url required for NDWI")
                elif expression == "nbr":
                    band1_url = nir_url  # NIR (B08)
                    band2_url = swir_url  # SWIR2 (B12)
                    if not band1_url or not band2_url:
                        raise ValueError("nir_url and swir_url required for NBR")

                with _Reader(band1_url) as src1, _Reader(band2_url) as src2:
                    img1 = src1.tile(x, y, z, tilesize=256)
                    img2 = src2.tile(x, y, z, tilesize=256)

                    b1 = img1.data[0].astype(np.float32)
                    b2 = img2.data[0].astype(np.float32)

                    denom = b1 + b2
                    if expression == "ndvi":
                        index = np.where(denom != 0, (b2 - b1) / denom, 0.0)
                    elif expression == "ndwi":
                        index = np.where(denom != 0, (b1 - b2) / denom, 0.0)
                    elif expression == "nbr":
                        index = np.where(denom != 0, (b1 - b2) / denom, 0.0)

                    lo, hi = INDEX_RESCALE[expression]
                    scaled = np.clip((index - lo) / (hi - lo), 0, 1)
                    scaled_uint8 = (scaled * 255).astype(np.uint8)

                    cm_lut = _colormap_lut(INDEX_COLORMAPS[expression])
                    rgba = cm_lut[scaled_uint8]

                    nodata_mask = (b1 == 0) & (b2 == 0)
                    rgba[nodata_mask] = [0, 0, 0, 0]

                    img_pil = _Image.fromarray(rgba, "RGBA")
                    buf = io.BytesIO()
                    img_pil.save(buf, format="PNG")
                    return buf.getvalue()
            elif expression == "single_band":
                # Generic colormap rendering for any single-band raster (soil
                # properties, anomaly z-scores, drought severity, etc.).
                # Caller passes explicit colormap + rescale via query string.
                if not colormap:
                    raise ValueError("colormap query param required for single_band mode")
                if not rescale:
                    raise ValueError("rescale query param required (e.g. '0,5')")
                try:
                    lo_str, hi_str = rescale.split(",", 1)
                    lo, hi = float(lo_str), float(hi_str)
                except ValueError:
                    raise ValueError(f"rescale must be 'min,max' floats, got: {rescale}")
                if hi <= lo:
                    raise ValueError(f"rescale max ({hi}) must be > min ({lo})")

                with _Reader(url) as src:
                    img = src.tile(x, y, z, indexes=[band_index], tilesize=256)
                    band = img.data[0].astype(np.float32)

                    # Use rio-tiler's mask — it respects the COG's nodata metadata
                    # (nodata tag, internal mask band, alpha band). Hardcoding
                    # `band == 0` masks valid pixels for many style presets:
                    # NDVI=0 (no veg), NDRE=0 (bare soil), z-score=0 (at-mean),
                    # 0°C temperature, 0 dB SAR backscatter, etc. iSDAsoil happens
                    # to encode nodata as 0 but its COG nodata tag says so —
                    # img.mask handles both cases correctly.
                    # img.mask convention: 255 = valid data, 0 = nodata.
                    nodata_mask = img.mask == 0

                    scaled = np.clip((band - lo) / (hi - lo), 0, 1)
                    scaled_uint8 = (scaled * 255).astype(np.uint8)

                    cm_lut = _colormap_lut(colormap)
                    rgba = cm_lut[scaled_uint8]
                    rgba[nodata_mask] = [0, 0, 0, 0]

                    img_pil = _Image.fromarray(rgba, "RGBA")
                    buf = io.BytesIO()
                    img_pil.save(buf, format="PNG")
                    return buf.getvalue()
            else:
                raise ValueError("expression must be one of: visual, ndvi, ndwi, nbr, single_band")

    try:
        async with _COG_TILE_SEMAPHORE:
            loop = asyncio.get_running_loop()
            content = await loop.run_in_executor(None, _render_tile)

        await tile_cache.put(cache_id, z, x, y, content, fmt="cog")

        return Response(content=content, media_type="image/png", headers=_TILE_HEADERS)
    except _TileOutsideBounds:
        empty = _transparent_tile()
        await tile_cache.put(cache_id, z, x, y, empty, fmt="cog")
        return Response(content=empty, media_type="image/png", headers=_TILE_HEADERS)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception:
        logger.exception("COG tile render failed z=%d x=%d y=%d", z, x, y)
        return Response(
            content=_transparent_tile(),
            media_type="image/png",
            headers={**_TILE_HEADERS, "Cache-Control": "no-cache"},
        )
