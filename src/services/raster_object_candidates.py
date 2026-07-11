from __future__ import annotations

import math
import os
import tempfile
import time
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
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
    """Extract object review polygons from an uploaded RGB orthophoto."""
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
    target_masks: dict[str, Any] = {}
    sampled_masks: dict[str, int] = {}

    for target in targets:
        mask = _mask_for_target(target, valid, brightness, grvi, saturation, r, g, b)
        mask = rasterio.features.sieve(
            mask.astype("uint8"), size=_sieve_size(out_w, out_h, target)
        ).astype(bool)
        target_masks[target] = mask
        sampled_masks[target] = int(np.count_nonzero(mask))

    candidate_features: list[dict[str, Any]] = []
    fastsam_attempt: dict[str, Any] | None = None
    used_engine = _fallback_engine_name(payload.engine_preference)
    runtime = "python/rasterio/numpy/shapely"

    if _should_try_fastsam(payload.engine_preference):
        rgb_uint8 = _normalized_rgb_uint8(r, g, b)
        fastsam_attempt = _features_from_fastsam(
            rgb_uint8,
            target_masks=target_masks,
            targets=targets,
            source_transform=source_transform,
            source_crs=source_crs,
            min_area_m2=payload.min_area_m2,
            max_area_m2=payload.max_area_m2,
            confidence_threshold=payload.confidence_threshold,
            max_candidates=payload.max_candidates,
        )
        if fastsam_attempt.get("status") == "success":
            candidate_features = list(fastsam_attempt.get("features") or [])
            used_engine = "fastsam_s_candidate_masks_v1"
            runtime = "python/ultralytics/fastsam/rasterio/shapely"
        elif _strict_fastsam_requested(payload.engine_preference):
            elapsed_ms = round((time.perf_counter() - start) * 1000.0, 1)
            return _fastsam_required_error_result(
                payload,
                fastsam_attempt=fastsam_attempt,
                raster_bounds=raster_bounds,
                targets=targets,
                sample_shape=f"{out_w}x{out_h}",
                sampled_masks=sampled_masks,
                elapsed_ms=elapsed_ms,
            )

    if not candidate_features:
        for target, mask in target_masks.items():
            if sampled_masks[target] == 0:
                continue
            candidate_features.extend(
                _features_from_mask(
                    mask,
                    target=target,
                    source_transform=source_transform,
                    source_crs=source_crs,
                    min_area_m2=payload.min_area_m2,
                    max_area_m2=payload.max_area_m2,
                    confidence_threshold=payload.confidence_threshold,
                    max_candidates=payload.max_candidates,
                    screening_model="rasterio_numpy_candidate_extractor_v2",
                )
            )

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
        "class_counts": _count_by_class(candidate_features),
        "requested_targets": targets,
        "sample_shape": f"{out_w}x{out_h}",
        "sampled_mask_pixels": sampled_masks,
        "confidence_threshold": payload.confidence_threshold,
        "min_area_m2": payload.min_area_m2,
        "max_area_m2": payload.max_area_m2,
        "elapsed_ms": elapsed_ms,
        "evidence_level": "candidate_polygons_not_confirmed_assets",
        "honesty_note": (
            "These are object mask overlays from the uploaded image. Treat the number "
            "as polygons to inspect, not a final object count."
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
        "screening_model": used_engine,
        "analysis_plan": _analysis_plan_for_request(payload.engine_preference),
    }
    if fastsam_attempt:
        summary["fastsam_status"] = {
            key: value
            for key, value in fastsam_attempt.items()
            if key not in {"features"}
        }
        if used_engine != "fastsam_s_candidate_masks_v1":
            summary["fastsam_fallback_reason"] = fastsam_attempt.get(
                "error"
            ) or fastsam_attempt.get("status")
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
                "used": used_engine,
                "runtime": runtime,
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


