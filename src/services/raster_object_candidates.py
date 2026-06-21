from __future__ import annotations

import math
import os
import tempfile
import time
from dataclasses import dataclass
from typing import Any

from shapely.geometry import shape


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


def analyze_raster_object_candidates(payload: RasterObjectCandidateInput) -> dict[str, Any]:
    """Extract object candidate polygons from an uploaded RGB orthophoto.

    SamGeo is the preferred segmentation engine when requested. The lightweight
    rasterio/numpy path remains as a fallback so Sage can still answer honestly
    when model checkpoints are missing or unavailable.
    """
    _validate_payload(payload)
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

        red = ds.read(1, out_shape=(out_h, out_w), resampling=Resampling.bilinear, masked=True)
        green = ds.read(2, out_shape=(out_h, out_w), resampling=Resampling.bilinear, masked=True)
        blue = ds.read(3, out_shape=(out_h, out_w), resampling=Resampling.bilinear, masked=True)

        raster_bounds = payload.bounds_wgs84
        if not raster_bounds and ds.crs:
            raster_bounds = list(transform_bounds(ds.crs, "EPSG:4326", *ds.bounds, densify_pts=21))

        if ds.crs:
            source_crs = ds.crs
            source_transform = ds.transform * Affine.scale(ds.width / out_w, ds.height / out_h)
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
        mask = rasterio.features.sieve(mask.astype("uint8"), size=_sieve_size(out_w, out_h, target)).astype(bool)
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
    )[: payload.max_candidates]

    for index, feature in enumerate(candidate_features, start=1):
        feature["properties"]["candidate_rank"] = index

    elapsed_ms = round((time.perf_counter() - start) * 1000.0, 1)
    summary = {
        "source_layer_id": payload.layer_id,
        "source_layer_name": payload.layer_name,
        "candidate_count": len(candidate_features),
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
            "These are raster-derived object candidates. Treat counts as candidates, "
            "not confirmed houses, until checked against Open Buildings, OSM, field "
            "survey, SAM/YOLO segmentation, or human review."
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
        summary["candidate_building_count"] = int(summary["class_counts"].get("building", 0))
    if samgeo_attempt and samgeo_attempt.get("status") != "success":
        summary["samgeo_fallback_reason"] = samgeo_attempt.get("error") or samgeo_attempt.get("status")

    geoparquet = _write_features_to_geoparquet(candidate_features)
    result = {
        "status": "success",
        "summary": summary,
        "bbox": raster_bounds,
        "geojson": {"type": "FeatureCollection", "features": candidate_features},
        "engines": {
            "selection": {
                "requested": payload.engine_preference,
                "used": "rasterio_numpy_candidate_extractor_v1",
                "runtime": "python/rasterio/numpy/shapely",
            },
            "samgeo_attempt": _summarize_attempt(samgeo_attempt),
            "optional_engines": _optional_engine_status(),
            "next_upgrade_path": [
                "SAMGeo/segment-geospatial for promptable object masks",
                "YOLO segmentation fine-tuned on Rwanda orthophotos for class labels/counts",
                "GeoLibre-Rust/WASM for browser-side terrain/raster tools and polygon post-processing",
                "Open Buildings/OSM/local surveys for confirmed building footprints",
            ],
        },
    }
    if geoparquet:
        result["geoparquet"] = geoparquet
        summary["geoparquet_size_bytes"] = geoparquet["size_bytes"]
        summary["geoparquet_feature_count"] = geoparquet["feature_count"]
    return result


def _maybe_analyze_with_samgeo(payload: RasterObjectCandidateInput) -> dict[str, Any] | None:
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
            "engines": {"selection": {"requested": payload.engine_preference, "used": "unavailable"}},
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
    if os.environ.get("MUNDI_SAMGEO_AUTO", "").strip().lower() in {"1", "true", "yes"}:
        return True
    model_type = os.environ.get("MUNDI_SAMGEO_MODEL_TYPE", "vit_b")
    checkpoint_dir = os.environ.get("MUNDI_SAMGEO_CHECKPOINT_DIR") or os.environ.get(
        "TORCH_HOME",
        os.path.expanduser("~/.cache/torch/hub/checkpoints"),
    )
    checkpoint_name = {
        "vit_h": "sam_vit_h_4b8939.pth",
        "vit_l": "sam_vit_l_0b3195.pth",
        "vit_b": "sam_vit_b_01ec64.pth",
    }.get(model_type)
    return bool(checkpoint_name and os.path.exists(os.path.join(checkpoint_dir, checkpoint_name)))


def _analyze_with_samgeo(payload: RasterObjectCandidateInput, *, start: float) -> dict[str, Any]:
    _ensure_geoai_cache_env()
    from samgeo import SamGeo

    targets = _normalize_targets(payload.target_classes)
    model_type = os.environ.get("MUNDI_SAMGEO_MODEL_TYPE", "vit_b")
    checkpoint_dir = os.environ.get("MUNDI_SAMGEO_CHECKPOINT_DIR") or None
    device = os.environ.get("MUNDI_SAMGEO_DEVICE") or None
    points_per_side = int(os.environ.get("MUNDI_SAMGEO_POINTS_PER_SIDE", "16"))
    pred_iou_thresh = float(os.environ.get("MUNDI_SAMGEO_PRED_IOU_THRESH", "0.84"))
    stability_score_thresh = float(os.environ.get("MUNDI_SAMGEO_STABILITY_THRESH", "0.88"))
    min_mask_region_area = int(os.environ.get("MUNDI_SAMGEO_MIN_MASK_REGION_AREA", "24"))
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
    )[: payload.max_candidates]
    for index, feature in enumerate(candidate_features, start=1):
        feature["properties"]["candidate_rank"] = index

    elapsed_ms = round((time.perf_counter() - start) * 1000.0, 1)
    geoparquet = _write_features_to_geoparquet(candidate_features)
    summary = {
        "source_layer_id": payload.layer_id,
        "source_layer_name": payload.layer_name,
        "candidate_count": len(candidate_features),
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
            "SamGeo segmented object masks from the uploaded raster. These are still "
            "candidate objects, not confirmed houses or infrastructure, until checked "
            "against Open Buildings, OSM, field survey, a trained class detector, or human review."
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
        summary["candidate_building_count"] = int(summary["class_counts"].get("building", 0))
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
        "tree": "vegetation_patch",
        "trees": "vegetation_patch",
        "crop": "vegetation_patch",
        "crops": "vegetation_patch",
        "field": "vegetation_patch",
        "fields": "vegetation_patch",
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
        if target in {
            "building",
            "road",
            "linear_boundary",
            "vegetation_patch",
            "bare_rectangle",
            "water",
        } and target not in normalized:
            normalized.append(target)
    return normalized or ["building"]


def _write_sample_rgb_geotiff(payload: RasterObjectCandidateInput, output_path: str) -> dict[str, Any]:
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
            raise ValueError("SamGeo object extraction needs an RGB raster with at least 3 bands.")

        red = ds.read(1, out_shape=(out_h, out_w), resampling=Resampling.bilinear, masked=True)
        green = ds.read(2, out_shape=(out_h, out_w), resampling=Resampling.bilinear, masked=True)
        blue = ds.read(3, out_shape=(out_h, out_w), resampling=Resampling.bilinear, masked=True)
        valid = _valid_rgb_mask(red, green, blue)
        r, g, b = _normalize_rgb(red, green, blue, valid)
        rgb = (np.stack([r, g, b]) * 255.0).astype("uint8")
        rgb[:, ~valid] = 0

        raster_bounds = payload.bounds_wgs84
        if not raster_bounds and ds.crs:
            raster_bounds = list(transform_bounds(ds.crs, "EPSG:4326", *ds.bounds, densify_pts=21))

        if ds.crs:
            source_crs = ds.crs
            source_transform = ds.transform * Affine.scale(ds.width / out_w, ds.height / out_h)
        elif payload.bounds_wgs84:
            source_crs = CRS.from_epsg(4326)
            west, south, east, north = payload.bounds_wgs84
            source_transform = from_bounds(west, south, east, north, out_w, out_h)
        else:
            raise ValueError("SamGeo object extraction needs raster CRS or stored WGS84 bounds.")

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

            sampled_masks[target] = sampled_masks.get(target, 0) + int(np.count_nonzero(segment_pixels))
            confidence = _confidence(target, area_m2, aspect, source_geom.length)
            if confidence < confidence_threshold:
                continue
            try:
                geometry_wgs84 = transform_geom(mask_ds.crs, "EPSG:4326", geom, precision=7)
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
            if aspect <= 5.5 and grvi <= 0.18 and brightness >= 0.30:
                candidates.append((score + 0.20, target))
        elif target == "road":
            if aspect >= 2.5 and grvi <= 0.12 and saturation <= 0.65:
                candidates.append((score + 0.10, target))
        elif target == "linear_boundary":
            if aspect >= 2.0:
                candidates.append((score, target))
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
    if target == "building":
        bright_roof = brightness >= 0.52
        low_vegetation = grvi <= 0.10
        neutral_or_colored_roof = saturation <= 0.62
        blue_or_metal_roof = (b >= g * 0.92) & (brightness >= 0.42) & (grvi <= 0.16)
        return valid & low_vegetation & (bright_roof | blue_or_metal_roof) & neutral_or_colored_roof
    if target == "road":
        return valid & (grvi <= 0.08) & (brightness >= 0.38) & (saturation <= 0.55)
    if target == "linear_boundary":
        import numpy as np

        gradient = np.zeros_like(brightness, dtype="float32")
        gradient[:, 1:] = np.maximum(gradient[:, 1:], np.abs(brightness[:, 1:] - brightness[:, :-1]))
        gradient[1:, :] = np.maximum(gradient[1:, :], np.abs(brightness[1:, :] - brightness[:-1, :]))
        valid_gradient = gradient[valid]
        threshold = 0.18
        if valid_gradient.size:
            threshold = max(threshold, float(np.percentile(valid_gradient, 88)))
        return valid & (gradient >= threshold) & (brightness >= 0.16)
    if target == "bare_rectangle":
        return valid & (grvi <= 0.05) & (brightness >= 0.42) & (saturation <= 0.50)
    if target == "water":
        return valid & (brightness <= 0.28) & (b >= r * 0.95) & (grvi <= 0.18)
    return valid & (grvi >= 0.18) & (brightness >= 0.20)


def _sieve_size(width: int, height: int, target: str) -> int:
    total = max(1, width * height)
    if target == "building":
        return max(6, int(total * 0.000006))
    if target in {"road", "linear_boundary", "vegetation_patch"}:
        return max(12, int(total * 0.000015))
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
    for geom, value in rasterio.features.shapes(mask.astype("uint8"), mask=mask, transform=source_transform):
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
    return sorted(features, key=lambda feature: feature["properties"]["confidence"], reverse=True)[:max_candidates]


def _class_max_area(target: str, max_area_m2: float) -> float:
    if target == "building":
        return min(max_area_m2, 1_500.0)
    if target == "road":
        return max(max_area_m2, 10_000.0)
    if target == "linear_boundary":
        return max(max_area_m2, 12_000.0)
    if target == "vegetation_patch":
        return max(max_area_m2, 25_000.0)
    return max_area_m2


def _area_m2(geom: Any, crs: Any) -> float:
    is_geographic = bool(getattr(crs, "is_geographic", False))
    if crs and not is_geographic:
        return float(abs(geom.area))
    minx, miny, maxx, maxy = geom.bounds
    mid_lat = (miny + maxy) / 2.0
    return float(abs(geom.area) * 111_320.0 * 111_320.0 * max(math.cos(math.radians(mid_lat)), 0.1))


def _confidence(target: str, area_m2: float, aspect: float, perimeter: float) -> float:
    if target == "building":
        area_score = _triangular_score(area_m2, low=12, ideal=90, high=600)
        aspect_score = _clamp(1.0 - max(0.0, aspect - 1.0) / 5.0, 0.0, 1.0)
        compactness = _compactness(area_m2, perimeter)
        return _clamp(0.25 + 0.38 * area_score + 0.22 * aspect_score + 0.15 * compactness, 0.0, 0.96)
    if target == "road":
        return _clamp(0.30 + min(aspect / 12.0, 0.45) + min(area_m2 / 5000.0, 0.20), 0.0, 0.92)
    if target == "linear_boundary":
        return _clamp(0.25 + min(aspect / 14.0, 0.45) + min(area_m2 / 8000.0, 0.18), 0.0, 0.88)
    if target == "vegetation_patch":
        return _clamp(0.35 + min(area_m2 / 3000.0, 0.45), 0.0, 0.90)
    return _clamp(0.35 + _triangular_score(area_m2, low=20, ideal=400, high=5000) * 0.45, 0.0, 0.90)


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
        "vegetation_patch": "vegetation/crop/tree patch candidate",
        "bare_rectangle": "bare/playing-area rectangle candidate",
        "water": "water/wetness candidate",
    }.get(target, f"{target} candidate")


