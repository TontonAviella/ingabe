from __future__ import annotations

import importlib.metadata
from typing import Any

import numpy as np


def forge3d_available() -> tuple[bool, str | None, str | None]:
    try:
        import forge3d  # noqa: F401
    except Exception as exc:
        return False, None, str(exc)
    try:
        version = importlib.metadata.version("forge3d")
    except Exception:
        version = None
    return True, version, None


def build_forge3d_impact_layer(
    geojson: dict[str, Any],
    *,
    height_property: str = "risk_score",
    height_scale: float = 45.0,
) -> dict[str, Any]:
    """Construct a Forge3D BuildingLayer from impact polygons when available.

    The web map still renders through MapLibre unless a Forge3D viewer/exporter
    is selected. This function proves the impact output is compatible with the
    Python Forge3D scene model and returns a compact summary for tool results.
    """
    available, version, error = forge3d_available()
    if not available:
        return {
            "available": False,
            "active": False,
            "version": version,
            "error": error,
        }

    try:
        from shapely.geometry import shape
        from forge3d import Building, BuildingLayer
    except Exception as exc:
        return {
            "available": True,
            "active": False,
            "version": version,
            "error": str(exc),
        }

    buildings = []
    features = geojson.get("features") or []
    for idx, feature in enumerate(features):
        geom = feature.get("geometry")
        props = feature.get("properties") or {}
        if not isinstance(geom, dict):
            continue
        poly = shape(geom)
        if poly.geom_type == "MultiPolygon":
            poly = max(poly.geoms, key=lambda part: part.area)
        if poly.geom_type != "Polygon" or poly.is_empty:
            continue
        coords = list(poly.exterior.coords)
        if len(coords) < 4:
            continue
        positions = np.array([[x, y, 0.0] for x, y in coords[:-1]], dtype=np.float32)
        if len(positions) < 3:
            continue
        indices = []
        for tri_idx in range(1, len(positions) - 1):
            indices.append([0, tri_idx, tri_idx + 1])
        value = _num(props.get(height_property), 0.0)
        buildings.append(
            Building(
                id=str(props.get("id") or props.get("sphere_asset_id") or f"impact-{idx + 1}"),
                positions=positions,
                indices=np.array(indices, dtype=np.uint32),
                height=max(0.0, value * height_scale),
                attributes=dict(props),
            )
        )

    layer = BuildingLayer(
        name="impact-extrusions",
        buildings=buildings,
        crs_epsg=4326,
        source_format="geojson",
    )
    return {
        "available": True,
        "active": len(buildings) > 0,
        "version": version,
        "layer_type": "forge3d.BuildingLayer",
        "building_count": len(layer.buildings),
        "height_property": height_property,
        "height_scale": height_scale,
    }


def _num(value: Any, default: float) -> float:
    if value is None:
        return default
    try:
        return float(value)
    except (TypeError, ValueError):
        return default
