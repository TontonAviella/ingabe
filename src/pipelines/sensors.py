"""Dagster sensors for event-driven pipeline triggers.

Implements sensors that detect new file uploads to S3/MinIO and trigger
the appropriate processing pipelines based on file type.

Also includes a satellite scene sensor that polls Sentinel Hub Catalog API
for new Sentinel-2 L2A scenes over Rwanda and invalidates the tile cache.
"""

import json
import logging
import math
import threading
import urllib.request

import requests
from dagster import RunRequest, SensorEvaluationContext, SkipReason, sensor

from src.pipelines.resources import PostgresResource, RedisResource, S3Resource
from src.database.models import LAYER_TYPE_RASTER, LAYER_TYPE_VECTOR, LAYER_TYPE_POINT_CLOUD

logger = logging.getLogger(__name__)

# Rwanda bounding box (WGS84)
_RWANDA_BBOX = [28.86, -2.84, 30.90, -1.05]

# Sentinel Hub Catalog API
_SH_CATALOG_URL = "https://services.sentinel-hub.com/api/v1/catalog/1.0.0/search"


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
            if layer_type == LAYER_TYPE_RASTER:
                # Trigger raster processing pipeline
                run_requests.append(
                    RunRequest(
                        run_key=f"raster_{layer_id}_{created_on_str}",
                        job_name="raster_processing_job",
                        tags={
                            "layer_id": layer_id,
                            "layer_type": LAYER_TYPE_RASTER,
                            "s3_key": s3_key,
                            "trigger": "s3_upload_sensor",
                        },
                    )
                )
                context.log.info(f"Triggered raster pipeline for layer {layer_id}")

            elif layer_type == LAYER_TYPE_VECTOR:
                # Trigger vector processing pipeline
                run_requests.append(
                    RunRequest(
                        run_key=f"vector_{layer_id}_{created_on_str}",
                        job_name="vector_processing_job",
                        tags={
                            "layer_id": layer_id,
                            "layer_type": LAYER_TYPE_VECTOR,
                            "s3_key": s3_key,
                            "trigger": "s3_upload_sensor",
                        },
                    )
                )
                context.log.info(f"Triggered vector pipeline for layer {layer_id}")

            elif layer_type == LAYER_TYPE_POINT_CLOUD:
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


def _warm_satellite_cache(base_url: str = "http://localhost:8000"):
    """Pre-fetch all satellite tiles covering Rwanda at z8-z13 for both TRUE-COLOR and NDVI.

    Runs synchronously — designed to be called in a background thread.
    Takes ~15-20 minutes to complete (~5,500 tiles).
    """
    west, south, east, north = 28.86, -2.84, 30.90, -1.05
    layers = ["TRUE-COLOR", "NDVI"]
    cached = 0

    def _lng_to_x(lng: float, z: int) -> int:
        return int((lng + 180) / 360 * (1 << z))

    def _lat_to_y(lat: float, z: int) -> int:
        r = math.radians(lat)
        return int((1 - math.log(math.tan(r) + 1 / math.cos(r)) / math.pi) / 2 * (1 << z))

    for layer in layers:
        for z in range(8, 14):
            x0, x1 = _lng_to_x(west, z), _lng_to_x(east, z)
            y0, y1 = _lat_to_y(north, z), _lat_to_y(south, z)
            for x in range(x0, x1 + 1):
                for y in range(y0, y1 + 1):
                    url = (
                        f"{base_url}/api/satellite/{z}/{x}/{y}.png"
                        f"?layer={layer}&collection=sentinel-2-l2a"
                    )
                    try:
                        urllib.request.urlopen(url, timeout=30)
                        cached += 1
                    except Exception:
                        pass

    logger.info("Satellite cache warming complete: %d tiles cached", cached)


