from __future__ import annotations

import math
import os
import tempfile
import time
from dataclasses import dataclass
from typing import Any

from shapely.geometry import shape

_SUPPORTED_TARGETS = {
    "building",
    "road",
    "linear_boundary",
    "tree_canopy",
    "crop_patch",
    "vegetation_patch",
    "bare_rectangle",
    "water",
}


@dataclass(frozen=True)
class RasterObjectCandidateInput:
    raster_url: str
    layer_id: str
    layer_name: str
    bounds_wgs84: list[float] | None
    target_classes: list[str]
    max_candidates: int
    max_sample_pixels: int
    min_area_m2: float
    max_area_m2: float
    confidence_threshold: float
    engine_preference: str


def analyze_raster_object_candidates(
    payload: RasterObjectCandidateInput,
) -> dict[str, Any]:
    """Extract basic object review polygons from an uploaded RGB orthophoto."""
    _validate_payload(payload)

    start = time.perf_counter()

    import numpy as np
    import rasterio
    import rasterio.features
    from affine import Affine
    from rasterio.crs import CRS
    from rasterio.enums import Resampling
    from rasterio.transform import from_bounds
    from rasterio.warp import transform_bounds, transform_geom

    with rasterio.open(payload.raster_url) as ds:
        out_h, out_w = _target_shape(ds.width, ds.height, payload.max_sample_pixels)
        if ds.count < 3:
            return {
                "status": "unsupported_raster",
                "error": "Object candidate extraction needs an RGB raster with at least 3 bands.",
            }

        red = ds.read(
            1, out_shape=(out_h, out_w), resampling=Resampling.bilinear, masked=True
        )
        green = ds.read(
            2, out_shape=(out_h, out_w), resampling=Resampling.bilinear, masked=True
        )
        blue = ds.read(
            3, out_shape=(out_h, out_w), resampling=Resampling.bilinear, masked=True
        )

        raster_bounds = payload.bounds_wgs84
        if not raster_bounds and ds.crs:
            raster_bounds = list(
                transform_bounds(ds.crs, "EPSG:4326", *ds.bounds, densify_pts=21)
            )

        if ds.crs:
            source_crs = ds.crs
            source_transform = ds.transform * Affine.scale(
                ds.width / out_w, ds.height / out_h
            )
        elif payload.bounds_wgs84:
            source_crs = CRS.from_epsg(4326)
            west, south, east, north = payload.bounds_wgs84
            source_transform = from_bounds(west, south, east, north, out_w, out_h)
        else:
            return {
                "status": "unsupported_raster",
                "error": "Object candidate extraction needs raster CRS or stored WGS84 bounds.",
            }

    valid = _valid_rgb_mask(red, green, blue)
    if int(np.count_nonzero(valid)) == 0:
        return {"status": "error", "error": "No valid RGB pixels were available."}

    r, g, b = _normalize_rgb(red, green, blue, valid)
    brightness = (r + g + b) / 3.0
    max_channel = np.maximum(np.maximum(r, g), b)
    min_channel = np.minimum(np.minimum(r, g), b)
    saturation = np.zeros_like(max_channel, dtype="float32")
    np.divide(
        max_channel - min_channel,
        max_channel,
        out=saturation,
        where=max_channel > 0,
    )
    with np.errstate(divide="ignore", invalid="ignore"):
        grvi = np.where((g + r) > 0, (g - r) / (g + r), 0.0)

    targets = _normalize_targets(payload.target_classes)
    candidate_features: list[dict[str, Any]] = []
    class_counts: dict[str, int] = {}
    sampled_masks: dict[str, int] = {}

    for target in targets:
        mask = _mask_for_target(target, valid, brightness, grvi, saturation, r, g, b)
        mask = rasterio.features.sieve(
            mask.astype("uint8"), size=_sieve_size(out_w, out_h, target)
        ).astype(bool)
        sampled_masks[target] = int(np.count_nonzero(mask))
        if sampled_masks[target] == 0:
            continue

        target_features = _features_from_mask(
            mask,
            target=target,
            source_transform=source_transform,
            source_crs=source_crs,
            min_area_m2=payload.min_area_m2,
            max_area_m2=payload.max_area_m2,
            confidence_threshold=payload.confidence_threshold,
            max_candidates=payload.max_candidates,
        )
        class_counts[target] = len(target_features)
        candidate_features.extend(target_features)

    candidate_features = sorted(
        candidate_features,
        key=lambda feature: (
            feature["properties"].get("confidence", 0),
            -feature["properties"].get("area_m2", 0),
        ),
        reverse=True,
    )
    pre_cap_candidate_count = len(candidate_features)
    candidate_features = candidate_features[: payload.max_candidates]

    for index, feature in enumerate(candidate_features, start=1):
        feature["properties"]["candidate_rank"] = index

    elapsed_ms = round((time.perf_counter() - start) * 1000.0, 1)
    summary = {
        "source_layer_id": payload.layer_id,
        "source_layer_name": payload.layer_name,
        "candidate_count": len(candidate_features),
        "pre_cap_candidate_count": pre_cap_candidate_count,
        "max_candidates": payload.max_candidates,
        "candidate_count_capped": pre_cap_candidate_count > payload.max_candidates,
        "class_counts": _count_by_class(candidate_features) or class_counts,
        "requested_targets": targets,
        "sample_shape": f"{out_w}x{out_h}",
        "sampled_mask_pixels": sampled_masks,
        "confidence_threshold": payload.confidence_threshold,
        "min_area_m2": payload.min_area_m2,
        "max_area_m2": payload.max_area_m2,
        "elapsed_ms": elapsed_ms,
        "evidence_level": "candidate_polygons_not_confirmed_assets",
        "honesty_note": (
            "These are object review marks from the uploaded image. Treat the number "
            "as marks to inspect, not a final object count."
        ),
        "analytics_format": "geoparquet",
        "render_transport": "geojson",
        "geojson_role": "live_map_transport_only",
        "live_preview_transport": "gzip_geojson_when_smaller",
        "candidate_count_available": True,
        "count_semantics": "candidate_screening",
        "count_units": "candidate_polygons",
        "confirmed_count": False,
        "confirmed_count_available": False,
        "confirmed_building_count": None,
        "candidate_building_count": None,
        "screening_model": _fallback_engine_name(payload.engine_preference),
        "analysis_plan": _analysis_plan_for_request(payload.engine_preference),
    }
    if "building" in targets:
        summary["candidate_building_count"] = int(
            summary["class_counts"].get("building", 0)
        )
    geoparquet = _write_features_to_geoparquet(candidate_features)
    result = {
        "status": "success",
        "summary": summary,
        "bbox": raster_bounds,
        "geojson": {"type": "FeatureCollection", "features": candidate_features},
        "engines": {
            "selection": {
                "requested": payload.engine_preference,
                "used": _fallback_engine_name(payload.engine_preference),
                "runtime": "python/rasterio/numpy/shapely",
                "planner_order": _planner_order_for_request(payload.engine_preference),
            },
            "optional_engines": _optional_engine_status(),
            "next_upgrade_path": [
                "GeoLibre-Rust/WASM for polygon cleanup, vector conversion, PMTiles, and terrain/spectral context",
                "Reference footprints or field review for trusted house/building counts",
            ],
        },
    }
    if geoparquet:
        result["geoparquet"] = geoparquet
        summary["geoparquet_size_bytes"] = geoparquet["size_bytes"]
        summary["geoparquet_feature_count"] = geoparquet["feature_count"]
    return result


