import os
import json
import asyncpg
import gzip
from fastapi import (
    APIRouter,
    HTTPException,
    status,
    Request,
    Depends,
)
from fastapi.responses import StreamingResponse, Response, RedirectResponse
from src.dependencies.db_pool import get_pooled_connection
from src.dependencies.dag import get_layer
from pydantic import BaseModel, Field
from src.database.models import MapLayer
from src.dependencies.session import (
    verify_session_required,
    session_user_id,
    UserContext,
)
import logging
import re
from src.dependencies.redis_client import get_redis_client
import tempfile
import asyncio
import io
from PIL import Image
from rio_tiler.io import Reader
from rio_tiler.colormap import cmap
from rio_tiler.errors import TileOutsideBounds

from src.utils import (
    get_bucket_name,
    get_async_s3_client,
    s3_op,
)
import subprocess
from src.upload.dask_raster import DASK_AVAILABLE, RasterPipeline
from src.tile_cache import tile_cache
from src.structures import get_async_db_connection, async_conn
from src.postgis_tiles import fetch_mvt_tile, MVT_LAYER_NAME
from src.dependencies.layer_describer import LayerDescriber, get_layer_describer
from opentelemetry import trace
from src.dependencies.base_map import get_base_map_provider
from src.utils import generate_id
from boto3.s3.transfer import TransferConfig

one_shot_config = TransferConfig(multipart_threshold=5 * 1024**3)  # 5 GiB

# Global semaphore to limit concurrent social image renderings
# This prevents OOM issues when many maps load simultaneously
SOCIAL_RENDER_SEMAPHORE = asyncio.Semaphore(2)  # Max 2 concurrent renders

logger = logging.getLogger(__name__)
tracer = trace.get_tracer(__name__)

redis = get_redis_client()


layer_router = APIRouter()


@layer_router.get(
    "/layer/{layer_id}.cog.tif",
    operation_id="view_layer_as_cog_tif",
)
async def get_layer_cog_tif(
    request: Request,
    layer: MapLayer = Depends(get_layer),
):
    # Check if layer is a raster type
    if layer.type != "raster":
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Layer is not a raster type. COG can only be generated from raster data.",
        )

    if layer.remote_url and layer.remote_url.endswith(".tif"):
        return RedirectResponse(url=layer.remote_url, status_code=302)

    async with get_async_db_connection() as conn:
        bucket_name = get_bucket_name()

        # Check if metadata has cog_key
        cog_key = layer.metadata_dict.get("cog_key")

        # Set up MinIO/S3 client
        s3_client = await get_async_s3_client()

        if not cog_key:
            lock_key = f"lock:cog:{layer.layer_id}"
            lock = redis.lock(lock_key, timeout=600, blocking_timeout=30)
            loop = asyncio.get_running_loop()
            acquired = await loop.run_in_executor(None, lambda: lock.acquire(blocking=True))
            if not acquired:
                raise HTTPException(
                    status_code=423,
                    detail="COG generation in progress. Please refresh in a moment. This will take 2-3 minutes.",
                )
            try:
                row = await conn.fetchrow(
                    "SELECT metadata FROM map_layers WHERE layer_id = $1",
                    layer.layer_id,
                )
                if (
                    row
                    and isinstance(row["metadata"], dict)
                    and row["metadata"].get("cog_key")
                ):
                    cog_key = row["metadata"]["cog_key"]
                else:
                    with tempfile.TemporaryDirectory() as temp_dir:
                        # Download the raster file from S3
                        s3_key: str = str(layer.s3_key or "")
                        file_extension = os.path.splitext(s3_key)[1] if s3_key else ""
                        local_input_file = os.path.join(
                            temp_dir, f"layer_{layer.layer_id}{file_extension}"
                        )
                        s3 = await get_async_s3_client()
                        await s3_op(s3.download_file(bucket_name, s3_key, local_input_file),
                                    "download", f"layer {layer.layer_id}")

                        local_cog_file = os.path.join(
                            temp_dir, f"layer_{layer.layer_id}.cog.tif"
                        )

                        # Try Dask pipeline first, fall back to GDAL subprocess
                        if DASK_AVAILABLE:
                            try:
                                loop = asyncio.get_running_loop()
                                await loop.run_in_executor(
                                    None,
                                    RasterPipeline.create_cog,
                                    local_input_file,
                                    local_cog_file,
                                )
                                logger.info(
                                    "COG generated via Dask pipeline for %s",
                                    layer.layer_id,
                                )
                            except Exception as dask_err:
                                logger.warning(
                                    "Dask COG generation failed for %s, falling back to gdalwarp: %s",
                                    layer.layer_id,
                                    dask_err,
                                )
                                await _generate_cog_subprocess(
                                    local_input_file,
                                    local_cog_file,
                                    temp_dir,
                                    layer,
                                )
                        else:
                            await _generate_cog_subprocess(
                                local_input_file,
                                local_cog_file,
                                temp_dir,
                                layer,
                            )

                        # Upload the COG file to S3
                        cog_key = f"cog/layer/{layer.layer_id}.cog.tif"
                        s3 = await get_async_s3_client()
                        await s3_op(s3.upload_file(local_cog_file, bucket_name, cog_key, Config=one_shot_config),
                                    "COG upload", f"layer {layer.layer_id}")

                        # Update the layer metadata with the COG key
                        metadata = layer.metadata_dict or {}
                        if not isinstance(metadata, dict):
                            metadata = {}
                        metadata["cog_key"] = cog_key

                        # Update the database
                        await conn.execute(
                            """
                            UPDATE map_layers
                            SET metadata = $1
                            WHERE layer_id = $2
                            """,
                            json.dumps(metadata),
                            layer.layer_id,
                        )

                        # Invalidate cached tiles — the COG has changed
                        await tile_cache.invalidate_layer(layer.layer_id)
            finally:
                try:
                    lock.release()
                except Exception:
                    logger.debug("Failed to release COG generation lock", exc_info=True)

        # Ensure cog_key is available if it was just generated
        if not cog_key:
            _meta = layer.metadata_dict or {}
            cog_key = _meta.get("cog_key") if isinstance(_meta, dict) else None
            if not cog_key:
                # This case should ideally not be reached if generation logic is sound
                raise HTTPException(
                    status_code=500, detail="COG key missing after generation attempt."
                )

        # Get the file size first to handle range requests
        s3_head = await s3_op(s3_client.head_object(Bucket=bucket_name, Key=cog_key),
                             "head_object", f"COG {cog_key}")
        file_size = s3_head["ContentLength"]

        # Check for Range header to support byte serving
        range_header = request.headers.get("range", None) if request else None
        start_byte = 0
        end_byte = file_size - 1

        # Parse the Range header if present
        if range_header:
            range_match = re.search(r"bytes=(\d+)-(\d*)", range_header)
            if range_match:
                start_byte = int(range_match.group(1))
                end_group = range_match.group(2)
                if end_group:
                    end_byte = min(int(end_group), file_size - 1)
                else:
                    end_byte = file_size - 1

            # Calculate content length for the range
            content_length = end_byte - start_byte + 1

            # Get the specified range from S3
            s3_response = await s3_op(
                s3_client.get_object(Bucket=bucket_name, Key=cog_key, Range=f"bytes={start_byte}-{end_byte}"),
                "get_object (range)", f"COG {cog_key}",
            )

            # Set response status and headers for partial content
            status_code = 206  # Partial Content
            headers = {
                "Content-Range": f"bytes {start_byte}-{end_byte}/{file_size}",
                "Accept-Ranges": "bytes",
                "Content-Length": str(content_length),
                "Content-Type": "image/tiff",
                "Access-Control-Allow-Origin": "*",
                "Access-Control-Allow-Methods": "GET, OPTIONS",
                "Access-Control-Allow-Headers": "Range, Content-Type",
            }
        else:
            # Get the entire file
            s3_response = await s3_op(s3_client.get_object(Bucket=bucket_name, Key=cog_key),
                                      "get_object", f"COG {cog_key}")
            status_code = 200
            headers = {
                "Content-Length": str(file_size),
                "Content-Type": "image/tiff",
                "Accept-Ranges": "bytes",
                "Access-Control-Allow-Origin": "*",
                "Access-Control-Allow-Methods": "GET, OPTIONS",
                "Access-Control-Allow-Headers": "Range, Content-Type",
            }

        # Create an async generator to stream the file
        async def stream_s3_file():
            # Get the body of the S3 object (this is a stream)
            body = s3_response["Body"]

            # Stream the content in chunks
            chunk_size = 8192  # 8KB chunks
            while True:
                chunk = await body.read(chunk_size)
                if not chunk:
                    break
                yield chunk

            # Close the body
            body.close()

        # Return a streaming response with the appropriate status and headers
        return StreamingResponse(
            stream_s3_file(), status_code=status_code, headers=headers
        )


