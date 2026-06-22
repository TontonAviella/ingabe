"""GeoLibre-Rust/WASM execution bridge for Ingabe geospatial workflows.

This module is the runtime bridge between Sage/Dagster and ``geolibre_wasm``.
GeoLibre exposes the full Whitebox + GeoLibre manifest catalog; callers should
choose tools intentionally, while this service handles runtime discovery,
bounded output summaries, and privacy-safe proof events.
"""

from __future__ import annotations

import base64
import hashlib
import importlib.metadata
import importlib.util
import json
import logging
import os
import tempfile
import time
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path
from typing import Any, Mapping

from src.services.pipeline_evidence import record_pipeline_evidence
from src.services.posthog_analytics import capture_backend_event, elapsed_ms

logger = logging.getLogger(__name__)

GEOLIBRE_BACKEND = "geolibre_wasm"
DEFAULT_MAX_OUTPUT_BYTES = 32 * 1024 * 1024


@dataclass(frozen=True)
class GeolibreRunInput:
    tool_id: str
    args: list[str]
    input_files: Mapping[str, bytes | str]
    source_category: str
    pipeline_family: str
    analysis_domain: str
    evidence_kind: str
    max_output_bytes: int = DEFAULT_MAX_OUTPUT_BYTES
    distinct_id: str = "geolibre-runner"
    context: Mapping[str, Any] = field(default_factory=dict)


def geolibre_runner_status(include_manifest_sample: bool = False) -> dict[str, Any]:
    """Return current GeoLibre-WASM runtime status without claiming a run happened."""

    package = _python_package_status("geolibre_wasm", "geolibre-wasm")
    installed = bool(package["installed"])
    status: dict[str, Any] = {
        "installed": installed,
        "package": package,
        "backend": GEOLIBRE_BACKEND,
        "runtime": "WASI via wasmtime",
        "active_for": [
            "Whitebox/GeoLibre geoprocessing manifests",
            "raster/DEM/satellite COG processing",
            "vector/Open Buildings conversion and cleanup",
            "GeoParquet/PMTiles/XYZ outputs",
            "terrain/hydrology/LiDAR tools",
        ]
        if installed
        else [],
    }
    if not installed:
        status["error"] = "geolibre_wasm is not installed in this runtime."
        return status

    try:
        _ensure_geolibre_cache_env()
        import geolibre_wasm as gl

        tools = gl.list_tools()
        status["tool_count"] = len(tools)
        status["runtime_path"] = gl.runtime_path()
        status["runtime_cached"] = Path(str(status["runtime_path"])).is_file()
        if include_manifest_sample:
            manifests = _manifest_by_id()
            status["sample_tools"] = [
                _compact_manifest(manifests[tool])
                for tool in (
                    "spectral_index",
                    "write_geoparquet",
                    "raster_to_tiles",
                    "write_pmtiles",
                    "slope",
                    "extract_sinks",
                )
                if tool in manifests
            ]
    except Exception as exc:
        status["error"] = str(exc)
    return status


def list_geolibre_tool_manifests(
    *,
    search: str = "",
    source: str = "",
    category: str = "",
    limit: int = 50,
) -> dict[str, Any]:
    """List compact GeoLibre/Whitebox manifests for discovery and routing."""

    manifests = list(_manifest_by_id().values())
    needle = search.strip().lower()
    source_norm = source.strip().lower()
    category_norm = category.strip().lower()

    def matches(manifest: Mapping[str, Any]) -> bool:
        if source_norm and str(manifest.get("source", "")).lower() != source_norm:
            return False
        if category_norm and str(manifest.get("category", "")).lower() != category_norm:
            return False
        if not needle:
            return True
        haystack = " ".join(
            str(manifest.get(key) or "")
            for key in ("id", "name", "description", "category", "source")
        ).lower()
        return needle in haystack

    filtered = [_compact_manifest(manifest) for manifest in manifests if matches(manifest)]
    capped = max(1, min(int(limit), 200))
    return {
        "status": "success",
        "backend": GEOLIBRE_BACKEND,
        "tool_count": len(manifests),
        "matched_count": len(filtered),
        "returned_count": min(len(filtered), capped),
        "tools": filtered[:capped],
    }


