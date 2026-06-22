from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import h3
from pyproj import Transformer
from shapely.geometry import mapping, shape
from shapely.geometry.base import BaseGeometry
from shapely.ops import transform


DEFAULT_MAX_HEXES = 50_000

_AREA_TRANSFORM = Transformer.from_crs("EPSG:4326", "EPSG:6933", always_xy=True).transform


class AdminH3Error(ValueError):
    """Raised when an admin polygon cannot be converted into H3 cells."""


@dataclass(frozen=True)
class AdminH3Options:
    resolution: int
    admin_level: str | None = None
    id_property: str | None = None
    name_property: str | None = None
    max_hexes: int = DEFAULT_MAX_HEXES
    min_overlap_ratio: float = 0.0
    include_geometry: bool = True


def admin_geojson_to_h3(
    geojson: dict[str, Any],
    *,
    options: AdminH3Options,
) -> dict[str, Any]:
    """Convert admin-boundary GeoJSON features into an H3 membership layer.

    Output features are full H3 hexagons with boundary metadata. The
    `overlap_ratio` property is the fraction of each hexagon covered by the
    admin polygon, so downstream analysis can weight edge cells instead of
    pretending clipped boundaries are full cells.
    """
    _validate_options(options)

    output_features: list[dict[str, Any]] = []
    total_candidates = 0

    for feature_index, feature in enumerate(_iter_features(geojson)):
        geom = _feature_geometry(feature, feature_index)
        if geom is None:
            continue

        props = feature.get("properties") or {}
        if not isinstance(props, dict):
            props = {}

        hex_ids = h3.geo_to_cells(mapping(geom), res=options.resolution)
        total_candidates += len(hex_ids)
        if total_candidates > options.max_hexes:
            raise AdminH3Error(
                f"Generated {total_candidates} H3 cells, above limit {options.max_hexes}. "
                "Use a coarser resolution or smaller admin boundary."
            )

        admin_area_m2 = _area_m2(geom)
        admin_id = _admin_id(props, options, feature_index)
        admin_name = _admin_name(props, options, admin_id)

        for h3_index in sorted(hex_ids):
            hex_geom = h3_cell_geometry(h3_index)
            intersection = geom.intersection(hex_geom)
            if intersection.is_empty:
                continue

            hex_area_m2 = _area_m2(hex_geom)
            intersection_area_m2 = _area_m2(intersection)
            overlap_ratio = intersection_area_m2 / hex_area_m2 if hex_area_m2 else 0.0
            if overlap_ratio < options.min_overlap_ratio:
                continue

            feature_out = {
                "type": "Feature",
                "properties": {
                    "h3_index": h3_index,
                    "h3_resolution": options.resolution,
                    "admin_level": options.admin_level,
                    "admin_id": admin_id,
                    "admin_name": admin_name,
                    "source_feature_index": feature_index,
                    "overlap_ratio": round(overlap_ratio, 6),
                    "admin_overlap_ratio": round(
                        intersection_area_m2 / admin_area_m2 if admin_area_m2 else 0.0,
                        8,
                    ),
                    "centroid_inside": bool(geom.covers(hex_geom.centroid)),
                    "intersection_area_m2": round(intersection_area_m2, 3),
                    "hex_area_m2": round(hex_area_m2, 3),
                    "admin_area_m2": round(admin_area_m2, 3),
                },
            }
            feature_out["geometry"] = (
                h3_cell_geojson_geometry(h3_index)
                if options.include_geometry
                else None
            )
            output_features.append(feature_out)

    return {
        "type": "FeatureCollection",
        "features": output_features,
        "metadata": {
            "h3_resolution": options.resolution,
            "admin_level": options.admin_level,
            "feature_count": len(output_features),
            "max_hexes": options.max_hexes,
            "min_overlap_ratio": options.min_overlap_ratio,
            "geometry_included": options.include_geometry,
        },
    }


def h3_cell_geojson_geometry(h3_index: str) -> dict[str, Any]:
    coords = h3_cell_geojson_ring(h3_index)
    return {"type": "Polygon", "coordinates": [coords]}