async def _generate_cog_subprocess(
    local_input_file: str,
    local_cog_file: str,
    temp_dir: str,
    layer: MapLayer,
) -> None:
    """Legacy COG generation via GDAL subprocess calls.

    This is the fallback path when the Dask pipeline is unavailable or fails.
    Kept as a standalone function so it can be tested independently.
    """

    async def _run_cmd(cmd: list[str], timeout_seconds: int = 30) -> str:
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            stdout_bytes, stderr_bytes = await asyncio.wait_for(
                proc.communicate(), timeout=timeout_seconds
            )
        except asyncio.TimeoutError:
            try:
                proc.kill()
            except Exception:
                logger.debug("Failed to kill timed-out process", exc_info=True)
            raise HTTPException(
                status_code=status.HTTP_504_GATEWAY_TIMEOUT,
                detail=f"Command timed out after {timeout_seconds}s: {' '.join(cmd)}",
            )
        if (proc.returncode or 0) != 0:
            stderr_text = (stderr_bytes or b"").decode("utf-8", "ignore")
            raise subprocess.CalledProcessError(
                returncode=int(proc.returncode or 1),
                cmd=cmd,
                output=stdout_bytes,
                stderr=stderr_text,
            )
        return (stdout_bytes or b"").decode("utf-8", "ignore")

    # Get raster info
    gdalinfo_cmd = ["gdalinfo", "-json", local_input_file]
    try:
        gdalinfo_out = await _run_cmd(gdalinfo_cmd, timeout_seconds=30)
        gdalinfo_json = json.loads(gdalinfo_out)
    except (subprocess.CalledProcessError, json.JSONDecodeError):
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to process raster info for layer {layer.layer_id}.",
        )

    input_file_for_cog = local_input_file
    num_bands = len(gdalinfo_json.get("bands", []))
    needs_color_ramp_suffix = False

    if num_bands == 1:
        try:
            local_rgb_file = os.path.join(
                temp_dir, f"layer_{layer.layer_id}_rgb.tif"
            )
            rgb_cmd = [
                "gdal_translate", "-of", "GTiff",
                "-expand", "rgb",
                local_input_file, local_rgb_file,
            ]
            await _run_cmd(rgb_cmd)
            input_file_for_cog = local_rgb_file
        except subprocess.CalledProcessError:
            meta = layer.metadata_dict or {}
            if isinstance(meta, dict) and "raster_value_stats_b1" in meta:
                needs_color_ramp_suffix = True

    # Build gdalwarp command for COG creation
    warp_cmd_base = [
        "gdalwarp", "-t_srs", "EPSG:3857",
        "-r", "bilinear", "-of", "COG",
        "-co", "BLOCKSIZE=256",
    ]
    if needs_color_ramp_suffix:
        warp_cmd_base.extend(["-ot", "Float32"])
        warp_compress = ["-co", "COMPRESS=LZW"]
    else:
        warp_compress = ["-co", "COMPRESS=JPEG", "-co", "QUALITY=85"]

    warp_cmd = (
        warp_cmd_base + warp_compress
        + ["-co", "OVERVIEWS=AUTO", input_file_for_cog, local_cog_file]
    )

    try:
        await _run_cmd(warp_cmd)
    except subprocess.CalledProcessError:
        raise HTTPException(status_code=500, detail="COG generation failed")