def _validate_payload(payload: RasterObjectCandidateInput) -> None:
    if payload.max_candidates < 1:
        raise ValueError("max_candidates must be at least 1")
    if payload.max_sample_pixels < 10_000:
        raise ValueError("max_sample_pixels must be at least 10000")
    if payload.min_area_m2 <= 0:
        raise ValueError("min_area_m2 must be positive")
    if payload.max_area_m2 <= payload.min_area_m2:
        raise ValueError("max_area_m2 must be greater than min_area_m2")
    if not 0 <= payload.confidence_threshold <= 1:
        raise ValueError("confidence_threshold must be between 0 and 1")


def _target_shape(width: int, height: int, max_sample_pixels: int) -> tuple[int, int]:
    total = max(1, width * height)
    cap = min(max_sample_pixels, 2_000_000)
    if total <= cap:
        return height, width
    scale = math.sqrt(cap / total)
    return max(1, int(height * scale)), max(1, int(width * scale))


def _valid_rgb_mask(red: Any, green: Any, blue: Any) -> Any:
    import numpy as np

    return (
        ~np.ma.getmaskarray(red)
        & ~np.ma.getmaskarray(green)
        & ~np.ma.getmaskarray(blue)
    )


def _normalize_rgb(red: Any, green: Any, blue: Any, valid: Any) -> tuple[Any, Any, Any]:
    import numpy as np

    arrays = [
        red.astype("float32").filled(np.nan),
        green.astype("float32").filled(np.nan),
        blue.astype("float32").filled(np.nan),
    ]
    values = np.concatenate([arr[valid] for arr in arrays])
    finite = values[np.isfinite(values)]
    if finite.size == 0:
        scale = 1.0
        offset = 0.0
    else:
        offset = float(np.percentile(finite, 1))
        scale = float(np.percentile(finite, 99)) - offset
        if scale <= 0:
            scale = float(np.nanmax(finite)) or 1.0
            offset = 0.0
    return tuple(np.clip((arr - offset) / scale, 0.0, 1.0) for arr in arrays)  # type: ignore[return-value]


