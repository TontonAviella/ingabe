from __future__ import annotations

import math
import os
import tempfile
import time
from dataclasses import dataclass
from functools import lru_cache
from typing import Any

from shapely.geometry import mapping, shape

TERRAMIND_PLANNER_ENGINE_ALIASES = {
    "terramind",
    "terramind_first",
    "terramind_samgeo",
    "terramind_geolibre",
    "geoai_planner",
    "semantic_planner",
}

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
    """Extract object candidate polygons from an uploaded RGB orthophoto.

    TerraMind-style semantic planning is preferred when requested, with SamGeo
    reserved for explicit promptable-mask runs. The lightweight rasterio/numpy
    path remains as a fallback so Sage can still answer honestly when model
    checkpoints are missing or unavailable.
    """
    _validate_payload(payload)
    terramind_attempt = _maybe_analyze_with_terramind_planner(payload)
    if terramind_attempt and terramind_attempt.get("status") == "success":
        return terramind_attempt

    samgeo_attempt = _maybe_analyze_with_samgeo(payload)
    if samgeo_attempt and samgeo_attempt.get("status") == "success":
        return samgeo_attempt

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
        "analysis_plan": _analysis_plan_for_request(payload.engine_preference),
    }
    if "building" in targets:
        summary["candidate_building_count"] = int(
            summary["class_counts"].get("building", 0)
        )
    if terramind_attempt and terramind_attempt.get("status") != "success":
        summary["terramind_planner_fallback_reason"] = terramind_attempt.get(
            "error"
        ) or terramind_attempt.get("status")
        summary["semantic_planner_used"] = False
        summary["semantic_planner_status"] = terramind_attempt.get("status")
    if samgeo_attempt and samgeo_attempt.get("status") != "success":
        summary["samgeo_fallback_reason"] = samgeo_attempt.get(
            "error"
        ) or samgeo_attempt.get("status")

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
                "semantic_planner_used": False,
                "planner_order": _planner_order_for_request(payload.engine_preference),
            },
            "terramind_planner_attempt": _summarize_attempt(terramind_attempt),
            "samgeo_attempt": _summarize_attempt(samgeo_attempt),
            "optional_engines": _optional_engine_status(),
            "next_upgrade_path": [
                "Fine-tuned TerraMind/TerraTorch semantic heads for Rwanda drone classes",
                "SamGeo prompt masks only after semantic regions are selected",
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


def _maybe_analyze_with_terramind_planner(
    payload: RasterObjectCandidateInput,
) -> dict[str, Any] | None:
    preference = str(payload.engine_preference or "auto").strip().lower()
    if preference not in TERRAMIND_PLANNER_ENGINE_ALIASES:
        return None

    if not _module_status("terratorch")["installed"]:
        return {
            "status": "terramind_unavailable",
            "error": (
                "TerraMind/TerraTorch is not installed in this runtime; using the "
                "quick raster planner instead."
            ),
            "engines": {
                "selection": {
                    "requested": payload.engine_preference,
                    "used": "rasterio_numpy_semantic_proxy",
                    "planner_order": _planner_order_for_request(payload.engine_preference),
                }
            },
        }

    if os.environ.get("MUNDI_TERRAMIND_RASTER_PLANNER", "").strip().lower() not in {
        "1",
        "true",
        "yes",
    }:
        return {
            "status": "terramind_planner_disabled",
            "error": (
                "TerraMind is installed but the live raster planner is disabled; "
                "using the quick raster planner instead."
            ),
            "engines": {
                "selection": {
                    "requested": payload.engine_preference,
                    "used": "rasterio_numpy_semantic_proxy",
                    "planner_order": _planner_order_for_request(payload.engine_preference),
                }
            },
        }

    start = time.perf_counter()
    try:
        return _analyze_with_terramind_planner(payload, start=start)
    except Exception as exc:
        return {
            "status": "terramind_planner_failed",
            "error": f"{type(exc).__name__}: {exc}",
            "engines": {
                "selection": {
                    "requested": payload.engine_preference,
                    "used": "terramind_rgb_chip_planner_v1",
                    "runtime": "python/terratorch/torch/rasterio/opencv",
                    "failed": True,
                    "planner_order": _planner_order_for_request(payload.engine_preference),
                }
            },
        }


def _analyze_with_terramind_planner(
    payload: RasterObjectCandidateInput, *, start: float
) -> dict[str, Any]:
    import numpy as np
    import rasterio
    from affine import Affine
    from rasterio.crs import CRS
    from rasterio.enums import Resampling
    from rasterio.transform import from_bounds
    from rasterio.warp import transform_bounds, transform_geom

    targets = _normalize_targets(payload.target_classes)
    if set(targets) != {"building"}:
        return {
            "status": "terramind_target_not_supported",
            "error": (
                "The live TerraMind planner is currently wired for roof/building "
                "review marks; using the quick raster planner for the requested "
                "multi-class target set."
            ),
            "engines": {
                "selection": {
                    "requested": payload.engine_preference,
                    "used": "rasterio_numpy_semantic_proxy",
                    "planner_order": _planner_order_for_request(payload.engine_preference),
                }
            },
        }

    max_sample_pixels = min(
        payload.max_sample_pixels,
        int(os.environ.get("MUNDI_TERRAMIND_MAX_SAMPLE_PIXELS", "1200000")),
    )
    max_chips = max(24, int(os.environ.get("MUNDI_TERRAMIND_MAX_CHIPS", "320")))

    with rasterio.open(payload.raster_url) as ds:
        out_h, out_w = _target_shape(ds.width, ds.height, max_sample_pixels)
        if ds.count < 3:
            return {
                "status": "unsupported_raster",
                "error": "TerraMind object planning needs an RGB raster with at least 3 bands.",
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
                "error": "TerraMind object planning needs raster CRS or stored WGS84 bounds.",
            }

    valid = _valid_rgb_mask(red, green, blue)
    if int(np.count_nonzero(valid)) == 0:
        return {"status": "error", "error": "No valid RGB pixels were available."}

    r, g, b = _normalize_rgb(red, green, blue, valid)
    rgb_uint8 = (np.stack([r, g, b], axis=-1) * 255.0).astype("uint8")
    rgb_uint8[~valid] = 0
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

    candidates = _terramind_roof_component_candidates(
        valid=valid,
        brightness=brightness,
        grvi=grvi,
        saturation=saturation,
        blue=b,
        max_components=max(payload.max_candidates * 4, max_chips),
    )
    if not candidates:
        return {
            "status": "terramind_no_roof_candidates",
            "error": "TerraMind did not find roof-like RGB regions to score.",
            "engines": {
                "selection": {
                    "requested": payload.engine_preference,
                    "used": "terramind_rgb_chip_planner_v1",
                    "planner_order": _planner_order_for_request(payload.engine_preference),
                }
            },
        }

    candidates = candidates[:max_chips]
    negative_seed_boxes = _terramind_negative_seed_boxes(
        valid=valid,
        brightness=brightness,
        grvi=grvi,
        saturation=saturation,
        max_boxes=max(32, min(96, max_chips // 2)),
    )
    _score_candidates_with_terramind(
        rgb_uint8=rgb_uint8,
        candidates=candidates,
        negative_seed_boxes=negative_seed_boxes,
    )

    threshold = max(0.50, min(payload.confidence_threshold, 0.72))
    features: list[dict[str, Any]] = []
    for candidate in sorted(
        candidates,
        key=lambda item: item.get("combined_score", item["visual_score"]),
        reverse=True,
    ):
        confidence = float(candidate.get("combined_score", candidate["visual_score"]))
        if confidence < threshold:
            continue

        geom = _sample_bbox_to_source_geom(candidate["bbox"], source_transform)
        area_m2 = _area_m2(geom, source_crs)
        if area_m2 < payload.min_area_m2 or area_m2 > _class_max_area(
            "building", payload.max_area_m2
        ):
            continue

        minx, miny, maxx, maxy = geom.bounds
        width = max(maxx - minx, 1e-9)
        height = max(maxy - miny, 1e-9)
        aspect = max(width, height) / max(min(width, height), 1e-9)
        if aspect > 5.5:
            continue

        geom_mapping = mapping(geom)
        try:
            geometry_wgs84 = transform_geom(
                source_crs, "EPSG:4326", geom_mapping, precision=7
            )
        except Exception:
            geometry_wgs84 = geom_mapping

        features.append(
            {
                "type": "Feature",
                "geometry": geometry_wgs84,
                "properties": {
                    "candidate_class": "building",
                    "candidate_label": "possible house/roof shape",
                    "confidence": round(confidence, 3),
                    "terramind_score": round(
                        float(candidate.get("terramind_score", confidence)), 3
                    ),
                    "visual_score": round(float(candidate["visual_score"]), 3),
                    "area_m2": round(area_m2, 2),
                    "aspect_ratio": round(aspect, 2),
                    "mean_brightness": round(float(candidate["mean_brightness"]), 3),
                    "mean_grvi": round(float(candidate["mean_grvi"]), 3),
                    "mean_saturation": round(float(candidate["mean_saturation"]), 3),
                    "local_roof_density": int(candidate.get("local_roof_density", 0)),
                    "evidence_basis": (
                        "TerraMind selected a compact roof-like region from the "
                        "orthophoto before any mask refinement."
                    ),
                    "confirmed": False,
                    "recommended_action": _recommended_action_for_target("building"),
                    "screening_model": "terramind_rgb_chip_planner_v1",
                    "sam_refined": False,
                },
            }
        )
        if len(features) >= payload.max_candidates:
            break

    if not features:
        return {
            "status": "terramind_no_features_after_filters",
            "error": "TerraMind scored candidates, but none passed the live geometry filters.",
            "engines": {
                "selection": {
                    "requested": payload.engine_preference,
                    "used": "terramind_rgb_chip_planner_v1",
                    "planner_order": _planner_order_for_request(payload.engine_preference),
                }
            },
        }

    for index, feature in enumerate(features, start=1):
        feature["properties"]["candidate_rank"] = index

    elapsed_ms = round((time.perf_counter() - start) * 1000.0, 1)
    geoparquet = _write_features_to_geoparquet(features)
    summary = {
        "source_layer_id": payload.layer_id,
        "source_layer_name": payload.layer_name,
        "candidate_count": len(features),
        "pre_cap_candidate_count": len(candidates),
        "max_candidates": payload.max_candidates,
        "candidate_count_capped": len(candidates) > payload.max_candidates,
        "class_counts": {"building": len(features)},
        "requested_targets": targets,
        "sample_shape": f"{out_w}x{out_h}",
        "terramind_candidate_chips_scored": len(candidates),
        "terramind_negative_seed_chips": len(negative_seed_boxes),
        "confidence_threshold": threshold,
        "min_area_m2": payload.min_area_m2,
        "max_area_m2": payload.max_area_m2,
        "elapsed_ms": elapsed_ms,
        "evidence_level": "terramind_review_marks_not_confirmed_assets",
        "honesty_note": (
            "These are possible house/roof shapes selected from the image for review. "
            "Treat the number as marks shown, not a final house count."
        ),
        "analytics_format": "geoparquet",
        "render_transport": "geojson",
        "geojson_role": "live_map_transport_only",
        "live_preview_transport": "gzip_geojson_when_smaller",
        "candidate_count_available": True,
        "count_semantics": "candidate_screening",
        "count_units": "possible_roof_marks",
        "confirmed_count": False,
        "confirmed_count_available": False,
        "confirmed_building_count": None,
        "candidate_building_count": len(features),
        "semantic_planner_used": True,
        "semantic_planner_status": "terramind_rgb_chip_planner_v1",
        "sam_refinement_used": False,
        "analysis_plan": _analysis_plan_for_request(payload.engine_preference),
    }
    if geoparquet:
        summary["geoparquet_size_bytes"] = geoparquet["size_bytes"]
        summary["geoparquet_feature_count"] = geoparquet["feature_count"]

    result: dict[str, Any] = {
        "status": "success",
        "summary": summary,
        "bbox": raster_bounds,
        "geojson": {"type": "FeatureCollection", "features": features},
        "engines": {
            "selection": {
                "requested": payload.engine_preference,
                "used": "terramind_rgb_chip_planner_v1",
                "runtime": "python/terratorch/torch/rasterio/opencv/shapely",
                "semantic_planner_used": True,
                "sam_refinement_used": False,
                "planner_order": _planner_order_for_request(payload.engine_preference),
            },
            "optional_engines": _optional_engine_status(),
            "next_upgrade_path": [
                "Fine-tune TerraMind/TerraTorch heads on Rwanda roof, road, tree, crop, and field-boundary labels",
                "Run SamGeo only as a bounded box-prompt refiner after TerraMind selects regions",
                "Use GeoLibre-Rust/WASM for large polygon cleanup, vector conversion, PMTiles, and terrain/spectral context",
                "Add human-reviewed labels for trusted counts rather than treating review marks as final truth",
            ],
        },
    }
    if geoparquet:
        result["geoparquet"] = geoparquet
    return result


def _maybe_analyze_with_samgeo(
    payload: RasterObjectCandidateInput,
) -> dict[str, Any] | None:
    preference = str(payload.engine_preference or "auto").strip().lower()
    if preference not in {"auto", "samgeo", "segment-geospatial", "segment_geospatial"}:
        return None

    _ensure_geoai_cache_env()
    if not _module_status("samgeo")["installed"]:
        if preference == "auto":
            return None
        return {
            "status": "samgeo_unavailable",
            "error": "SamGeo is not installed. Install segment-geospatial to use this engine.",
            "engines": {
                "selection": {
                    "requested": payload.engine_preference,
                    "used": "unavailable",
                }
            },
        }

    if preference == "auto" and not _samgeo_auto_enabled():
        return None

    start = time.perf_counter()
    try:
        return _analyze_with_samgeo(payload, start=start)
    except Exception as exc:
        return {
            "status": "samgeo_failed",
            "error": f"{type(exc).__name__}: {exc}",
            "engines": {
                "selection": {
                    "requested": payload.engine_preference,
                    "used": "samgeo",
                    "runtime": "python/samgeo/torch",
                    "failed": True,
                }
            },
        }


def _samgeo_auto_enabled() -> bool:
    _ensure_geoai_cache_env()
    return os.environ.get("MUNDI_SAMGEO_AUTO", "").strip().lower() in {
        "1",
        "true",
        "yes",
    }


def _analyze_with_samgeo(
    payload: RasterObjectCandidateInput, *, start: float
) -> dict[str, Any]:
    _ensure_geoai_cache_env()
    from samgeo import SamGeo

    targets = _normalize_targets(payload.target_classes)
    model_type = os.environ.get("MUNDI_SAMGEO_MODEL_TYPE", "vit_b")
    checkpoint_dir = os.environ.get("MUNDI_SAMGEO_CHECKPOINT_DIR") or None
    device = os.environ.get("MUNDI_SAMGEO_DEVICE") or None
    points_per_side = int(os.environ.get("MUNDI_SAMGEO_POINTS_PER_SIDE", "16"))
    pred_iou_thresh = float(os.environ.get("MUNDI_SAMGEO_PRED_IOU_THRESH", "0.84"))
    stability_score_thresh = float(
        os.environ.get("MUNDI_SAMGEO_STABILITY_THRESH", "0.88")
    )
    min_mask_region_area = int(
        os.environ.get("MUNDI_SAMGEO_MIN_MASK_REGION_AREA", "24")
    )
    min_size_pixels = int(os.environ.get("MUNDI_SAMGEO_MIN_SIZE_PIXELS", "0"))

    with tempfile.TemporaryDirectory() as temp_dir:
        sample_path = os.path.join(temp_dir, "samgeo_input.tif")
        mask_path = os.path.join(temp_dir, "samgeo_mask.tif")
        sample_meta = _write_sample_rgb_geotiff(payload, sample_path)

        sam = SamGeo(
            model_type=model_type,
            automatic=True,
            device=device,
            checkpoint_dir=checkpoint_dir,
            sam_kwargs={
                "points_per_side": points_per_side,
                "pred_iou_thresh": pred_iou_thresh,
                "stability_score_thresh": stability_score_thresh,
                "min_mask_region_area": min_mask_region_area,
            },
        )
        sam.generate(
            sample_path,
            output=mask_path,
            foreground=True,
            unique=True,
            min_size=max(0, min_size_pixels),
            max_size=None,
        )
        candidate_features, class_counts, sampled_masks = _features_from_samgeo_mask(
            mask_path,
            sample_path,
            targets=targets,
            min_area_m2=payload.min_area_m2,
            max_area_m2=payload.max_area_m2,
            confidence_threshold=payload.confidence_threshold,
            max_candidates=payload.max_candidates,
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
    geoparquet = _write_features_to_geoparquet(candidate_features)
    summary = {
        "source_layer_id": payload.layer_id,
        "source_layer_name": payload.layer_name,
        "candidate_count": len(candidate_features),
        "pre_cap_candidate_count": pre_cap_candidate_count,
        "max_candidates": payload.max_candidates,
        "candidate_count_capped": pre_cap_candidate_count > payload.max_candidates,
        "class_counts": _count_by_class(candidate_features) or class_counts,
        "requested_targets": targets,
        "sample_shape": sample_meta["sample_shape"],
        "sampled_mask_pixels": sampled_masks,
        "confidence_threshold": payload.confidence_threshold,
        "min_area_m2": payload.min_area_m2,
        "max_area_m2": payload.max_area_m2,
        "elapsed_ms": elapsed_ms,
        "evidence_level": "samgeo_candidate_masks_not_confirmed_assets",
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
    }
    if "building" in targets:
        summary["candidate_building_count"] = int(
            summary["class_counts"].get("building", 0)
        )
    if geoparquet:
        summary["geoparquet_size_bytes"] = geoparquet["size_bytes"]
        summary["geoparquet_feature_count"] = geoparquet["feature_count"]

    result: dict[str, Any] = {
        "status": "success",
        "summary": summary,
        "bbox": sample_meta["bbox"],
        "geojson": {"type": "FeatureCollection", "features": candidate_features},
        "engines": {
            "selection": {
                "requested": payload.engine_preference,
                "used": "samgeo",
                "runtime": "python/samgeo/torch",
                "model_type": model_type,
                "points_per_side": points_per_side,
                "min_size_pixels": max(0, min_size_pixels),
                "sampled_before_segmentation": True,
            },
            "optional_engines": _optional_engine_status(),
            "next_upgrade_path": [
                "Use prompt/box/text SamGeo modes for user-selected object classes",
                "Fine-tune YOLO/SegFormer/U-Net on Rwanda orthophotos for semantic labels/counts",
                "Persist PMTiles/MVT for very large candidate layers",
                "Validate counts against Open Buildings/OSM/local surveys",
            ],
        },
    }
    if geoparquet:
        result["geoparquet"] = geoparquet
    return result


def _ensure_geoai_cache_env() -> None:
    cache_root = os.environ.get("XDG_CACHE_HOME") or "/cache"
    try:
        os.makedirs(cache_root, exist_ok=True)
    except OSError:
        cache_root = "/tmp/ingabe_geoai_cache"
        os.makedirs(cache_root, exist_ok=True)
    os.environ.setdefault("XDG_CACHE_HOME", cache_root)

    defaults = {
        "MPLCONFIGDIR": os.path.join(cache_root, "matplotlib"),
        "TORCH_HOME": os.path.join(cache_root, "torch"),
        "MUNDI_SAMGEO_CHECKPOINT_DIR": os.path.join(cache_root, "samgeo"),
    }
    for key, path in defaults.items():
        os.environ.setdefault(key, path)
        try:
            os.makedirs(os.environ[key], exist_ok=True)
        except OSError:
            fallback = os.path.join("/tmp/ingabe_geoai_cache", key.lower())
            os.makedirs(fallback, exist_ok=True)
            os.environ[key] = fallback


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


def _terramind_roof_component_candidates(
    *,
    valid: Any,
    brightness: Any,
    grvi: Any,
    saturation: Any,
    blue: Any,
    max_components: int,
) -> list[dict[str, Any]]:
    import cv2
    import numpy as np

    total_pixels = max(1, int(valid.shape[0] * valid.shape[1]))
    bright_roof = (brightness >= 0.50) & (grvi <= 0.14) & (saturation <= 0.68)
    pale_roof = (brightness >= 0.58) & (grvi <= 0.18) & (saturation <= 0.42)
    blue_or_metal_roof = (
        (brightness >= 0.42)
        & (blue >= 0.34)
        & (grvi <= 0.18)
        & (saturation <= 0.72)
    )
    roof_mask = (valid & (bright_roof | pale_roof | blue_or_metal_roof)).astype(
        "uint8"
    )
    if int(np.count_nonzero(roof_mask)) == 0:
        return []

    kernel = np.ones((3, 3), dtype="uint8")
    roof_mask = cv2.morphologyEx(roof_mask, cv2.MORPH_CLOSE, kernel)
    roof_mask = cv2.morphologyEx(roof_mask, cv2.MORPH_OPEN, kernel)
    count, labels, stats, centroids = cv2.connectedComponentsWithStats(
        roof_mask, connectivity=8
    )

    min_pixels = max(3, int(total_pixels * 0.000003))
    max_pixels = max(700, int(total_pixels * 0.0025))
    candidates: list[dict[str, Any]] = []
    for label in range(1, count):
        x, y, w, h, area_px = [int(value) for value in stats[label]]
        if area_px < min_pixels or area_px > max_pixels:
            continue
        if w <= 0 or h <= 0:
            continue
        aspect = max(w, h) / max(min(w, h), 1)
        fill_ratio = area_px / max(w * h, 1)
        if aspect > 4.8 or fill_ratio < 0.10:
            continue

        segment = labels[y : y + h, x : x + w] == label
        if not bool(np.any(segment)):
            continue
        mean_brightness = float(np.nanmean(brightness[y : y + h, x : x + w][segment]))
        mean_grvi = float(np.nanmean(grvi[y : y + h, x : x + w][segment]))
        mean_saturation = float(
            np.nanmean(saturation[y : y + h, x : x + w][segment])
        )
        if not all(
            np.isfinite(value)
            for value in (mean_brightness, mean_grvi, mean_saturation)
        ):
            continue

        bright_score = _clamp((mean_brightness - 0.42) / 0.36, 0.0, 1.0)
        green_score = _clamp((0.20 - mean_grvi) / 0.28, 0.0, 1.0)
        saturation_score = _clamp(
            1.0 - max(0.0, mean_saturation - 0.16) / 0.58,
            0.0,
            1.0,
        )
        shape_score = _clamp(1.0 - max(0.0, aspect - 1.0) / 4.2, 0.0, 1.0)
        fill_score = _clamp(fill_ratio / 0.45, 0.0, 1.0)
        visual_score = _clamp(
            0.18
            + 0.27 * bright_score
            + 0.22 * green_score
            + 0.15 * saturation_score
            + 0.12 * shape_score
            + 0.06 * fill_score,
            0.0,
            0.98,
        )
        candidates.append(
            {
                "bbox": [x, y, x + w, y + h],
                "centroid": [float(centroids[label][0]), float(centroids[label][1])],
                "area_px": area_px,
                "aspect": aspect,
                "fill_ratio": fill_ratio,
                "mean_brightness": mean_brightness,
                "mean_grvi": mean_grvi,
                "mean_saturation": mean_saturation,
                "visual_score": visual_score,
            }
        )

    if not candidates:
        return []

    _annotate_local_roof_density(candidates, valid.shape)
    for candidate in candidates:
        density_score = _clamp(candidate.get("local_roof_density", 0) / 18.0, 0.0, 1.0)
        candidate["visual_score"] = _clamp(
            float(candidate["visual_score"]) + 0.08 * density_score, 0.0, 0.99
        )

    return sorted(candidates, key=lambda item: item["visual_score"], reverse=True)[
        :max_components
    ]


def _annotate_local_roof_density(
    candidates: list[dict[str, Any]], sample_shape: tuple[int, int]
) -> None:
    if not candidates:
        return
    try:
        import numpy as np
        from sklearn.neighbors import NearestNeighbors

        points = np.array(
            [candidate["centroid"] for candidate in candidates], dtype="float32"
        )
        radius = max(28.0, min(sample_shape) * 0.055)
        neighbors = NearestNeighbors(radius=radius).fit(points)
        density = neighbors.radius_neighbors(points, return_distance=False)
        for candidate, nearby in zip(candidates, density):
            candidate["local_roof_density"] = int(len(nearby))
    except Exception:
        for candidate in candidates:
            candidate["local_roof_density"] = 1


def _terramind_negative_seed_boxes(
    *,
    valid: Any,
    brightness: Any,
    grvi: Any,
    saturation: Any,
    max_boxes: int,
) -> list[list[int]]:
    import numpy as np

    height, width = valid.shape
    chip = max(24, int(min(height, width) * 0.045))
    step = max(16, chip // 2)
    boxes: list[tuple[float, list[int]]] = []
    vegetation = valid & (grvi >= 0.22) & (brightness <= 0.64)
    road_or_soil = valid & (grvi <= 0.08) & (brightness >= 0.30) & (saturation >= 0.18)
    dark_texture = valid & (brightness <= 0.34) & (grvi >= 0.05)
    negative = vegetation | road_or_soil | dark_texture

    for y in range(0, max(1, height - chip), step):
        for x in range(0, max(1, width - chip), step):
            y2 = min(height, y + chip)
            x2 = min(width, x + chip)
            window_valid = valid[y:y2, x:x2]
            if float(np.mean(window_valid)) < 0.70:
                continue
            negative_fraction = float(np.mean(negative[y:y2, x:x2]))
            if negative_fraction < 0.55:
                continue
            boxes.append((negative_fraction, [x, y, x2, y2]))

    boxes.sort(key=lambda item: item[0], reverse=True)
    return [box for _, box in boxes[:max_boxes]]


def _score_candidates_with_terramind(
    *,
    rgb_uint8: Any,
    candidates: list[dict[str, Any]],
    negative_seed_boxes: list[list[int]],
) -> None:
    import cv2
    import numpy as np
    import torch

    if not candidates:
        return

    positive_seed_count = min(
        len(candidates), max(1, min(32, max(8, len(candidates) // 5)))
    )
    positive_seed_indices = list(range(positive_seed_count))
    candidate_boxes = [candidate["bbox"] for candidate in candidates]
    seed_boxes = [candidate_boxes[index] for index in positive_seed_indices]
    all_boxes = candidate_boxes + seed_boxes + negative_seed_boxes
    embeddings: list[torch.Tensor] = []

    device = os.environ.get("MUNDI_TERRAMIND_DEVICE", "cpu")
    batch_size = max(1, int(os.environ.get("MUNDI_TERRAMIND_BATCH_SIZE", "24")))
    model = _terramind_rgb_model(device)
    mean = np.array([87.271, 80.931, 66.667], dtype="float32")
    std = np.array([58.767, 47.663, 42.631], dtype="float32")

    for start_index in range(0, len(all_boxes), batch_size):
        batch_boxes = all_boxes[start_index : start_index + batch_size]
        tensors = []
        for bbox in batch_boxes:
            crop = _crop_expanded_square(rgb_uint8, bbox)
            resized = cv2.resize(crop, (224, 224), interpolation=cv2.INTER_AREA)
            arr = (resized.astype("float32") - mean) / std
            tensors.append(torch.from_numpy(arr.transpose(2, 0, 1)))
        batch = torch.stack(tensors).to(device=device, dtype=torch.float32)
        with torch.no_grad():
            output = model(batch)[-1].mean(dim=1)
            output = torch.nn.functional.normalize(output, dim=1)
        embeddings.append(output.detach().cpu())

    encoded = torch.cat(embeddings, dim=0)
    candidate_embeddings = encoded[: len(candidates)]
    seed_start = len(candidates)
    seed_end = seed_start + len(seed_boxes)
    positive_embeddings = encoded[seed_start:seed_end]
    negative_embeddings = encoded[seed_end:] if negative_seed_boxes else None
    positive_centroid = torch.nn.functional.normalize(
        positive_embeddings.mean(dim=0, keepdim=True), dim=1
    )

    for index, candidate in enumerate(candidates):
        vector = candidate_embeddings[index : index + 1]
        pos_sim = float((vector @ positive_centroid.T).item())
        if negative_embeddings is not None and negative_embeddings.numel() > 0:
            neg_sim = float((vector @ negative_embeddings.T).max().item())
        else:
            neg_sim = pos_sim - 0.08
        margin = pos_sim - neg_sim
        terramind_score = float(1.0 / (1.0 + math.exp(-5.0 * margin)))
        combined_score = _clamp(
            0.40 * float(candidate["visual_score"]) + 0.60 * terramind_score,
            0.0,
            0.99,
        )
        candidate["terramind_score"] = terramind_score
        candidate["terramind_margin"] = margin
        candidate["combined_score"] = combined_score


@lru_cache(maxsize=2)
def _terramind_rgb_model(device: str) -> Any:
    _ensure_geoai_cache_env()
    import torch
    from terratorch.registry import TERRATORCH_BACKBONE_REGISTRY

    torch.set_num_threads(
        max(1, int(os.environ.get("MUNDI_TERRAMIND_TORCH_THREADS", "2")))
    )
    model = TERRATORCH_BACKBONE_REGISTRY.build(
        "terramind_v1_tiny",
        pretrained=True,
        modalities=["untok_sen2rgb@224"],
        merge_method="mean",
    )
    model.to(device)
    model.eval()
    return model


def _crop_expanded_square(rgb_uint8: Any, bbox: list[int]) -> Any:
    import numpy as np

    height, width = rgb_uint8.shape[:2]
    x0, y0, x1, y1 = [int(value) for value in bbox]
    cx = (x0 + x1) / 2.0
    cy = (y0 + y1) / 2.0
    size = max(x1 - x0, y1 - y0, 8)
    size = int(size * 2.4)
    half = max(4, size // 2)
    left = max(0, int(round(cx - half)))
    right = min(width, int(round(cx + half)))
    top = max(0, int(round(cy - half)))
    bottom = min(height, int(round(cy + half)))
    crop = rgb_uint8[top:bottom, left:right]
    if crop.size == 0:
        return np.zeros((16, 16, 3), dtype="uint8")
    return crop


def _sample_bbox_to_source_geom(bbox: list[int], source_transform: Any) -> Any:
    from shapely.geometry import box

    x0, y0, x1, y1 = [int(value) for value in bbox]
    points = [
        source_transform * (x0, y0),
        source_transform * (x1, y0),
        source_transform * (x1, y1),
        source_transform * (x0, y1),
    ]
    xs = [point[0] for point in points]
    ys = [point[1] for point in points]
    return box(min(xs), min(ys), max(xs), max(ys))


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


def _write_sample_rgb_geotiff(
    payload: RasterObjectCandidateInput, output_path: str
) -> dict[str, Any]:
    import numpy as np
    import rasterio
    from affine import Affine
    from rasterio.crs import CRS
    from rasterio.enums import Resampling
    from rasterio.transform import from_bounds
    from rasterio.warp import transform_bounds

    with rasterio.open(payload.raster_url) as ds:
        out_h, out_w = _target_shape(ds.width, ds.height, payload.max_sample_pixels)
        if ds.count < 3:
            raise ValueError(
                "SamGeo object extraction needs an RGB raster with at least 3 bands."
            )

        red = ds.read(
            1, out_shape=(out_h, out_w), resampling=Resampling.bilinear, masked=True
        )
        green = ds.read(
            2, out_shape=(out_h, out_w), resampling=Resampling.bilinear, masked=True
        )
        blue = ds.read(
            3, out_shape=(out_h, out_w), resampling=Resampling.bilinear, masked=True
        )
        valid = _valid_rgb_mask(red, green, blue)
        r, g, b = _normalize_rgb(red, green, blue, valid)
        rgb = (np.stack([r, g, b]) * 255.0).astype("uint8")
        rgb[:, ~valid] = 0

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
            raise ValueError(
                "SamGeo object extraction needs raster CRS or stored WGS84 bounds."
            )

        profile = {
            "driver": "GTiff",
            "width": out_w,
            "height": out_h,
            "count": 3,
            "dtype": "uint8",
            "crs": source_crs,
            "transform": source_transform,
            "compress": "deflate",
            "nodata": 0,
        }
        with rasterio.open(output_path, "w", **profile) as out:
            out.write(rgb)

    return {"bbox": raster_bounds, "sample_shape": f"{out_w}x{out_h}"}


def _features_from_samgeo_mask(
    mask_path: str,
    sample_path: str,
    *,
    targets: list[str],
    min_area_m2: float,
    max_area_m2: float,
    confidence_threshold: float,
    max_candidates: int,
) -> tuple[list[dict[str, Any]], dict[str, int], dict[str, int]]:
    import numpy as np
    import rasterio
    import rasterio.features
    from rasterio.warp import transform_geom

    features: list[dict[str, Any]] = []
    class_counts: dict[str, int] = {}
    sampled_masks: dict[str, int] = {target: 0 for target in targets}
    with rasterio.open(mask_path) as mask_ds, rasterio.open(sample_path) as rgb_ds:
        band = mask_ds.read(1)
        mask = band != 0
        if int(np.count_nonzero(mask)) == 0:
            return features, class_counts, sampled_masks

        red = rgb_ds.read(1, masked=True)
        green = rgb_ds.read(2, masked=True)
        blue = rgb_ds.read(3, masked=True)
        valid = _valid_rgb_mask(red, green, blue)
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

        for geom, raw_value in rasterio.features.shapes(
            band.astype("int32"),
            mask=mask,
            transform=mask_ds.transform,
        ):
            value = int(raw_value)
            if value == 0:
                continue
            source_geom = shape(geom)
            if source_geom.is_empty or not source_geom.is_valid:
                source_geom = source_geom.buffer(0)
            if source_geom.is_empty:
                continue

            area_m2 = _area_m2(source_geom, mask_ds.crs)
            minx, miny, maxx, maxy = source_geom.bounds
            width = max(maxx - minx, 1e-9)
            height = max(maxy - miny, 1e-9)
            aspect = max(width, height) / max(min(width, height), 1e-9)
            segment_pixels = band == value
            segment_valid = segment_pixels & valid
            if not bool(np.any(segment_valid)):
                continue
            segment_brightness = float(np.nanmean(brightness[segment_valid]))
            segment_grvi = float(np.nanmean(grvi[segment_valid]))
            segment_saturation = float(np.nanmean(saturation[segment_valid]))
            target = _classify_samgeo_object(
                targets,
                area_m2=area_m2,
                aspect=aspect,
                brightness=segment_brightness,
                grvi=segment_grvi,
                saturation=segment_saturation,
                min_area_m2=min_area_m2,
                max_area_m2=max_area_m2,
            )
            if target is None:
                continue

            sampled_masks[target] = sampled_masks.get(target, 0) + int(
                np.count_nonzero(segment_pixels)
            )
            confidence = _confidence(target, area_m2, aspect, source_geom.length)
            if confidence < confidence_threshold:
                continue
            try:
                geometry_wgs84 = transform_geom(
                    mask_ds.crs, "EPSG:4326", geom, precision=7
                )
            except Exception:
                geometry_wgs84 = geom
            class_counts[target] = class_counts.get(target, 0) + 1
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
                        "mean_brightness": round(segment_brightness, 3),
                        "mean_grvi": round(segment_grvi, 3),
                        "mean_saturation": round(segment_saturation, 3),
                        "evidence_basis": f"SamGeo object mask; {_evidence_for_target(target)}",
                        "confirmed": False,
                        "recommended_action": _recommended_action_for_target(target),
                        "screening_model": "samgeo_candidate_masks_v1",
                    },
                }
            )
            if len(features) >= max_candidates * 3:
                break
    return features, class_counts, sampled_masks


def _classify_samgeo_object(
    targets: list[str],
    *,
    area_m2: float,
    aspect: float,
    brightness: float,
    grvi: float,
    saturation: float,
    min_area_m2: float,
    max_area_m2: float,
) -> str | None:
    candidates: list[tuple[float, str]] = []
    for target in targets:
        if area_m2 < min_area_m2 or area_m2 > _class_max_area(target, max_area_m2):
            continue
        score = _confidence(target, area_m2, aspect, max(math.sqrt(area_m2), 1.0) * 4.0)
        if target == "building":
            if aspect <= 4.5 and grvi <= 0.08 and brightness >= 0.42 and saturation <= 0.58:
                candidates.append((score + 0.20, target))
        elif target == "road":
            if aspect >= 2.5 and grvi <= 0.12 and saturation <= 0.65:
                candidates.append((score + 0.10, target))
        elif target == "linear_boundary":
            if aspect >= 2.0:
                candidates.append((score, target))
        elif target == "tree_canopy":
            if grvi >= 0.16 and 0.16 <= brightness <= 0.62:
                candidates.append((score + 0.10, target))
        elif target == "crop_patch":
            if grvi >= 0.12 and brightness >= 0.20 and area_m2 >= min_area_m2 * 2:
                candidates.append((score + 0.08, target))
        elif target == "vegetation_patch":
            if grvi >= 0.14 and brightness >= 0.18:
                candidates.append((score + 0.12, target))
        elif target == "water":
            if brightness <= 0.32 and grvi <= 0.18:
                candidates.append((score + 0.10, target))
        elif target == "bare_rectangle":
            if grvi <= 0.08 and brightness >= 0.30 and aspect <= 6.0:
                candidates.append((score, target))
    if not candidates:
        return None
    return max(candidates, key=lambda item: item[0])[1]


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
    preference = str(engine_preference or "auto").strip().lower()
    if preference in TERRAMIND_PLANNER_ENGINE_ALIASES:
        return (
            "First choose likely semantic regions from the raster, then turn those "
            "regions into review polygons; SamGeo is only a downstream mask refiner "
            "when explicitly enabled."
        )
    if preference in {"samgeo", "segment-geospatial", "segment_geospatial"}:
        return (
            "Use SamGeo automatic masks because it was explicitly requested, then "
            "filter those masks into review polygons."
        )
    return (
        "Use quick raster evidence to create review polygons; deeper TerraMind or "
        "SamGeo passes can refine them when configured."
    )


def _planner_order_for_request(engine_preference: str) -> list[str]:
    preference = str(engine_preference or "auto").strip().lower()
    if preference in TERRAMIND_PLANNER_ENGINE_ALIASES:
        return [
            "TerraMind/TerraTorch semantic backbone or configured semantic head",
            "target-specific raster candidate masks",
            "GeoLibre-ready polygon cleanup/vector output",
            "optional SamGeo prompt refinement after regions are selected",
        ]
    if preference in {"samgeo", "segment-geospatial", "segment_geospatial"}:
        return [
            "SamGeo automatic mask generation",
            "target-specific raster mask classification",
            "GeoParquet/PMTiles review layer output",
        ]
    return [
        "target-specific raster candidate masks",
        "GeoLibre-ready polygon cleanup/vector output",
    ]


def _fallback_engine_name(engine_preference: str) -> str:
    preference = str(engine_preference or "auto").strip().lower()
    if preference in TERRAMIND_PLANNER_ENGINE_ALIASES:
        return "rasterio_semantic_proxy_waiting_for_terramind_head_v1"
    return "rasterio_numpy_candidate_extractor_v2"


def _summarize_attempt(attempt: dict[str, Any] | None) -> dict[str, Any] | None:
    if not attempt:
        return None
    selection = attempt.get("engines", {}).get("selection")
    return {
        "status": attempt.get("status"),
        "error": attempt.get("error"),
        "selection": selection if isinstance(selection, dict) else None,
    }


def _optional_engine_status() -> dict[str, dict[str, Any]]:
    return {
        "terramind_terratorch": {
            **_module_status("terratorch"),
            "note": (
                "TerraMind/TerraTorch is the semantic backbone path. It should "
                "choose likely object/land-cover regions before any promptable "
                "masking or polygon cleanup step."
            ),
        },
        "samgeo": _module_status("samgeo"),
        "segment_geospatial": _module_status("segment_geospatial"),
        "ultralytics": _module_status("ultralytics"),
        "torch": _module_status("torch"),
        "opencv": _module_status("cv2"),
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
