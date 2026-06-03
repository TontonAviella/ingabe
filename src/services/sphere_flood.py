from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

import numpy as np


SQM_TO_SQFT = 10.7639104167
M_TO_FT = 3.280839895


@dataclass(frozen=True)
class SphereFloodInput:
    exposure_geojson: str
    flood_depth_m: float
    flood_type: str
    default_occupancy: str
    default_building_value_usd: float
    default_content_value_usd: float
    default_area_m2: float
    default_first_floor_height_m: float


class ConstantDepthRaster:
    def __init__(self, depth_ft: float):
        self.depth_ft = depth_ft

    def get_value_vectorized(self, geometry):
        return np.full(len(geometry), self.depth_ft, dtype=float)


def sphere_available() -> tuple[bool, str | None]:
    try:
        import sphere.core  # noqa: F401
        import sphere.flood  # noqa: F401
    except Exception as exc:
        return False, str(exc)
    return True, None


def analyze_sphere_flood_impact(payload: SphereFloodInput) -> dict[str, Any]:
    """Run HAZUS-style Sphere flood loss against asset exposure GeoJSON."""
    available, error = sphere_available()
    if not available:
        return {
            "status": "unavailable",
            "engine": "sphere",
            "error": error or "Sphere packages are not installed",
            "install_hint": "Install niyamit-sphere==0.2.0 with sphere-core, sphere-data, and sphere-flood.",
        }

    try:
        import geopandas as gpd
        import pandas as pd
        from shapely.geometry import shape
        from sphere.core.schemas.buildings import Buildings
        from sphere.flood.analysis.hazus_flood import HazusFloodAnalysis
        from sphere.flood.default_vulnerability import DefaultFloodVulnerability
    except Exception as exc:
        return {
            "status": "unavailable",
            "engine": "sphere",
            "error": str(exc),
            "install_hint": "Sphere is present but its geospatial dependencies could not be imported.",
        }

    features = _load_features(payload.exposure_geojson)
    if not features:
        return {
            "status": "error",
            "engine": "sphere",
            "error": "exposure_geojson must contain at least one Feature",
        }

    rows: list[dict[str, Any]] = []
    original_geometries = []
    for idx, feature in enumerate(features):
        geometry = feature.get("geometry")
        if not isinstance(geometry, dict):
            continue
        geom = shape(geometry)
        point = geom if geom.geom_type == "Point" else geom.representative_point()
        props = dict(feature.get("properties") or {})
        rows.append(
            {
                "id": str(props.get("id") or props.get("asset_id") or f"asset-{idx + 1}"),
                "occupancy_type": str(props.get("occupancy_type") or payload.default_occupancy),
                "first_floor_height": _num(props.get("first_floor_height_ft"), payload.default_first_floor_height_m * M_TO_FT),
                "foundation_type": int(_num(props.get("foundation_type"), 7)),
                "number_stories": _num(props.get("number_stories"), 1),
                "area": _num(props.get("area_sqft"), _num(props.get("area_m2"), payload.default_area_m2) * SQM_TO_SQFT),
                "building_cost": _num(props.get("building_value_usd"), payload.default_building_value_usd),
                "content_cost": _num(props.get("content_value_usd"), payload.default_content_value_usd),
                "inventory_cost": _num(props.get("inventory_value_usd"), 0),
                "geometry": point,
                "_original_properties": props,
            }
        )
        original_geometries.append(geom)

    if not rows:
        return {
            "status": "error",
            "engine": "sphere",
            "error": "No valid geometries found in exposure_geojson",
        }

    gdf = gpd.GeoDataFrame(rows, geometry="geometry", crs="EPSG:4326")
    buildings = Buildings(gdf)
    vulnerability = DefaultFloodVulnerability(buildings=buildings, flood_type=payload.flood_type)
    analysis = HazusFloodAnalysis(
        buildings=buildings,
        vulnerability_func=vulnerability,
        depth_grid=ConstantDepthRaster(payload.flood_depth_m * M_TO_FT),
    )
    analysis.calculate_losses()

    result_gdf = buildings.gdf
    output_features: list[dict[str, Any]] = []
    total_loss = 0.0
    max_damage = 0.0
    for idx, row in result_gdf.reset_index(drop=True).iterrows():
        props = dict(row.get("_original_properties") or {})
        building_loss = _num(row.get("building_loss"), 0)
        content_loss = _num(row.get("content_loss"), 0)
        inventory_loss = _num(row.get("inventory_loss"), 0)
        total_asset_loss = building_loss + content_loss + inventory_loss
        damage_percent = _num(row.get("building_damage_percent"), 0)
        total_loss += total_asset_loss
        max_damage = max(max_damage, damage_percent)
        props.update(
            {
                "sphere_asset_id": row.get("id"),
                "flood_depth_m": round(payload.flood_depth_m, 3),
                "depth_in_structure_ft": round(_num(row.get("depth_in_structure"), 0), 3),
                "building_damage_percent": round(damage_percent, 2),
                "content_damage_percent": round(_num(row.get("content_damage_percent"), 0), 2),
                "building_loss_usd": round(building_loss, 2),
                "content_loss_usd": round(content_loss, 2),
                "inventory_loss_usd": round(inventory_loss, 2),
                "total_loss_usd": round(total_asset_loss, 2),
                "debris_total_tons": round(_num(row.get("debris_total"), 0), 3),
                "restoration_min_days": round(_num(row.get("restoration_minimum"), 0), 1),
                "restoration_max_days": round(_num(row.get("restoration_maximum"), 0), 1),
                "risk_score": round(min(100.0, damage_percent), 1),
            }
        )
        output_features.append(
            {
                "type": "Feature",
                "geometry": original_geometries[idx].__geo_interface__,
                "properties": props,
            }
        )

    return {
        "status": "success",
        "engine": "sphere",
        "method": "HAZUS-style flood vulnerability/loss via niyamit-sphere",
        "summary": {
            "asset_count": len(output_features),
            "flood_depth_m": payload.flood_depth_m,
            "total_loss_usd": round(total_loss, 2),
            "max_building_damage_percent": round(max_damage, 2),
        },
        "geojson": {
            "type": "FeatureCollection",
            "features": output_features,
        },
        "map": {
            "style_hint": "sphere_flood_damage",
            "height_property": "risk_score",
            "height_scale": 45,
        },
    }


def _load_features(raw_geojson: str) -> list[dict[str, Any]]:
    data = json.loads(raw_geojson)
    if data.get("type") == "FeatureCollection":
        return [feature for feature in data.get("features", []) if isinstance(feature, dict)]
    if data.get("type") == "Feature":
        return [data]
    raise ValueError("exposure_geojson must be a Feature or FeatureCollection")


def _num(value: Any, default: float) -> float:
    if value is None:
        return float(default)
    try:
        if isinstance(value, str) and not value.strip():
            return float(default)
        out = float(value)
    except (TypeError, ValueError):
        return float(default)
    if np.isnan(out):
        return float(default)
    return out
