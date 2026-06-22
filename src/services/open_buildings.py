from __future__ import annotations

import csv
import io
import json
import math
import urllib.request
from dataclasses import dataclass
from typing import Any

import h3
from shapely.geometry import Point, mapping, shape
from shapely.geometry.base import BaseGeometry
from shapely import wkt

from src.services.h3_spatial_insight import (
    H3SpatialInsightInput,
    create_h3_spatial_insight,
)


OPEN_BUILDINGS_TILES_GEOJSON_URL = (
    "https://openbuildings-public-dot-gweb-research.uw.r.appspot.com/public/tiles.geojson"
)
OPEN_BUILDINGS_SCORE_THRESHOLDS_URL = (
    "https://storage.googleapis.com/open-buildings-data/v3/score_thresholds_s2_level_4.csv"
)
OPEN_BUILDINGS_POLYGON_GCS_PREFIX = "gs://open-buildings-data/v3/polygons_s2_level_4_gzip"
OPEN_BUILDINGS_POLYGON_HTTPS_PREFIX = (
    "https://storage.googleapis.com/open-buildings-data/v3/polygons_s2_level_4_gzip"
)


@dataclass(frozen=True)
class OpenBuildingsExposureInput:
    location_label: str
    bbox: list[float]
    h3_resolution: int
    min_confidence: float
    buildings_geojson: str
    open_buildings_csv: str
    risk_factors_json: str
    max_buildings: int
    max_hexes: int
    include_ingest_plan: bool
    fetch_tile_metadata: bool


def analyze_open_buildings_exposure(payload: OpenBuildingsExposureInput) -> dict[str, Any]:
    """Analyze Open Buildings footprints as H3 housing/infrastructure exposure."""

    _validate_payload(payload)
    features = _load_building_features(
        buildings_geojson=payload.buildings_geojson,
        open_buildings_csv=payload.open_buildings_csv,
        bbox=payload.bbox,
        min_confidence=payload.min_confidence,
        max_buildings=payload.max_buildings,
    )

    ingest_plan = (
        build_open_buildings_ingest_plan(
            bbox=payload.bbox,
            fetch_tile_metadata=payload.fetch_tile_metadata,
        )
        if payload.include_ingest_plan or not features
        else None
    )

    if not features:
        return {
            "status": "needs_open_buildings_data",
            "summary": {
                "location": payload.location_label,
                "building_count": 0,
                "message": (
                    "No building footprints were provided to this live call. "
                    "Use the ingest plan to fetch/cache Open Buildings for this area, "
                    "then rerun exposure analysis from cached GeoParquet/PostGIS/H3."
                ),
            },
            "ingest_plan": ingest_plan,
            "dataset": open_buildings_dataset_contract(),
        }

    exposure_geojson = {
        "type": "FeatureCollection",
        "features": features,
    }
    h3_result = create_h3_spatial_insight(
        H3SpatialInsightInput(
            location_label=payload.location_label,
            bbox=payload.bbox,
            h3_resolution=payload.h3_resolution,
            domain="housing",
            analysis_goal="screen building exposure and settlement risk",
            risk_factors_json=payload.risk_factors_json,
            exposure_geojson=json.dumps(exposure_geojson),
            max_hexes=payload.max_hexes,
        )
    )
    if h3_result.get("status") != "success":
        return h3_result

    cell_stats = _aggregate_buildings_to_h3(features, payload.h3_resolution)
    _merge_cell_stats(h3_result["geojson"], cell_stats)

    counts = [stats["building_count"] for stats in cell_stats.values()]
    total_area = sum(_number(feature["properties"].get("area_in_meters")) or 0.0 for feature in features)
    confidences = [
        _number(feature["properties"].get("confidence"))
        for feature in features
        if _number(feature["properties"].get("confidence")) is not None
    ]

    h3_summary = h3_result["summary"]
    h3_summary.update(
        {
            "building_count": len(features),
            "building_area_m2": round(total_area, 2),
            "mean_building_area_m2": round(total_area / len(features), 2),
            "mean_confidence": round(sum(confidences) / len(confidences), 4) if confidences else None,
            "occupied_h3_cell_count": len(cell_stats),
            "max_buildings_in_cell": max(counts) if counts else 0,
            "source": _source_name(payload),
        }
    )

    h3_result["status"] = "success"
    h3_result["dataset"] = open_buildings_dataset_contract()
    h3_result["building_exposure_geojson"] = exposure_geojson
    h3_result["building_exposure_feature_count"] = len(features)
    h3_result["ingest_plan"] = ingest_plan
    h3_result["engines"]["exposure"] = {
        "source": "Google Open Buildings V3",
        "live_processing": "provided_geojson_or_csv_only",
        "cached_target": "GeoParquet/PostGIS + H3 aggregates + PMTiles/MVT",
        "note": (
            "This call does not download large Open Buildings tiles in Sage's live path. "
            "Production should prefetch/cache tiles, then serve exposure from local storage."
        ),
    }
    return h3_result