@layer_router.get(
    "/layer/{layer_id}.pmtiles",
    operation_id="view_layer_as_pmtiles",
)
async def get_layer_pmtiles(
    request: Request,
    layer: MapLayer = Depends(get_layer),
):
    # Check if layer is a vector type
    if layer.type != "vector":
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Layer is not a vector type. PMTiles can only be generated from vector data.",
        )

    if layer.remote_url and layer.remote_url.endswith(".pmtiles"):
        return RedirectResponse(url=layer.remote_url, status_code=302)

    # Set up S3 client and bucket
    bucket_name = get_bucket_name()

    # Check if metadata has pmtiles_key
    pmtiles_key = layer.metadata_dict.get("pmtiles_key")

    # Lazy PMTiles generation: if upload-time generation failed or was
    # skipped, generate now (with a distributed lock to prevent stampede).
    if not pmtiles_key:
        lock_key = f"lock:pmtiles:{layer.layer_id}"
        lock = redis.lock(lock_key, timeout=600, blocking_timeout=30)
        loop = asyncio.get_running_loop()
        acquired = await loop.run_in_executor(None, lambda: lock.acquire(blocking=True))
        if not acquired:
            raise HTTPException(
                status_code=status.HTTP_423_LOCKED,
                detail="Vector tiles are generating. Please refresh in a moment.",
            )
        try:
            # Re-check after acquiring lock (another request may have finished)
            async with get_async_db_connection() as conn:
                row = await conn.fetchrow(
                    "SELECT metadata FROM map_layers WHERE layer_id = $1",
                    layer.layer_id,
                )
                if (
                    row
                    and isinstance(row["metadata"], dict)
                    and row["metadata"].get("pmtiles_key")
                ):
                    pmtiles_key = row["metadata"]["pmtiles_key"]

            if not pmtiles_key:
                # Download the source file and generate PMTiles
                from src.upload.pmtiles import generate_pmtiles_from_ogr_source

                with tempfile.TemporaryDirectory() as temp_dir:
                    local_file = os.path.join(temp_dir, "source")
                    s3_dl = await get_async_s3_client()
                    await s3_op(s3_dl.download_file(bucket_name, layer.s3_key, local_file),
                                "download", f"PMTiles source {layer.layer_id}")

                    feature_count = layer.feature_count or 1
                    pmtiles_key = await generate_pmtiles_from_ogr_source(
                        layer.layer_id,
                        local_file,
                        feature_count,
                        str(layer.owner_uuid),
                    )
                    logger.info(
                        "Lazy PMTiles generated for layer %s -> %s",
                        layer.layer_id,
                        pmtiles_key,
                    )
        except HTTPException:
            raise
        except Exception as e:
            logger.warning(
                "Lazy PMTiles generation failed for %s: %s", layer.layer_id, e
            )
            raise HTTPException(
                status_code=status.HTTP_423_LOCKED,
                detail="Vector tile generation failed. Please try again.",
            )
        finally:
            try:
                lock.release()
            except Exception:
                logger.debug("Failed to release PMTiles generation lock", exc_info=True)

    if not pmtiles_key:
        raise HTTPException(
            status_code=status.HTTP_423_LOCKED,
            detail="Vector tiles are still generating. Please refresh in a moment.",
        )

    # Get the file size first to handle range requests using async S3
    s3 = await get_async_s3_client()
    s3_head = await s3_op(s3.head_object(Bucket=bucket_name, Key=pmtiles_key),
                         "head_object", f"PMTiles {pmtiles_key}")
    file_size = s3_head["ContentLength"]

    # Check for Range header to support byte serving
    range_header = request.headers.get("range", None) if request else None
    start_byte = 0
    end_byte = file_size - 1

    # Parse the Range header if present
    if range_header:
        range_match = re.search(r"bytes=(\d+)-(\d*)", range_header)
        if range_match:
            start_byte = int(range_match.group(1))
            end_group = range_match.group(2)
            if end_group:
                end_byte = min(int(end_group), file_size - 1)
            else:
                end_byte = file_size - 1

        # Calculate content length for the range
        content_length = end_byte - start_byte + 1

    # Create streaming function that handles S3 connection properly
    async def stream_s3_file():
        s3 = await get_async_s3_client()
        if range_header:
            # Get range from S3
            s3_response = await s3_op(
                s3.get_object(Bucket=bucket_name, Key=pmtiles_key, Range=f"bytes={start_byte}-{end_byte}"),
                "get_object (range)", f"PMTiles {pmtiles_key}",
            )
        else:
            # Get entire file from S3
            s3_response = await s3_op(s3.get_object(Bucket=bucket_name, Key=pmtiles_key),
                                      "get_object", f"PMTiles {pmtiles_key}")

        # Read all content and yield in chunks
        body = s3_response["Body"]
        chunk_size = 8192
        while True:
            chunk = await body.read(chunk_size)
            if not chunk:
                break
            yield chunk

    # Set headers based on range request
    if range_header:
        status_code = 206  # Partial Content
        headers = {
            "Content-Range": f"bytes {start_byte}-{end_byte}/{file_size}",
            "Accept-Ranges": "bytes",
            "Content-Length": str(content_length),
            "Content-Type": "application/octet-stream",
        }
    else:
        status_code = 200
        headers = {
            "Accept-Ranges": "bytes",
            "Content-Length": str(file_size),
            "Content-Type": "application/octet-stream",
        }

    # Return a streaming response with the appropriate status and headers
    return StreamingResponse(stream_s3_file(), status_code=status_code, headers=headers)