def run_geolibre_tool(payload: GeolibreRunInput) -> dict[str, Any]:
    """Run one GeoLibre/Whitebox tool and emit proof events.

    The response intentionally returns file summaries, not raw output bytes.
    Callers that need bytes should invoke the service directly in a controlled
    job and persist outputs to S3/PostGIS/GeoParquet/PMTiles.
    """

    started_at = time.monotonic()
    manifest = _manifest_for_tool(payload.tool_id)
    result: dict[str, Any]

    try:
        import geolibre_wasm as gl

        normalized_input = _normalize_input_files(payload.input_files)
        run = gl.run_tool(
            payload.tool_id,
            args=list(payload.args),
            input=normalized_input,
        )
        file_summaries, output_bytes, omitted = _summarize_output_files(
            run.files,
            max_output_bytes=payload.max_output_bytes,
        )
        status = "success" if int(run.exit_code) == 0 else "error"
        result = {
            "status": status,
            "backend": GEOLIBRE_BACKEND,
            "tool_id": payload.tool_id,
            "tool_source": manifest.get("source"),
            "tool_category": manifest.get("category"),
            "exit_code": int(run.exit_code),
            "stdout": _trim_stdout(run.stdout),
            "args_count": len(payload.args),
            "input_file_count": len(normalized_input),
            "output_file_count": len(run.files),
            "output_bytes": output_bytes,
            "output_files": file_summaries,
            "output_files_omitted": omitted,
            "elapsed_ms": elapsed_ms(started_at),
        }
    except Exception as exc:
        logger.exception("GeoLibre tool run failed for %s", payload.tool_id)
        result = {
            "status": "error",
            "backend": GEOLIBRE_BACKEND,
            "tool_id": payload.tool_id,
            "tool_source": manifest.get("source"),
            "tool_category": manifest.get("category"),
            "exit_code": None,
            "error_type": type(exc).__name__,
            "error": str(exc)[:500],
            "args_count": len(payload.args),
            "input_file_count": len(payload.input_files),
            "output_file_count": 0,
            "output_bytes": 0,
            "elapsed_ms": elapsed_ms(started_at),
        }

    _capture_geolibre_evidence(result, payload=payload)
    return result


def run_geolibre_smoke_suite() -> dict[str, Any]:
    """Exercise GeoLibre with small Open Buildings-like vector and satellite raster inputs."""

    started_at = time.monotonic()
    status = geolibre_runner_status(include_manifest_sample=True)
    if not status.get("installed"):
        suite = {
            "status": "error",
            "backend": GEOLIBRE_BACKEND,
            "error": status.get("error", "geolibre_wasm unavailable"),
        }
        _capture_geolibre_suite_evidence(suite, started_at)
        return suite

    vector_result = run_geolibre_tool(
        GeolibreRunInput(
            tool_id="write_geoparquet",
            args=[
                "--input=/work/open_buildings.geojson",
                "--output=/work/open_buildings.parquet",
                "--compression=zstd",
            ],
            input_files={
                "open_buildings.geojson": json.dumps(_sample_open_buildings_geojson()).encode(
                    "utf-8"
                )
            },
            source_category="open_buildings",
            pipeline_family="open_buildings_geolibre",
            analysis_domain="housing",
            evidence_kind="open_buildings_vector_conversion",
            context={"geolibre_workflow": "smoke_suite"},
        )
    )

    raster_result = run_geolibre_tool(
        GeolibreRunInput(
            tool_id="spectral_index",
            args=[
                "--input=/work/sentinel_multiband.tif",
                "--index=ndvi",
                "--red=1",
                "--nir=2",
                "--output=/work/ndvi.tif",
            ],
            input_files={"sentinel_multiband.tif": _sample_satellite_geotiff()},
            source_category="satellite",
            pipeline_family="satellite_geolibre",
            analysis_domain="agriculture",
            evidence_kind="satellite_spectral_index",
            context={"geolibre_workflow": "smoke_suite"},
        )
    )

    runs = [vector_result, raster_result]
    success_count = sum(1 for item in runs if item.get("status") == "success")
    suite = {
        "status": "success" if success_count == len(runs) else "error",
        "backend": GEOLIBRE_BACKEND,
        "tool_count": status.get("tool_count"),
        "runtime_cached": status.get("runtime_cached"),
        "runtime_path_present": bool(status.get("runtime_path")),
        "sample_workflow_count": len(runs),
        "sample_success_count": success_count,
        "workflows": [
            {
                "tool_id": item.get("tool_id"),
                "source_category": source,
                "status": item.get("status"),
                "output_file_count": item.get("output_file_count"),
                "output_bytes": item.get("output_bytes"),
            }
            for item, source in zip(runs, ("open_buildings", "satellite"))
        ],
        "elapsed_ms": elapsed_ms(started_at),
    }
    _capture_geolibre_suite_evidence(suite, started_at)
    return suite