def open_buildings_dataset_contract() -> dict[str, Any]:
    return {
        "name": "Google Open Buildings V3",
        "coverage_note": "Rwanda is listed in V3 coverage.",
        "license_options": ["CC-BY-4.0", "ODbL-1.0"],
        "polygon_data": {
            "https_prefix": OPEN_BUILDINGS_POLYGON_HTTPS_PREFIX,
            "gcs_prefix": OPEN_BUILDINGS_POLYGON_GCS_PREFIX,
            "format": "CSV.GZ per S2 level-4 tile; geometry is WKT.",
        },
        "metadata": {
            "tiles_geojson": OPEN_BUILDINGS_TILES_GEOJSON_URL,
            "score_thresholds_csv": OPEN_BUILDINGS_SCORE_THRESHOLDS_URL,
        },
        "best_runtime_use": (
            "Ingest once, filter by confidence, convert to GeoParquet/PostGIS, "
            "precompute H3/admin aggregates, and serve map tiles from cache."
        ),
    }


def build_open_buildings_ingest_plan(
    *,
    bbox: list[float],
    fetch_tile_metadata: bool,
) -> dict[str, Any]:
    candidate_tiles: list[dict[str, Any]] = []
    metadata_error: str | None = None
    if fetch_tile_metadata:
        try:
            candidate_tiles = select_open_buildings_tiles_for_bbox(bbox)
        except Exception as exc:
            metadata_error = str(exc)

    return {
        "status": "ready_to_ingest" if candidate_tiles or not metadata_error else "metadata_lookup_failed",
        "bbox": bbox,
        "candidate_tile_count": len(candidate_tiles),
        "candidate_tiles": candidate_tiles[:12],
        "metadata_error": metadata_error,
        "steps": [
            "Fetch tile metadata and select S2 level-4 tiles intersecting the requested bbox.",
            "Download only those CSV.GZ polygon files, not the whole 178GB global archive.",
            "Filter rows by confidence and bbox/geometry intersection.",
            "Convert WKT polygons to GeoParquet and load durable copy into PostGIS.",
            "Precompute H3 aggregates for building count, area, and confidence.",
            "Create PMTiles/MVT for map display; keep Sage live calls on cached outputs.",
        ],
        "commands": [
            "curl -L '<tile_url>' | gunzip > open_buildings_tile.csv",
            "python scripts/ingest_open_buildings.py --bbox west,south,east,north --min-confidence 0.75",
        ],
    }