@layer_router.get(
    "/layer/{layer_id}.laz",
    operation_id="view_layer_as_laz",
)
async def get_layer_laz(
    request: Request,
    layer: MapLayer = Depends(get_layer),
):
    if layer.type != "point_cloud":
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Layer is not a point cloud type",
        )

    # Set up S3 client and bucket
    bucket_name = get_bucket_name()

    # Check if layer has s3_key
    s3_key = layer.s3_key

    # If S3 key doesn't exist, return error
    if not s3_key:
        raise HTTPException(
            status_code=status.HTTP_501_NOT_IMPLEMENTED,
            detail="LAZ file for this layer has not been generated yet",
        )

    # Get the file size first to handle range requests using async S3
    s3 = await get_async_s3_client()
    s3_head = await s3_op(s3.head_object(Bucket=bucket_name, Key=s3_key),
                         "head_object", f"LAZ {s3_key}")
    file_size = s3_head["ContentLength"]

    # Check for Range header to support byte serving
    range_header = request.headers.get("range", None) if request else None
    start_byte = 0
    end_byte = file_size - 1

    # Parse the Range header if present
    if range_header:
        range_match = re.search(r"bytes=(\d+)-(\d*)", range_header)
        if range_match:
            start_byte = int(range_match.group(1))
            end_group = range_match.group(2)
            if end_group:
                end_byte = min(int(end_group), file_size - 1)
            else:
                end_byte = file_size - 1

        # Calculate content length for the range
        content_length = end_byte - start_byte + 1

    # Create streaming function that handles S3 connection properly
    async def stream_s3_file():
        s3 = await get_async_s3_client()
        if range_header:
            # Get range from S3
            s3_response = await s3_op(
                s3.get_object(Bucket=bucket_name, Key=s3_key, Range=f"bytes={start_byte}-{end_byte}"),
                "get_object (range)", f"LAZ {s3_key}",
            )
        else:
            # Get entire file from S3
            s3_response = await s3_op(s3.get_object(Bucket=bucket_name, Key=s3_key),
                                      "get_object", f"LAZ {s3_key}")

        # Read all content and yield in chunks
        body = s3_response["Body"]
        chunk_size = 8192
        while True:
            chunk = await body.read(chunk_size)
            if not chunk:
                break
            yield chunk

    # Set headers based on range request
    if range_header:
        status_code = 206  # Partial Content
        headers = {
            "Content-Range": f"bytes {start_byte}-{end_byte}/{file_size}",
            "Accept-Ranges": "bytes",
            "Content-Length": str(content_length),
            "Content-Type": "application/octet-stream",
        }
    else:
        status_code = 200
        headers = {
            "Accept-Ranges": "bytes",
            "Content-Length": str(file_size),
            "Content-Type": "application/octet-stream",
        }

    # Return a streaming response with the appropriate status and headers
    return StreamingResponse(stream_s3_file(), status_code=status_code, headers=headers)


