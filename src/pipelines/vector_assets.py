"""Dagster assets for vector processing workflows.

Wraps vector processing operations including FlatGeoBuf conversion,
PMTiles generation (via tippecanoe), and Iceberg table registration.
"""

import json
import logging
import os
import subprocess
import tempfile
from typing import Any

from dagster import AssetExecutionContext, asset

from src.pipelines.resources import PostgresResource, S3Resource
from src.services.lakehouse import get_lakehouse_manager

logger = logging.getLogger(__name__)


@asset(
    description="Raw vector file uploaded to S3",
    metadata={
        "dagster/group": "vector_processing",
    },
)
def raw_vector_upload(
    context: AssetExecutionContext,
    postgres: PostgresResource,
) -> dict[str, Any]:
    """Materialize when a vector file is uploaded to S3.

    This asset represents the initial state of a vector file after upload.
    Queries the database for recent vector uploads.

    Returns:
        Dict containing layer metadata
    """
    query = """
        SELECT layer_id, name, s3_key, size_bytes, geometry_type, feature_count, created_at
        FROM map_layers
        WHERE type = 'vector'
        AND created_at > NOW() - INTERVAL '1 hour'
        ORDER BY created_at DESC
        LIMIT 10
    """

    results = postgres.execute_query(query)

    if not results:
        context.log.warning("No recent vector uploads found")
        return {"status": "no_uploads", "count": 0}

    layers = []
    for row in results:
        layers.append({
            "layer_id": row[0],
            "name": row[1],
            "s3_key": row[2],
            "size_bytes": row[3],
            "geometry_type": row[4],
            "feature_count": row[5],
            "created_at": str(row[6]),
        })

    context.log.info(f"Found {len(layers)} recent vector uploads")
    return {"status": "success", "layers": layers, "count": len(layers)}


@asset(
    description="Vector data converted to FlatGeoBuf format",
    deps=[raw_vector_upload],
    metadata={
        "dagster/group": "vector_processing",
    },
)
def flatgeobuf_conversion(
    context: AssetExecutionContext,
    s3: S3Resource,
    postgres: PostgresResource,
) -> dict[str, Any]:
    """Convert vector files to FlatGeoBuf format if not already in that format.

    FlatGeoBuf is an efficient cloud-optimized format for vector data.
    This asset checks for layers that need conversion and processes them.

    Returns:
        Dict containing conversion results
    """
    # Find vector layers that are not FlatGeoBuf
    query = """
        SELECT layer_id, s3_key, metadata
        FROM map_layers
        WHERE type = 'vector'
        AND s3_key NOT LIKE '%.fgb'
        AND created_at > NOW() - INTERVAL '1 day'
        LIMIT 5
    """

    results = postgres.execute_query(query)

    if not results:
        context.log.info("No vectors need FlatGeoBuf conversion")
        return {"status": "no_work", "count": 0}

    processed = []
    errors = []

    with s3.get_client() as s3_client:
        for layer_id, s3_key, metadata_json in results:
            try:
                context.log.info(f"Converting layer {layer_id} to FlatGeoBuf")

                # Download source file
                with tempfile.NamedTemporaryFile(delete=False) as tmp_in:
                    s3_client.download_file(s3.bucket_name, s3_key, tmp_in.name)
                    input_path = tmp_in.name

                try:
                    # Convert to FlatGeoBuf using ogr2ogr
                    with tempfile.NamedTemporaryFile(suffix=".fgb", delete=False) as tmp_out:
                        output_path = tmp_out.name

                    cmd = [
                        "ogr2ogr",
                        "-f", "FlatGeobuf",
                        output_path,
                        input_path,
                    ]
                    subprocess.run(cmd, check=True, capture_output=True)

                    # Upload to S3
                    fgb_key = f"vector/layer/{layer_id}.fgb"
                    s3_client.upload_file(output_path, s3.bucket_name, fgb_key)

                    # Update database with FGB key
                    metadata = json.loads(metadata_json) if metadata_json else {}
                    metadata["fgb_key"] = fgb_key
                    metadata["original_s3_key"] = s3_key

                    with postgres.get_sync_connection() as conn:
                        with conn.cursor() as cur:
                            cur.execute(
                                "UPDATE map_layers SET metadata = %s WHERE layer_id = %s",
                                (json.dumps(metadata), layer_id),
                            )
                        conn.commit()

                    processed.append({
                        "layer_id": layer_id,
                        "fgb_key": fgb_key,
                    })
                    context.log.info(f"FlatGeoBuf created for {layer_id} -> {fgb_key}")

                finally:
                    # Cleanup
                    try:
                        os.unlink(input_path)
                        os.unlink(output_path)
                    except OSError:
                        pass

            except Exception as e:
                context.log.error(f"FlatGeoBuf conversion failed for {layer_id}: {e}")
                errors.append({"layer_id": layer_id, "error": str(e)})

    return {
        "status": "success",
        "processed": processed,
        "errors": errors,
        "count": len(processed),
    }


