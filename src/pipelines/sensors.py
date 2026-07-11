"""Dagster sensors for event-driven pipeline triggers.

Implements sensors that detect new file uploads to S3/MinIO and trigger
the appropriate processing pipelines based on file type.

Also includes a satellite scene sensor that polls the public Earth Search STAC API
for new Sentinel-2 L2A scenes over Rwanda and invalidates the tile cache.
"""

import json
import logging
import os
import time
from datetime import datetime, timedelta, timezone

from dagster import RunRequest, SensorEvaluationContext, SkipReason, sensor

from src.pipelines.resources import PostgresResource, RedisResource, S3Resource
from src.pipelines.posthog_observability import (
    capture_satellite_scene_sensor_success,
    observed_dagster_sensor,
)
from src.database.models import LAYER_TYPE_RASTER, LAYER_TYPE_VECTOR, LAYER_TYPE_POINT_CLOUD
from src.services.stac_service import STACService

logger = logging.getLogger(__name__)

# Rwanda bounding box (WGS84)
_RWANDA_BBOX = [28.86, -2.84, 30.90, -1.05]


def _validated_upload_cursor(
    cursor: str | None,
    *,
    now: datetime,
    max_backlog_hours: int,
) -> tuple[str, str | None]:
    """Return a safe cursor and why an existing backlog should be skipped."""
    current = now.astimezone(timezone.utc)
    current_cursor = current.isoformat()
    if not cursor:
        return current_cursor, "uninitialized"

    try:
        parsed, layer_id = _decode_upload_cursor(cursor)
    except (TypeError, ValueError):
        return current_cursor, "invalid"

    if parsed < current - timedelta(hours=max_backlog_hours):
        return current_cursor, "stale"
    return _encode_upload_cursor(parsed, layer_id), None


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _upload_timestamp_cursor(created_on: datetime | str) -> str:
    """Normalize a database upload timestamp for cursor persistence."""
    if isinstance(created_on, datetime):
        parsed = created_on
    else:
        parsed = datetime.fromisoformat(str(created_on).replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc).isoformat()


def _decode_upload_cursor(cursor: str) -> tuple[datetime, str]:
    raw = str(cursor or "").strip()
    layer_id = ""
    if raw.startswith("{"):
        payload = json.loads(raw)
        if not isinstance(payload, dict):
            raise ValueError("Upload cursor must be an object")
        raw = str(payload.get("created_on") or "")
        layer_id = str(payload.get("layer_id") or "")
    parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc), layer_id


def _encode_upload_cursor(created_on: datetime | str, layer_id: str = "") -> str:
    timestamp = _upload_timestamp_cursor(created_on)
    if not layer_id:
        return timestamp
    return json.dumps(
        {"created_on": timestamp, "layer_id": str(layer_id)},
        separators=(",", ":"),
        sort_keys=True,
    )