def _evidence_for_target(target: str) -> str:
    return {
        "building": "bright or metal/roof-like low-vegetation raster segment; not a confirmed building",
        "road": "elongated bright low-vegetation raster segment; not a confirmed road",
        "linear_boundary": "linear contrast segment; not a surveyed field or farm boundary",
        "vegetation_patch": "green raster segment grouped as vegetation/crop/tree context",
        "bare_rectangle": "bright low-vegetation segment with object-like area",
        "water": "dark/blue raster segment; not a hydrology-confirmed water body",
    }.get(target, "raster-derived candidate segment")


def _recommended_action_for_target(target: str) -> str:
    if target == "building":
        return "Verify with Open Buildings/OSM/local survey or a trained detector before using as a house count."
    if target == "road":
        return "Verify with OSM/road centerlines or field inspection before using as infrastructure evidence."
    if target == "linear_boundary":
        return "Verify with surveyed parcel data or field mapping before using as a farm/field boundary."
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


def _write_features_to_geoparquet(features: list[dict[str, Any]]) -> dict[str, Any] | None:
    if not features:
        return None

    import geopandas as gpd

    gdf = gpd.GeoDataFrame.from_features(features, crs="EPSG:4326")
    if gdf.empty:
        return None

    fd, output_path = tempfile.mkstemp(prefix="raster-object-candidates-", suffix=".parquet")
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
