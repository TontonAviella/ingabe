"""Vector file helpers backed by pyogrio/GeoPandas."""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class VectorFeatureRecord:
    feature_id: int
    geometry: dict[str, Any] | None
    properties: dict[str, Any]


def _read_dataframe(
    path: str,
    *,
    layer: str | None = None,
    max_features: int | None = None,
):
    import pyogrio

    kwargs: dict[str, Any] = {}
    if layer is not None:
        kwargs["layer"] = layer
    if max_features is not None:
        kwargs["max_features"] = max_features
    return pyogrio.read_dataframe(path, **kwargs)


def _has_renderable_geometry(path: str, layer: str) -> bool:
    import pyogrio

    lowered_path = path.lower()
    if lowered_path.endswith((".kml", ".kmz")) and "overlay" in layer.lower():
        return False

    try:
        info = pyogrio.read_info(path, layer=layer)
    except Exception:
        return False

    feature_count = info.get("features")
    if feature_count == 0:
        return False

    geometry_type = str(info.get("geometry_type") or "").lower()
    if geometry_type and geometry_type not in {"unknown", "none"}:
        return True

    # KML often reports Unknown at the layer level, so sample a few rows and
    # require at least one non-null geometry before we create a map layer.
    try:
        sample = pyogrio.read_dataframe(
            path,
            layer=layer,
            columns=[],
            max_features=16,
        )
    except Exception:
        return False

    if sample.empty:
        return False
    try:
        return bool(sample.geometry.notna().any())
    except Exception:
        return False


def list_renderable_vector_layers(path: str) -> list[str]:
    import pyogrio

    layers = []
    for row in pyogrio.list_layers(path).tolist():
        if not row or not row[0]:
            continue
        layer = str(row[0])
        if _has_renderable_geometry(path, layer):
            layers.append(layer)
    return layers


def _target_crs_is_wgs84(crs: Any) -> bool:
    if crs is None:
        return False
    try:
        epsg = crs.to_epsg()
    except Exception:
        epsg = None
    if epsg == 4326:
        return True
    return str(crs).upper() in {"EPSG:4326", "OGC:CRS84"}


def read_vector_feature_records(
    path: str,
    *,
    layer: str | None = None,
    max_features: int | None = None,
    reproject_to_wgs84: bool = True,
) -> list[VectorFeatureRecord]:
    """Read vector features as GeoJSON-shaped records.

    The app's enrichment tools expect EPSG:4326 GeoJSON geometries because they
    mask/query satellite products in geographic coordinates.
    """

    gdf = _read_dataframe(path, layer=layer, max_features=max_features)
    if gdf.empty:
        return []

    if reproject_to_wgs84 and gdf.crs is not None and not _target_crs_is_wgs84(gdf.crs):
        gdf = gdf.to_crs("EPSG:4326")

    try:
        from shapely import force_2d

        geometry_name = gdf.geometry.name
        gdf = gdf.copy()
        gdf[geometry_name] = gdf.geometry.apply(
            lambda geom: force_2d(geom) if geom is not None else None
        )
    except Exception:
        pass

    records: list[VectorFeatureRecord] = []
    for feature_id, feature in enumerate(gdf.iterfeatures(na="null"), start=1):
        records.append(
            VectorFeatureRecord(
                feature_id=feature_id,
                geometry=feature.get("geometry"),
                properties=dict(feature.get("properties") or {}),
            )
        )
    return records


def compute_vector_bounds(path: str, *, layer: str | None = None) -> list[float] | None:
    import pyogrio
    from pyproj import Transformer

    kwargs: dict[str, Any] = {}
    if layer is not None:
        kwargs["layer"] = layer

    info = pyogrio.read_info(path, **kwargs)
    raw_bounds = info.get("total_bounds")
    if raw_bounds is None:
        return None

    bounds = [float(value) for value in raw_bounds]
    if len(bounds) != 4 or any(math.isnan(value) for value in bounds):
        return None

    crs = info.get("crs")
    if crs is not None and not _target_crs_is_wgs84(crs):
        transformer = Transformer.from_crs(crs, "EPSG:4326", always_xy=True)
        x1, y1 = transformer.transform(bounds[0], bounds[1])
        x2, y2 = transformer.transform(bounds[2], bounds[3])
        bounds = [x1, y1, x2, y2]

    return bounds


def write_vector_enrichment(
    input_path: str,
    output_path: str,
    metric_key: str,
    values_by_feature_id: dict[int, float | int | None],
    *,
    layer: str | None = None,
) -> None:
    """Copy a vector file and add one numeric enrichment column."""

    import pyogrio

    read_kwargs: dict[str, Any] = {}
    if layer is not None:
        read_kwargs["layer"] = layer

    info = pyogrio.read_info(input_path, **read_kwargs)
    gdf = _read_dataframe(input_path, layer=layer)
    gdf[metric_key] = [
        values_by_feature_id.get(feature_id)
        for feature_id in range(1, len(gdf) + 1)
    ]

    write_kwargs: dict[str, Any] = {}
    driver = info.get("driver")
    if driver:
        write_kwargs["driver"] = driver
    pyogrio.write_dataframe(gdf, output_path, **write_kwargs)