def select_open_buildings_tiles_for_bbox(bbox: list[float]) -> list[dict[str, Any]]:
    with urllib.request.urlopen(OPEN_BUILDINGS_TILES_GEOJSON_URL, timeout=20) as response:
        data = json.loads(response.read().decode("utf-8"))
    query_geom = _bbox_geom(bbox)
    selected: list[dict[str, Any]] = []
    for feature in data.get("features") or []:
        if not isinstance(feature, dict):
            continue
        geom_data = feature.get("geometry")
        props = feature.get("properties") or {}
        if not isinstance(geom_data, dict) or not isinstance(props, dict):
            continue
        geom = shape(geom_data)
        if not geom.is_valid:
            geom = geom.buffer(0)
        if geom.intersects(query_geom):
            selected.append(
                {
                    "tile_id": props.get("tile_id"),
                    "tile_url": props.get("tile_url"),
                    "gcs_url": _https_to_gcs(str(props.get("tile_url") or "")),
                    "size_mb": props.get("size_mb"),
                }
            )
    selected.sort(key=lambda row: str(row.get("tile_id") or ""))
    return selected


def bbox_geometry(bbox: list[float]) -> BaseGeometry:
    return _bbox_geom(bbox)


def open_buildings_row_geometry(row: dict[str, str]) -> BaseGeometry | None:
    return _geometry_from_open_buildings_row(row)


def parse_number(value: Any) -> float | None:
    return _number(value)


def _load_building_features(
    *,
    buildings_geojson: str,
    open_buildings_csv: str,
    bbox: list[float],
    min_confidence: float,
    max_buildings: int,
) -> list[dict[str, Any]]:
    raw_features: list[dict[str, Any]] = []
    if buildings_geojson.strip():
        raw_features.extend(_features_from_geojson(buildings_geojson))
    if open_buildings_csv.strip():
        raw_features.extend(_features_from_csv(open_buildings_csv))

    query_geom = _bbox_geom(bbox)
    accepted: list[dict[str, Any]] = []
    for feature in raw_features:
        geom_data = feature.get("geometry")
        props = feature.get("properties") or {}
        if not isinstance(geom_data, dict) or not isinstance(props, dict):
            continue
        confidence = _number(props.get("confidence"))
        if confidence is not None and confidence < min_confidence:
            continue
        geom = shape(geom_data)
        if geom.is_empty:
            continue
        if not geom.is_valid:
            geom = geom.buffer(0)
        if geom.is_empty or not geom.intersects(query_geom):
            continue
        accepted.append(
            {
                "type": "Feature",
                "geometry": mapping(geom),
                "properties": {
                    "source": props.get("source", "open_buildings"),
                    "confidence": confidence,
                    "area_in_meters": _number(props.get("area_in_meters")),
                    "full_plus_code": props.get("full_plus_code") or props.get("plus_code"),
                },
            }
        )
        if len(accepted) >= max_buildings:
            break
    return accepted


def _features_from_geojson(raw: str) -> list[dict[str, Any]]:
    data = json.loads(raw)
    if not isinstance(data, dict):
        raise ValueError("buildings_geojson must be a GeoJSON object or empty string")
    if data.get("type") == "FeatureCollection":
        features = data.get("features") or []
    elif data.get("type") == "Feature":
        features = [data]
    else:
        features = [{"type": "Feature", "geometry": data, "properties": {}}]
    return [feature for feature in features if isinstance(feature, dict)]


def _features_from_csv(raw: str) -> list[dict[str, Any]]:
    rows = csv.DictReader(io.StringIO(raw))
    features: list[dict[str, Any]] = []
    for row in rows:
        geom = _geometry_from_open_buildings_row(row)
        if geom is None or geom.is_empty:
            continue
        features.append(
            {
                "type": "Feature",
                "geometry": mapping(geom),
                "properties": {
                    "source": "open_buildings_csv",
                    "confidence": _number(row.get("confidence")),
                    "area_in_meters": _number(row.get("area_in_meters")),
                    "full_plus_code": row.get("full_plus_code"),
                },
            }
        )
    return features


def _geometry_from_open_buildings_row(row: dict[str, str]) -> BaseGeometry | None:
    geometry_wkt = (row.get("geometry") or "").strip()
    if geometry_wkt:
        try:
            geom = wkt.loads(geometry_wkt)
        except Exception:
            return None
        return geom if geom.is_valid else geom.buffer(0)
    lat = _number(row.get("latitude"))
    lon = _number(row.get("longitude"))
    if lat is None or lon is None:
        return None
    return Point(lon, lat)