@layer_router.get(
    "/layer/{layer_id}/{z}/{x}/{y}.png",
    operation_id="get_raster_xyz_tile",
)
async def get_raster_xyz_tile(
    z: int,
    x: int,
    y: int,
    request: Request,
    layer: MapLayer = Depends(get_layer),
):
    if layer.type != "raster":
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Layer is not a raster type",
        )

    if z < 0 or z > 22 or x < 0 or y < 0 or x >= (1 << z) or y >= (1 << z):
        raise HTTPException(status_code=400, detail="Invalid tile coordinates")

    # ETag from layer last_edited timestamp — enables browser 304 responses
    _etag = None
    if layer.last_edited:
        _etag = f'"{layer.layer_id}-{int(layer.last_edited.timestamp())}"'
        if_none_match = request.headers.get("if-none-match")
        if if_none_match and if_none_match == _etag:
            return Response(status_code=304)

    _tile_headers = {
        "Cache-Control": "public, max-age=300",
        "Access-Control-Allow-Origin": "*",
    }
    if _etag:
        _tile_headers["ETag"] = _etag

    # --- Server-side cache: Redis -----------------------------------------
    cached = await tile_cache.get(layer.layer_id, z, x, y)
    if cached is not None:
        return Response(
            content=cached,
            media_type="image/png",
            headers=_tile_headers,
        )

    # --- Cache miss: render from COG -------------------------------------
    # prefer COG key from metadata when present; fall back to original s3_key
    metadata = layer.metadata_dict or {}
    cog_key = metadata.get("cog_key")

    if not cog_key:
        # No COG yet — return a transparent tile immediately instead of
        # trying to read the raw raster (which times out on Render's 30s proxy).
        # The frontend can retry later once background COG generation completes.
        buf = io.BytesIO()
        Image.new("RGBA", (256, 256), (0, 0, 0, 0)).save(buf, format="PNG")
        return Response(
            content=buf.getvalue(),
            media_type="image/png",
            headers={**_tile_headers, "X-COG-Status": "pending", "Cache-Control": "no-cache"},
        )

    s3_key = cog_key

    bucket = get_bucket_name()
    s3 = await get_async_s3_client(signature_version="s3v4")
    asset_url = await s3_op(
        s3.generate_presigned_url("get_object", Params={"Bucket": bucket, "Key": s3_key}, ExpiresIn=180),
        "presigned URL", f"raster tile {s3_key}",
    )

    try:
        with Reader(asset_url) as src:
            img = src.tile(x, y, z)

            if "raster_value_stats_b1" in metadata:
                min_val = metadata["raster_value_stats_b1"]["min"]
                max_val = metadata["raster_value_stats_b1"]["max"]

                img.rescale(in_range=((min_val, max_val),), out_range=((0, 255),))

                cm = cmap.get("spectral_r")
                content = img.render(img_format="PNG", colormap=cm)
            else:
                # png has alpha support; expect newer rio-tiler which returns bytes
                content = img.render(img_format="PNG")

        # Store in Redis for subsequent requests
        await tile_cache.put(layer.layer_id, z, x, y, content)

        return Response(
            content=content,
            media_type="image/png",
            headers=_tile_headers,
        )
    except TileOutsideBounds:
        # Expected: tile coords outside raster extent — transparent tile, cached
        buf = io.BytesIO()
        Image.new("RGBA", (256, 256), (0, 0, 0, 0)).save(buf, format="PNG")
        empty_png = buf.getvalue()
        await tile_cache.put(layer.layer_id, z, x, y, empty_png)
        return Response(
            content=empty_png,
            media_type="image/png",
            headers=_tile_headers,
        )
    except Exception as e:
        # Real render error — log with full context, return transparent tile
        # but do NOT cache (transient errors should be retried)
        error_class = type(e).__name__
        logger.error(
            "Raster tile render failed for layer=%s z=%d x=%d y=%d: %s: %s",
            layer.layer_id, z, x, y, error_class, str(e),
            exc_info=True
        )
        buf = io.BytesIO()
        Image.new("RGBA", (256, 256), (0, 0, 0, 0)).save(buf, format="PNG")
        return Response(
            content=buf.getvalue(),
            media_type="image/png",
            headers=_tile_headers,
        )


@layer_router.get(
    "/layer/{layer_id}/{z}/{x}/{y}.mvt",
    operation_id="get_layer_mvt_tile",
)
async def get_layer_mvt_tile(
    z: int,
    x: int,
    y: int,
    request: Request,
    layer: MapLayer = Depends(get_layer),
):
    # Validate tile coordinates
    if z < 0 or z > 18 or x < 0 or y < 0 or x >= (1 << z) or y >= (1 << z):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid tile coordinates"
        )

    # ETag from layer last_edited timestamp — enables browser 304 responses
    _mvt_etag = None
    if layer.last_edited:
        _mvt_etag = f'"{layer.layer_id}-mvt-{int(layer.last_edited.timestamp())}"'
        if_none_match = request.headers.get("if-none-match")
        if if_none_match and if_none_match == _mvt_etag:
            return Response(status_code=304)

    # --- Server-side cache: Redis (MVT) ------------------------------------
    _mvt_headers_base = {
        "Access-Control-Allow-Origin": "*",
        "Cache-Control": "public, max-age=300",
        "Vary": "Accept-Encoding",
    }
    if _mvt_etag:
        _mvt_headers_base["ETag"] = _mvt_etag

    cached_mvt = await tile_cache.get(layer.layer_id, z, x, y, fmt="mvt")
    if cached_mvt is not None:
        accept_encoding = request.headers.get("accept-encoding", "").lower()
        if "gzip" in accept_encoding and len(cached_mvt) > 0:
            compressed = gzip.compress(cached_mvt, compresslevel=6)
            return Response(
                content=compressed,
                media_type="application/vnd.mapbox-vector-tile",
                headers={**_mvt_headers_base, "Content-Encoding": "gzip"},
            )
        return Response(
            content=cached_mvt,
            media_type="application/vnd.mapbox-vector-tile",
            headers=_mvt_headers_base,
        )

    async with async_conn("mvt") as conn:
        # Get PostGIS connection details (authorization handled by get_layer)
        connection_details = await conn.fetchrow(
            """
            SELECT connection_uri
            FROM project_postgres_connections
            WHERE id = $1 AND soft_deleted_at IS NULL
            """,
            layer.postgis_connection_id,
        )

        if not connection_details:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="PostGIS connection not found",
            )

    # ST_TileEnvelope requires PostGIS 3.0.0 which was 2019... so
    try:
        # some geometries just aren't valid, so make them valid.
        async with get_pooled_connection(
            connection_details["connection_uri"]
        ) as postgis_conn:
            # race between the tile fetch and client disconnect detection
            # note that proxies sometimes swallow these disconnection events
            async def watch_disconnect():
                while True:
                    message = await request.receive()
                    if message["type"] == "http.disconnect":
                        return "disconnect"

            fetchval_task = asyncio.create_task(
                fetch_mvt_tile(layer, postgis_conn, z, x, y)
            )
            disconnect_task = asyncio.create_task(watch_disconnect())

            done, pending = await asyncio.wait(
                [fetchval_task, disconnect_task], return_when=asyncio.FIRST_COMPLETED
            )

            # cancel the old query if it's still running
            for task in pending:
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass

            completed_task = done.pop()
            if completed_task == disconnect_task:
                return Response(
                    content=b"", media_type="application/vnd.mapbox-vector-tile"
                )
            else:
                mvt_data = completed_task.result()

        if mvt_data is None:
            mvt_data = b""

        # Store raw (uncompressed) MVT in Redis for subsequent requests
        if mvt_data:
            await tile_cache.put(layer.layer_id, z, x, y, mvt_data, fmt="mvt")

        # Check if client accepts gzip encoding and if there's data to compress
        accept_encoding = request.headers.get("accept-encoding", "").lower()
        should_compress = "gzip" in accept_encoding and len(mvt_data) > 0

        if should_compress:
            compressed_data = gzip.compress(mvt_data, compresslevel=6)
            return Response(
                content=compressed_data,
                media_type="application/vnd.mapbox-vector-tile",
                headers={**_mvt_headers_base, "Content-Encoding": "gzip"},
            )
        else:
            return Response(
                content=mvt_data,
                media_type="application/vnd.mapbox-vector-tile",
                headers=_mvt_headers_base,
            )

    except asyncpg.exceptions.InternalServerError as e:
        # Re-raise any other internal server errors that aren't handled by the fallback
        raise e


