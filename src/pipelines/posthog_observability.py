"""PostHog observability helpers for Dagster geospatial pipelines.

These helpers keep Dagster telemetry privacy-safe and small. They report only
pipeline state, counts, timings, and freshness hints: never raw imagery,
credentials, S3 object names, URLs, or large result payloads.
"""

from __future__ import annotations

import functools
import logging
import time
from collections.abc import Callable, Mapping
from datetime import datetime, timezone
from typing import Any, TypeVar

from src.services.posthog_analytics import capture_backend_event, elapsed_ms
from src.services.pipeline_evidence import record_pipeline_evidence

logger = logging.getLogger(__name__)

_F = TypeVar("_F", bound=Callable[..., Any])

_SUCCESS_STATUSES = {
    "exists",
    "ok",
    "ready",
    "success",
    "up_to_date",
    "waiting_for_data",
}
_SKIPPED_STATUSES = {
    "no_data",
    "no_features",
    "no_layers",
    "no_parcels",
    "skipped",
    "table_not_ready",
}
_FAILURE_STATUSES = {
    "error",
    "failed",
    "failure",
    "timeout",
}

_COUNT_KEYS = {
    "alerts_created",
    "cell_hexagons",
    "classification_rows",
    "dates_processed",
    "district_hexagons",
    "districts_analyzed",
    "districts_assessed",
    "districts_processed",
    "districts_scanned",
    "errors_count",
    "features",
    "h3_cells_updated",
    "layers_checked",
    "parcels_processed",
    "output_bytes",
    "output_file_count",
    "pmtiles_size_bytes",
    "rows_aggregated",
    "rows_written",
    "sample_success_count",
    "sample_workflow_count",
    "scene_count",
    "tiles_invalidated",
    "tool_count",
    "total_alerts",
    "total_rows",
}
_SAFE_STRING_KEYS = {
    "backend",
    "date_range",
    "job_status",
    "range",
    "reason",
}


def observed_dagster_asset(
    *,
    asset_name: str,
    pipeline_family: str,
    source_category: str,
    analysis_domain: str = "agriculture",
    evidence_kind: str = "scheduled_asset",
) -> Callable[[_F], _F]:
    """Decorate a Dagster asset function with PostHog completion telemetry."""

    def decorator(fn: _F) -> _F:
        @functools.wraps(fn)
        def wrapper(context: Any, *args: Any, **kwargs: Any) -> Any:
            started_at = time.monotonic()
            try:
                result = fn(context, *args, **kwargs)
            except Exception as exc:
                capture_dagster_asset_exception(
                    context,
                    asset_name=asset_name,
                    pipeline_family=pipeline_family,
                    source_category=source_category,
                    analysis_domain=analysis_domain,
                    evidence_kind=evidence_kind,
                    elapsed_ms_value=elapsed_ms(started_at),
                    error_type=type(exc).__name__,
                )
                raise

            capture_dagster_asset_result(
                context,
                asset_name=asset_name,
                pipeline_family=pipeline_family,
                source_category=source_category,
                analysis_domain=analysis_domain,
                evidence_kind=evidence_kind,
                result=result,
                elapsed_ms_value=elapsed_ms(started_at),
            )
            return result

        return wrapper  # type: ignore[return-value]

    return decorator


def observed_dagster_sensor(
    *,
    sensor_name: str,
    pipeline_family: str,
    source_category: str,
) -> Callable[[_F], _F]:
    """Decorate a Dagster sensor function with PostHog evaluation telemetry."""

    def decorator(fn: _F) -> _F:
        @functools.wraps(fn)
        def wrapper(context: Any, *args: Any, **kwargs: Any) -> Any:
            started_at = time.monotonic()
            try:
                result = fn(context, *args, **kwargs)
            except Exception as exc:
                capture_dagster_sensor_result(
                    context,
                    sensor_name=sensor_name,
                    pipeline_family=pipeline_family,
                    source_category=source_category,
                    result={"status": "error", "error_type": type(exc).__name__},
                    elapsed_ms_value=elapsed_ms(started_at),
                )
                raise

            capture_dagster_sensor_result(
                context,
                sensor_name=sensor_name,
                pipeline_family=pipeline_family,
                source_category=source_category,
                result=result,
                elapsed_ms_value=elapsed_ms(started_at),
            )
            return result

        return wrapper  # type: ignore[return-value]

    return decorator