def _normalized_rgb_uint8(r: Any, g: Any, b: Any) -> Any:
    import numpy as np

    channels = [
        np.nan_to_num(channel, nan=0.0, posinf=1.0, neginf=0.0) for channel in (r, g, b)
    ]
    return np.dstack(
        [np.clip(channel * 255, 0, 255).astype("uint8") for channel in channels]
    )


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
            texture_threshold = max(
                texture_threshold, float(np.percentile(valid_gradient, 62))
            )
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
            smooth_threshold = min(
                smooth_threshold, float(np.percentile(valid_gradient, 92))
            )
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
    screening_model: str = "raster_object_candidates_v1",
    extra_properties: dict[str, Any] | None = None,
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
        min_rect = source_geom.minimum_rotated_rectangle
        rectangularity_area = float(getattr(min_rect, "area", 0.0) or 0.0)
        rectangularity = (
            float(source_geom.area) / rectangularity_area
            if rectangularity_area > 0
            else 0.0
        )

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
        if (
            target == "building"
            and extra_properties
            and extra_properties.get("fastsam_support") == "supplemental_roof_recall"
            and (rectangularity < 0.42 or aspect > 3.6)
        ):
            continue
        effective_confidence_threshold = _effective_confidence_threshold(
            target, confidence_threshold
        )
        if confidence < effective_confidence_threshold:
            continue

        try:
            geometry_wgs84 = transform_geom(source_crs, "EPSG:4326", geom, precision=7)
        except Exception:
            geometry_wgs84 = geom

        properties = {
            "candidate_class": target,
            "candidate_label": _label_for_target(target),
            "confidence": round(confidence, 3),
            "area_m2": round(area_m2, 2),
            "aspect_ratio": round(aspect, 2),
            "rectangularity": round(rectangularity, 3),
            "evidence_basis": _evidence_for_target(target),
            "confirmed": False,
            "recommended_action": _recommended_action_for_target(target),
            "screening_model": screening_model,
            "confidence_threshold_used": round(effective_confidence_threshold, 3),
        }
        if extra_properties:
            properties.update(extra_properties)
        features.append(
            {
                "type": "Feature",
                "geometry": geometry_wgs84,
                "properties": properties,
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


def _effective_confidence_threshold(target: str, confidence_threshold: float) -> float:
    if target == "building":
        return max(confidence_threshold, 0.65)
    return confidence_threshold


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
        return (
            "Spot-check the marked linework before using it as road or track evidence."
        )
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


def _should_try_fastsam(engine_preference: str) -> bool:
    engine = str(engine_preference or "").strip().lower().replace("-", "_")
    return engine in {"auto", "fastsam", "fastsam_s", "ultralytics_fastsam"}


def _strict_fastsam_requested(engine_preference: str) -> bool:
    engine = str(engine_preference or "").strip().lower().replace("-", "_")
    return engine in {"fastsam", "fastsam_s", "ultralytics_fastsam"}


def _fastsam_required_error_result(
    payload: RasterObjectCandidateInput,
    *,
    fastsam_attempt: dict[str, Any],
    raster_bounds: list[float] | None,
    targets: list[str],
    sample_shape: str,
    sampled_masks: dict[str, int],
    elapsed_ms: float,
) -> dict[str, Any]:
    reason = str(
        fastsam_attempt.get("error")
        or fastsam_attempt.get("reason")
        or fastsam_attempt.get("status")
        or "FastSAM did not return usable masks."
    )
    summary = {
        "source_layer_id": payload.layer_id,
        "source_layer_name": payload.layer_name,
        "candidate_count": 0,
        "pre_cap_candidate_count": 0,
        "max_candidates": payload.max_candidates,
        "candidate_count_capped": False,
        "class_counts": {},
        "requested_targets": targets,
        "sample_shape": sample_shape,
        "sampled_mask_pixels": sampled_masks,
        "confidence_threshold": payload.confidence_threshold,
        "min_area_m2": payload.min_area_m2,
        "max_area_m2": payload.max_area_m2,
        "elapsed_ms": elapsed_ms,
        "evidence_level": "fastsam_required_but_unavailable",
        "honesty_note": (
            "FastSAM was requested for this orthophoto mask run, but it was not "
            "available. No fallback mask was rendered."
        ),
        "candidate_count_available": False,
        "count_semantics": "not_available_fastsam_required",
        "count_units": "candidate_polygons",
        "confirmed_count": False,
        "confirmed_count_available": False,
        "confirmed_building_count": None,
        "candidate_building_count": 0,
        "screening_model": "fastsam_required_unavailable",
        "fastsam_status": {
            key: value
            for key, value in fastsam_attempt.items()
            if key not in {"features"}
        },
        "fastsam_fallback_reason": reason,
    }
    return {
        "status": "error",
        "error": f"FastSAM is required for this mask run but is not available: {reason}",
        "summary": summary,
        "bbox": raster_bounds,
        "engines": {
            "selection": {
                "requested": payload.engine_preference,
                "used": "fastsam_required_unavailable",
                "runtime": "unavailable",
                "planner_order": _planner_order_for_request(payload.engine_preference),
            },
            "optional_engines": _optional_engine_status(),
        },
    }


def _features_from_fastsam(
    rgb_uint8: Any,
    *,
    target_masks: dict[str, Any],
    targets: list[str],
    source_transform: Any,
    source_crs: Any,
    min_area_m2: float,
    max_area_m2: float,
    confidence_threshold: float,
    max_candidates: int,
) -> dict[str, Any]:
    import numpy as np

    status = _fastsam_weights_status()
    if not status["available"]:
        return {"status": "unavailable", "error": status["reason"], "weights": status}

    try:
        model = _load_fastsam_model(status["path"])
        if _should_use_fastsam_tiles(rgb_uint8.shape[1], rgb_uint8.shape[0]):
            features, mask_count = _features_from_fastsam_tiles(
                model,
                rgb_uint8,
                target_masks=target_masks,
                targets=targets,
                source_transform=source_transform,
                source_crs=source_crs,
                min_area_m2=min_area_m2,
                max_area_m2=max_area_m2,
                confidence_threshold=confidence_threshold,
                max_candidates=max_candidates,
            )
            return {
                "status": "success" if features else "empty",
                "mask_count": mask_count,
                "feature_count": len(features),
                "weights": status,
                "features": features,
                "inference_mode": "tiled",
                "tile_size": _fastsam_tile_size(),
                "tile_stride": _fastsam_tile_stride(),
            }
        imgsz = _fastsam_imgsz(rgb_uint8.shape[1], rgb_uint8.shape[0])
        results = model(
            rgb_uint8,
            imgsz=imgsz,
            device=os.environ.get("MUNDI_FASTSAM_DEVICE", "cpu"),
            retina_masks=True,
            verbose=False,
        )
    except Exception as exc:
        return {
            "status": "error",
            "error": f"{type(exc).__name__}: {exc}",
            "weights": status,
        }

    if not results:
        return {"status": "empty", "mask_count": 0, "weights": status}
    masks_obj = getattr(results[0], "masks", None)
    data = getattr(masks_obj, "data", None)
    if data is None:
        return {"status": "empty", "mask_count": 0, "weights": status}

    try:
        mask_stack = data.detach().cpu().numpy()
    except AttributeError:
        mask_stack = np.asarray(data)
    if mask_stack.ndim == 2:
        mask_stack = mask_stack[None, :, :]

    features = _features_from_fastsam_mask_stack(
        mask_stack,
        rgb_shape=rgb_uint8.shape[:2],
        target_masks=target_masks,
        targets=targets,
        source_transform=source_transform,
        source_crs=source_crs,
        min_area_m2=min_area_m2,
        max_area_m2=max_area_m2,
        confidence_threshold=confidence_threshold,
        max_candidates=max_candidates,
    )
    return {
        "status": "success" if features else "empty",
        "mask_count": int(mask_stack.shape[0]),
        "feature_count": len(features),
        "weights": status,
        "features": features,
        "inference_mode": "full_frame",
    }


def _features_from_fastsam_mask_stack(
    mask_stack: Any,
    *,
    rgb_shape: tuple[int, int],
    target_masks: dict[str, Any],
    targets: list[str],
    source_transform: Any,
    source_crs: Any,
    min_area_m2: float,
    max_area_m2: float,
    confidence_threshold: float,
    max_candidates: int,
) -> list[dict[str, Any]]:
    import numpy as np

    features: list[dict[str, Any]] = []
    accepted_coverage_masks: dict[str, Any] = {}
    height, width = rgb_shape
    for mask_index, raw_mask in enumerate(mask_stack):
        object_mask = _resize_bool_mask(raw_mask > 0.5, width=width, height=height)
        object_pixels = int(np.count_nonzero(object_mask))
        if object_pixels == 0:
            continue
        for target in targets:
            target_mask = target_masks.get(target)
            if target_mask is None:
                continue
            refined_mask = object_mask & target_mask
            overlap_pixels = int(np.count_nonzero(refined_mask))
            if overlap_pixels == 0:
                continue
            object_overlap = overlap_pixels / max(object_pixels, 1)
            target_pixels = int(np.count_nonzero(target_mask))
            target_overlap = overlap_pixels / max(target_pixels, 1)
            if not _fastsam_target_evidence_is_strong_enough(
                target=target,
                object_overlap=object_overlap,
                target_overlap=target_overlap,
                overlap_pixels=overlap_pixels,
                image_pixels=height * width,
            ):
                continue
            geometry_mask = _fastsam_geometry_mask_for_target(
                target=target,
                object_mask=object_mask,
                refined_mask=refined_mask,
            )
            feature_batch = _features_from_mask(
                geometry_mask,
                target=target,
                source_transform=source_transform,
                source_crs=source_crs,
                min_area_m2=min_area_m2,
                max_area_m2=max_area_m2,
                confidence_threshold=confidence_threshold,
                max_candidates=max_candidates,
                screening_model="fastsam_s_candidate_masks_v1",
                extra_properties={
                    "fastsam_mask_index": int(mask_index),
                    "fastsam_object_overlap": round(object_overlap, 3),
                    "fastsam_target_overlap": round(target_overlap, 3),
                    "fastsam_overlap_pixels": int(overlap_pixels),
                    "fastsam_geometry_source": _fastsam_geometry_source_for_target(
                        target
                    ),
                },
            )
            features.extend(feature_batch)
            if feature_batch:
                coverage_mask = accepted_coverage_masks.get(target)
                if coverage_mask is None:
                    coverage_mask = np.zeros((height, width), dtype=bool)
                    accepted_coverage_masks[target] = coverage_mask
                coverage_mask |= geometry_mask
            if len(features) >= max_candidates * 4:
                break
        if len(features) >= max_candidates * 4:
            break

    if "building" in targets and len(features) < max_candidates:
        features.extend(
            _fastsam_supplemental_roof_evidence_features(
                target_masks=target_masks,
                accepted_coverage_mask=accepted_coverage_masks.get("building"),
                source_transform=source_transform,
                source_crs=source_crs,
                min_area_m2=min_area_m2,
                max_area_m2=max_area_m2,
                confidence_threshold=confidence_threshold,
                max_candidates=max_candidates - len(features),
            )
        )

    return sorted(
        _dedupe_candidate_features(features),
        key=lambda feature: feature["properties"].get("confidence", 0),
        reverse=True,
    )


def _features_from_fastsam_tiles(
    model: Any,
    rgb_uint8: Any,
    *,
    target_masks: dict[str, Any],
    targets: list[str],
    source_transform: Any,
    source_crs: Any,
    min_area_m2: float,
    max_area_m2: float,
    confidence_threshold: float,
    max_candidates: int,
) -> tuple[list[dict[str, Any]], int]:
    import numpy as np
    from affine import Affine

    height, width = rgb_uint8.shape[:2]
    tile_size = _fastsam_tile_size()
    stride = _fastsam_tile_stride()
    features: list[dict[str, Any]] = []
    accepted_coverage_masks: dict[str, Any] = {}
    target_pixel_counts = {
        target: int(np.count_nonzero(mask)) for target, mask in target_masks.items()
    }
    mask_count = 0

    for y0 in _tile_starts(height, tile_size, stride):
        for x0 in _tile_starts(width, tile_size, stride):
            tile = rgb_uint8[
                y0 : min(y0 + tile_size, height),
                x0 : min(x0 + tile_size, width),
            ]
            if tile.size == 0 or float(tile.mean()) <= 1.0:
                continue
            results = model(
                tile,
                imgsz=_fastsam_imgsz(tile.shape[1], tile.shape[0]),
                device=os.environ.get("MUNDI_FASTSAM_DEVICE", "cpu"),
                retina_masks=True,
                conf=float(os.environ.get("MUNDI_FASTSAM_TILE_CONF", "0.35")),
                iou=float(os.environ.get("MUNDI_FASTSAM_TILE_IOU", "0.8")),
                verbose=False,
            )
            if not results:
                continue
            masks_obj = getattr(results[0], "masks", None)
            data = getattr(masks_obj, "data", None)
            if data is None:
                continue
            try:
                mask_stack = data.detach().cpu().numpy()
            except AttributeError:
                mask_stack = np.asarray(data)
            if mask_stack.ndim == 2:
                mask_stack = mask_stack[None, :, :]
            tile_target_masks = {
                target: target_mask[
                    y0 : y0 + tile.shape[0],
                    x0 : x0 + tile.shape[1],
                ]
                for target, target_mask in target_masks.items()
            }
            for raw_mask in mask_stack:
                local_mask = _resize_bool_mask(
                    raw_mask > 0.5,
                    width=tile.shape[1],
                    height=tile.shape[0],
                )
                mask_count += 1
                feature_batch = _features_from_fastsam_object_mask(
                    local_mask,
                    mask_index=mask_count - 1,
                    target_masks=tile_target_masks,
                    targets=targets,
                    source_transform=source_transform * Affine.translation(x0, y0),
                    source_crs=source_crs,
                    min_area_m2=min_area_m2,
                    max_area_m2=max_area_m2,
                    confidence_threshold=confidence_threshold,
                    max_candidates=max_candidates,
                    target_pixel_counts=target_pixel_counts,
                    image_pixels=height * width,
                )
                features.extend(feature_batch)
                accepted_targets = {
                    str(feature.get("properties", {}).get("candidate_class") or "")
                    for feature in feature_batch
                }
                for target in accepted_targets.intersection(targets):
                    coverage_mask = accepted_coverage_masks.get(target)
                    if coverage_mask is None:
                        coverage_mask = np.zeros((height, width), dtype=bool)
                        accepted_coverage_masks[target] = coverage_mask
                    coverage_mask[
                        y0 : y0 + tile.shape[0],
                        x0 : x0 + tile.shape[1],
                    ] |= local_mask
                if len(features) >= max_candidates * 4:
                    break
            if len(features) >= max_candidates * 4:
                break
        if len(features) >= max_candidates * 4:
            break

    if "building" in targets and len(features) < max_candidates:
        features.extend(
            _fastsam_supplemental_roof_evidence_features(
                target_masks=target_masks,
                accepted_coverage_mask=accepted_coverage_masks.get("building"),
                source_transform=source_transform,
                source_crs=source_crs,
                min_area_m2=min_area_m2,
                max_area_m2=max_area_m2,
                confidence_threshold=confidence_threshold,
                max_candidates=max_candidates - len(features),
            )
        )

    return (
        sorted(
            _dedupe_candidate_features(features),
            key=lambda feature: feature["properties"].get("confidence", 0),
            reverse=True,
        ),
        mask_count,
    )


def _features_from_fastsam_object_mask(
    object_mask: Any,
    *,
    mask_index: int,
    target_masks: dict[str, Any],
    targets: list[str],
    source_transform: Any,
    source_crs: Any,
    min_area_m2: float,
    max_area_m2: float,
    confidence_threshold: float,
    max_candidates: int,
    target_pixel_counts: dict[str, int] | None = None,
    image_pixels: int | None = None,
) -> list[dict[str, Any]]:
    import numpy as np

    features: list[dict[str, Any]] = []
    object_pixels = int(np.count_nonzero(object_mask))
    if object_pixels == 0:
        return features
    height, width = object_mask.shape[:2]
    for target in targets:
        target_mask = target_masks.get(target)
        if target_mask is None:
            continue
        refined_mask = object_mask & target_mask
        overlap_pixels = int(np.count_nonzero(refined_mask))
        if overlap_pixels == 0:
            continue
        object_overlap = overlap_pixels / max(object_pixels, 1)
        target_pixels = (
            target_pixel_counts.get(target, 0)
            if target_pixel_counts is not None
            else int(np.count_nonzero(target_mask))
        )
        target_overlap = overlap_pixels / max(target_pixels, 1)
        if not _fastsam_target_evidence_is_strong_enough(
            target=target,
            object_overlap=object_overlap,
            target_overlap=target_overlap,
            overlap_pixels=overlap_pixels,
            image_pixels=image_pixels or height * width,
        ):
            continue
        geometry_mask = _fastsam_geometry_mask_for_target(
            target=target,
            object_mask=object_mask,
            refined_mask=refined_mask,
        )
        features.extend(
            _features_from_mask(
                geometry_mask,
                target=target,
                source_transform=source_transform,
                source_crs=source_crs,
                min_area_m2=min_area_m2,
                max_area_m2=max_area_m2,
                confidence_threshold=confidence_threshold,
                max_candidates=max_candidates,
                screening_model="fastsam_s_candidate_masks_v1",
                extra_properties={
                    "fastsam_mask_index": int(mask_index),
                    "fastsam_object_overlap": round(object_overlap, 3),
                    "fastsam_target_overlap": round(target_overlap, 3),
                    "fastsam_overlap_pixels": int(overlap_pixels),
                    "fastsam_geometry_source": _fastsam_geometry_source_for_target(
                        target
                    ),
                    "fastsam_inference_mode": "tiled",
                },
            )
        )
    return features


def _fastsam_supplemental_roof_evidence_features(
    *,
    target_masks: dict[str, Any],
    accepted_coverage_mask: Any | None,
    source_transform: Any,
    source_crs: Any,
    min_area_m2: float,
    max_area_m2: float,
    confidence_threshold: float,
    max_candidates: int,
) -> list[dict[str, Any]]:
    if max_candidates <= 0:
        return []

    import numpy as np

    roof_mask = target_masks.get("building")
    if roof_mask is None:
        return []

    available_mask = roof_mask.copy()
    context_mask = None
    if accepted_coverage_mask is not None:
        covered = accepted_coverage_mask.astype(bool, copy=False)
        context_mask = _dilated_context_mask(
            covered,
            radius=max(30, int(min(available_mask.shape[:2]) * 0.08)),
        )
        available_mask &= ~covered
    if context_mask is None:
        return []
    available_mask &= context_mask

    if int(np.count_nonzero(available_mask)) == 0:
        return []

    # Roofs that become too small after whole-image downsampling may not get
    # their own FastSAM object. Add only compact roof-evidence components so we
    # recover recall without returning the huge blocky color-mask layer again.
    return _features_from_mask(
        available_mask,
        target="building",
        source_transform=source_transform,
        source_crs=source_crs,
        min_area_m2=min_area_m2,
        max_area_m2=min(max_area_m2, 420.0),
        confidence_threshold=max(0.58, confidence_threshold + 0.06),
        max_candidates=max_candidates,
        screening_model="fastsam_s_candidate_masks_v1",
        extra_properties={
            "fastsam_geometry_source": "roof_evidence_component_after_fastsam",
            "fastsam_support": "supplemental_roof_recall",
        },
    )


def _dilated_context_mask(mask: Any, *, radius: int) -> Any:
    import numpy as np

    if radius <= 0:
        return mask.astype(bool)
    try:
        from scipy import ndimage

        return ndimage.binary_dilation(
            mask.astype(bool),
            structure=np.ones((3, 3), dtype=bool),
            iterations=radius,
        )
    except Exception:
        # If scipy is unavailable, keep the path conservative instead of adding
        # broad color-only recall away from FastSAM-supported roof masks.
        return mask.astype(bool)


def _fastsam_target_evidence_is_strong_enough(
    *,
    target: str,
    object_overlap: float,
    target_overlap: float,
    overlap_pixels: int,
    image_pixels: int,
) -> bool:
    min_pixels = max(8, int(image_pixels * 0.000002))
    if overlap_pixels < min_pixels:
        return False
    if target == "building":
        # Building overlays must be real FastSAM objects with roof-like evidence
        # across a meaningful part of the object. The old OR check let large
        # vegetation/ground masks through and then saved roof-colored fragments.
        return object_overlap >= 0.24
    return object_overlap >= _fastsam_object_overlap_threshold(
        target
    ) or target_overlap >= _fastsam_target_overlap_threshold(target)


def _fastsam_geometry_mask_for_target(
    *,
    target: str,
    object_mask: Any,
    refined_mask: Any,
) -> Any:
    if target == "building":
        return object_mask
    return refined_mask


def _fastsam_geometry_source_for_target(target: str) -> str:
    if target == "building":
        return "fastsam_object_mask"
    return "fastsam_mask_refined_by_target_evidence"


def _fastsam_object_overlap_threshold(target: str) -> float:
    if target in {"road", "linear_boundary"}:
        return 0.04
    if target in {"crop_patch", "vegetation_patch"}:
        return 0.10
    return 0.14


def _fastsam_target_overlap_threshold(target: str) -> float:
    if target in {"road", "linear_boundary"}:
        return 0.02
    if target in {"crop_patch", "vegetation_patch"}:
        return 0.04
    return 0.06


def _resize_bool_mask(mask: Any, *, width: int, height: int) -> Any:
    import numpy as np

    if mask.shape == (height, width):
        return mask.astype(bool)
    from PIL import Image

    resized = Image.fromarray(mask.astype("uint8") * 255).resize(
        (width, height),
        resample=Image.Resampling.NEAREST,
    )
    return np.asarray(resized) > 0


def _should_use_fastsam_tiles(width: int, height: int) -> bool:
    raw = os.environ.get("MUNDI_FASTSAM_TILED")
    if raw is not None:
        return raw.strip().lower() not in {"0", "false", "no", "off"}
    return max(width, height) > _fastsam_tile_size()


def _fastsam_tile_size() -> int:
    raw = os.environ.get("MUNDI_FASTSAM_TILE_SIZE")
    try:
        return max(256, int(raw)) if raw else 640
    except (TypeError, ValueError):
        return 640


def _fastsam_tile_stride() -> int:
    raw = os.environ.get("MUNDI_FASTSAM_TILE_STRIDE")
    try:
        stride = int(raw) if raw else 512
    except (TypeError, ValueError):
        stride = 512
    return max(128, min(stride, _fastsam_tile_size()))


def _tile_starts(length: int, tile_size: int, stride: int) -> list[int]:
    if length <= tile_size:
        return [0]
    starts = list(range(0, max(1, length - tile_size + 1), stride))
    final_start = max(0, length - tile_size)
    if starts[-1] != final_start:
        starts.append(final_start)
    return starts


def _dedupe_candidate_features(features: list[dict[str, Any]]) -> list[dict[str, Any]]:
    from shapely.geometry import box
    from shapely.strtree import STRtree

    sorted_features = sorted(
        features,
        key=lambda feature: feature["properties"].get("confidence", 0),
        reverse=True,
    )
    parsed_bounds = [
        tuple(shape(feature["geometry"]).bounds) for feature in sorted_features
    ]
    bbox_geometries = [box(*bounds) for bounds in parsed_bounds]
    seen: set[tuple[str, tuple[float, float, float, float]]] = set()
    deduped: list[dict[str, Any]] = []
    accepted_indexes: dict[str, list[tuple[list[int], Any] | None]] = {}

    for feature_index, feature in enumerate(sorted_features):
        bounds = parsed_bounds[feature_index]
        rounded_bounds = tuple(round(value, 7) for value in bounds)
        klass = str(feature.get("properties", {}).get("candidate_class") or "")
        key = (klass, rounded_bounds)
        if key in seen:
            continue

        index_levels = accepted_indexes.setdefault(klass, [])
        is_duplicate = False
        for level in index_levels:
            if level is None:
                continue
            level_feature_indexes, tree = level
            for local_index in tree.query(bbox_geometries[feature_index]):
                other_index = level_feature_indexes[int(local_index)]
                if _bbox_iou(bounds, parsed_bounds[other_index]) >= 0.72:
                    is_duplicate = True
                    break
            if is_duplicate:
                break
        if is_duplicate:
            continue

        seen.add(key)
        deduped.append(feature)
        pending_indexes = [feature_index]
        level_index = 0
        while True:
            if level_index == len(index_levels):
                index_levels.append(
                    (
                        pending_indexes,
                        STRtree([bbox_geometries[index] for index in pending_indexes]),
                    )
                )
                break
            existing_level = index_levels[level_index]
            if existing_level is None:
                index_levels[level_index] = (
                    pending_indexes,
                    STRtree([bbox_geometries[index] for index in pending_indexes]),
                )
                break
            pending_indexes = existing_level[0] + pending_indexes
            index_levels[level_index] = None
            level_index += 1
    return deduped


def _bbox_iou(
    a: tuple[float, float, float, float],
    b: tuple[float, float, float, float],
) -> float:
    ax1, ay1, ax2, ay2 = a
    bx1, by1, bx2, by2 = b
    ix1 = max(ax1, bx1)
    iy1 = max(ay1, by1)
    ix2 = min(ax2, bx2)
    iy2 = min(ay2, by2)
    if ix2 <= ix1 or iy2 <= iy1:
        return 0.0
    intersection = (ix2 - ix1) * (iy2 - iy1)
    area_a = max((ax2 - ax1) * (ay2 - ay1), 0.0)
    area_b = max((bx2 - bx1) * (by2 - by1), 0.0)
    union = area_a + area_b - intersection
    return intersection / union if union > 0 else 0.0


def _fastsam_imgsz(width: int, height: int) -> int:
    raw = os.environ.get("MUNDI_FASTSAM_IMGSZ")
    if raw:
        try:
            return max(128, int(raw))
        except ValueError:
            pass
    longest = max(width, height)
    rounded = int(math.ceil(longest / 32) * 32)
    return min(1536, max(256, rounded))


def _fastsam_weights_status() -> dict[str, Any]:
    candidates = []
    env_path = os.environ.get("MUNDI_FASTSAM_WEIGHTS")
    if env_path:
        candidates.append(Path(env_path))
    project_root = Path(__file__).resolve().parents[2]
    candidates.extend(
        [
            project_root / "FastSAM-s.pt",
            Path.cwd() / "FastSAM-s.pt",
            Path("/app/FastSAM-s.pt"),
        ]
    )
    for candidate in candidates:
        if candidate.exists():
            return {
                "available": True,
                "path": str(candidate),
                "size_bytes": candidate.stat().st_size,
            }
    return {
        "available": False,
        "reason": "FastSAM-s.pt not found; set MUNDI_FASTSAM_WEIGHTS or place it at the project root.",
        "checked_paths": [str(path) for path in candidates],
    }


@lru_cache(maxsize=2)
def _load_fastsam_model(weights_path: str) -> Any:
    from ultralytics import FastSAM

    return FastSAM(weights_path)


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
    if _should_try_fastsam(engine_preference):
        return (
            "Use FastSAM masks for object-shaped regions, refine them with "
            "target-specific raster evidence, then treat the marks as visual "
            "review aids rather than trusted counts."
        )
    return (
        "Use basic raster evidence to create review polygons, then treat the marks "
        "as visual review aids rather than trusted counts."
    )


def _planner_order_for_request(engine_preference: str) -> list[str]:
    if _should_try_fastsam(engine_preference):
        return [
            "FastSAM generic object masks",
            "target-specific raster evidence refinement",
            "GeoLibre-ready polygon cleanup/vector output",
        ]
    return [
        "target-specific raster candidate masks",
        "GeoLibre-ready polygon cleanup/vector output",
    ]


def _fallback_engine_name(engine_preference: str) -> str:
    if _should_try_fastsam(engine_preference):
        return "rasterio_numpy_candidate_extractor_v2_after_fastsam_unavailable"
    return "rasterio_numpy_candidate_extractor_v2"


def _optional_engine_status() -> dict[str, dict[str, Any]]:
    return {
        "fastsam": {
            **_module_status("ultralytics"),
            "weights": _fastsam_weights_status(),
            "note": (
                "FastSAM provides generic object masks; Ingabe refines those masks "
                "with raster evidence for roofs, roads/tracks, trees, crops, field "
                "boundaries, bare soil, and water mask overlays."
            ),
        },
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