def _normalize_targets(target_classes: list[str]) -> list[str]:
    aliases = {
        "houses": "building",
        "house": "building",
        "homes": "building",
        "home": "building",
        "buildings": "building",
        "roof": "building",
        "roofs": "building",
        "settlement": "building",
        "settlements": "building",
        "road": "road",
        "roads": "road",
        "track": "road",
        "tracks": "road",
        "tree": "tree_canopy",
        "trees": "tree_canopy",
        "canopy": "tree_canopy",
        "canopies": "tree_canopy",
        "crop": "crop_patch",
        "crops": "crop_patch",
        "cropland": "crop_patch",
        "field": "crop_patch",
        "fields": "crop_patch",
        "vegetation": "vegetation_patch",
        "vegetation_patch": "vegetation_patch",
        "vegetation_patches": "vegetation_patch",
        "boundary": "linear_boundary",
        "boundaries": "linear_boundary",
        "field_boundary": "linear_boundary",
        "field_boundaries": "linear_boundary",
        "farm_boundary": "linear_boundary",
        "farm_boundaries": "linear_boundary",
        "parcel_boundary": "linear_boundary",
        "parcel_boundaries": "linear_boundary",
        "fence": "linear_boundary",
        "fences": "linear_boundary",
        "court": "bare_rectangle",
        "playground": "bare_rectangle",
        "playing_area": "bare_rectangle",
        "water": "water",
        "wetness": "water",
    }
    normalized: list[str] = []
    for raw in target_classes or ["building"]:
        key = str(raw or "").strip().lower().replace(" ", "_")
        target = aliases.get(key, key)
        if target in _SUPPORTED_TARGETS and target not in normalized:
            normalized.append(target)
    return normalized or ["building"]


