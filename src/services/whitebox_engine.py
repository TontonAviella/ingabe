from __future__ import annotations

import importlib.util
from pathlib import Path
import shutil
from typing import Any


CURATED_WHITEBOX_DOMAINS: dict[str, list[str]] = {
    "terrain": [
        "slope",
        "aspect",
        "hillshade",
        "curvature",
        "topographic wetness index",
    ],
    "hydrology": [
        "fill or breach depressions",
        "flow direction",
        "flow accumulation",
        "watershed delineation",
        "stream extraction",
    ],
    "urban_environment": [
        "runoff concentration",
        "flood-prone low points",
        "erosion-prone slopes",
        "drainage-path screening",
        "terrain suitability around buildings and roads",
    ],
    "drone_lidar": [
        "DEM-derived terrain metrics",
        "point-cloud info",
        "point-cloud tiling",
        "ground filtering",
        "surface interpolation",
    ],
}


def whitebox_engine_status() -> dict[str, Any]:
    """Return a truthful runtime status for WhiteboxTools.

    WhiteboxTools is useful to Ingabe as an analytical backend, especially for
    terrain/hydrology/drone-derived surfaces. It is not a map renderer, so the
    frontend should still visualize outputs through MapLibre/rasterd/PMTiles.
    """

    python_spec = importlib.util.find_spec("whitebox")
    cli_path = shutil.which("whitebox_tools") or shutil.which("whitebox_tools.exe")
    package_binary_path = _package_binary_path(python_spec)
    binary_path = cli_path or package_binary_path

    frontend_installed = python_spec is not None
    executable_ready = binary_path is not None
    installed = frontend_installed or executable_ready
    status: dict[str, Any] = {
        "installed": installed,
        "python_frontend_installed": frontend_installed,
        "executable_ready": executable_ready,
        "binary_path": binary_path,
        "cli_path": cli_path,
        "engine_role": "analysis_backend",
        "map_renderer": False,
        "active_for": (
            [
                "terrain analysis",
                "hydrology/runoff analysis",
                "drone DEM/LiDAR analysis",
                "urban and environmental risk screening",
            ]
            if executable_ready
            else []
        ),
        "recommended_integration": (
            "Expose a curated Whitebox runner behind Sage/Hermes and render "
            "outputs as H3, raster, or vector layers through the existing map."
        ),
        "curated_domains": CURATED_WHITEBOX_DOMAINS,
    }

    if python_spec is None and binary_path is None:
        status["error"] = (
            "WhiteboxTools is not installed in this runtime. Add the whitebox "
            "Python frontend or a whitebox_tools binary sidecar before enabling "
            "Whitebox-backed Sage tools."
        )
    elif not executable_ready:
        status["error"] = (
            "The whitebox Python frontend is installed, but the WhiteboxTools "
            "executable is not present yet. Bake/download the binary before "
            "enabling Whitebox-backed Sage tools."
        )
    else:
        status["note"] = (
            "WhiteboxTools should run as a job/sidecar for expensive analysis; "
            "do not expose the whole catalog directly to the LLM."
        )

    return status


def _package_binary_path(python_spec: Any) -> str | None:
    if python_spec is None or not python_spec.submodule_search_locations:
        return None

    package_dir = Path(next(iter(python_spec.submodule_search_locations)))
    candidates = [
        package_dir / "WBT" / "whitebox_tools",
        package_dir / "whitebox_tools",
        package_dir / "WBT" / "whitebox_tools.exe",
        package_dir / "whitebox_tools.exe",
    ]
    for candidate in candidates:
        if candidate.is_file():
            if candidate.suffix == ".py":
                continue
            return str(candidate)
    return None
