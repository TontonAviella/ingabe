"""H3 cell scoring for the drone-raster context layer (raster_h3_context).

Scores a cell from observed evidence (rain, flooding, slope, imperviousness,
drainage, environment) with domain weights, and words the likely issue and
the action. The old model-fed spatial-insight tool that also used this module
was removed (2026-10-05).
"""

from __future__ import annotations

import math
from typing import Any

import h3


def h3_cell_geojson_geometry(h3_index: str) -> dict[str, Any]:
    boundary = h3.cell_to_boundary(h3_index)
    coords = [[lng, lat] for lat, lng in boundary]
    if coords and coords[0] != coords[-1]:
        coords.append(coords[0])
    return {"type": "Polygon", "coordinates": [coords]}


def _cell_confidence(exposure_count: int, evidence: dict[str, Any]) -> str:
    if exposure_count > 0 and len(evidence["factor_keys"]) >= 2:
        return "medium"
    if exposure_count > 0 or evidence["factor_keys"]:
        return "low"
    return "none"


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
