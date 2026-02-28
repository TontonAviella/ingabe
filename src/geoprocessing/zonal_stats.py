"""Zonal statistics using exactextract library.

Provides fast and accurate raster zonal statistics for polygons using the
exactextract C++ library through Python bindings. This module handles both
S3-stored and remote raster data sources, and supports vector layers in
various formats (FlatGeoBuf, GeoJSON, GeoPackage).
"""

import asyncio
import logging
from typing import Any

from fastapi import HTTPException, status

from src.fs_lru import layer_cache
from src.structures import get_async_read_connection
from src.database.models import LAYER_TYPE_RASTER, LAYER_TYPE_POSTGIS

logger = logging.getLogger(__name__)

# Default statistics to compute
DEFAULT_STATS = ["mean", "sum", "min", "max", "count", "stdev", "variance"]


async def compute_zonal_statistics(
    raster_layer_id: str,
    zones_layer_id: str,
    stats: list[str] | None = None,
    timeout: int = 30,
) -> dict[str, Any]:
    """Compute zonal statistics from a raster layer using polygon zones.

    Uses exactextract for fast and accurate pixel-polygon coverage calculations.
    Runs the computation in a thread pool executor to avoid blocking the event loop.

    Args:
        raster_layer_id: Layer ID of the raster data source
        zones_layer_id: Layer ID of the vector polygon zones
        stats: List of statistics to compute. Defaults to mean, sum, min, max, count, stdev, variance.
               Available: mean, sum, min, max, count, stdev, variance, median, quantile, mode,
               majority, minority, variety, weighted_mean, weighted_sum, etc.
               See exactextract documentation for full list.
        timeout: Maximum execution time in seconds (default: 30)

    Returns:
        Dictionary containing:
            - status: "success" or "error"
            - results: List of feature dictionaries with properties containing computed statistics
            - stats_computed: List of statistics that were computed
            - feature_count: Number of features processed
            - error: Error message if status is "error"

    Raises:
        HTTPException: If layers not found, wrong type, CRS mismatch, or computation fails
    """
    if stats is None:
        stats = DEFAULT_STATS

    cache = layer_cache()

    # Fetch layer metadata from database
    async with get_async_read_connection() as conn:
        raster_layer = await conn.fetchrow(
            """
            SELECT layer_id, name, type, s3_key, remote_url, metadata
            FROM map_layers
            WHERE layer_id = $1
            """,
            raster_layer_id,
        )

        zones_layer = await conn.fetchrow(
            """
            SELECT layer_id, name, type, s3_key, remote_url, postgis_connection_id
            FROM map_layers
            WHERE layer_id = $1
            """,
            zones_layer_id,
        )

    if not raster_layer:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Raster layer {raster_layer_id} not found",
        )

    if not zones_layer:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Zones layer {zones_layer_id} not found",
        )

    # Validate layer types
    if raster_layer["type"] != LAYER_TYPE_RASTER:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Layer {raster_layer_id} is not a raster layer (type: {raster_layer['type']})",
        )

    if zones_layer["type"] == LAYER_TYPE_RASTER:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Zones layer {zones_layer_id} must be a vector layer, not raster",
        )

    # PostGIS layers not yet supported - would require streaming features
    if zones_layer["type"] == LAYER_TYPE_POSTGIS:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"PostGIS zones layer {zones_layer_id} not yet supported. Please export to vector file first.",
        )

    # Use layer cache to get local file paths
    # This is important for reliable access to both raster and vector data
    try:
        async with (
            cache.layer_filename(raster_layer_id) as raster_path,
            cache.layer_filename(zones_layer_id) as zones_path,
        ):

            def compute_stats_sync():
                """Synchronous function to run in executor."""
                try:
                    import exactextract
                    from osgeo import gdal

                    # Verify raster file is accessible
                    raster_ds = gdal.Open(raster_path)
                    if raster_ds is None:
                        raise ValueError(
                            f"Unable to open raster layer {raster_layer_id} at {raster_path}"
                        )
                    band_count = raster_ds.RasterCount
                    raster_ds = None  # Close dataset

                    logger.info(
                        "Computing zonal statistics for raster %s (%d bands) with zones %s: stats=%s",
                        raster_layer_id,
                        band_count,
                        zones_layer_id,
                        ", ".join(stats),
                    )

                    # Run exactextract with local cached files
                    results = exactextract.exact_extract(
                        raster=raster_path,
                        vec=zones_path,
                        ops=stats,
                        include_geom=False,
                        output="pandas",  # Return as pandas DataFrame for easier manipulation
                    )

                    # Convert pandas DataFrame to list of dicts for JSON serialization
                    if hasattr(results, "to_dict"):
                        result_list = results.to_dict(orient="records")
                    else:
                        # If exactextract returns a list directly (older versions)
                        result_list = results

                    return {
                        "status": "success",
                        "results": result_list,
                        "stats_computed": stats,
                        "feature_count": len(result_list),
                        "raster_band_count": band_count,
                    }

                except ImportError as e:
                    raise ImportError(
                        f"exactextract package not installed: {e}. Install with: pip install exactextract"
                    ) from e

            # Run the synchronous function in an executor with timeout
            loop = asyncio.get_running_loop()
            try:
                result = await asyncio.wait_for(
                    loop.run_in_executor(None, compute_stats_sync), timeout=timeout
                )
                return result
            except asyncio.TimeoutError:
                raise HTTPException(
                    status_code=status.HTTP_504_GATEWAY_TIMEOUT,
                    detail=f"Zonal statistics computation timed out after {timeout} seconds",
                )
            except ImportError as e:
                raise HTTPException(
                    status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                    detail=f"exactextract library not available: {str(e)}",
                )

    except KeyError as e:
        # Layer not found in cache
        logger.warning("Layer not found in cache: %s", e)
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Layer file not found: {e}",
        )
    except HTTPException:
        # Re-raise HTTP exceptions
        raise
    except Exception as e:
        logger.exception("Error computing zonal statistics")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Error computing zonal statistics: {str(e)}",
        )