@asset(
    description="PMTiles vector tiles generated via tippecanoe",
    deps=[flatgeobuf_conversion],
    metadata={
        "dagster/group": "vector_processing",
    },
)
def vector_tile_generation(
    context: AssetExecutionContext,
    s3: S3Resource,
    postgres: PostgresResource,
) -> dict[str, Any]:
    """Generate PMTiles vector tiles using tippecanoe.

    PMTiles are cloud-optimized vector tiles for efficient web visualization.
    This asset wraps the existing tippecanoe processing from src/upload/pmtiles.py.

    Returns:
        Dict containing tile generation results
    """
    # Find layers without PMTiles
    query = """
        SELECT layer_id, s3_key, metadata
        FROM map_layers
        WHERE type = 'vector'
        AND (metadata->>'pmtiles_key') IS NULL
        AND created_at > NOW() - INTERVAL '1 day'
        LIMIT 5
    """

    results = postgres.execute_query(query)

    if not results:
        context.log.info("No vectors need PMTiles generation")
        return {"status": "no_work", "count": 0}

    processed = []
    errors = []

    with s3.get_client() as s3_client:
        for layer_id, s3_key, metadata_json in results:
            try:
                context.log.info(f"Generating PMTiles for layer {layer_id}")

                # Download vector file
                with tempfile.NamedTemporaryFile(delete=False) as tmp_in:
                    s3_client.download_file(s3.bucket_name, s3_key, tmp_in.name)
                    input_path = tmp_in.name

                try:
                    # Generate PMTiles using tippecanoe
                    with tempfile.NamedTemporaryFile(suffix=".pmtiles", delete=False) as tmp_out:
                        output_path = tmp_out.name

                    # tippecanoe command (simplified - actual command may vary)
                    cmd = [
                        "tippecanoe",
                        "-o", output_path,
                        "-l", layer_id,
                        "--force",
                        "--drop-densest-as-needed",
                        "--extend-zooms-if-still-dropping",
                        input_path,
                    ]
                    subprocess.run(cmd, check=True, capture_output=True)

                    # Upload to S3
                    pmtiles_key = f"tiles/layer/{layer_id}.pmtiles"
                    s3_client.upload_file(output_path, s3.bucket_name, pmtiles_key)

                    # Update database
                    metadata = json.loads(metadata_json) if metadata_json else {}
                    metadata["pmtiles_key"] = pmtiles_key

                    with postgres.get_sync_connection() as conn:
                        with conn.cursor() as cur:
                            cur.execute(
                                "UPDATE map_layers SET metadata = %s WHERE layer_id = %s",
                                (json.dumps(metadata), layer_id),
                            )
                        conn.commit()

                    processed.append({
                        "layer_id": layer_id,
                        "pmtiles_key": pmtiles_key,
                    })
                    context.log.info(f"PMTiles generated for {layer_id} -> {pmtiles_key}")

                finally:
                    # Cleanup
                    try:
                        os.unlink(input_path)
                        os.unlink(output_path)
                    except OSError:
                        pass

            except Exception as e:
                context.log.error(f"PMTiles generation failed for {layer_id}: {e}")
                errors.append({"layer_id": layer_id, "error": str(e)})

    return {
        "status": "success",
        "processed": processed,
        "errors": errors,
        "count": len(processed),
    }


@asset(
    description="Vector data registered as Iceberg table in lakehouse",
    deps=[vector_tile_generation],
    metadata={
        "dagster/group": "vector_processing",
    },
)
def iceberg_registration(
    context: AssetExecutionContext,
    postgres: PostgresResource,
) -> dict[str, Any]:
    """Register vector layers as Iceberg tables in the lakehouse.

    Uses the Phase 2 lakehouse manager (src/services/lakehouse.py) to
    create Iceberg tables for vector data, enabling time-travel, ACID
    transactions, and efficient analytical queries.

    Returns:
        Dict containing registration results
    """
    # Find vector layers not yet registered in Iceberg
    query = """
        SELECT layer_id, name, metadata
        FROM map_layers
        WHERE type = 'vector'
        AND (metadata->>'iceberg_registered') IS NULL
        AND created_at > NOW() - INTERVAL '1 day'
        LIMIT 5
    """

    results = postgres.execute_query(query)

    if not results:
        context.log.info("No vectors need Iceberg registration")
        return {"status": "no_work", "count": 0}

    lakehouse = get_lakehouse_manager()
    processed = []
    errors = []

    for layer_id, name, metadata_json in results:
        try:
            context.log.info(f"Registering layer {layer_id} in Iceberg lakehouse")

            # Register the table
            table = lakehouse.register_vector_table(layer_id=layer_id)

            # Update database metadata
            metadata = json.loads(metadata_json) if metadata_json else {}
            metadata["iceberg_registered"] = True
            metadata["iceberg_table"] = f"vector_layers.layer_{layer_id}"

            with postgres.get_sync_connection() as conn:
                with conn.cursor() as cur:
                    cur.execute(
                        "UPDATE map_layers SET metadata = %s WHERE layer_id = %s",
                        (json.dumps(metadata), layer_id),
                    )
                conn.commit()

            processed.append({
                "layer_id": layer_id,
                "table_name": f"vector_layers.layer_{layer_id}",
            })
            context.log.info(f"Iceberg table registered for {layer_id}")

        except Exception as e:
            context.log.error(f"Iceberg registration failed for {layer_id}: {e}")
            errors.append({"layer_id": layer_id, "error": str(e)})

    return {
        "status": "success",
        "processed": processed,
        "errors": errors,
        "count": len(processed),
    }
