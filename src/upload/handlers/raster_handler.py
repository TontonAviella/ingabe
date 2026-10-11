"""Raster upload handler — GeoTIFF, JPEG, PNG, DEM.

Metadata extraction uses ``gdalinfo -json`` as a subprocess to avoid
GDAL segfaults that kill uvicorn workers.  COG generation is handled
by a background task in ``postgres_routes.py`` using ``gdalwarp`` subprocess.
"""

import asyncio
import json
import logging
import os
import re
import time
from typing import Optional
from urllib.parse import urlparse

from fastapi import HTTPException, status

from src.services.photo_inspections import PHOTO_EXTENSIONS
from src.upload.base import BaseUploadHandler, HandlerResult, UploadContext

logger = logging.getLogger(__name__)

DEFAULT_INLINE_STATS_MAX_BYTES = 16 * 1024 * 1024

NO_MAP_POSITION = (
    "This photo has no map position. Upload a georeferenced GeoTIFF, or turn on photo inspections."
)


class RasterUploadHandler(BaseUploadHandler):
    """Handles raster file uploads (GeoTIFF, JPEG, PNG, DEM).

    Preprocessing extracts bounds, CRS, and band statistics.
    No file conversion is performed — the original file is uploaded to S3.
    """

    async def preprocess(self, ctx: UploadContext) -> HandlerResult:
        return HandlerResult(layer_type="raster")

    async def create_layers(
        self, ctx: UploadContext, result: HandlerResult
    ) -> HandlerResult:
        """Extract raster metadata and insert a single layer row."""
        bounds = await self._extract_metadata(ctx)
        result.bounds = bounds

        metadata_to_store = dict(ctx.metadata_dict)

        await ctx.conn.execute(
            """
            INSERT INTO map_layers
            (layer_id, owner_uuid, name, type, metadata, bounds, geometry_type, feature_count, s3_key, size_bytes, source_map_id)
            VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11)
            """,
            ctx.layer_id,
            ctx.user_id,
            ctx.layer_name,
            "raster",
            json.dumps(metadata_to_store),
            bounds,
            None,
            None,
            ctx.s3_key,
            ctx.file_size_bytes,
            ctx.map_id,
        )
        result.created_layer_ids.append(ctx.layer_id)
        result.first_layer_name = ctx.layer_name
        result.first_layer_url = f"/api/layer/{ctx.layer_id}.cog.tif"

        # Enqueue brain hook: match raster to field pages via spatial overlap
        try:
            from src.dependencies.brain_dep import get_brain_service
            brain_svc = get_brain_service()
            await brain_svc.enqueue_hook(ctx.conn, "raster_upload", {
                "layer_id": ctx.layer_id,
                "layer_name": ctx.layer_name,
                "user_id": ctx.user_id,
                "bounds": bounds,
            })
        except Exception:
            logger.debug("Brain hook enqueue skipped (tables may not exist yet)")

        return result

    async def _extract_metadata(self, ctx: UploadContext):
        """Extract bounds and statistics via gdalinfo subprocess (crash-safe).

        ALL in-process GDAL/rasterio calls can segfault on certain rasters
        (e.g. multi-band NDVI/NDRE), killing the uvicorn worker.
        Using ``gdalinfo -json`` as a subprocess is 100% isolated.

        A JPG or PNG that GDAL cannot place on the map is refused (400): its
        pixel corners are not coordinates and would break the map style.
        """
        stats = (
            os.environ.get("RASTER_UPLOAD_COMPUTE_STATS", "0") == "1"
            or _should_compute_inline_stats(ctx)
        )
        started = time.monotonic()
        info = await gdalinfo_json(ctx.temp_file_path, stats=stats)
        elapsed = time.monotonic() - started
        if info is None:
            logger.warning("gdalinfo failed for %s", ctx.layer_id)
            return None
        if ctx.file_ext.lower() in PHOTO_EXTENSIONS and not has_map_position(info):
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=NO_MAP_POSITION)

        # Extract bounds in EPSG:4326
        bounds = None
        if "wgs84Extent" in info and "coordinates" in info["wgs84Extent"]:
            coords = info["wgs84Extent"]["coordinates"][0]  # ring of [lon, lat]
            lons = [c[0] for c in coords]
            lats = [c[1] for c in coords]
            bounds = [min(lons), min(lats), max(lons), max(lats)]
        elif "cornerCoordinates" in info:
            cc = info["cornerCoordinates"]
            ul = cc.get("upperLeft", [0, 0])
            lr = cc.get("lowerRight", [0, 0])
            bounds = [ul[0], lr[1], lr[0], ul[1]]

        # Extract EPSG code from CRS
        # Use the LAST AUTHORITY["EPSG", ...] in the WKT — for projected CRS
        # (e.g. UTM), the outermost (last) AUTHORITY is the projected SRID,
        # while inner ones belong to the geographic CRS.
        crs_info = info.get("coordinateSystem", {})
        wkt = crs_info.get("wkt", "")
        if wkt:
            matches = re.findall(r'"EPSG",\s*"?(\d+)"?', wkt)
            if matches:
                ctx.metadata_dict["original_srid"] = int(matches[-1])
        else:
            ctx.metadata_dict["crs_missing"] = True

        # Band statistics (single-band only)
        bands = info.get("bands", [])
        if len(bands) == 1:
            band = bands[0]
            if "computedMin" in band and "computedMax" in band:
                ctx.metadata_dict["raster_value_stats_b1"] = {
                    "min": band["computedMin"],
                    "max": band["computedMax"],
                }
            elif "minimum" in band and "maximum" in band:
                ctx.metadata_dict["raster_value_stats_b1"] = {
                    "min": band["minimum"],
                    "max": band["maximum"],
                }

        # Store band count and dimensions
        size = info.get("size", [0, 0])
        ctx.metadata_dict["band_count"] = len(bands)
        ctx.metadata_dict["width"] = size[0] if size else 0
        ctx.metadata_dict["height"] = size[1] if len(size) > 1 else 0

        logger.info(
            "Raster metadata extracted via gdalinfo subprocess for %s in %.2fs",
            ctx.layer_id,
            elapsed,
        )
        return bounds


