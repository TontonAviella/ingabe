from __future__ import annotations

import importlib.metadata
import importlib.util
import os
from pathlib import Path
from typing import Any

import httpx

from src.services.forge3d_adapter import forge3d_available
from src.services.geolibre_runner import geolibre_runner_status
from src.services.sphere_flood import sphere_available
from src.services.tessera_embeddings import tessera_embedding_status
from src.services.whitebox_engine import whitebox_engine_status


async def get_spatial_engine_capabilities(
    include_rasterd: bool = True,
    include_geokernel: bool = True,
    include_whitebox: bool = True,
    include_tessera: bool = True,
) -> dict[str, Any]:
    sphere_ok, sphere_error = sphere_available()
    forge_ok, forge_version, forge_error = forge3d_available()
    capabilities: dict[str, Any] = {
        "sphere": {
            "installed": sphere_ok,
            "active_for": ["flood asset damage/loss"] if sphere_ok else [],
            "error": sphere_error,
        },
        "forge3d_python": {
            "installed": forge_ok,
            "version": forge_version,
            "active_for": ["impact extrusion scene model"] if forge_ok else [],
            "error": forge_error,
        },
        "map_runtime": {
            "browser_map": "MapLibre/deck.gl",
            "geojson_3d_extrusion": True,
            "note": "Browser map extrusion remains MapLibre/deck.gl unless a Forge3D viewer/export path is selected.",
        },
        "geolibre_wasm": _geolibre_wasm_status(),
    }
    if include_whitebox:
        capabilities["whitebox_tools"] = whitebox_engine_status()
    if include_tessera:
        capabilities["tessera_embeddings"] = tessera_embedding_status()
    if include_rasterd:
        capabilities["rasterd"] = await _rasterd_status()
    if include_geokernel:
        capabilities["geokernel"] = await _geokernel_status()
    return capabilities


def _python_package_status(
    import_name: str,
    package_name: str | None = None,
) -> dict[str, Any]:
    spec = importlib.util.find_spec(import_name)
    installed = spec is not None
    version = None
    if installed:
        try:
            version = importlib.metadata.version(package_name or import_name)
        except importlib.metadata.PackageNotFoundError:
            version = None
    return {
        "installed": installed,
        "version": version,
    }


def _geolibre_wasm_status() -> dict[str, Any]:
    package = _python_package_status("geolibre_wasm", "geolibre-wasm")
    cache_dir = Path(os.environ.get("XDG_CACHE_HOME") or (Path.home() / ".cache")) / "geolibre"
    runtime_cached = bool(list(cache_dir.glob("geolibre-cli-*.wasm"))) if cache_dir.exists() else False
    installed = bool(package["installed"])
    status = {
        "installed": installed,
        "package": package,
        "runtime_cached": runtime_cached,
        "active_for": (
            [
                "browser/Python WASM geoprocessing",
                "GeoParquet read/write",
                "COG/raster rendering",
                "spectral indices",
                "terrain/hydrology/LiDAR tools",
                "PMTiles/XYZ raster tiles",
            ]
            if installed
            else []
        ),
        "note": (
            "GeoLibre-WASM is a geoprocessing/runtime tool suite, not a vision model. "
            "It helps prepare, render, convert, tile, and post-process raster/vector data."
        ),
    }
    if installed:
        try:
            runner = geolibre_runner_status(include_manifest_sample=False)
            for key in ("tool_count", "runtime_path", "runtime_cached", "error"):
                if key in runner:
                    status[key] = runner[key]
        except Exception as exc:
            status["error"] = str(exc)
    return status


async def _rasterd_status() -> dict[str, Any]:
    base_url = os.environ.get("RASTER_TILE_ENGINE_URL")
    if not base_url:
        return {
            "configured": False,
            "reachable": False,
            "engine": None,
            "forge3d_cog_feature": None,
        }
    try:
        async with httpx.AsyncClient(timeout=2.0) as client:
            response = await client.get(f"{base_url.rstrip('/')}/healthz")
            response.raise_for_status()
            data = response.json()
    except Exception as exc:
        return {
            "configured": True,
            "reachable": False,
            "engine": None,
            "forge3d_cog_feature": None,
            "error": str(exc),
        }
    return {
        "configured": True,
        "reachable": True,
        "engine": data.get("engine"),
        "renderer": data.get("renderer"),
        "forge3d_cog_feature": data.get("forge3d_cog_feature", data.get("forge3d_compiled")),
        "forge3d_runtime": data.get("forge3d_runtime"),
        "raw": data,
    }


async def _geokernel_status() -> dict[str, Any]:
    base_url = os.environ.get("GEOKERNEL_URL")
    if not base_url:
        return {
            "configured": False,
            "reachable": False,
            "engine": None,
            "active_for": [],
        }
    try:
        async with httpx.AsyncClient(timeout=2.0) as client:
            response = await client.get(f"{base_url.rstrip('/')}/healthz")
            response.raise_for_status()
            data = response.json()
    except Exception as exc:
        return {
            "configured": True,
            "reachable": False,
            "engine": None,
            "active_for": [],
            "error": str(exc),
        }
    return {
        "configured": True,
        "reachable": True,
        "engine": data.get("engine"),
        "active_for": ["admin H3 overlap acceleration"],
        "capabilities": data.get("capabilities", []),
        "geometry_engine": data.get("geometry_engine"),
        "robust_kernel_available": data.get("robust_kernel_available"),
        "raw": data,
    }
