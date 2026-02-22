"""Dagster sensors for event-driven pipeline triggers.

Implements sensors that detect new file uploads to S3/MinIO and trigger
the appropriate processing pipelines based on file type.
"""

import logging

from dagster import RunRequest, SensorEvaluationContext, sensor

from src.pipelines.resources import PostgresResource, S3Resource

logger = logging.getLogger(__name__)


def build_s3_upload_sensor(raster_job, vector_job):
    """Factory that creates the s3_upload_sensor with proper job targets."""

    @sensor(
        name="s3_upload_sensor",
        description="Detects new files uploaded to S3 and triggers processing pipelines",
        minimum_interval_seconds=60,  # Check every minute
        jobs=[raster_job, vector_job],
    )
    def s3_upload_sensor(
        context: SensorEvaluationContext,
        s3: S3Resource,
        postgres: PostgresResource,
    ) -> list[RunRequest]:
        """Detect new S3 uploads and trigger appropriate assets.

        Since MinIO doesn't have native S3 event notifications like AWS,
        this sensor uses a polling strategy:
        1. Query database for recent uploads (last processed timestamp)
        2. For each new upload, trigger the appropriate asset

        The sensor maintains cursor state to track the last processed upload.

        Returns:
            List of RunRequests for newly uploaded files
        """
        # Get cursor from previous run (last processed timestamp)
        last_processed = context.cursor or "1970-01-01 00:00:00"

        # Query for new uploads since last check
        query = """
            SELECT layer_id, name, type, s3_key, created_on
            FROM map_layers
            WHERE created_on > %s
            ORDER BY created_on ASC
            LIMIT 20
        """

        try:
            results = postgres.execute_query(query, (last_processed,))
        except Exception as e:
            context.log.error(f"Failed to query for new uploads: {e}")
            return []

        if not results:
            context.log.debug("No new uploads detected")
            return []

        context.log.info(f"Detected {len(results)} new uploads")

        run_requests = []
        latest_timestamp = last_processed

        for layer_id, name, layer_type, s3_key, created_on in results:
            # Update latest timestamp
            created_on_str = str(created_on)
            if created_on_str > latest_timestamp:
                latest_timestamp = created_on_str

            # Determine which assets to trigger based on layer type
            if layer_type == "raster":
                # Trigger raster processing pipeline
                run_requests.append(
                    RunRequest(
                        run_key=f"raster_{layer_id}_{created_on_str}",
                        job_name="raster_processing_job",
                        tags={
                            "layer_id": layer_id,
                            "layer_type": "raster",
                            "s3_key": s3_key,
                            "trigger": "s3_upload_sensor",
                        },
                    )
                )
                context.log.info(f"Triggered raster pipeline for layer {layer_id}")

            elif layer_type == "vector":
                # Trigger vector processing pipeline
                run_requests.append(
                    RunRequest(
                        run_key=f"vector_{layer_id}_{created_on_str}",
                        job_name="vector_processing_job",
                        tags={
                            "layer_id": layer_id,
                            "layer_type": "vector",
                            "s3_key": s3_key,
                            "trigger": "s3_upload_sensor",
                        },
                    )
                )
                context.log.info(f"Triggered vector pipeline for layer {layer_id}")

            elif layer_type == "point_cloud":
                # Point cloud processing (not fully implemented yet)
                context.log.info(f"Point cloud upload detected: {layer_id} (skipping)")
            else:
                context.log.warning(f"Unknown layer type: {layer_type} for layer {layer_id}")

        # Update cursor to latest timestamp
        if run_requests:
            context.update_cursor(latest_timestamp)
            context.log.info(f"Updated cursor to {latest_timestamp}")

        return run_requests

    return s3_upload_sensor


def build_failed_cog_retry_sensor(raster_job):
    """Factory that creates the failed_cog_retry_sensor with proper job target."""

    @sensor(
        name="failed_cog_retry_sensor",
        description="Retry COG generation for layers that failed",
        minimum_interval_seconds=3600,  # Check every hour
        job=raster_job,
    )
    def failed_cog_retry_sensor(
        context: SensorEvaluationContext,
        postgres: PostgresResource,
    ) -> list[RunRequest]:
        """Detect raster layers without COGs and retry generation.

        Looks for raster layers that:
        1. Don't have a cog_key in metadata
        2. Were created more than 1 hour ago
        3. Haven't been processed recently

        Returns:
            List of RunRequests for retry attempts
        """
        query = """
            SELECT layer_id, name, s3_key, created_on
            FROM map_layers
            WHERE type = 'raster'
            AND (metadata->>'cog_key') IS NULL
            AND created_on < NOW() - INTERVAL '1 hour'
            AND created_on > NOW() - INTERVAL '7 days'
            LIMIT 10
        """

        try:
            results = postgres.execute_query(query)
        except Exception as e:
            context.log.error(f"Failed to query for COG retries: {e}")
            return []

        if not results:
            context.log.debug("No rasters need COG retry")
            return []

        context.log.info(f"Found {len(results)} rasters needing COG generation")

        run_requests = []
        for layer_id, name, s3_key, created_on in results:
            run_requests.append(
                RunRequest(
                    run_key=f"cog_retry_{layer_id}_{str(created_on)}",
                    tags={
                        "layer_id": layer_id,
                        "s3_key": s3_key,
                        "operation": "cog_retry",
                        "trigger": "failed_cog_retry_sensor",
                    },
                )
            )

        return run_requests

    return failed_cog_retry_sensor