def _mask_for_target(
    target: str,
    valid: Any,
    brightness: Any,
    grvi: Any,
    saturation: Any,
    r: Any,
    g: Any,
    b: Any,
) -> Any:
    import numpy as np

    if target == "building":
        bright_roof = brightness >= 0.52
        low_vegetation = grvi <= 0.10
        neutral_or_colored_roof = saturation <= 0.62
        blue_or_metal_roof = (b >= g * 0.92) & (brightness >= 0.42) & (grvi <= 0.16)
        return (
            valid
            & low_vegetation
            & (bright_roof | blue_or_metal_roof)
            & neutral_or_colored_roof
        )
    if target == "road":
        return valid & (grvi <= 0.08) & (brightness >= 0.38) & (saturation <= 0.55)
    if target == "linear_boundary":
        gradient = _gradient_strength(brightness)
        valid_gradient = gradient[valid]
        threshold = 0.18
        if valid_gradient.size:
            threshold = max(threshold, float(np.percentile(valid_gradient, 88)))
        return valid & (gradient >= threshold) & (brightness >= 0.16)
    if target == "tree_canopy":
        gradient = _gradient_strength(brightness)
        valid_gradient = gradient[valid]
        texture_threshold = 0.04
        if valid_gradient.size:
            texture_threshold = max(texture_threshold, float(np.percentile(valid_gradient, 62)))
        return (
            valid
            & (grvi >= 0.16)
            & (brightness >= 0.16)
            & (brightness <= 0.66)
            & (gradient >= texture_threshold)
        )
    if target == "crop_patch":
        gradient = _gradient_strength(brightness)
        valid_gradient = gradient[valid]
        smooth_threshold = 0.35
        if valid_gradient.size:
            smooth_threshold = min(smooth_threshold, float(np.percentile(valid_gradient, 92)))
        return (
            valid
            & (grvi >= 0.12)
            & (brightness >= 0.20)
            & (gradient <= smooth_threshold)
        )
    if target == "bare_rectangle":
        return valid & (grvi <= 0.05) & (brightness >= 0.42) & (saturation <= 0.50)
    if target == "water":
        return valid & (brightness <= 0.28) & (b >= r * 0.95) & (grvi <= 0.18)
    return valid & (grvi >= 0.18) & (brightness >= 0.20)


def _gradient_strength(values: Any) -> Any:
    import numpy as np

    gradient = np.zeros_like(values, dtype="float32")
    gradient[:, 1:] = np.maximum(
        gradient[:, 1:], np.abs(values[:, 1:] - values[:, :-1])
    )
    gradient[1:, :] = np.maximum(
        gradient[1:, :], np.abs(values[1:, :] - values[:-1, :])
    )
    return gradient


def _sieve_size(width: int, height: int, target: str) -> int:
    total = max(1, width * height)
    if target == "building":
        return max(6, int(total * 0.000006))
    if target in {"road", "linear_boundary"}:
        return max(12, int(total * 0.000015))
    if target == "tree_canopy":
        return max(10, int(total * 0.00001))
    if target in {"crop_patch", "vegetation_patch"}:
        return max(18, int(total * 0.00002))
    return max(8, int(total * 0.00001))


def _features_from_mask(
    mask: Any,
    *,
    target: str,
    source_transform: Any,
    source_crs: Any,
    min_area_m2: float,
    max_area_m2: float,
    confidence_threshold: float,
    max_candidates: int,
) -> list[dict[str, Any]]:
    import rasterio.features
    from rasterio.warp import transform_geom

    features: list[dict[str, Any]] = []
    for geom, value in rasterio.features.shapes(
        mask.astype("uint8"), mask=mask, transform=source_transform
    ):
        if int(value) != 1:
            continue
        source_geom = shape(geom)
        if source_geom.is_empty or not source_geom.is_valid:
            source_geom = source_geom.buffer(0)
        if source_geom.is_empty:
            continue

        area_m2 = _area_m2(source_geom, source_crs)
        if area_m2 < min_area_m2 or area_m2 > _class_max_area(target, max_area_m2):
            continue

        minx, miny, maxx, maxy = source_geom.bounds
        width = max(maxx - minx, 1e-9)
        height = max(maxy - miny, 1e-9)
        aspect = max(width, height) / max(min(width, height), 1e-9)
        if target == "building" and aspect > 5.5:
            continue
        if target == "tree_canopy" and area_m2 > 3_000 and aspect > 4.0:
            continue
        if target == "road" and aspect < 2.5 and area_m2 < 500:
            continue
        if target == "linear_boundary" and aspect < 2.0 and area_m2 < 250:
            continue

        confidence = _confidence(target, area_m2, aspect, source_geom.length)
        if confidence < confidence_threshold:
            continue

        try:
            geometry_wgs84 = transform_geom(source_crs, "EPSG:4326", geom, precision=7)
        except Exception:
            geometry_wgs84 = geom

        features.append(
            {
                "type": "Feature",
                "geometry": geometry_wgs84,
                "properties": {
                    "candidate_class": target,
                    "candidate_label": _label_for_target(target),
                    "confidence": round(confidence, 3),
                    "area_m2": round(area_m2, 2),
                    "aspect_ratio": round(aspect, 2),
                    "evidence_basis": _evidence_for_target(target),
                    "confirmed": False,
                    "recommended_action": _recommended_action_for_target(target),
                    "screening_model": "raster_object_candidates_v1",
                },
            }
        )
        if len(features) >= max_candidates * 3:
            break
    return sorted(
        features, key=lambda feature: feature["properties"]["confidence"], reverse=True
    )


