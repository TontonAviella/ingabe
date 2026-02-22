"""Dagster assets for raster processing workflows.

Wraps the existing Dask-based raster pipeline (src/upload/dask_raster.py)
and zonal statistics (src/geoprocessing/zonal_stats.py) into Dagster assets.
"""

import json
import logging
import os
import tempfile
from typing import Any

from dagster import AssetExecutionContext, asset

from src.geoprocessing.zonal_stats import compute_zonal_statistics
from src.pipelines.resources import PostgresResource, S3Resource, run_async
from src.upload.dask_raster import RasterPipeline

logger = logging.getLogger(__name__)


@asset(
    description="Raw raster file uploaded to S3",
    metadata={
        "dagster/group": "raster_processing",
    },
)
def raw_raster_upload(
    context: AssetExecutionContext,
    s3: S3Resource,
    postgres: PostgresResource,
) -> dict[str, Any]:
    """Materialize when a raster file is uploaded to S3.

    This asset represents the initial state of a raster file after upload.
    It's the starting point for the raster processing pipeline.

    Returns:
        Dict containing layer metadata (layer_id, s3_key, file_size, etc.)
    """
    # This asset would typically be triggered by a sensor detecting new S3 uploads
    # For now, we'll query the database for recent raster uploads

    query = """
        SELECT layer_id, name, s3_key, size_bytes, metadata, created_at
        FROM map_layers
        WHERE type = 'raster'
        AND created_at > NOW() - INTERVAL '1 hour'
        ORDER BY created_at DESC
        LIMIT 10
    """

    results = postgres.execute_query(query)

    if not results:
        context.log.warning("No recent raster uploads found")
        return {"status": "no_uploads", "count": 0}

    layers = []
    for row in results:
        layers.append({
            "layer_id": row[0],
            "name": row[1],
            "s3_key": row[2],
            "size_bytes": row[3],
            "metadata": json.loads(row[4]) if row[4] else {},
            "created_at": str(row[5]),
        })

    context.log.info(f"Found {len(layers)} recent raster uploads")
    return {"status": "success", "layers": layers, "count": len(layers)}


@asset(
    description="Cloud-Optimized GeoTIFF generated from raw raster",
    deps=[raw_raster_upload],
    metadata={
        "dagster/group": "raster_processing",
    },
)
def cog_generation(
    context: AssetExecutionContext,
    s3: S3Resource,
    postgres: PostgresResource,
) -> dict[str, Any]:
    """Generate COG from raw raster files.

    Wraps the existing Dask-based COG generation pipeline from
    src/upload/dask_raster.py. Processes rasters that don't have COGs yet.

    Returns:
        Dict containing COG generation results
    """
    if not RasterPipeline.is_available():
        context.log.error("Dask pipeline not available, skipping COG generation")
        return {"status": "error", "message": "Dask pipeline not available"}

    # Find rasters without COG metadata
    query = """
        SELECT layer_id, s3_key, metadata
        FROM map_layers
        WHERE type = 'raster'
        AND (metadata->>'cog_key') IS NULL
        AND created_at > NOW() - INTERVAL '1 day'
        LIMIT 5
    """

    results = postgres.execute_query(query)

    if not results:
        context.log.info("No rasters need COG generation")
        return {"status": "no_work", "count": 0}

    processed = []
    errors = []

    with s3.get_client() as s3_client:
        for layer_id, s3_key, metadata_json in results:
            try:
                context.log.info(f"Generating COG for layer {layer_id}")

                # Download raster from S3
                with tempfile.NamedTemporaryFile(suffix=".tif", delete=False) as tmp_in:
                    s3_client.download_file(s3.bucket_name, s3_key, tmp_in.name)
                    input_path = tmp_in.name

                try:
                    # Generate COG
                    with tempfile.NamedTemporaryFile(suffix=".cog.tif", delete=False) as tmp_out:
                        cog_path = tmp_out.name

                    RasterPipeline.create_cog(
                        input_path=input_path,
                        output_path=cog_path,
                        target_crs="EPSG:3857",
                    )

                    # Upload COG to S3
                    cog_key = f"cog/layer/{layer_id}.cog.tif"
                    s3_client.upload_file(cog_path, s3.bucket_name, cog_key)

                    # Update database with COG key
                    metadata = json.loads(metadata_json) if metadata_json else {}
                    metadata["cog_key"] = cog_key

                    with postgres.get_sync_connection() as conn:
                        with conn.cursor() as cur:
                            cur.execute(
                                "UPDATE map_layers SET metadata = %s WHERE layer_id = %s",
                                (json.dumps(metadata), layer_id),
                            )
                        conn.commit()

                    processed.append({
                        "layer_id": layer_id,
                        "cog_key": cog_key,
                    })
                    context.log.info(f"COG generated for layer {layer_id} -> {cog_key}")

                finally:
                    # Cleanup temp files
                    try:
                        os.unlink(input_path)
                        os.unlink(cog_path)
                    except OSError:
                        pass

            except Exception as e:
                context.log.error(f"COG generation failed for layer {layer_id}: {e}")
                errors.append({"layer_id": layer_id, "error": str(e)})

    return {
        "status": "success",
        "processed": processed,
        "errors": errors,
        "count": len(processed),
    }