def _python_package_status(import_name: str, package_name: str) -> dict[str, Any]:
    spec = importlib.util.find_spec(import_name)
    installed = spec is not None
    version = None
    if installed:
        try:
            version = importlib.metadata.version(package_name)
        except importlib.metadata.PackageNotFoundError:
            version = None
    return {"installed": installed, "version": version}


def _ensure_geolibre_cache_env() -> None:
    if os.environ.get("XDG_CACHE_HOME"):
        return

    default_cache = Path.home() / ".cache"
    try:
        default_cache.mkdir(parents=True, exist_ok=True)
        probe = default_cache / ".ingabe-geolibre-write-test"
        probe.write_text("ok", encoding="utf-8")
        probe.unlink(missing_ok=True)
        return
    except OSError:
        fallback = Path(
            os.environ.get("GEOLIBRE_CACHE_HOME", "/tmp/ingabe-geolibre-cache")
        )
        fallback.mkdir(parents=True, exist_ok=True)
        os.environ["XDG_CACHE_HOME"] = str(fallback)


@lru_cache(maxsize=1)
def _manifest_by_id() -> dict[str, dict[str, Any]]:
    _ensure_geolibre_cache_env()
    import geolibre_wasm as gl

    manifests = gl.list_manifests()
    by_id: dict[str, dict[str, Any]] = {}
    for manifest in manifests:
        tool_id = str(manifest.get("id") or manifest.get("name") or "")
        if tool_id:
            by_id[tool_id] = dict(manifest)
    return by_id


def _manifest_for_tool(tool_id: str) -> dict[str, Any]:
    normalized = str(tool_id or "").strip()
    manifests = _manifest_by_id()
    if normalized not in manifests:
        raise ValueError(f"Unknown GeoLibre tool_id '{tool_id}'.")
    return manifests[normalized]


def _compact_manifest(manifest: Mapping[str, Any]) -> dict[str, Any]:
    params = manifest.get("parameters") or manifest.get("params") or []
    return {
        "id": manifest.get("id"),
        "name": manifest.get("name") or manifest.get("id"),
        "source": manifest.get("source"),
        "category": manifest.get("category"),
        "description": str(manifest.get("description") or "")[:240],
        "parameter_count": len(params) if isinstance(params, list) else 0,
        "parameters": [
            {
                "name": param.get("name"),
                "io_role": param.get("io_role"),
                "data_kind": param.get("data_kind"),
            }
            for param in params[:12]
            if isinstance(param, dict)
        ],
    }


def _normalize_input_files(
    input_files: Mapping[str, bytes | str],
) -> dict[str, bytes | str]:
    normalized: dict[str, bytes | str] = {}
    for raw_name, value in input_files.items():
        name = str(raw_name).strip().lstrip("/")
        if not name or name.startswith("..") or "/../" in name:
            raise ValueError(f"Invalid GeoLibre input filename '{raw_name}'.")
        if isinstance(value, bytes):
            normalized[name] = value
        elif isinstance(value, str):
            normalized[name] = value
        else:
            raise ValueError(f"Input '{name}' must be bytes or a string URL/path.")
    return normalized


def _summarize_output_files(
    files: Mapping[str, bytes],
    *,
    max_output_bytes: int,
) -> tuple[list[dict[str, Any]], int, int]:
    summaries: list[dict[str, Any]] = []
    total = 0
    omitted = 0
    for path, data in sorted(files.items()):
        size = len(data)
        total += size
        if total <= max_output_bytes:
            summaries.append(
                {
                    "path": path,
                    "size_bytes": size,
                    "sha256": hashlib.sha256(data).hexdigest()[:16],
                    "media_kind": _media_kind(path),
                }
            )
        else:
            omitted += 1
    return summaries, total, omitted


def _media_kind(path: str) -> str:
    suffix = Path(path).suffix.lower()
    if suffix in {".tif", ".tiff"}:
        return "raster_geotiff"
    if suffix in {".parquet"}:
        return "geoparquet"
    if suffix in {".geojson", ".json"}:
        return "vector_geojson"
    if suffix in {".pmtiles"}:
        return "pmtiles"
    if suffix in {".png", ".jpg", ".jpeg", ".webp"}:
        return "image"
    if suffix in {".csv"}:
        return "table"
    return "file"


def _trim_stdout(stdout: Any) -> list[str]:
    if isinstance(stdout, str):
        lines = stdout.splitlines()
    elif isinstance(stdout, list):
        lines = [str(item) for item in stdout]
    else:
        lines = []
    return [line[:240] for line in lines[-20:]]