def _class_max_area(target: str, max_area_m2: float) -> float:
    if target == "building":
        return min(max_area_m2, 1_500.0)
    if target == "road":
        return max(max_area_m2, 10_000.0)
    if target == "linear_boundary":
        return max(max_area_m2, 12_000.0)
    if target == "tree_canopy":
        return max(max_area_m2, 3_000.0)
    if target == "crop_patch":
        return max(max_area_m2, 25_000.0)
    if target == "vegetation_patch":
        return max(max_area_m2, 25_000.0)
    return max_area_m2


def _area_m2(geom: Any, crs: Any) -> float:
    is_geographic = bool(getattr(crs, "is_geographic", False))
    if crs and not is_geographic:
        return float(abs(geom.area))
    minx, miny, maxx, maxy = geom.bounds
    mid_lat = (miny + maxy) / 2.0
    return float(
        abs(geom.area)
        * 111_320.0
        * 111_320.0
        * max(math.cos(math.radians(mid_lat)), 0.1)
    )


def _confidence(target: str, area_m2: float, aspect: float, perimeter: float) -> float:
    if target == "building":
        area_score = _triangular_score(area_m2, low=12, ideal=90, high=600)
        aspect_score = _clamp(1.0 - max(0.0, aspect - 1.0) / 5.0, 0.0, 1.0)
        compactness = _compactness(area_m2, perimeter)
        return _clamp(
            0.25 + 0.38 * area_score + 0.22 * aspect_score + 0.15 * compactness,
            0.0,
            0.96,
        )
    if target == "road":
        return _clamp(
            0.30 + min(aspect / 12.0, 0.45) + min(area_m2 / 5000.0, 0.20), 0.0, 0.92
        )
    if target == "linear_boundary":
        return _clamp(
            0.25 + min(aspect / 14.0, 0.45) + min(area_m2 / 8000.0, 0.18), 0.0, 0.88
        )
    if target == "tree_canopy":
        area_score = _triangular_score(area_m2, low=8, ideal=120, high=2500)
        shape_score = _clamp(1.0 - max(0.0, aspect - 1.0) / 6.0, 0.0, 1.0)
        return _clamp(0.30 + 0.40 * area_score + 0.18 * shape_score, 0.0, 0.90)
    if target == "crop_patch":
        return _clamp(0.32 + min(area_m2 / 8000.0, 0.48), 0.0, 0.88)
    if target == "vegetation_patch":
        return _clamp(0.35 + min(area_m2 / 3000.0, 0.45), 0.0, 0.90)
    return _clamp(
        0.35 + _triangular_score(area_m2, low=20, ideal=400, high=5000) * 0.45,
        0.0,
        0.90,
    )


def _triangular_score(value: float, *, low: float, ideal: float, high: float) -> float:
    if value <= low or value >= high:
        return 0.0
    if value == ideal:
        return 1.0
    if value < ideal:
        return (value - low) / (ideal - low)
    return (high - value) / (high - ideal)


def _compactness(area: float, perimeter: float) -> float:
    if perimeter <= 0:
        return 0.0
    return _clamp((4.0 * math.pi * area) / (perimeter * perimeter), 0.0, 1.0)


def _clamp(value: float, minimum: float, maximum: float) -> float:
    return max(minimum, min(maximum, value))


def _label_for_target(target: str) -> str:
    return {
        "building": "building/roof candidate",
        "road": "road/track candidate",
        "linear_boundary": "field/farm boundary candidate",
        "tree_canopy": "tree/canopy candidate",
        "crop_patch": "crop/field patch candidate",
        "vegetation_patch": "vegetation/crop/tree patch candidate",
        "bare_rectangle": "bare/playing-area rectangle candidate",
        "water": "water/wetness candidate",
    }.get(target, f"{target} candidate")