def h3_cell_geometry(h3_index: str) -> BaseGeometry:
    return shape(h3_cell_geojson_geometry(h3_index))


def h3_cell_geojson_ring(h3_index: str) -> list[list[float]]:
    boundary = h3.cell_to_boundary(h3_index)
    coords = [[lng, lat] for lat, lng in boundary]
    if coords and coords[0] != coords[-1]:
        coords.append(coords[0])
    return coords


def _validate_options(options: AdminH3Options) -> None:
    if not 0 <= options.resolution <= 15:
        raise AdminH3Error("H3 resolution must be between 0 and 15.")
    if options.max_hexes < 1:
        raise AdminH3Error("max_hexes must be at least 1.")
    if not 0.0 <= options.min_overlap_ratio <= 1.0:
        raise AdminH3Error("min_overlap_ratio must be between 0 and 1.")


def _iter_features(geojson: dict[str, Any]) -> list[dict[str, Any]]:
    if not isinstance(geojson, dict):
        raise AdminH3Error("GeoJSON must be an object.")

    geojson_type = geojson.get("type")
    if geojson_type == "FeatureCollection":
        features = geojson.get("features")
        if not isinstance(features, list):
            raise AdminH3Error("FeatureCollection must include a features array.")
        return [feature for feature in features if isinstance(feature, dict)]
    if geojson_type == "Feature":
        return [geojson]
    if geojson_type in {"Polygon", "MultiPolygon"}:
        return [{"type": "Feature", "properties": {}, "geometry": geojson}]

    raise AdminH3Error("GeoJSON must be a FeatureCollection, Feature, Polygon, or MultiPolygon.")


def _feature_geometry(feature: dict[str, Any], feature_index: int) -> BaseGeometry | None:
    geom_obj = feature.get("geometry")
    if not isinstance(geom_obj, dict):
        raise AdminH3Error(f"Feature {feature_index} is missing a GeoJSON geometry.")

    geom = shape(geom_obj)
    if geom.is_empty:
        return None
    if geom.geom_type not in {"Polygon", "MultiPolygon"}:
        return None
    if not geom.is_valid:
        geom = geom.buffer(0)
    if geom.is_empty:
        return None
    if geom.geom_type not in {"Polygon", "MultiPolygon"}:
        raise AdminH3Error(f"Feature {feature_index} did not resolve to a polygon geometry.")
    return geom


def _area_m2(geom: BaseGeometry) -> float:
    if geom.is_empty:
        return 0.0
    return float(transform(_AREA_TRANSFORM, geom).area)


def _admin_id(props: dict[str, Any], options: AdminH3Options, feature_index: int) -> str:
    if options.id_property and props.get(options.id_property) is not None:
        return str(props[options.id_property])

    level = (options.admin_level or "").lower()
    candidates = [
        "admin_id",
        "id",
        "gid",
        "code",
        f"{level}_id" if level else "",
        f"{level}_code" if level else "",
    ]
    value = _first_property(props, candidates)
    return str(value) if value is not None else f"admin-{feature_index + 1}"


def _admin_name(props: dict[str, Any], options: AdminH3Options, admin_id: str) -> str:
    if options.name_property and props.get(options.name_property) is not None:
        return str(props[options.name_property])

    level = (options.admin_level or "").lower()
    candidates = [
        "admin_name",
        "name",
        f"{level}_name" if level else "",
        level if level else "",
        "district",
        "district_name",
        "sector",
        "sector_name",
        "cell",
        "cell_name",
        "village",
        "village_name",
    ]
    value = _first_property(props, candidates)
    return str(value) if value is not None else admin_id


def _first_property(props: dict[str, Any], candidates: list[str]) -> Any | None:
    lower_lookup = {str(key).lower(): value for key, value in props.items()}
    for candidate in candidates:
        if not candidate:
            continue
        if candidate in props and props[candidate] is not None:
            return props[candidate]
        value = lower_lookup.get(candidate.lower())
        if value is not None:
            return value
    return None
