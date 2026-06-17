from __future__ import annotations

import importlib.metadata
import sys
from typing import Any


def tessera_embedding_status() -> dict[str, Any]:
    """Report TESSERA/GeoTessera availability without overstating usage."""

    geotessera = _package_version("geotessera")
    app_python_minor = f"{sys.version_info.major}.{sys.version_info.minor}"
    return {
        "engine_role": "satellite_embedding_memory",
        "map_renderer": False,
        "model_family": "TESSERA",
        "installed": geotessera is not None,
        "python_package": "geotessera",
        "installed_version": geotessera,
        "app_python_version": ".".join(map(str, sys.version_info[:3])),
        "latest_known_package_note": (
            f"GeoTessera 0.9.x requires Python >=3.12; this app image is Python {app_python_minor}. "
            "Use a 3.11-compatible GeoTessera release or access precomputed embeddings "
            "through cached files/services."
        ),
        "active_for": [
            "satellite land-pattern embeddings",
            "place similarity",
            "annual land/environment change screening",
            "H3/admin feature enrichment",
        ]
        if geotessera
        else [],
        "not_active_for": [
            "building footprint extraction",
            "road/drainage object detection",
            "drone-resolution damage segmentation",
            "browser map rendering",
        ],
        "recommended_runtime_path": (
            "Precompute/copy TESSERA embeddings for the AOI, aggregate to H3/admin "
            "features, store locally, and keep Sage live calls on cached vectors."
        ),
    }


def _package_version(name: str) -> str | None:
    try:
        return importlib.metadata.version(name)
    except importlib.metadata.PackageNotFoundError:
        return None
