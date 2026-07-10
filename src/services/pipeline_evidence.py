"""Local geospatial pipeline evidence ledger.

PostHog is the observability source of truth for operators, but the running app
may only have a capture key, not a query key. This small JSON ledger gives Sage a
local, privacy-safe way to answer "is this pipeline fresh?" without inventing.
"""

from __future__ import annotations

import json
import logging
import os
import tempfile
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping

try:
    import fcntl
except ImportError:  # pragma: no cover - Windows fallback for local dev only.
    fcntl = None  # type: ignore[assignment]

_DEFAULT_PATH = "/tmp/ingabe_cache/pipeline_evidence.json"
_MAX_EVENTS = 80
_FALLBACK_RELATIVE_PATH = Path("ingabe") / "pipeline_evidence.json"

logger = logging.getLogger(__name__)

_SAFE_KEYS = {
    "analysis_domain",
    "asset_name",
    "backend",
    "cache_warming_started",
    "date_range",
    "elapsed_ms",
    "exit_code",
    "errors_count",
    "evidence_kind",
    "features",
    "freshness_lag_hours",
    "geolibre_workflow",
    "h3_cells_updated",
    "input_file_count",
    "job_name",
    "latest_datetime",
    "manifest_count",
    "output_bytes",
    "output_file_count",
    "pipeline_family",
    "pmtiles_size_bytes",
    "reason",
    "result_type",
    "rows_aggregated",
    "rows_written",
    "run_request_count",
    "scene_count",
    "sensor_name",
    "sample_success_count",
    "sample_workflow_count",
    "skipped",
    "source_category",
    "status",
    "success",
    "tiles_invalidated",
    "tool_category",
    "tool_count",
    "tool_id",
    "tool_source",
    "total_rows",
}


def record_pipeline_evidence(event: str, properties: Mapping[str, Any]) -> bool:
    """Record a bounded, sanitized pipeline proof locally."""
    if _truthy_env("PIPELINE_EVIDENCE_DISABLED"):
        return False

    record = _sanitize_record(event, properties)
    if not record:
        return False

    errors: list[str] = []
    for path in _candidate_evidence_paths():
        try:
            _write_record(path, record)
            return True
        except OSError as exc:
            errors.append(f"{path}: {type(exc).__name__}: {exc}")

    if errors:
        logger.warning(
            "Pipeline evidence write skipped; no writable path: %s",
            "; ".join(errors),
        )
    return False


def read_pipeline_evidence(
    *,
    source_category: str | None = None,
    pipeline_family: str | None = None,
    max_items: int = 20,
    stale_after_hours: float = 24.0,
) -> dict[str, Any]:
    """Return latest local pipeline evidence, optionally filtered."""
    path, payload = _read_first_available_payload()
    latest = list(payload.get("latest", {}).values())
    if source_category:
        latest = [
            item
            for item in latest
            if item.get("source_category") == source_category
        ]
    if pipeline_family:
        latest = [
            item
            for item in latest
            if item.get("pipeline_family") == pipeline_family
        ]

    latest = sorted(
        latest,
        key=lambda item: str(item.get("recorded_at", "")),
        reverse=True,
    )[: max(1, min(int(max_items), 50))]

    for item in latest:
        age_hours = _age_hours(str(item.get("recorded_at", "")))
        item["age_hours"] = age_hours
        item["stale"] = age_hours is None or age_hours > stale_after_hours

    status = "ok" if latest else "no_evidence"
    return {
        "status": status,
        "evidence_path_configured": str(path),
        "source_category": source_category,
        "pipeline_family": pipeline_family,
        "stale_after_hours": stale_after_hours,
        "evidence_count": len(latest),
        "latest": latest,
        "updated_at": payload.get("updated_at"),
    }


def _sanitize_record(event: str, properties: Mapping[str, Any]) -> dict[str, Any]:
    safe = {
        key: value
        for key, value in properties.items()
        if key in _SAFE_KEYS and _is_primitive(value)
    }
    if not safe.get("pipeline_family") and not safe.get("asset_name") and not safe.get("sensor_name"):
        return {}
    safe["event"] = str(event)[:80]
    safe["recorded_at"] = _now_iso()
    return safe


def _record_key(record: Mapping[str, Any]) -> str:
    name = record.get("asset_name") or record.get("sensor_name") or record.get("event") or "unknown"
    return ":".join(
        [
            str(record.get("source_category") or "unknown"),
            str(record.get("pipeline_family") or "unknown"),
            str(name),
            str(record.get("event") or "unknown"),
        ]
    )


def _read_payload(path: Path) -> dict[str, Any]:
    try:
        with path.open("r", encoding="utf-8") as handle:
            payload = json.load(handle)
            if isinstance(payload, dict):
                payload.setdefault("latest", {})
                payload.setdefault("events", [])
                return payload
    except Exception:
        return _empty_payload()
    return _empty_payload()


def _empty_payload() -> dict[str, Any]:
    return {"updated_at": None, "latest": {}, "events": []}


def _write_record(path: Path, record: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with _evidence_lock(path):
        payload = _read_payload(path)
        events = [dict(record), *payload.get("events", [])][:_MAX_EVENTS]

        latest = payload.get("latest", {})
        latest[_record_key(record)] = dict(record)

        next_payload = {
            "updated_at": _now_iso(),
            "latest": latest,
            "events": events,
        }
        _atomic_write(path, next_payload)


def _atomic_write(path: Path, payload: Mapping[str, Any]) -> None:
    fd, temp_path = tempfile.mkstemp(dir=str(path.parent), prefix=".pipeline_evidence.", suffix=".json")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, sort_keys=True)
        os.replace(temp_path, path)
    finally:
        try:
            os.unlink(temp_path)
        except FileNotFoundError:
            pass


@contextmanager
def _evidence_lock(path: Path):
    lock_path = path.with_suffix(path.suffix + ".lock")
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with lock_path.open("a+") as handle:
        if fcntl is not None:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            if fcntl is not None:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def _read_first_available_payload() -> tuple[Path, dict[str, Any]]:
    candidates = _candidate_evidence_paths()
    for path in candidates:
        if path.exists():
            payload = _read_payload(path)
            if (
                payload.get("updated_at")
                or payload.get("latest")
                or payload.get("events")
            ):
                return path, payload
    return candidates[0], _empty_payload()


def _candidate_evidence_paths() -> list[Path]:
    configured = os.environ.get("PIPELINE_EVIDENCE_PATH")
    if configured and Path(configured) != Path(_DEFAULT_PATH):
        return [Path(configured)]

    candidates = [Path(configured or _DEFAULT_PATH)]
    xdg_cache_home = os.environ.get("XDG_CACHE_HOME")
    if xdg_cache_home:
        candidates.append(Path(xdg_cache_home) / _FALLBACK_RELATIVE_PATH)

    home = os.environ.get("HOME")
    if home:
        candidates.append(Path(home) / ".cache" / _FALLBACK_RELATIVE_PATH)

    deduped: list[Path] = []
    seen: set[str] = set()
    for path in candidates:
        key = str(path)
        if key not in seen:
            seen.add(key)
            deduped.append(path)
    return deduped


def _evidence_path() -> Path:
    return _candidate_evidence_paths()[0]


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _age_hours(value: str) -> float | None:
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return round((datetime.now(timezone.utc) - dt.astimezone(timezone.utc)).total_seconds() / 3600, 3)
    except Exception:
        return None


def _is_primitive(value: Any) -> bool:
    return value is None or isinstance(value, (bool, int, float, str))


def _truthy_env(name: str) -> bool:
    return os.environ.get(name, "").strip().lower() in {"1", "true", "yes", "on"}