def build_satellite_scene_sensor():
    """Factory that creates a sensor to detect new Sentinel-2 scenes over Rwanda.

    Polls the Sentinel Hub Catalog API every 4 hours. On new scene detection:
    1. Invalidates all cached satellite tiles in Redis
    2. Publishes a notification to the ``ws:satellite`` Redis Pub/Sub channel
    """

    @sensor(
        name="satellite_scene_sensor",
        description="Detects new Sentinel-2 L2A scenes over Rwanda and invalidates tile cache",
        minimum_interval_seconds=4 * 3600,  # Every 4 hours
    )
    def satellite_scene_sensor(
        context: SensorEvaluationContext,
        redis: RedisResource,
    ):
        """Poll Sentinel Hub Catalog for new S2 L2A scenes over Rwanda."""
        import os
        from datetime import datetime, timezone

        sh_client_id = os.environ.get("SH_CLIENT_ID", "")
        sh_client_secret = os.environ.get("SH_CLIENT_SECRET", "")

        if not sh_client_id or not sh_client_secret:
            return SkipReason("Sentinel Hub credentials not configured")

        # Get OAuth2 token
        token_url = (
            "https://services.sentinel-hub.com/auth/realms/main/"
            "protocol/openid-connect/token"
        )
        try:
            token_resp = requests.post(
                token_url,
                data={
                    "grant_type": "client_credentials",
                    "client_id": sh_client_id,
                    "client_secret": sh_client_secret,
                },
                timeout=15,
            )
            token_resp.raise_for_status()
            access_token = token_resp.json()["access_token"]
        except Exception as e:
            context.log.error(f"Failed to get SH access token: {e}")
            return SkipReason(f"Failed to get SH access token: {e}")

        # Build catalog search — look for scenes from the last 7 days
        last_cursor = context.cursor or "2020-01-01T00:00:00Z"

        search_body = {
            "collections": ["sentinel-2-l2a"],
            "bbox": _RWANDA_BBOX,
            "datetime": f"{last_cursor}/..".replace("+00:00", "Z"),
            "limit": 5,
            "fields": {
                "include": ["properties.datetime"],
            },
        }

        try:
            resp = requests.post(
                _SH_CATALOG_URL,
                headers={
                    "Authorization": f"Bearer {access_token}",
                    "Content-Type": "application/json",
                },
                json=search_body,
                timeout=30,
            )
            resp.raise_for_status()
            catalog_result = resp.json()
        except Exception as e:
            context.log.error(f"Sentinel Hub Catalog search failed: {e}")
            return SkipReason(f"Catalog search failed: {e}")

        features = catalog_result.get("features", [])
        if not features:
            context.log.debug("No new Sentinel-2 scenes over Rwanda")
            return SkipReason("No new scenes detected")

        # Find the latest scene datetime
        latest_dt = last_cursor
        for feat in features:
            dt = feat.get("properties", {}).get("datetime", "")
            if dt > latest_dt:
                latest_dt = dt

        context.log.info(
            f"Detected {len(features)} new Sentinel-2 scene(s) over Rwanda, latest: {latest_dt}"
        )

        # Invalidate satellite tile cache + publish WebSocket notification
        try:
            with redis.get_client() as redis_client:
                # Invalidate sat:* keys
                deleted = 0
                cursor_val = 0
                while True:
                    cursor_val, keys = redis_client.scan(cursor=cursor_val, match="sat:*", count=200)
                    if keys:
                        deleted += redis_client.delete(*keys)
                    if cursor_val == 0:
                        break
                context.log.info(f"Invalidated {deleted} cached satellite tiles")

                # Publish notification via Redis Pub/Sub
                notification = json.dumps({
                    "type": "satellite_update",
                    "scene_count": len(features),
                    "latest_datetime": latest_dt,
                    "timestamp": datetime.now(timezone.utc).isoformat(),
                })
                redis_client.publish("ws:satellite", notification)
                context.log.info("Published satellite update notification")
        except Exception as e:
            context.log.warning(f"Failed to invalidate cache / publish notification: {e}")

        # Re-warm tile cache in background thread (non-blocking)
        try:
            t = threading.Thread(target=_warm_satellite_cache, daemon=True)
            t.start()
            context.log.info("Started background cache warming thread")
        except Exception as e:
            context.log.warning(f"Failed to start cache warming: {e}")

        # Update cursor to latest scene datetime
        context.update_cursor(latest_dt)

    return satellite_scene_sensor
