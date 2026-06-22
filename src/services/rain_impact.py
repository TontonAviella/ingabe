from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any


_CROP_STAGE_VULNERABILITY = {
    "planting": 0.75,
    "germination": 0.85,
    "vegetative": 0.55,
    "flowering": 0.9,
    "grain_fill": 0.7,
    "harvest": 0.8,
    "storage": 0.65,
    "unknown": 0.6,
}


@dataclass(frozen=True)
class RainImpactInput:
    location_label: str
    bbox: list[float]
    rainfall_mm_24h: float
    rainfall_mm_72h: float
    soil_saturation: str
    crop_stage: str
    forecast_summary: str
    exposure_geojson: str


def parse_bbox(raw_bbox: str) -> list[float]:
    parts = [float(part.strip()) for part in raw_bbox.split(",")]
    if len(parts) != 4:
        raise ValueError("bbox must have four values: west,south,east,north")
    west, south, east, north = parts
    if west >= east or south >= north:
        raise ValueError("bbox must be ordered as west,south,east,north")
    return parts


def analyze_expected_rain_impact(payload: RainImpactInput) -> dict[str, Any]:
    """Estimate agriculture impact from forecast rainfall and exposure geometry."""
    features = _load_exposure_features(payload.exposure_geojson)
    if not features:
        features = _make_bbox_mesh(payload.bbox)

    rainfall_score = min(payload.rainfall_mm_24h / 80.0, 1.0) * 45.0
    accumulation_score = min(payload.rainfall_mm_72h / 160.0, 1.0) * 25.0
    saturation_score = _soil_saturation_score(payload.soil_saturation) * 20.0
    crop_score = _crop_stage_score(payload.crop_stage) * 10.0

    scored_features: list[dict[str, Any]] = []
    risk_values: list[float] = []
    for idx, feature in enumerate(features):
        geometry = feature.get("geometry")
        if not isinstance(geometry, dict):
            continue
        props = dict(feature.get("properties") or {})
        local_modifier = _local_modifier(idx, len(features))
        risk_score = max(
            0.0,
            min(
                100.0,
                rainfall_score
                + accumulation_score
                + saturation_score
                + crop_score
                + local_modifier,
            ),
        )
        risk_values.append(risk_score)
        props.update(
            {
                "risk_score": round(risk_score, 1),
                "risk_level": _risk_level(risk_score),
                "expected_impact": _expected_impact(risk_score),
                "rainfall_24h_mm": round(payload.rainfall_mm_24h, 1),
                "rainfall_72h_mm": round(payload.rainfall_mm_72h, 1),
                "soil_saturation": payload.soil_saturation,
                "crop_stage": payload.crop_stage,
            }
        )
        scored_features.append(
            {
                "type": "Feature",
                "geometry": geometry,
                "properties": props,
            }
        )

    risk_values = risk_values or [0.0]
    summary = {
        "location": payload.location_label,
        "forecast_summary": payload.forecast_summary,
        "max_risk_score": round(max(risk_values), 1),
        "mean_risk_score": round(sum(risk_values) / len(risk_values), 1),
        "highest_risk_level": _risk_level(max(risk_values)),
        "feature_count": len(scored_features),
        "likely_impacts": _likely_impacts(max(risk_values)),
        "recommended_actions": _recommended_actions(max(risk_values)),
    }

    return {
        "status": "success",
        "summary": summary,
        "bbox": payload.bbox,
        "geojson": {
            "type": "FeatureCollection",
            "features": scored_features,
        },
        "map": {
            "style_hint": "rain_impact_risk",
            "height_property": "risk_score",
            "height_scale": 55,
        },
    }


def _load_exposure_features(raw_geojson: str) -> list[dict[str, Any]]:
    if not raw_geojson.strip():
        return []
    data = json.loads(raw_geojson)
    if data.get("type") == "FeatureCollection":
        features = data.get("features") or []
    elif data.get("type") == "Feature":
        features = [data]
    else:
        raise ValueError("exposure_geojson must be a Feature or FeatureCollection")
    return [feature for feature in features if isinstance(feature, dict)]


def _make_bbox_mesh(bbox: list[float], cells_per_side: int = 4) -> list[dict[str, Any]]:
    west, south, east, north = bbox
    width = (east - west) / cells_per_side
    height = (north - south) / cells_per_side
    features: list[dict[str, Any]] = []
    for row in range(cells_per_side):
        for col in range(cells_per_side):
            x0 = west + col * width
            x1 = x0 + width
            y0 = south + row * height
            y1 = y0 + height
            features.append(
                {
                    "type": "Feature",
                    "geometry": {
                        "type": "Polygon",
                        "coordinates": [[
                            [x0, y0],
                            [x1, y0],
                            [x1, y1],
                            [x0, y1],
                            [x0, y0],
                        ]],
                    },
                    "properties": {"cell": f"{row + 1}-{col + 1}"},
                }
            )
    return features


def _soil_saturation_score(value: str) -> float:
    normalized = value.strip().lower()
    if normalized in {"dry", "low"}:
        return 0.2
    if normalized in {"normal", "medium", "moderate"}:
        return 0.5
    if normalized in {"wet", "high", "saturated"}:
        return 0.85
    return 0.5


def _crop_stage_score(value: str) -> float:
    return _CROP_STAGE_VULNERABILITY.get(value.strip().lower(), _CROP_STAGE_VULNERABILITY["unknown"])


def _local_modifier(index: int, total: int) -> float:
    if total <= 1:
        return 0.0
    # Deterministic variation to make the coarse mesh legible until better
    # terrain/drainage/exposure layers are wired into the pipeline.
    wave = ((index * 37) % 17) - 8
    return float(wave)


def _risk_level(score: float) -> str:
    if score >= 75:
        return "severe"
    if score >= 55:
        return "high"
    if score >= 35:
        return "moderate"
    return "low"


def _expected_impact(score: float) -> str:
    if score >= 75:
        return "Flash flooding, waterlogging, erosion, access disruption, and storage/input damage are likely."
    if score >= 55:
        return "Localized flooding, saturated soils, delayed field operations, and runoff damage are possible."
    if score >= 35:
        return "Wet-field delays, disease pressure, and lowland ponding should be monitored."
    return "Limited direct damage expected, but monitor drainage and field access."


def _likely_impacts(score: float) -> list[str]:
    impacts = ["field access delays", "higher fungal disease pressure"]
    if score >= 35:
        impacts.extend(["lowland ponding", "fertilizer leaching risk"])
    if score >= 55:
        impacts.extend(["crop waterlogging", "erosion on exposed slopes", "rural road disruption"])
    if score >= 75:
        impacts.extend(["flash flooding", "storage/input damage", "possible replanting needs"])
    return impacts


def _recommended_actions(score: float) -> list[str]:
    actions = [
        "check drainage channels before rainfall peaks",
        "avoid fertilizer or pesticide application immediately before heavy rain",
    ]
    if score >= 55:
        actions.extend(
            [
                "move seed, fertilizer, and harvested produce off low floors",
                "prioritize field visits in lowland plots after the event",
            ]
        )
    if score >= 75:
        actions.extend(
            [
                "send farmer alerts for flood-prone cells",
                "prepare access route alternatives for collection and extension teams",
            ]
        )
    return actions
