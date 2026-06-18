from __future__ import annotations

import json
import math
from dataclasses import dataclass
from typing import Any

import h3
from shapely.geometry import shape
from shapely.geometry.base import BaseGeometry

from src.services.whitebox_engine import whitebox_engine_status

_EVIDENCE_FACTOR_KEYS = {
    "rainfall_mm_24h",
    "rain_24h_mm",
    "rainfall_mm_72h",
    "rain_72h_mm",
    "soil_saturation",
    "flood_depth_m",
    "water_depth_m",
    "flood_index",
    "flood_risk",
    "slope_degrees",
    "slope_mean",
    "slope",
    "imperviousness",
    "built_up_fraction",
    "sealed_surface_fraction",
    "pollution_index",
    "environmental_stress",
    "heat_index_c",
    "temperature_c",
    "ndvi_stress",
    "drainage_deficit",
    "runoff_index",
    "wetness_index",
}


@dataclass(frozen=True)
class H3SpatialInsightInput:
    location_label: str
    bbox: list[float]
    h3_resolution: int
    domain: str
    analysis_goal: str
    risk_factors_json: str
    exposure_geojson: str
    max_hexes: int


def create_h3_spatial_insight(payload: H3SpatialInsightInput) -> dict[str, Any]:
    """Create an H3-indexed risk/insight layer for map-first analysis.

    This is intentionally H3-first: every output feature is a uniform hex cell
    with stable properties for rendering and follow-up analysis. WhiteboxTools
    feeds this contract later by supplying slope, flow, wetness, or terrain
    factors per cell.
    """

    _validate_payload(payload)
    factors = _load_risk_factors(payload.risk_factors_json)
    exposure_geoms = _load_exposure_geometries(payload.exposure_geojson)
    evidence = _evidence_summary(factors, exposure_geoms)
    if not evidence["has_evidence"]:
        return {
            "status": "error",
            "error": (
                "A spatial risk map needs at least one real evidence source: "
                "detected buildings/roads/farms/assets, rain/flood/drainage/slope/terrain "
                "metrics, drone-derived measurements, or other observed factors. "
                "No layer was created from basemap imagery alone."
            ),
            "required_evidence_examples": [
                "Open Buildings or local building footprints for housing exposure",
                "roads, culverts, drains, farms, or assets as exposure geometry",
                "forecast rain, flood depth, wetness, slope, runoff, or drone-derived measurements",
            ],
        }
    cells = sorted(h3.geo_to_cells(_bbox_polygon(payload.bbox), res=payload.h3_resolution))
    if len(cells) > payload.max_hexes:
        return {
            "status": "error",
            "error": (
                f"H3 resolution {payload.h3_resolution} generated {len(cells)} cells, "
                f"above max_hexes={payload.max_hexes}. Use a coarser resolution or smaller bbox."
            ),
        }

    features: list[dict[str, Any]] = []
    scores: list[float] = []
    exposed_cells = 0

    for h3_index in cells:
        geometry = h3_cell_geojson_geometry(h3_index)
        hex_geom = shape(geometry)
        exposure_count = _count_exposures(hex_geom, exposure_geoms)
        if exposure_count:
            exposed_cells += 1

        risk_score = _score_cell(
            exposure_count=exposure_count,
            domain=payload.domain,
            factors=factors,
        )
        scores.append(risk_score)
        issue = _likely_issue(payload.domain, risk_score, factors)
        features.append(
            {
                "type": "Feature",
                "geometry": geometry,
                "properties": {
                    "h3_index": h3_index,
                    "h3_resolution": payload.h3_resolution,
                    "domain": _normalize_domain(payload.domain),
                    "analysis_goal": payload.analysis_goal,
                    "risk_score": round(risk_score, 1),
                    "risk_level": _risk_level(risk_score),
                    "likely_issue": issue,
                    "recommended_action": _recommended_action(payload.domain, risk_score, issue),
                    "exposure_count": exposure_count,
                    "has_exposure": exposure_count > 0,
                    "evidence_level": _cell_evidence_level(exposure_count, evidence),
                    "evidence_basis": evidence["label"],
                    "confidence": _cell_confidence(exposure_count, evidence),
                    "screening_model": "h3_spatial_insight_v1",
                },
            }
        )

    scores = scores or [0.0]
    whitebox = whitebox_engine_status()
    top_cells = sorted(
        (
            {
                "h3_index": f["properties"]["h3_index"],
                "risk_score": f["properties"]["risk_score"],
                "risk_level": f["properties"]["risk_level"],
                "likely_issue": f["properties"]["likely_issue"],
                "exposure_count": f["properties"]["exposure_count"],
            }
            for f in features
        ),
        key=lambda row: row["risk_score"],
        reverse=True,
    )[:8]

    return {
        "status": "success",
        "summary": {
            "location": payload.location_label,
            "domain": _normalize_domain(payload.domain),
            "analysis_goal": payload.analysis_goal,
            "h3_resolution": payload.h3_resolution,
            "cell_count": len(features),
            "exposed_cell_count": exposed_cells,
            "max_risk_score": round(max(scores), 1),
            "mean_risk_score": round(sum(scores) / len(scores), 1),
            "high_or_severe_cell_count": sum(1 for score in scores if score >= 60),
            "evidence_basis": evidence["label"],
            "evidence_factor_count": len(evidence["factor_keys"]),
            "ignored_factor_keys": evidence["ignored_factor_keys"],
            "confidence": evidence["overall_confidence"],
            "top_cells": top_cells,
        },
        "bbox": payload.bbox,
        "geojson": {
            "type": "FeatureCollection",
            "features": features,
        },
        "engines": {
            "grid": {
                "name": "H3",
                "runtime": "python-h3-v4",
                "rust_target": "mundi-geokernel/h3o",
            },
            "analysis": {
                "whitebox_tools_ready": bool(whitebox.get("executable_ready")),
                "whitebox_tools_used": False,
                "note": (
                    "This layer is based only on supplied exposure geometry and "
                    "risk factors. It does not infer buildings, crops, roads, or "
                    "land use from the basemap image by itself."
                ),
            },
            "render": {
                "runtime": "MapLibre/deck.gl",
                "style_hint": "h3_spatial_insight_risk",
                "height_property": "risk_score",
                "height_scale": 45,
            },
            "transport": {
                "internal": "geojson_feature_collection",
                "best_for": "in-process scoring and temporary conversion only",
                "browser_target": "MVT/PMTiles",
                "analytics_target": "GeoParquet",
                "large_layer_target": "MVT/PMTiles from Rust geokernel/h3o or a cached tiler",
            },
        },
    }