def capture_dagster_asset_result(
    context: Any,
    *,
    asset_name: str,
    pipeline_family: str,
    source_category: str,
    analysis_domain: str,
    evidence_kind: str,
    result: Any,
    elapsed_ms_value: int,
) -> None:
    """Capture a completed Dagster asset and optional geospatial flow events."""
    status = _status_from_result(result)
    properties = {
        **_context_properties(context),
        **_result_properties(result),
        "asset_name": asset_name,
        "pipeline_family": pipeline_family,
        "source_category": source_category,
        "analysis_domain": analysis_domain,
        "evidence_kind": evidence_kind,
        "status": status,
        "success": _is_success_status(status),
        "skipped": status in _SKIPPED_STATUSES,
        "elapsed_ms": elapsed_ms_value,
    }
    _capture("dagster_asset_completed", properties)
    _capture("geospatial_pipeline_flow_completed", properties)
    if source_category == "satellite":
        _capture("satellite_pipeline_completed", properties)


def capture_dagster_asset_exception(
    context: Any,
    *,
    asset_name: str,
    pipeline_family: str,
    source_category: str,
    analysis_domain: str,
    evidence_kind: str,
    elapsed_ms_value: int,
    error_type: str,
) -> None:
    properties = {
        **_context_properties(context),
        "asset_name": asset_name,
        "pipeline_family": pipeline_family,
        "source_category": source_category,
        "analysis_domain": analysis_domain,
        "evidence_kind": evidence_kind,
        "status": "error",
        "success": False,
        "error_type": error_type,
        "elapsed_ms": elapsed_ms_value,
    }
    _capture("dagster_asset_failed", properties)
    _capture("geospatial_pipeline_flow_completed", properties)
    if source_category == "satellite":
        _capture("satellite_pipeline_completed", properties)


def capture_dagster_sensor_result(
    context: Any,
    *,
    sensor_name: str,
    pipeline_family: str,
    source_category: str,
    result: Any,
    elapsed_ms_value: int,
    extra_properties: Mapping[str, Any] | None = None,
) -> None:
    status = _status_from_result(result)
    properties = {
        **_context_properties(context),
        **_result_properties(result),
        "sensor_name": sensor_name,
        "pipeline_family": pipeline_family,
        "source_category": source_category,
        "status": status,
        "success": _is_success_status(status),
        "skipped": status in _SKIPPED_STATUSES,
        "elapsed_ms": elapsed_ms_value,
    }
    if extra_properties:
        properties.update(_safe_extra_properties(extra_properties))
    _capture("dagster_sensor_evaluated", properties)


def capture_satellite_scene_sensor_success(
    context: Any,
    *,
    scene_count: int,
    latest_datetime: str,
    tiles_invalidated: int,
    cache_warming_started: bool,
    elapsed_ms_value: int,
) -> None:
    """Capture the specific proof event Sage/PostHog need for satellite freshness."""
    properties = {
        **_context_properties(context),
        "sensor_name": "satellite_scene_sensor",
        "pipeline_family": "satellite_scene_catalog",
        "source_category": "satellite",
        "analysis_domain": "agriculture",
        "evidence_kind": "sentinel_2_scene_catalog",
        "status": "ok",
        "success": True,
        "scene_count": int(scene_count),
        "latest_datetime": latest_datetime,
        "freshness_lag_hours": _freshness_lag_hours(latest_datetime),
        "tiles_invalidated": int(tiles_invalidated),
        "cache_warming_started": bool(cache_warming_started),
        "elapsed_ms": elapsed_ms_value,
    }
    _capture("satellite_pipeline_completed", properties)
    _capture("geospatial_pipeline_flow_completed", properties)