@asset(
    description="Zonal statistics computed for raster-vector pairs",
    deps=[cog_generation],
    metadata={
        "dagster/group": "raster_processing",
    },
)
def zonal_statistics(
    context: AssetExecutionContext,
    postgres: PostgresResource,
) -> dict[str, Any]:
    """Run exactextract zonal statistics on raster-vector pairs.

    Wraps the existing zonal statistics function from
    src/geoprocessing/zonal_stats.py. Processes pending zonal stats requests.

    This asset would typically be triggered by user requests or scheduled
    for common raster-vector combinations (e.g., administrative boundaries).

    Returns:
        Dict containing zonal statistics results
    """
    # For demonstration, we'll look for recent raster layers and compute
    # stats against a default zones layer if available

    # Find recent raster layers
    raster_query = """
        SELECT layer_id, name
        FROM map_layers
        WHERE type = 'raster'
        AND created_at > NOW() - INTERVAL '1 day'
        LIMIT 3
    """

    rasters = postgres.execute_query(raster_query)

    if not rasters:
        context.log.info("No recent rasters for zonal statistics")
        return {"status": "no_rasters", "count": 0}

    # Find vector layers that could serve as zones
    zones_query = """
        SELECT layer_id, name
        FROM map_layers
        WHERE type = 'vector'
        AND geometry_type IN ('Polygon', 'MultiPolygon')
        LIMIT 1
    """

    zones = postgres.execute_query(zones_query)

    if not zones:
        context.log.info("No polygon layers available for zonal statistics")
        return {"status": "no_zones", "count": 0}

    zones_layer_id = zones[0][0]
    results_list = []

    for raster_id, raster_name in rasters:
        try:
            context.log.info(f"Computing zonal stats for {raster_name} (layer {raster_id})")

            # Run async function in sync context
            result = run_async(
                compute_zonal_statistics(
                    raster_layer_id=raster_id,
                    zones_layer_id=zones_layer_id,
                    stats=["mean", "sum", "min", "max", "count"],
                    timeout=60,
                )
            )

            results_list.append({
                "raster_layer_id": raster_id,
                "zones_layer_id": zones_layer_id,
                "result": result,
            })

        except Exception as e:
            context.log.error(f"Zonal stats failed for {raster_id}: {e}")
            results_list.append({
                "raster_layer_id": raster_id,
                "zones_layer_id": zones_layer_id,
                "error": str(e),
            })

    return {
        "status": "success",
        "results": results_list,
        "count": len(results_list),
    }