@layer_router.get(
    "/layer/{layer_id}.geojson",
    operation_id="view_layer_as_geojson",
)
async def get_layer_geojson(
    layer: MapLayer = Depends(get_layer),
):
    # ─── PostGIS layers: execute query and return GeoJSON directly ────────
    if layer.type == "postgis" and layer.postgis_connection_id and layer.postgis_query:
        async with async_conn("geojson") as conn:
            connection_details = await conn.fetchrow(
                """
                SELECT connection_uri
                FROM project_postgres_connections
                WHERE id = $1 AND soft_deleted_at IS NULL
                """,
                layer.postgis_connection_id,
            )
            if not connection_details:
                raise HTTPException(
                    status_code=status.HTTP_404_NOT_FOUND,
                    detail="PostGIS connection not found",
                )

        async with get_pooled_connection(
            connection_details["connection_uri"]
        ) as postgis_conn:
            rows = await postgis_conn.fetch(
                f"""
                SELECT jsonb_build_object(
                    'type', 'FeatureCollection',
                    'features', COALESCE(jsonb_agg(
                        jsonb_build_object(
                            'type', 'Feature',
                            'geometry', ST_AsGeoJSON(ST_Transform(sub.geom, 4326))::jsonb,
                            'properties', to_jsonb(sub) - 'geom'
                        )
                    ), '[]'::jsonb)
                ) AS fc
                FROM ({layer.postgis_query}) AS sub
                """
            )
            geojson_content = rows[0]["fc"] if rows else '{"type":"FeatureCollection","features":[]}'

        # Return as string if it's a dict (jsonb comes back as dict)
        import json as _json
        content = _json.dumps(geojson_content) if isinstance(geojson_content, dict) else str(geojson_content)
        return Response(
            content=content,
            media_type="application/geo+json",
            headers={
                "Access-Control-Allow-Origin": "*",
                "Cache-Control": "public, max-age=300",
            },
        )

    # Check if layer is a vector type
    if layer.type != "vector":
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Layer is not a vector type. GeoJSON format is only available for vector and PostGIS data.",
        )

    # Get unified OGR source (works for S3 and remote URLs)
    async with await layer.get_ogr_source() as ogr_source:
        with tempfile.TemporaryDirectory() as temp_dir:
            # Convert to GeoJSON using ogr2ogr with unified source
            local_geojson_file = os.path.join(
                temp_dir, f"layer_{layer.layer_id}.geojson"
            )
            ogr_cmd = [
                "ogr2ogr",
                "-f",
                "GeoJSON",
                "-t_srs",
                "EPSG:4326",  # Ensure coordinates are in WGS84
                "-lco",
                "COORDINATE_PRECISION=6",  # ~1m precision at equator
                "-skipfailures",  # Skip features with NULL geometries or other issues
                local_geojson_file,
                ogr_source,
            ]

            process = await asyncio.create_subprocess_exec(*ogr_cmd)
            await process.wait()
            if process.returncode != 0:
                raise HTTPException(
                    status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                    detail="Failed to convert layer to GeoJSON format",
                )

            # Read the GeoJSON file and return it
            with open(local_geojson_file, "r") as f:
                geojson_content = f.read()

        # Return the GeoJSON with appropriate headers and cache control
        return Response(
            content=geojson_content,
            media_type="application/geo+json",
            headers={
                "Content-Disposition": f'attachment; filename="{layer.name}.geojson"',
                "Access-Control-Allow-Origin": "*",
                "Cache-Control": "public, max-age=86400",  # Cache for 24 hours
            },
        )