def _aggregate_buildings_to_h3(
    features: list[dict[str, Any]],
    h3_resolution: int,
) -> dict[str, dict[str, Any]]:
    cells: dict[str, dict[str, Any]] = {}
    for feature in features:
        geom = shape(feature["geometry"])
        point = geom.representative_point()
        h3_index = h3.latlng_to_cell(point.y, point.x, h3_resolution)
        stats = cells.setdefault(
            h3_index,
            {
                "building_count": 0,
                "building_area_m2": 0.0,
                "confidence_sum": 0.0,
                "confidence_count": 0,
            },
        )
        stats["building_count"] += 1
        stats["building_area_m2"] += _number(feature["properties"].get("area_in_meters")) or 0.0
        confidence = _number(feature["properties"].get("confidence"))
        if confidence is not None:
            stats["confidence_sum"] += confidence
            stats["confidence_count"] += 1

    for stats in cells.values():
        count = int(stats.pop("confidence_count"))
        confidence_sum = float(stats.pop("confidence_sum"))
        stats["building_area_m2"] = round(float(stats["building_area_m2"]), 2)
        stats["mean_confidence"] = round(confidence_sum / count, 4) if count else None
    return cells


def _merge_cell_stats(geojson: dict[str, Any], cell_stats: dict[str, dict[str, Any]]) -> None:
    counts = [stats["building_count"] for stats in cell_stats.values()]
    max_count = max(counts) if counts else 0
    for feature in geojson.get("features") or []:
        props = feature.get("properties") or {}
        h3_index = props.get("h3_index")
        stats = cell_stats.get(h3_index, {})
        building_count = int(stats.get("building_count") or 0)
        props["building_count"] = building_count
        props["building_area_m2"] = stats.get("building_area_m2", 0.0)
        props["mean_building_confidence"] = stats.get("mean_confidence")
        props["settlement_density_rank"] = (
            round(building_count / max_count, 4) if max_count else 0.0
        )
        props["risk_score"] = round(
            min(100.0, float(props.get("risk_score") or 0.0) + min(building_count, 10) * 1.8),
            1,
        )


def _validate_payload(payload: OpenBuildingsExposureInput) -> None:
    west, south, east, north = payload.bbox
    if west >= east or south >= north:
        raise ValueError("bbox must be ordered as west,south,east,north")
    if not 0 <= payload.h3_resolution <= 15:
        raise ValueError("h3_resolution must be between 0 and 15")
    if not 0 <= payload.min_confidence <= 1:
        raise ValueError("min_confidence must be between 0 and 1")
    if payload.max_buildings < 1:
        raise ValueError("max_buildings must be at least 1")
    if payload.max_hexes < 1:
        raise ValueError("max_hexes must be at least 1")


def _bbox_geom(bbox: list[float]) -> BaseGeometry:
    west, south, east, north = bbox
    return shape(
        {
            "type": "Polygon",
            "coordinates": [[
                [west, south],
                [east, south],
                [east, north],
                [west, north],
                [west, south],
            ]],
        }
    )


def _number(value: Any) -> float | None:
    if value is None or value == "":
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _https_to_gcs(url: str) -> str | None:
    prefix = "https://storage.googleapis.com/open-buildings-data/v3/polygons_s2_level_4_gzip/"
    if not url.startswith(prefix):
        return None
    return f"{OPEN_BUILDINGS_POLYGON_GCS_PREFIX}/{url[len(prefix):]}"


def _source_name(payload: OpenBuildingsExposureInput) -> str:
    if payload.buildings_geojson.strip() and payload.open_buildings_csv.strip():
        return "provided_geojson_and_open_buildings_csv"
    if payload.buildings_geojson.strip():
        return "provided_geojson"
    if payload.open_buildings_csv.strip():
        return "open_buildings_csv"
    return "none"