def _capture_geolibre_evidence(
    result: Mapping[str, Any],
    *,
    payload: GeolibreRunInput,
) -> None:
    success = result.get("status") == "success"
    props = {
        **{
            key: value
            for key, value in payload.context.items()
            if isinstance(value, (str, int, float, bool))
        },
        "backend": GEOLIBRE_BACKEND,
        "pipeline_family": payload.pipeline_family,
        "source_category": payload.source_category,
        "analysis_domain": payload.analysis_domain,
        "evidence_kind": payload.evidence_kind,
        "status": result.get("status"),
        "success": success,
        "tool_id": result.get("tool_id"),
        "tool_source": result.get("tool_source"),
        "tool_category": result.get("tool_category"),
        "exit_code": result.get("exit_code"),
        "input_file_count": result.get("input_file_count"),
        "output_file_count": result.get("output_file_count"),
        "output_bytes": result.get("output_bytes"),
        "elapsed_ms": result.get("elapsed_ms"),
    }
    record_pipeline_evidence("geolibre_tool_completed", props)
    record_pipeline_evidence("geospatial_pipeline_flow_completed", props)
    capture_backend_event(
        "geolibre_tool_completed",
        distinct_id=payload.distinct_id,
        properties=props,
        groups={"pipeline": payload.pipeline_family},
    )
    capture_backend_event(
        "geospatial_pipeline_flow_completed",
        distinct_id=payload.distinct_id,
        properties=props,
        groups={"pipeline": payload.pipeline_family},
    )


def _capture_geolibre_suite_evidence(
    suite: Mapping[str, Any],
    started_at: float,
) -> None:
    props = {
        "backend": GEOLIBRE_BACKEND,
        "pipeline_family": "geolibre_runtime",
        "source_category": "geolibre",
        "analysis_domain": "platform",
        "evidence_kind": "runtime_smoke",
        "status": suite.get("status"),
        "success": suite.get("status") == "success",
        "tool_count": suite.get("tool_count"),
        "sample_workflow_count": suite.get("sample_workflow_count"),
        "sample_success_count": suite.get("sample_success_count"),
        "elapsed_ms": suite.get("elapsed_ms") or elapsed_ms(started_at),
    }
    record_pipeline_evidence("geolibre_runtime_probe_completed", props)
    record_pipeline_evidence("geospatial_pipeline_flow_completed", props)
    capture_backend_event(
        "geolibre_runtime_probe_completed",
        distinct_id="geolibre-runner",
        properties=props,
        groups={"pipeline": "geolibre_runtime"},
    )
    capture_backend_event(
        "geospatial_pipeline_flow_completed",
        distinct_id="geolibre-runner",
        properties=props,
        groups={"pipeline": "geolibre_runtime"},
    )


def _sample_open_buildings_geojson() -> dict[str, Any]:
    return {
        "type": "FeatureCollection",
        "features": [
            {
                "type": "Feature",
                "geometry": {
                    "type": "Polygon",
                    "coordinates": [
                        [
                            [30.0599, -1.9501],
                            [30.0601, -1.9501],
                            [30.0601, -1.9499],
                            [30.0599, -1.9499],
                            [30.0599, -1.9501],
                        ]
                    ],
                },
                "properties": {
                    "source": "open_buildings_v3",
                    "confidence": 0.91,
                    "area_in_meters": 84.5,
                },
            }
        ],
    }


def _sample_satellite_geotiff() -> bytes:
    import numpy as np
    import rasterio
    from rasterio.transform import from_origin

    with tempfile.NamedTemporaryFile(suffix=".tif", delete=False) as tmp:
        path = tmp.name
    try:
        red = np.full((5, 5), 1000, dtype="uint16")
        nir = np.full((5, 5), 3000, dtype="uint16")
        green = np.full((5, 5), 800, dtype="uint16")
        blue = np.full((5, 5), 500, dtype="uint16")
        with rasterio.open(
            path,
            "w",
            driver="GTiff",
            height=5,
            width=5,
            count=4,
            dtype="uint16",
            crs="EPSG:4326",
            transform=from_origin(30.0, -1.0, 0.0001, 0.0001),
        ) as dst:
            dst.write(red, 1)
            dst.write(nir, 2)
            dst.write(green, 3)
            dst.write(blue, 4)
        return Path(path).read_bytes()
    finally:
        try:
            os.unlink(path)
        except OSError:
            pass


def decode_inline_file_value(value: str) -> bytes | str:
    """Decode a JSON tool input value into bytes, URL, or plain text bytes."""

    text = str(value)
    if text.startswith(("http://", "https://")):
        return text
    if text.startswith("base64:"):
        return base64.b64decode(text.removeprefix("base64:"))
    return text.encode("utf-8")