def capture_dagster_hook_event(
    context: Any,
    *,
    status: str,
    elapsed_ms_value: int | None = None,
    error_type: str | None = None,
) -> None:
    """Capture hook-level success/failure for jobs that attach Dagster hooks."""
    properties = {
        **_context_properties(context),
        "status": status,
        "success": status.lower() in {"ok", "success"},
        "hook_name": "dagster_pipeline_hook",
    }
    if elapsed_ms_value is not None:
        properties["elapsed_ms"] = elapsed_ms_value
    if error_type:
        properties["error_type"] = error_type
    _capture("dagster_hook_completed", properties)


def _capture(event: str, properties: Mapping[str, Any]) -> None:
    try:
        record_pipeline_evidence(event, properties)
        capture_backend_event(
            event,
            distinct_id="dagster-pipeline",
            properties=properties,
            groups={"pipeline": str(properties.get("pipeline_family", "dagster"))},
        )
    except Exception as exc:
        logger.debug("Dagster PostHog capture skipped for %s: %s", event, exc)


def _context_properties(context: Any) -> dict[str, Any]:
    props: dict[str, Any] = {}
    for name in ("run_id", "job_name"):
        value = getattr(context, name, None)
        if value:
            props[name] = str(value)
    op = getattr(context, "op", None)
    op_name = getattr(op, "name", None)
    if op_name:
        props["op_name"] = str(op_name)
    asset_key = getattr(context, "asset_key", None)
    if asset_key:
        props["asset_key"] = str(asset_key)
    cursor = getattr(context, "cursor", None)
    if cursor:
        props["cursor_present"] = True
    return props


def _result_properties(result: Any) -> dict[str, Any]:
    if result is None:
        return {"result_type": "none"}

    if _is_skip_reason(result):
        return {
            "result_type": "skip_reason",
            "reason": str(getattr(result, "skip_message", "") or result)[:120],
        }

    if isinstance(result, list):
        return {
            "result_type": "run_request_list",
            "run_request_count": len(result),
        }

    if not isinstance(result, Mapping):
        return {"result_type": type(result).__name__}

    props: dict[str, Any] = {"result_type": "mapping"}
    result_keys = sorted(str(k) for k in result.keys())
    props["result_key_count"] = len(result_keys)
    props["result_keys_sample"] = ",".join(result_keys[:12])
    for key, value in result.items():
        key_str = str(key)
        if key_str in _COUNT_KEYS and isinstance(value, (int, float)) and not isinstance(value, bool):
            props[key_str] = value
        elif key_str in _SAFE_STRING_KEYS and value is not None:
            props[key_str] = str(value)[:120]
    if isinstance(result.get("errors"), list):
        props["errors_count"] = len(result["errors"])
    if result.get("error"):
        props["error_present"] = True
    return props


def _safe_extra_properties(properties: Mapping[str, Any]) -> dict[str, Any]:
    safe: dict[str, Any] = {}
    for key, value in properties.items():
        key_str = str(key)
        if key_str in _COUNT_KEYS and isinstance(value, (int, float)) and not isinstance(value, bool):
            safe[key_str] = value
        elif key_str in _SAFE_STRING_KEYS and value is not None:
            safe[key_str] = str(value)[:120]
        elif isinstance(value, bool):
            safe[key_str] = value
    return safe


def _status_from_result(result: Any) -> str:
    if result is None:
        return "ok"
    if _is_skip_reason(result):
        return "skipped"
    if isinstance(result, list):
        return "ok"
    if isinstance(result, Mapping):
        status = str(result.get("status") or "").strip().lower()
        if status:
            return status[:40]
        if result.get("error"):
            return "error"
    return "ok"


def _is_success_status(status: str) -> bool:
    normalized = status.strip().lower()
    if normalized in _FAILURE_STATUSES:
        return False
    if normalized in _SKIPPED_STATUSES:
        return False
    return normalized in _SUCCESS_STATUSES or normalized == "ok"


def _is_skip_reason(result: Any) -> bool:
    return result.__class__.__name__ == "SkipReason"


def _freshness_lag_hours(value: str) -> float | None:
    try:
        normalized = value.replace("Z", "+00:00")
        dt = datetime.fromisoformat(normalized)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        lag = datetime.now(timezone.utc) - dt.astimezone(timezone.utc)
        return round(max(lag.total_seconds(), 0) / 3600, 2)
    except Exception:
        return None