class ColumnStatsResponse(BaseModel):
    column: str
    method: str
    k: int
    breaks: list[float]
    min: float
    max: float


@layer_router.get(
    "/layer/{layer_id}/column-stats",
    operation_id="get_layer_column_stats",
    response_model=ColumnStatsResponse,
    summary="Compute classification breaks for a numeric column",
)
async def get_layer_column_stats(
    layer: MapLayer = Depends(get_layer),
    column: str = None,
    k: int = 5,
    method: str = "quantile",
):
    """Return quantile or equal-interval classification breaks for a numeric column.

    Supports PostGIS and vector (FlatGeoBuf/GeoJSON) layers.
    ``k`` must be between 2 and 20 (inclusive).
    ``method`` must be one of ``quantile`` or ``equal_interval``.
    """
    if column is None:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="Query parameter 'column' is required",
        )
    if not (2 <= k <= 20):
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="'k' must be between 2 and 20",
        )
    if method not in ("quantile", "equal_interval"):
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="'method' must be 'quantile' or 'equal_interval'",
        )

    # ── helpers ────────────────────────────────────────────────────────────
    def _quantile_breaks(values: list[float], k: int) -> list[float]:
        """Return k+1 quantile break values (min … max) from a sorted list."""
        n = len(values)
        if n == 0:
            return []
        s = sorted(values)
        breaks: list[float] = [s[0]]
        for i in range(1, k):
            idx = (i * n) / k
            lo, hi = int(idx), min(int(idx) + 1, n - 1)
            frac = idx - lo
            breaks.append(s[lo] * (1 - frac) + s[hi] * frac)
        breaks.append(s[-1])
        return breaks

    def _equal_interval_breaks(values: list[float], k: int) -> list[float]:
        mn, mx = min(values), max(values)
        step = (mx - mn) / k
        return [mn + step * i for i in range(k + 1)]

    # ── PostGIS path ───────────────────────────────────────────────────────
    if layer.type == "postgis" and layer.postgis_connection_id and layer.postgis_query:
        async with async_conn("geojson") as conn:
            connection_details = await conn.fetchrow(
                """
                SELECT connection_uri
                FROM project_postgres_connections
                WHERE id = $1 AND soft_deleted_at IS NULL
                """,
                layer.postgis_connection_id,
            )
            if not connection_details:
                raise HTTPException(
                    status_code=status.HTTP_404_NOT_FOUND,
                    detail="PostGIS connection not found",
                )

        # Validate column name to avoid SQL injection (only allow safe identifiers)
        if not re.match(r'^[A-Za-z_][A-Za-z0-9_ ]*$', column):
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail="Invalid column name",
            )

        async with get_pooled_connection(
            connection_details["connection_uri"]
        ) as postgis_conn:
            if method == "quantile":
                rows = await postgis_conn.fetch(
                    f"""
                    SELECT "{column}"::float AS v
                    FROM ({layer.postgis_query}) AS sub
                    WHERE "{column}" IS NOT NULL
                    ORDER BY v
                    """
                )
                values = [r["v"] for r in rows]
                if not values:
                    raise HTTPException(
                        status_code=status.HTTP_400_BAD_REQUEST,
                        detail=f"Column '{column}' has no non-null numeric values",
                    )
                breaks = _quantile_breaks(values, k)
            else:  # equal_interval — computed in SQL for efficiency
                row = await postgis_conn.fetchrow(
                    f"""
                    SELECT MIN("{column}"::float) AS mn, MAX("{column}"::float) AS mx
                    FROM ({layer.postgis_query}) AS sub
                    WHERE "{column}" IS NOT NULL
                    """
                )
                if row is None or row["mn"] is None:
                    raise HTTPException(
                        status_code=status.HTTP_400_BAD_REQUEST,
                        detail=f"Column '{column}' has no non-null numeric values",
                    )
                mn, mx = float(row["mn"]), float(row["mx"])
                step = (mx - mn) / k
                breaks = [mn + step * i for i in range(k + 1)]
                values = [mn, mx]  # just for min/max below

        return ColumnStatsResponse(
            column=column,
            method=method,
            k=k,
            breaks=[round(b, 6) for b in breaks],
            min=round(min(values), 6),
            max=round(max(values), 6),
        )

    # ── Vector (FlatGeoBuf / GeoJSON) path ────────────────────────────────
    if layer.type != "vector":
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="column-stats is only supported for vector and PostGIS layers",
        )

    async with await layer.get_ogr_source() as ogr_source:
        with tempfile.TemporaryDirectory() as temp_dir:
            local_geojson = os.path.join(temp_dir, "layer.geojson")
            ogr_cmd = [
                "ogr2ogr",
                "-f", "GeoJSON",
                "-select", column,  # only the column we need
                "-t_srs", "EPSG:4326",
                "-skipfailures",
                local_geojson,
                ogr_source,
            ]
            proc = await asyncio.create_subprocess_exec(*ogr_cmd)
            await proc.wait()
            if proc.returncode != 0:
                raise HTTPException(
                    status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                    detail="Failed to read vector layer for column stats",
                )

            with open(local_geojson) as f:
                fc = json.load(f)

    raw_values = []
    for feat in fc.get("features", []):
        v = feat.get("properties", {}).get(column)
        if v is not None:
            try:
                raw_values.append(float(v))
            except (TypeError, ValueError):
                pass

    if not raw_values:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Column '{column}' has no non-null numeric values",
        )

    if method == "quantile":
        breaks = _quantile_breaks(raw_values, k)
    else:
        breaks = _equal_interval_breaks(raw_values, k)

    return ColumnStatsResponse(
        column=column,
        method=method,
        k=k,
        breaks=[round(b, 6) for b in breaks],
        min=round(min(raw_values), 6),
        max=round(max(raw_values), 6),
    )