def h3_cell_geojson_geometry(h3_index: str) -> dict[str, Any]:
    boundary = h3.cell_to_boundary(h3_index)
    coords = [[lng, lat] for lat, lng in boundary]
    if coords and coords[0] != coords[-1]:
        coords.append(coords[0])
    return {"type": "Polygon", "coordinates": [coords]}


def _validate_payload(payload: H3SpatialInsightInput) -> None:
    west, south, east, north = payload.bbox
    if west >= east or south >= north:
        raise ValueError("bbox must be ordered as west,south,east,north")
    if not 0 <= payload.h3_resolution <= 15:
        raise ValueError("h3_resolution must be between 0 and 15")
    if payload.max_hexes < 1:
        raise ValueError("max_hexes must be at least 1")


def _bbox_polygon(bbox: list[float]) -> dict[str, Any]:
    west, south, east, north = bbox
    return {
        "type": "Polygon",
        "coordinates": [[
            [west, south],
            [east, south],
            [east, north],
            [west, north],
            [west, south],
        ]],
    }


def _load_risk_factors(raw: str) -> dict[str, Any]:
    if not raw.strip():
        return {}
    data = json.loads(raw)
    if not isinstance(data, dict):
        raise ValueError("risk_factors_json must be a JSON object or empty string")
    return data


def _load_exposure_geometries(raw: str) -> list[BaseGeometry]:
    if not raw.strip():
        return []
    data = json.loads(raw)
    if not isinstance(data, dict):
        raise ValueError("exposure_geojson must be a GeoJSON object or empty string")
    if data.get("type") == "FeatureCollection":
        features = data.get("features") or []
    elif data.get("type") == "Feature":
        features = [data]
    else:
        features = [{"type": "Feature", "geometry": data, "properties": {}}]

    geoms: list[BaseGeometry] = []
    for feature in features:
        if not isinstance(feature, dict):
            continue
        geom_obj = feature.get("geometry")
        if not isinstance(geom_obj, dict):
            continue
        geom = shape(geom_obj)
        if geom.is_empty:
            continue
        if not geom.is_valid:
            geom = geom.buffer(0)
        if not geom.is_empty:
            geoms.append(geom)
    return geoms