def _evidence_for_target(target: str) -> str:
    return {
        "building": "compact bright or metal/roof-like low-vegetation segment; not a confirmed building",
        "road": "elongated bright low-vegetation raster segment; not a confirmed road",
        "linear_boundary": "linear contrast segment; not a surveyed field or farm boundary",
        "tree_canopy": "green textured canopy-like raster segment",
        "crop_patch": "green smoother field/crop-like raster segment",
        "vegetation_patch": "green raster segment grouped as vegetation/crop/tree context",
        "bare_rectangle": "bright low-vegetation segment with object-like area",
        "water": "dark/blue raster segment; not a hydrology-confirmed water body",
    }.get(target, "raster-derived candidate segment")


def _recommended_action_for_target(target: str) -> str:
    if target == "building":
        return "Spot-check the marked roof shapes before using the number as a house count."
    if target == "road":
        return "Spot-check the marked linework before using it as road or track evidence."
    if target == "linear_boundary":
        return "Verify with surveyed parcel data or field mapping before using as a farm/field boundary."
    if target == "tree_canopy":
        return "Use as a tree/canopy review mark; verify important trees against field knowledge or higher-resolution labels."
    if target == "crop_patch":
        return "Use as crop/field context; combine with NDVI/NDRE, boundaries, and crop calendar before crop diagnosis."
    if target == "vegetation_patch":
        return "Use as a visual vegetation patch; pair with field boundaries or NDVI/NDRE before crop diagnosis."
    if target == "water":
        return "Verify with SAR/NDWI/drainage evidence before using as water extent."
    return "Treat as a candidate and verify with domain evidence."


def _count_by_class(features: list[dict[str, Any]]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for feature in features:
        klass = str(feature.get("properties", {}).get("candidate_class") or "unknown")
        counts[klass] = counts.get(klass, 0) + 1
    return counts


def _write_features_to_geoparquet(
    features: list[dict[str, Any]],
) -> dict[str, Any] | None:
    if not features:
        return None

    import geopandas as gpd

    gdf = gpd.GeoDataFrame.from_features(features, crs="EPSG:4326")
    if gdf.empty:
        return None

    fd, output_path = tempfile.mkstemp(
        prefix="raster-object-candidates-", suffix=".parquet"
    )
    os.close(fd)
    compression = "zstd"
    try:
        gdf.to_parquet(output_path, index=False, compression=compression)
    except Exception:
        compression = "default"
        gdf.to_parquet(output_path, index=False)

    return {
        "path": output_path,
        "size_bytes": os.path.getsize(output_path),
        "compression": compression,
        "crs": "EPSG:4326",
        "feature_count": len(gdf),
        "role": "primary_analytics_store",
    }


def _analysis_plan_for_request(engine_preference: str) -> str:
    _ = engine_preference
    return (
        "Use basic raster evidence to create review polygons, then treat the marks "
        "as visual review aids rather than trusted counts."
    )


def _planner_order_for_request(engine_preference: str) -> list[str]:
    _ = engine_preference
    return [
        "target-specific raster candidate masks",
        "GeoLibre-ready polygon cleanup/vector output",
    ]


def _fallback_engine_name(engine_preference: str) -> str:
    _ = engine_preference
    return "rasterio_numpy_candidate_extractor_v2"


def _optional_engine_status() -> dict[str, dict[str, Any]]:
    return {
        "geolibre_rust_wasm": {
            **_module_status("geolibre_wasm"),
            "note": (
                "GeoLibre-WASM is available for geoprocessing, COG/tiles, "
                "GeoParquet, spectral/terrain, and browser-side expansion; it is "
                "not a semantic vision detector."
            ),
        },
    }


def _module_status(module_name: str) -> dict[str, Any]:
    try:
        module = __import__(module_name)
    except Exception as exc:
        return {"installed": False, "error": f"{type(exc).__name__}: {exc}"}
    return {"installed": True, "version": getattr(module, "__version__", None)}