# Re-export from service layer so existing callers continue to work.
# New code should import from src.services.layer_service directly.
from src.services.layer_service import describe_layer_internal  # noqa: F811


@layer_router.get(
    "/layer/{layer_id}/describe",
    operation_id="describe_layer",
)
async def describe_layer(
    layer_id: str,
    request: Request,
    session: UserContext = Depends(verify_session_required),
    layer_describer: LayerDescriber = Depends(get_layer_describer),
):
    markdown_response = await describe_layer_internal(
        layer_id, layer_describer, session.get_user_id()
    )

    return Response(
        content=markdown_response,
        media_type="text/plain",
    )


class SetStyleRequest(BaseModel):
    maplibre_json_layers: list = Field(
        description="Array of MapLibre layer objects like fill, line, symbol [(style spec v8)](https://maplibre.org/maplibre-style-spec/)"
    )
    map_id: str = Field(description="Map ID where this new style will be applied")


class SetStyleResponse(BaseModel):
    style_id: str = Field(description="ID of the created style")
    layer_id: str = Field(description="ID of the layer the style was applied to")


@layer_router.post(
    "/layers/{layer_id}/style",
    operation_id="set_layer_style",
    summary="Set layer style",
    response_model=SetStyleResponse,
)
async def set_layer_style(
    request: SetStyleRequest,
    layer: MapLayer = Depends(get_layer),
    user_id: str = Depends(session_user_id),
) -> SetStyleResponse:
    """Sets a layer's active style in the map to a MapLibre JSON layer list.

    This operation will fail if the style is invalid according to the
    [style spec](https://maplibre.org/maplibre-style-spec/layers/) and the source
    definition.

    Returns the created style_id and confirmation that it has been applied.
    """
    layer_id = layer.layer_id

    layers = request.maplibre_json_layers
    if not isinstance(layers, list):
        raise HTTPException(
            status_code=400,
            detail="Expected maplibre_json_layers to be an array of layer objects",
        )

    for layer_obj in layers:
        if not isinstance(layer_obj, dict):
            raise HTTPException(
                status_code=400,
                detail="Expected layer object to be a dict",
            )

        # will be removed later if not needed
        layer_obj["source-layer"] = MVT_LAYER_NAME
        # don't cross-get sources
        if layer_obj.get("source") != layer_id:
            raise HTTPException(
                status_code=400,
                detail=f"Layer source must be '{layer_id}'",
            )

    from src.symbology.verify import (
        StyleValidationError,
        verify_style_json_str,
    )

    try:
        await verify_style_json_str(
            json.dumps(layers),
            get_base_map_provider(),
            layer,
        )
    except StyleValidationError as e:
        raise HTTPException(
            status_code=400, detail=f"Style validation failed: {str(e)}"
        )

    style_id = generate_id(prefix="S")

    async with get_async_db_connection() as conn:
        await conn.execute(
            """
            INSERT INTO layer_styles
            (style_id, layer_id, style_json, created_by)
            VALUES ($1, $2, $3, $4)
            """,
            style_id,
            layer_id,
            json.dumps(layers),
            user_id,
        )

        await conn.execute(
            """
            INSERT INTO map_layer_styles (map_id, layer_id, style_id)
            VALUES ($1, $2, $3)
            ON CONFLICT (map_id, layer_id)
            DO UPDATE SET style_id = $3
            """,
            request.map_id,
            layer_id,
            style_id,
        )

    return SetStyleResponse(
        style_id=style_id,
        layer_id=layer_id,
    )


class LayerUpdateRequest(BaseModel):
    name: str = Field(description="New name for the layer")


class LayerUpdateResponse(BaseModel):
    layer_id: str = Field(description="ID of the updated layer")
    name: str = Field(description="New name of the layer")


@layer_router.patch(
    "/layer/{layer_id}",
    operation_id="update_layer",
    summary="Update layer",
    response_model=LayerUpdateResponse,
)
async def update_layer(
    update_data: LayerUpdateRequest,
    layer: MapLayer = Depends(get_layer),
    user_id: str = Depends(session_user_id),
) -> LayerUpdateResponse:
    """Updates properties of an existing layer. Currently supports updating
    the layer's display name.

    ```py
    result = httpx.patch(
        "https://app.mundi.ai/api/layer/L4b2c3d4e5f6",
        json={"name": "New name in layer list"},
        headers={"Authorization": f"Bearer {os.environ['MUNDI_API_KEY']}"}
    ).json()

    assert result == {
        "layer_id": "L4b2c3d4e5f6",
        "name": "New name in layer list",
        "message": "Layer updated successfully"
    }
    ```"""
    if user_id != str(layer.owner_uuid):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Only the layer owner can update this layer",
        )

    async with get_async_db_connection() as conn:
        await conn.execute(
            """
            UPDATE map_layers SET name = $1, last_edited = CURRENT_TIMESTAMP
            WHERE layer_id = $2
            """,
            update_data.name,
            layer.layer_id,
        )

    return LayerUpdateResponse(
        layer_id=layer.layer_id,
        name=update_data.name,
    )