def _evidence_summary(factors: dict[str, Any], exposure_geoms: list[BaseGeometry]) -> dict[str, Any]:
    factor_keys = sorted(
        key
        for key, value in factors.items()
        if key in _EVIDENCE_FACTOR_KEYS and _is_evidence_value(value)
    )
    ignored_factor_keys = sorted(key for key in factors if key not in _EVIDENCE_FACTOR_KEYS)
    has_exposure = bool(exposure_geoms)
    has_factors = bool(factor_keys)
    if has_exposure and has_factors:
        label = f"exposure geometry plus {len(factor_keys)} observed/modelled factor(s)"
        confidence = "medium"
    elif has_exposure:
        label = "exposure geometry only"
        confidence = "low"
    elif has_factors:
        label = f"{len(factor_keys)} observed/modelled factor(s), no local exposure geometry"
        confidence = "low"
    else:
        label = "no usable evidence"
        confidence = "none"
    return {
        "has_evidence": has_exposure or has_factors,
        "has_exposure": has_exposure,
        "factor_keys": factor_keys,
        "ignored_factor_keys": ignored_factor_keys,
        "label": label,
        "overall_confidence": confidence,
    }


def _is_evidence_value(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return math.isfinite(float(value))
    if isinstance(value, str):
        normalized = value.strip().lower()
        return bool(normalized) and normalized not in {"unknown", "n/a", "na", "none", "null"}
    if isinstance(value, (list, tuple, dict)):
        return bool(value)
    return value is not None


def _cell_evidence_level(exposure_count: int, evidence: dict[str, Any]) -> str:
    if exposure_count > 0 and evidence["factor_keys"]:
        return "cell exposure + area factors"
    if exposure_count > 0:
        return "cell exposure"
    if evidence["factor_keys"]:
        return "area factors only"
    return "none"


def _cell_confidence(exposure_count: int, evidence: dict[str, Any]) -> str:
    if exposure_count > 0 and len(evidence["factor_keys"]) >= 2:
        return "medium"
    if exposure_count > 0 or evidence["factor_keys"]:
        return "low"
    return "none"


def _count_exposures(hex_geom: BaseGeometry, exposure_geoms: list[BaseGeometry]) -> int:
    return sum(1 for geom in exposure_geoms if geom.intersects(hex_geom))


def _score_cell(
    *,
    exposure_count: int,
    domain: str,
    factors: dict[str, Any],
) -> float:
    domain_name = _normalize_domain(domain)
    base = {
        "housing": 18.0,
        "infrastructure": 20.0,
        "environment": 16.0,
        "drone": 14.0,
        "agriculture": 15.0,
        "mixed": 18.0,
    }.get(domain_name, 18.0)

    factor_score = (
        _rain_score(factors) * 0.22
        + _flood_score(factors) * 0.26
        + _slope_score(factors) * _slope_weight(domain_name)
        + _impervious_score(factors) * _impervious_weight(domain_name)
        + _environment_score(factors) * _environment_weight(domain_name)
        + _drainage_score(factors) * 0.16
    )
    exposure_bonus = min(exposure_count, 6) * _exposure_weight(domain_name)
    return max(0.0, min(100.0, base + factor_score + exposure_bonus))


def _numeric(factors: dict[str, Any], *names: str) -> float | None:
    for name in names:
        value = factors.get(name)
        if isinstance(value, (int, float)) and math.isfinite(float(value)):
            return float(value)
    return None


def _rain_score(factors: dict[str, Any]) -> float:
    rain_24h = _numeric(factors, "rainfall_mm_24h", "rain_24h_mm")
    rain_72h = _numeric(factors, "rainfall_mm_72h", "rain_72h_mm")
    score = 0.0
    if rain_24h is not None:
        score = max(score, min(rain_24h / 90.0, 1.0) * 100)
    if rain_72h is not None:
        score = max(score, min(rain_72h / 180.0, 1.0) * 100)
    saturation = str(factors.get("soil_saturation", "")).strip().lower()
    if saturation in {"wet", "saturated", "high"}:
        score = max(score, 72.0)
    return score


def _flood_score(factors: dict[str, Any]) -> float:
    depth = _numeric(factors, "flood_depth_m", "water_depth_m")
    if depth is not None:
        return min(depth / 1.5, 1.0) * 100
    flood_index = _numeric(factors, "flood_index", "flood_risk")
    if flood_index is not None:
        return min(max(flood_index, 0.0), 100.0)
    return 0.0


def _slope_score(factors: dict[str, Any]) -> float:
    slope = _numeric(factors, "slope_degrees", "slope_mean", "slope")
    if slope is None:
        return 0.0
    return min(max(slope, 0.0) / 30.0, 1.0) * 100


def _impervious_score(factors: dict[str, Any]) -> float:
    value = _numeric(factors, "imperviousness", "built_up_fraction", "sealed_surface_fraction")
    if value is None:
        return 0.0
    if value <= 1.0:
        return min(max(value, 0.0), 1.0) * 100
    return min(max(value, 0.0), 100.0)


def _environment_score(factors: dict[str, Any]) -> float:
    pollution = _numeric(factors, "pollution_index", "environmental_stress")
    heat = _numeric(factors, "heat_index_c", "temperature_c")
    ndvi_stress = _numeric(factors, "ndvi_stress")
    score = 0.0
    if pollution is not None:
        score = max(score, min(max(pollution, 0.0), 100.0))
    if heat is not None:
        score = max(score, min(max(heat - 28.0, 0.0) / 12.0, 1.0) * 100)
    if ndvi_stress is not None:
        score = max(score, min(max(ndvi_stress, 0.0), 1.0) * 100)
    return score


def _drainage_score(factors: dict[str, Any]) -> float:
    deficit = _numeric(factors, "drainage_deficit", "runoff_index", "wetness_index")
    if deficit is None:
        return 0.0
    if deficit <= 1.0:
        return min(max(deficit, 0.0), 1.0) * 100
    return min(max(deficit, 0.0), 100.0)


def _slope_weight(domain: str) -> float:
    return {"housing": 0.14, "infrastructure": 0.2, "environment": 0.18, "drone": 0.2}.get(domain, 0.14)


def _impervious_weight(domain: str) -> float:
    return {"housing": 0.18, "infrastructure": 0.16, "environment": 0.12, "drone": 0.06}.get(domain, 0.12)


def _environment_weight(domain: str) -> float:
    return {"housing": 0.1, "infrastructure": 0.08, "environment": 0.24, "drone": 0.12}.get(domain, 0.14)


def _exposure_weight(domain: str) -> float:
    return {"housing": 5.5, "infrastructure": 6.5, "environment": 3.5, "drone": 3.0}.get(domain, 4.0)


def _normalize_domain(value: str) -> str:
    normalized = str(value or "").strip().lower().replace("-", "_").replace(" ", "_")
    if normalized in {"urban", "city", "buildings", "building", "settlement"}:
        return "housing"
    if normalized in {"road", "roads", "utilities", "bridge", "bridges"}:
        return "infrastructure"
    if normalized in {"env", "ecology", "pollution"}:
        return "environment"
    if normalized in {"drone_data", "orthophoto", "orthomosaic", "dem", "lidar"}:
        return "drone"
    if normalized in {"farm", "field", "crop"}:
        return "agriculture"
    if normalized in {"housing", "infrastructure", "environment", "drone", "agriculture", "mixed"}:
        return normalized
    return "mixed"


def _risk_level(score: float) -> str:
    if score >= 80:
        return "severe"
    if score >= 60:
        return "high"
    if score >= 40:
        return "moderate"
    return "low"


def _likely_issue(domain: str, score: float, factors: dict[str, Any]) -> str:
    domain_name = _normalize_domain(domain)
    if _flood_score(factors) >= 50 or _rain_score(factors) >= 65:
        return "flooding or drainage stress"
    if _slope_score(factors) >= 55:
        return "slope instability or erosion"
    if _impervious_score(factors) >= 55:
        return "runoff from built-up or sealed surfaces"
    if _environment_score(factors) >= 55:
        return "environmental stress hotspot"
    if score >= 60:
        return {
            "housing": "settlement exposure hotspot",
            "infrastructure": "infrastructure exposure hotspot",
            "environment": "environmental monitoring hotspot",
            "drone": "drone-visible anomaly hotspot",
            "agriculture": "field risk hotspot",
        }.get(domain_name, "spatial risk hotspot")
    return "screening cell"


def _recommended_action(domain: str, score: float, issue: str) -> str:
    if score >= 80:
        urgency = "Inspect immediately"
    elif score >= 60:
        urgency = "Prioritize field verification"
    elif score >= 40:
        urgency = "Monitor and compare with local evidence"
    else:
        urgency = "Keep as baseline context"
    domain_name = _normalize_domain(domain)
    if domain_name == "housing":
        return f"{urgency}; check buildings, drainage channels, and nearby slopes."
    if domain_name == "infrastructure":
        return f"{urgency}; check roads, culverts, bridges, and utility corridors."
    if domain_name == "environment":
        return f"{urgency}; compare against water, vegetation, waste, and runoff evidence."
    if domain_name == "drone":
        return f"{urgency}; compare this hex with the drone orthophoto/DEM pixels."
    if domain_name == "agriculture":
        return f"{urgency}; compare with crop condition, soil wetness, and field boundaries."
    return f"{urgency}; verify the cell with the best available map, drone, or field evidence."
