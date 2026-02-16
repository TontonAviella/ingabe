"""Raster upload handler — GeoTIFF, JPEG, PNG, DEM.

Uses the Dask-based pipeline (``dask_raster.RasterPipeline``) for metadata
extraction and eager COG generation. Falls back to the legacy synchronous
GDAL path (``preprocess_raster``) if Dask/rioxarray are not installed.

COG generation happens at upload time by default so that the first tile
request is served from the pre-built COG without delay.  Set
``RASTER_EAGER_COG=false`` to revert to lazy generation on the first
``.cog.tif`` request.
"""

import asyncio
import json
import logging
import os
import tempfile

from src.upload.base import BaseUploadHandler, HandlerResult, UploadContext
from src.upload.dask_raster import DASK_AVAILABLE, RasterPipeline
from src.upload.preprocessing import preprocess_raster

logger = logging.getLogger(__name__)

# COG is generated at upload time by default.  Set RASTER_EAGER_COG=false to
# revert to lazy generation on the first .cog.tif request.
_EAGER_COG = os.environ.get("RASTER_EAGER_COG", "true").lower() not in ("false", "0", "no")

# Dagster integration (Phase 3): Set USE_DAGSTER=true to delegate processing
# to Dagster pipelines instead of inline processing during upload.
# When enabled, uploads complete faster and processing happens asynchronously.
_USE_DAGSTER = os.environ.get("USE_DAGSTER", "false").lower() in ("true", "1", "yes")


class RasterUploadHandler(BaseUploadHandler):
    """Handles raster file uploads (GeoTIFF, JPEG, PNG, DEM).

    Preprocessing extracts bounds, CRS, and band statistics.
    Uses Dask pipeline when available, GDAL fallback otherwise.
    No file conversion is performed — the original file is uploaded to S3.
    """

    async def preprocess(self, ctx: UploadContext) -> HandlerResult:
        # No conversion needed for rasters — upload the original file.
        # Bounds and metadata extraction happens in create_layers
        # (after S3 upload) since it's a read-only operation.
        return HandlerResult(layer_type="raster")

    async def create_layers(
        self, ctx: UploadContext, result: HandlerResult
    ) -> HandlerResult:
        """Extract raster metadata and insert a single layer row.

        Tries the Dask pipeline first for metadata extraction.
        Falls back to legacy ``preprocess_raster`` if unavailable.
        """
        bounds = await self._extract_metadata(ctx)
        result.bounds = bounds

        # COG generation at upload time (on by default, disable with RASTER_EAGER_COG=false)
        # If USE_DAGSTER is enabled, skip inline COG generation and let Dagster handle it
        cog_key = None
        if not _USE_DAGSTER and _EAGER_COG and DASK_AVAILABLE:
            cog_key = await self._eager_cog(ctx)
        elif _USE_DAGSTER:
            logger.info("USE_DAGSTER enabled - COG generation delegated to Dagster pipeline for layer %s", ctx.layer_id)

        # Include cog_key in metadata if generated
        metadata_to_store = dict(ctx.metadata_dict)
        if cog_key:
            metadata_to_store["cog_key"] = cog_key

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

        return result

    async def _extract_metadata(self, ctx: UploadContext):
        """Extract bounds and statistics — Dask pipeline with GDAL fallback."""
        if DASK_AVAILABLE:
            try:
                # Run Dask extraction in a thread to avoid blocking the event loop
                loop = asyncio.get_running_loop()
                extracted = await loop.run_in_executor(
                    None, RasterPipeline.extract_metadata, ctx.temp_file_path
                )
                bounds = RasterPipeline.apply_metadata_to_dict(
                    ctx.metadata_dict, extracted
                )
                logger.info(
                    "Raster metadata extracted via Dask pipeline for %s",
                    ctx.layer_id,
                )
                return bounds
            except Exception as e:
                logger.warning(
                    "Dask metadata extraction failed for %s, falling back to GDAL: %s",
                    ctx.layer_id,
                    e,
                )

        # Legacy GDAL fallback
        return preprocess_raster(ctx.temp_file_path, ctx.metadata_dict)

    async def _eager_cog(self, ctx: UploadContext) -> str | None:
        """Optionally generate COG at upload time.

        Returns the S3 key of the COG, or None on failure.
        """
        try:
            loop = asyncio.get_running_loop()
            with tempfile.TemporaryDirectory() as temp_dir:
                cog_path = os.path.join(temp_dir, f"{ctx.layer_id}.cog.tif")
                await loop.run_in_executor(
                    None,
                    RasterPipeline.create_cog,
                    ctx.temp_file_path,
                    cog_path,
                )

                # Upload COG to S3
                cog_key = f"cog/layer/{ctx.layer_id}.cog.tif"

                from src.utils import get_async_s3_client

                s3 = await get_async_s3_client()
                await s3.upload_file(cog_path, ctx.bucket_name, cog_key)

                logger.info(
                    "Eager COG generated and uploaded for %s -> %s",
                    ctx.layer_id,
                    cog_key,
                )
                return cog_key

        except Exception as e:
            logger.warning(
                "Eager COG generation failed for %s (will generate lazily): %s",
                ctx.layer_id,
                e,
            )
            return None