async def gdalinfo_json(path: str, *, stats: bool = False) -> Optional[dict]:
    """``gdalinfo -json`` of `path` (a local file or a /vsis3/ object), run in a subprocess so a GDAL crash cannot
    take the worker down. None when gdalinfo fails (its error is logged)."""
    cmd = ["gdalinfo", "-json"]
    if stats:
        cmd.append("-stats")

    env = os.environ.copy()
    if path.startswith("/vsis3/"):
        endpoint = urlparse(os.environ.get("S3_ENDPOINT_URL", "http://minio:9000"))
        env.setdefault("AWS_ACCESS_KEY_ID", os.environ.get("S3_ACCESS_KEY_ID", ""))
        env.setdefault("AWS_SECRET_ACCESS_KEY", os.environ.get("S3_SECRET_ACCESS_KEY", ""))
        env.setdefault("AWS_REGION", os.environ.get("S3_DEFAULT_REGION", "us-east-1"))
        env.setdefault("AWS_S3_ENDPOINT", endpoint.netloc)
        env.setdefault("AWS_HTTPS", "YES" if endpoint.scheme == "https" else "NO")
        env.setdefault("AWS_VIRTUAL_HOSTING", "FALSE")
        env.setdefault("GDAL_DISABLE_READDIR_ON_OPEN", "EMPTY_DIR")
        env.setdefault("CPL_VSIL_CURL_ALLOWED_EXTENSIONS", ".tif,.tiff")

    proc = await asyncio.create_subprocess_exec(
        *cmd, path,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        env=env,
    )
    stdout, stderr = await proc.communicate()
    if proc.returncode != 0:
        logger.warning("gdalinfo failed for %s: %s", path, stderr.decode())
        return None
    return json.loads(stdout)


def has_map_position(info: dict) -> bool:
    """True when gdalinfo places the raster on the map: it has a WGS84 extent or a coordinate system. A plain
    photo has neither, only pixel corners."""
    return bool(info.get("wgs84Extent")) or bool((info.get("coordinateSystem") or {}).get("wkt"))


def _should_compute_inline_stats(ctx: UploadContext) -> bool:
    """Compute tiny-raster stats inline without blocking large drone uploads."""
    try:
        max_bytes = int(
            os.environ.get(
                "RASTER_UPLOAD_INLINE_STATS_MAX_BYTES",
                str(DEFAULT_INLINE_STATS_MAX_BYTES),
            )
        )
    except ValueError:
        max_bytes = DEFAULT_INLINE_STATS_MAX_BYTES
    if max_bytes <= 0:
        return False

    size = ctx.file_size_bytes
    if size is None:
        if _is_gdal_virtual_path(ctx.temp_file_path):
            return False
        try:
            size = os.path.getsize(ctx.temp_file_path)
        except OSError:
            return False
    return size <= max_bytes


def _is_gdal_virtual_path(path: str) -> bool:
    return path.startswith(("/vsi",))