def build_s3_upload_sensor(raster_job, vector_job):
    """Factory that creates the s3_upload_sensor with proper job targets."""

    @sensor(
        name="s3_upload_sensor",
        description="Detects new files uploaded to S3 and triggers processing pipelines",
        minimum_interval_seconds=60,  # Check every minute
        jobs=[raster_job, vector_job],
    )
    @observed_dagster_sensor(
        sensor_name="s3_upload_sensor",
        pipeline_family="upload_ingest",
        source_category="upload",
    )
    def s3_upload_sensor(
        context: SensorEvaluationContext,
        s3: S3Resource,
        postgres: PostgresResource,
    ) -> list[RunRequest] | SkipReason:
        """Detect new S3 uploads and trigger appropriate assets.

        Since MinIO doesn't have native S3 event notifications like AWS,
        this sensor uses a polling strategy:
        1. Query database for recent uploads (last processed timestamp)
        2. For each new upload, trigger the appropriate asset

        The sensor maintains cursor state to track the last processed upload.

        Returns:
            List of RunRequests for newly uploaded files
        """
        max_backlog_hours = max(1, int(os.getenv("S3_UPLOAD_SENSOR_MAX_BACKLOG_HOURS", "24")))
        batch_size = max(1, min(20, int(os.getenv("S3_UPLOAD_SENSOR_BATCH_SIZE", "5"))))
        query_watermark = _utc_now()
        last_processed, skipped_backlog = _validated_upload_cursor(
            context.cursor,
            now=query_watermark,
            max_backlog_hours=max_backlog_hours,
        )
        if skipped_backlog:
            context.update_cursor(last_processed)
            return SkipReason(
                f"Initialized upload cursor at the current time ({skipped_backlog} cursor); "
                "historical layers were not replayed"
            )

        cursor_timestamp, cursor_layer_id = _decode_upload_cursor(last_processed)

        # Query for new uploads since last check
        query = """
            SELECT layer_id, name, type, s3_key, created_on
            FROM map_layers
            WHERE (created_on, layer_id) > (%s, %s)
              AND created_on <= %s
            ORDER BY created_on ASC, layer_id ASC
            LIMIT %s
        """

        try:
            results = postgres.execute_query(
                query,
                (cursor_timestamp, cursor_layer_id, query_watermark, batch_size),
            )
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
            created_on_str = _upload_timestamp_cursor(created_on)
            latest_timestamp = _encode_upload_cursor(created_on, str(layer_id))

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
    @observed_dagster_sensor(
        sensor_name="failed_cog_retry_sensor",
        pipeline_family="raster_cog_retry",
        source_category="raster",
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


def _parse_scene_datetime(value: str) -> datetime | None:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _search_new_satellite_scenes(
    last_cursor: str,
    *,
    now: datetime | None = None,
) -> tuple[list[dict], str]:
    """Return Earth Search scenes newer than the sensor cursor."""
    current_time = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    cursor_time = _parse_scene_datetime(last_cursor) or datetime(1970, 1, 1, tzinfo=timezone.utc)
    cursor_time = min(cursor_time, current_time)
    search_start = max(cursor_time, current_time - timedelta(days=7))
    datetime_range = (
        f"{search_start.isoformat().replace('+00:00', 'Z')}/"
        f"{current_time.isoformat().replace('+00:00', 'Z')}"
    )

    result = STACService("earth_search").search_imagery(
        bbox=_RWANDA_BBOX,
        datetime_range=datetime_range,
        max_cloud_cover=100.0,
        limit=25,
    )
    if result.get("error"):
        raise RuntimeError(str(result["error"]))

    scenes = []
    for item in result.get("items", []):
        scene_time = _parse_scene_datetime(str(item.get("datetime") or ""))
        if scene_time and scene_time > cursor_time:
            scenes.append(item)
    scenes.sort(key=lambda item: str(item.get("datetime") or ""))
    latest_datetime = str(scenes[-1]["datetime"]) if scenes else last_cursor
    return scenes, latest_datetime


def build_satellite_scene_sensor():
    """Factory that creates a sensor to detect new Sentinel-2 scenes over Rwanda.

    Polls the public Earth Search STAC API every 4 hours. On new scene detection:
    1. Invalidates all cached satellite tiles in Redis
    2. Publishes a notification to the ``ws:satellite`` Redis Pub/Sub channel
    """

    @sensor(
        name="satellite_scene_sensor",
        description="Detects new Sentinel-2 L2A scenes over Rwanda and invalidates tile cache",
        minimum_interval_seconds=4 * 3600,  # Every 4 hours
    )
    @observed_dagster_sensor(
        sensor_name="satellite_scene_sensor",
        pipeline_family="satellite_scene_catalog",
        source_category="satellite",
    )
    def satellite_scene_sensor(
        context: SensorEvaluationContext,
        redis: RedisResource,
    ):
        """Poll Earth Search for new S2 L2A scenes over Rwanda."""
        started_at = time.monotonic()
        last_cursor = context.cursor or "2020-01-01T00:00:00Z"
        try:
            features, latest_dt = _search_new_satellite_scenes(last_cursor)
        except Exception as e:
            context.log.error(f"Earth Search Catalog search failed: {e}")
            return SkipReason(f"Catalog search failed: {e}")

        if not features:
            context.log.debug("No new Sentinel-2 scenes over Rwanda")
            return SkipReason("No new scenes detected")

        context.log.info(
            f"Detected {len(features)} new Sentinel-2 scene(s) over Rwanda, latest: {latest_dt}"
        )

        # Invalidate satellite tile cache + publish WebSocket notification
        deleted = 0
        cache_warming_started = False
        try:
            with redis.get_client() as redis_client:
                # Invalidate sat:* keys
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
            context.log.error(f"Failed to invalidate cache / publish notification: {e}")
            return SkipReason(
                "Satellite update delivery failed; keeping the previous cursor for retry"
            )

        # Cache refill stays demand-driven; scene detection must not fan out tile requests.
        # Update cursor to latest scene datetime
        context.update_cursor(latest_dt)
        capture_satellite_scene_sensor_success(
            context,
            scene_count=len(features),
            latest_datetime=latest_dt,
            tiles_invalidated=deleted,
            cache_warming_started=cache_warming_started,
            elapsed_ms_value=int((time.monotonic() - started_at) * 1000),
        )

    return satellite_scene_sensor
