"""Tier 2 semantic tools — turn pixel numbers into farmer-language verdicts.

Composes:
  - describe_user_raster (raster_query.py): metadata + sanity check
  - compute_zonal_stats  (raster_query.py): mean/std/percentiles over polygon
  - dssat_service crop calendar: maps capture date → growth stage
  - NDVI threshold table (this file): expected NDVI per crop per stage

The threshold values are starting points from agricultural literature (FAO crop
calendars, regional studies). They will need Rwanda-specific calibration as we
collect ground truth from BK Insurance adjudications. NDVI ranges are quite
robust across regions for the same crop+stage, but absolute thresholds may
shift for high-altitude or peri-equatorial conditions.
"""

import asyncio
import json
import logging
from datetime import datetime
from typing import Any, Dict, List, Tuple

from pydantic import BaseModel, Field

from src.tools.pyd import IngabeToolCallMetaArgs
from src.tools.raster_query import (
    describe_user_raster,
    DescribeUserRasterArgs,
    compute_zonal_stats,
    ComputeZonalStatsArgs,
)

logger = logging.getLogger(__name__)


# Healthy NDVI ranges per crop per stage. Ranges are (low, high) — within
# the band is "healthy", below low is stress, above high is exceptional growth.
NDVI_HEALTH_RANGES: Dict[str, Dict[str, Tuple[float, float]]] = {
    "maize": {
        "planting":   (0.10, 0.30),
        "vegetative": (0.45, 0.70),
        "flowering":  (0.65, 0.85),
        "grain_fill": (0.55, 0.78),
        "maturity":   (0.30, 0.55),
    },
    "beans": {
        "planting":   (0.10, 0.28),
        "vegetative": (0.40, 0.65),
        "flowering":  (0.55, 0.75),
        "pod_fill":   (0.45, 0.70),
        "maturity":   (0.25, 0.50),
    },
    "rice": {
        "planting":   (0.15, 0.35),
        "vegetative": (0.45, 0.70),
        "flowering":  (0.65, 0.85),
        "grain_fill": (0.55, 0.78),
        "maturity":   (0.30, 0.55),
    },
    "sorghum": {
        "planting":   (0.10, 0.28),
        "vegetative": (0.40, 0.65),
        "flowering":  (0.55, 0.78),
        "grain_fill": (0.45, 0.72),
        "maturity":   (0.25, 0.50),
    },
    "wheat": {
        "planting":   (0.10, 0.28),
        "vegetative": (0.40, 0.65),
        "flowering":  (0.55, 0.78),
        "grain_fill": (0.45, 0.70),
        "maturity":   (0.25, 0.50),
    },
    "_default": {
        "_any": (0.40, 0.70),
    },
}


def _stage_from_dap(dap: int, total_dap: int, crop: str) -> str:
    """Days-after-planting → growth-stage label, by fraction of total cycle.

    Generic 5-bucket model. Beans use 'pod_fill', everything else uses
    'grain_fill'. If the date is before planting or well past harvest, returns
    '_any' so the threshold lookup falls back to the crop's vegetative range.
    """
    if total_dap <= 0 or dap < 0 or dap > total_dap + 30:
        return "_any"

    pct = dap / total_dap
    boundaries = [0.125, 0.375, 0.625, 0.875, 1.0]
    if crop == "beans":
        labels = ["planting", "vegetative", "flowering", "pod_fill", "maturity"]
    else:
        labels = ["planting", "vegetative", "flowering", "grain_fill", "maturity"]

    for i, b in enumerate(boundaries):
        if pct <= b:
            return labels[i]
    return labels[-1]


def _verdict_from_ndvi(
    mean_ndvi: float, healthy_low: float, healthy_high: float
) -> Dict[str, Any]:
    """Map mean NDVI vs the expected healthy range into a verdict label."""
    if mean_ndvi >= healthy_high:
        return {
            "level": "exceptional",
            "message": (
                f"NDVI {mean_ndvi:.2f} is above the typical healthy range "
                f"({healthy_low:.2f}-{healthy_high:.2f}) for this crop at this stage. "
                f"Vigorous canopy."
            ),
        }
    if mean_ndvi >= healthy_low:
        return {
            "level": "healthy",
            "message": (
                f"NDVI {mean_ndvi:.2f} is within the healthy range "
                f"({healthy_low:.2f}-{healthy_high:.2f}) for this crop at this stage."
            ),
        }
    deficit = healthy_low - mean_ndvi
    if deficit < 0.10:
        return {
            "level": "moderate_stress",
            "message": (
                f"NDVI {mean_ndvi:.2f} is below the healthy range "
                f"({healthy_low:.2f}-{healthy_high:.2f}). Moderate stress — "
                f"approximately {deficit*100:.0f}% below the lower bound."
            ),
        }
    return {
        "level": "severe_stress",
        "message": (
            f"NDVI {mean_ndvi:.2f} is well below the healthy range "
            f"({healthy_low:.2f}-{healthy_high:.2f}). Severe stress."
        ),
    }


def _action_for(level: str, stats: Dict[str, Any]) -> str:
    spread = stats.get("p90", 0) - stats.get("p10", 0)
    heterogeneous = spread > 0.15
    spread_note = (
        " The field looks heterogeneous (some patches noticeably worse than others) — "
        "investigate spatial pattern with find_stress_zones."
        if heterogeneous
        else " The field looks fairly uniform."
    )
    base = {
        "exceptional": "Field is performing above expected for this stage. Continue current management.",
        "healthy": "Field is on track. No action needed beyond routine monitoring.",
        "moderate_stress": (
            "Inspect the field within 1-2 days. Check soil moisture, recent weather, and "
            "any visible damage."
        ),
        "severe_stress": (
            "Severe stress — site visit recommended within 24-48 hours. Consider "
            "supplemental irrigation, pest/disease inspection, or other interventions. "
            "If insured, this may approach or breach the trigger threshold."
        ),
    }
    return base.get(level, "Monitor.") + spread_note


class InterpretRasterHealthArgs(BaseModel):
    layer_id: str = Field(
        ...,
        description="The layer_id of the user-uploaded NDVI raster (drone export, computed NDVI from satellite, etc.).",
    )
    crop: str = Field(
        ...,
        description=(
            "The crop in the field. Supported: 'maize', 'beans', 'rice', 'sorghum', "
            "'wheat'. Pass 'unknown' if the user hasn't specified — defaults will be used."
        ),
    )
    band: int = Field(
        ...,
        description=(
            "Which band of the raster contains NDVI (1-indexed). For typical drone exports "
            "with 4 bands [R, NDVI, NDRE, alpha], use band=2. For dedicated single-band "
            "NDVI rasters, use band=1. Default to 1 if uncertain."
        ),
    )
    polygon_geojson: str = Field(
        ...,
        description=(
            "GeoJSON Polygon string defining the field boundary, OR empty string '' to "
            "evaluate the whole raster."
        ),
    )
    captured_date: str = Field(
        ...,
        description=(
            "Date the imagery was captured, YYYY-MM-DD format. Pass empty string '' if "
            "unknown — today's date will be used as an approximation (less accurate for "
            "growth-stage inference)."
        ),
    )


async def interpret_raster_health(
    args: InterpretRasterHealthArgs, meta: IngabeToolCallMetaArgs
) -> dict:
    """Interpret pixel data from a user-uploaded NDVI raster as a farmer-language verdict on field health, given a crop type, growth stage, and field polygon. Composes describe_user_raster (for metadata + CRS sanity) plus compute_zonal_stats (for pixel statistics) plus the agricultural crop calendar (to map capture date to growth stage) plus a curated NDVI threshold table (to convert numbers to verdicts). Returns a verdict (exceptional / healthy / moderate_stress / severe_stress) plus evidence and recommended action — NOT raw band statistics. ALWAYS use this when the user asks about the health of a field they have a drone NDVI or NDVI raster for. Do NOT use for satellite-only questions (use get_field_health for those)."""
    from src.services.dssat_service import _CROP_CALENDARS, detect_current_season

    desc = await describe_user_raster(
        DescribeUserRasterArgs(layer_id=args.layer_id), meta
    )
    if "error" in desc:
        return desc

    # Type guard: refuse if the raster is not NDVI-shaped or multispectral.
    raster_type = desc.get("raster_type")
    if raster_type in ("rgb_visual", "dem"):
        return {
            "error": "wrong_raster_type",
            "message": (
                f"'{desc['name']}' is a {raster_type} raster — true NDVI cannot be derived "
                f"from it (no NIR band). Use analyze_rgb_field instead for visual greenness "
                f"and field-coverage analysis, or upload an NDVI raster (single-band float, "
                f"or 4-band drone export with NDVI in band 2) for true health analysis."
            ),
            "raster_type": raster_type,
            "recommended_tool": "analyze_rgb_field" if raster_type == "rgb_visual" else "compute_zonal_stats",
        }

    stats = await compute_zonal_stats(
        ComputeZonalStatsArgs(
            layer_id=args.layer_id,
            polygon_geojson=args.polygon_geojson,
            band=args.band,
        ),
        meta,
    )
    if "error" in stats:
        return {"error": f"Could not compute pixel stats: {stats['error']}"}

    mean_ndvi = stats["mean"]

    # Auto-rescale if the band is uint8-packed NDVI (common drone format).
    # Pattern: NDVI_byte = (NDVI_float + 1) * 127.5  →  decode to [-1, 1].
    looks_like_uint8_packed = (
        50 <= mean_ndvi <= 200
        and stats["max"] <= 255
        and stats["min"] >= 0
        and abs(mean_ndvi) > 1.5
    )
    if looks_like_uint8_packed:
        original_uint8_mean = mean_ndvi
        mean_ndvi = (mean_ndvi / 127.5) - 1.0
        stats = {
            **stats,
            "mean": round(mean_ndvi, 4),
            "min": round((stats["min"] / 127.5) - 1.0, 4),
            "max": round((stats["max"] / 127.5) - 1.0, 4),
            "p10": round((stats["p10"] / 127.5) - 1.0, 4),
            "p50": round((stats["p50"] / 127.5) - 1.0, 4),
            "p90": round((stats["p90"] / 127.5) - 1.0, 4),
            "_uint8_rescaled_from": round(original_uint8_mean, 1),
        }

    # Final NDVI plausibility check
    if not (-1.5 <= mean_ndvi <= 1.5):
        return {
            "error": "values_outside_ndvi_range",
            "message": (
                f"After rescaling attempts, the mean value {mean_ndvi:.2f} is still "
                f"outside the plausible NDVI range [-1, 1]. Band {args.band} of "
                f"'{desc['name']}' is probably not NDVI. Use describe_user_raster to "
                f"inspect band layout or compute_zonal_stats for raw numbers."
            ),
            "observed_mean": mean_ndvi,
            "raster_type": raster_type,
        }

    stage = "_any"
    dap = None
    season = None
    crop_in_calendar = args.crop in _CROP_CALENDARS
    if args.crop and args.crop != "unknown" and crop_in_calendar:
        try:
            ref_date = (
                datetime.fromisoformat(args.captured_date)
                if args.captured_date and args.captured_date.strip()
                else datetime.utcnow()
            )
        except ValueError:
            ref_date = datetime.utcnow()

        season = detect_current_season(args.crop, ref_date)
        cal = _CROP_CALENDARS[args.crop].get(season)
        if cal:
            try:
                planting_mmdd = cal["planting"]
                harvest_dap = int(cal["harvest_dap"])
                planting_month = int(planting_mmdd[:2])
                planting_day = int(planting_mmdd[3:5])
                planting_dt = datetime(ref_date.year, planting_month, planting_day)
                if planting_dt > ref_date:
                    planting_dt = datetime(ref_date.year - 1, planting_month, planting_day)
                dap_calc = (ref_date - planting_dt).days
                if 0 <= dap_calc <= harvest_dap + 30:
                    dap = dap_calc
                    stage = _stage_from_dap(dap, harvest_dap, args.crop)
            except Exception:
                logger.exception("DAP calculation failed for %s", args.crop)

    crop_key = args.crop if args.crop in NDVI_HEALTH_RANGES else "_default"
    crop_ranges = NDVI_HEALTH_RANGES.get(crop_key, NDVI_HEALTH_RANGES["_default"])
    if stage in crop_ranges:
        stage_key = stage
    elif crop_key == "_default":
        stage_key = "_any"
    else:
        stage_key = "vegetative" if "vegetative" in crop_ranges else next(iter(crop_ranges))
    healthy_low, healthy_high = crop_ranges[stage_key]

    verdict = _verdict_from_ndvi(mean_ndvi, healthy_low, healthy_high)
    recommended = _action_for(verdict["level"], stats)

    response = {
        "verdict": verdict["level"],
        "message": verdict["message"],
        "recommended_action": recommended,
        "evidence": {
            "layer_name": desc["name"],
            "area_analyzed_ha": desc.get("area_ha"),
            "captured_date": (
                args.captured_date.strip()
                if args.captured_date and args.captured_date.strip()
                else "today (assumed — provide capture date for better stage inference)"
            ),
            "crop": args.crop,
            "season": season,
            "growth_stage": stage,
            "days_after_planting": dap,
            "expected_healthy_ndvi_range": [healthy_low, healthy_high],
            "observed_ndvi_mean": mean_ndvi,
            "observed_ndvi_min": stats["min"],
            "observed_ndvi_max": stats["max"],
            "observed_ndvi_p10": stats["p10"],
            "observed_ndvi_p90": stats["p90"],
            "spread_p10_to_p90": round(stats["p90"] - stats["p10"], 4),
            "valid_pixel_pct": stats["valid_pixel_pct"],
            "polygon_used": stats["polygon_used"],
        },
    }

    if not crop_in_calendar and args.crop != "unknown":
        response["evidence"]["note"] = (
            f"Crop '{args.crop}' is not in the supported crop calendar — used "
            f"generic NDVI thresholds. Verdict accuracy is reduced."
        )

    if desc.get("sanity_warning"):
        response["sanity_warning"] = desc["sanity_warning"]

    # Build displayable_geojson tagging the field polygon with ndvi_mean + verdict
    # so Sage can paint the field with the field_health style preset.
    try:
        if args.polygon_geojson and args.polygon_geojson.strip():
            from shapely.geometry import shape as _shape
            geom = json.loads(args.polygon_geojson)
            if isinstance(geom, dict) and geom.get("type") in ("Polygon", "MultiPolygon"):
                feature = {
                    "type": "Feature",
                    "geometry": geom,
                    "properties": {
                        "ndvi_mean": mean_ndvi,
                        "verdict": verdict["level"],
                        "crop": args.crop,
                        "growth_stage": stage,
                    },
                }
                fc = {"type": "FeatureCollection", "features": [feature]}
                b = _shape(geom).bounds
                response["displayable_geojson"] = {
                    "geojson": fc,
                    "style_hint": "field_health",
                    "title": f"Field Health — {desc['name']} ({verdict['level']})",
                    "bbox": f"{b[0]},{b[1]},{b[2]},{b[3]}",
                }
    except Exception:
        logger.debug("displayable_geojson build skipped for interpret_raster_health", exc_info=True)

    # Append to Brain timeline so the verdict survives the conversation.
    try:
        from src.services.raster_brain_link import record_raster_analysis
        await record_raster_analysis(
            layer_id=args.layer_id,
            summary=(
                f"Health verdict: {verdict['level']}. "
                f"{args.crop} at {stage}, NDVI {mean_ndvi:.2f} "
                f"vs healthy {healthy_low:.2f}-{healthy_high:.2f}."
            ),
            source="interpret_raster_health",
            detail=json.dumps(response.get("evidence", {}), default=str)[:4000],
            owner_uuid=str(meta.user_uuid),
        )
    except Exception:
        logger.debug("Brain timeline write skipped for interpret_raster_health", exc_info=True)

    return response


# ── find_stress_zones ───────────────────────────────────────────────────────


class FindStressZonesArgs(BaseModel):
    layer_id: str = Field(
        ...,
        description="The layer_id of a user-uploaded NDVI raster (single-band float, or band 2 of a 4-band drone NDVI export).",
    )
    band: int = Field(
        ...,
        description="Which band contains NDVI (1-indexed). For single-band NDVI use 1; for typical 4-band drone NDVI export use 2.",
    )
    ndvi_threshold: float = Field(
        ...,
        description="Pixels with NDVI BELOW this threshold are considered stressed. Reasonable defaults: 0.30 for severe stress, 0.40 for moderate. For a vegetative-stage maize field expecting NDVI ~0.55-0.70, use 0.40 to flag clear stress patches.",
    )
    min_area_ha: float = Field(
        ...,
        description="Skip stress clusters smaller than this many hectares. Use 0.5 to filter out noise patches; use 0.1 for fine-grained analysis. Connected stress patches below this size are not reported.",
    )


async def find_stress_zones(
    args: FindStressZonesArgs, meta: IngabeToolCallMetaArgs
) -> dict:
    """Find connected clusters of low-NDVI pixels in a user-uploaded NDVI raster — the spatial answer to "where is the stress in my field?". Returns a list of stress zones with each zone's center coordinate, area in hectares, mean NDVI inside the cluster, and severity label (severe/moderate/mild). Refuses non-NDVI rasters with a pointer to the right tool. Useful for: locating drought patches, finding diseased zones, prioritizing field walks. For a single overall verdict use interpret_raster_health; for a histogram use get_value_distribution."""
    from src.structures import get_async_read_connection
    from src.utils import get_async_s3_client, get_bucket_name
    from src.tools.raster_query import (
        describe_user_raster,
        DescribeUserRasterArgs,
    )

    desc = await describe_user_raster(
        DescribeUserRasterArgs(layer_id=args.layer_id), meta
    )
    if "error" in desc:
        return desc

    raster_type = desc.get("raster_type")
    if raster_type in ("rgb_visual", "dem"):
        return {
            "error": "wrong_raster_type",
            "message": (
                f"'{desc['name']}' is a {raster_type} raster — true NDVI cannot "
                f"be derived from it. For RGB orthos use analyze_rgb_field "
                f"(GRVI-based stress detection)."
            ),
            "raster_type": raster_type,
            "recommended_tool": "analyze_rgb_field" if raster_type == "rgb_visual" else None,
        }

    async with get_async_read_connection() as conn:
        row = await conn.fetchrow(
            """
            SELECT layer_id, type, s3_key, bounds, metadata, owner_uuid
            FROM map_layers
            WHERE layer_id = $1
            """,
            args.layer_id,
        )
    if not row:
        return {"error": f"Layer {args.layer_id} not found."}
    if str(row["owner_uuid"]) != str(meta.user_uuid):
        return {"error": f"Layer {args.layer_id} is not owned by you."}

    metadata = (
        json.loads(row["metadata"])
        if isinstance(row["metadata"], str)
        else (dict(row["metadata"]) if row["metadata"] else {})
    )
    cog_key = metadata.get("cog_key") or row["s3_key"]
    if not cog_key:
        return {"error": "Layer has no COG yet — try again in a minute."}

    s3_client = await get_async_s3_client()
    bucket = get_bucket_name()
    cog_url = await s3_client.generate_presigned_url(
        "get_object",
        Params={"Bucket": bucket, "Key": cog_key},
        ExpiresIn=900,
    )

    import os as _os
    _os.environ.setdefault("GDAL_HTTP_UNSAFESSL", "YES")
    band = args.band
    ndvi_threshold = float(args.ndvi_threshold)
    min_area_ha = float(args.min_area_ha)

    def _compute() -> Dict[str, Any]:
        import numpy as np
        import rasterio
        from rasterio.enums import Resampling
        from scipy import ndimage

        with rasterio.open(cog_url) as ds:
            target_long = 1024
            long_native = max(ds.width, ds.height)
            ovr = max(1, long_native // target_long)
            out_h = max(1, ds.height // ovr)
            out_w = max(1, ds.width // ovr)
            arr = ds.read(
                band, out_shape=(out_h, out_w),
                resampling=Resampling.average, masked=True,
            )

            data = arr.data.astype("float32") if hasattr(arr, "data") else arr.astype("float32")

            # Auto-rescale uint8-packed NDVI [0,255] → [-1,1] if needed
            valid_for_check = data[~arr.mask] if hasattr(arr, "mask") else data.flatten()
            valid_for_check = valid_for_check[~np.isnan(valid_for_check)]
            if valid_for_check.size > 0 and valid_for_check.max() > 5 and valid_for_check.max() <= 255 and valid_for_check.min() >= 0:
                data = (data / 127.5) - 1.0

            invalid_mask = arr.mask if hasattr(arr, "mask") else np.isnan(data)
            stress_mask = (data < ndvi_threshold) & (~invalid_mask)
            if not stress_mask.any():
                return {
                    "stress_zones": [],
                    "total_stress_area_ha": 0.0,
                    "message": (
                        f"No stress detected — all {int((~invalid_mask).sum())} valid pixels "
                        f"have NDVI ≥ {ndvi_threshold}."
                    ),
                }

            # Connected components
            labeled, ncomponents = ndimage.label(stress_mask)
            if ncomponents == 0:
                return {"stress_zones": [], "total_stress_area_ha": 0.0}

            # Pixel area in m² from native bounds
            left, bottom, right, top = ds.bounds
            crs = ds.crs
            if crs and crs.is_projected:
                px_w_m = (right - left) / float(out_w)
                px_h_m = (top - bottom) / float(out_h)
            else:
                # WGS84 fallback (~1.5km × 0.9km drone field, equator-ish)
                center_lat = (bottom + top) / 2
                px_w_m = (right - left) * 111320.0 * abs(np.cos(np.radians(center_lat))) / float(out_w)
                px_h_m = (top - bottom) * 111320.0 / float(out_h)
            pixel_area_m2 = max(0.0001, abs(px_w_m * px_h_m))
            min_pixels = max(1, int((min_area_ha * 10000.0) / pixel_area_m2))

            zones: List[Dict[str, Any]] = []
            for cid in range(1, ncomponents + 1):
                cluster_mask = labeled == cid
                pixel_count = int(cluster_mask.sum())
                if pixel_count < min_pixels:
                    continue
                cluster_area_ha = round(pixel_count * pixel_area_m2 / 10000.0, 2)
                cluster_values = data[cluster_mask]
                cluster_mean = round(float(cluster_values.mean()), 4)
                cluster_min = round(float(cluster_values.min()), 4)

                # Center of cluster in pixel space → projected/WGS84
                rows_idx, cols_idx = np.where(cluster_mask)
                center_row = float(rows_idx.mean())
                center_col = float(cols_idx.mean())
                center_x = left + (center_col + 0.5) * px_w_m
                center_y = top - (center_row + 0.5) * px_h_m
                if crs and crs.is_projected:
                    from rasterio.warp import transform
                    lons, lats = transform(crs, "EPSG:4326", [center_x], [center_y])
                    center_lon, center_lat = lons[0], lats[0]
                else:
                    center_lon, center_lat = center_x, center_y

                if cluster_mean < 0.20:
                    severity = "severe"
                elif cluster_mean < ndvi_threshold:
                    severity = "moderate"
                else:
                    severity = "mild"

                zones.append({
                    "zone_id": cid,
                    "center_lon": round(center_lon, 6),
                    "center_lat": round(center_lat, 6),
                    "area_ha": cluster_area_ha,
                    "mean_ndvi": cluster_mean,
                    "min_ndvi": cluster_min,
                    "pixel_count": pixel_count,
                    "severity": severity,
                })

            zones.sort(key=lambda z: z["area_ha"], reverse=True)
            total_ha = round(sum(z["area_ha"] for z in zones), 2)
            return {
                "stress_zones": zones[:50],  # cap output at 50 zones
                "total_stress_area_ha": total_ha,
                "zone_count": len(zones),
                "min_pixels_per_zone": min_pixels,
                "pixel_area_m2": round(pixel_area_m2, 4),
            }

    try:
        result = await asyncio.wait_for(
            asyncio.get_running_loop().run_in_executor(None, _compute),
            timeout=90,
        )
        if "error" in result:
            return result
        response = {
            "layer_id": args.layer_id,
            "name": desc["name"],
            "ndvi_threshold": ndvi_threshold,
            "min_area_ha": min_area_ha,
            **result,
        }
        # Build displayable_geojson so Sage can paint the stress zones on the
        # map. We don't have polygon geometry from the connected-components
        # compute (only centroids + area), so buffer each center by its area
        # to make a small circular polygon that's good enough for visualization.
        try:
            import math as _math
            from shapely.geometry import Point as _Point, mapping as _mapping
            zones = result.get("stress_zones", [])
            severity_to_int = {"mild": 1, "moderate": 2, "severe": 3}
            features = []
            min_lon = min_lat = float("inf")
            max_lon = max_lat = float("-inf")
            for z in zones:
                lon, lat = z.get("center_lon"), z.get("center_lat")
                if lon is None or lat is None:
                    continue
                area_ha = float(z.get("area_ha") or 0.0)
                radius_m = _math.sqrt(max(area_ha, 0.01) * 10000.0 / _math.pi)
                radius_deg = radius_m / 111000.0
                poly = _Point(lon, lat).buffer(radius_deg)
                if not poly.is_valid or poly.is_empty:
                    continue
                features.append({
                    "type": "Feature",
                    "geometry": _mapping(poly),
                    "properties": {
                        "zone_id": z.get("zone_id"),
                        "severity": severity_to_int.get(z.get("severity"), 2),
                        "severity_label": z.get("severity"),
                        "area_ha": area_ha,
                        "mean_ndvi": z.get("mean_ndvi"),
                    },
                })
                b = poly.bounds
                min_lon, min_lat = min(min_lon, b[0]), min(min_lat, b[1])
                max_lon, max_lat = max(max_lon, b[2]), max(max_lat, b[3])
            if features:
                response["displayable_geojson"] = {
                    "geojson": {"type": "FeatureCollection", "features": features},
                    "style_hint": "stress_zones",
                    "title": f"Stress Zones — {desc['name']} ({len(features)} cluster{'s' if len(features) != 1 else ''})",
                    "bbox": f"{min_lon},{min_lat},{max_lon},{max_lat}",
                }
        except Exception:
            logger.debug("displayable_geojson build skipped for find_stress_zones", exc_info=True)
        # Append to Brain timeline so future Sage queries can recall the
        # zone count without re-running the connected-components compute.
        try:
            from src.services.raster_brain_link import record_raster_analysis
            zones = result.get("stress_zones", [])
            total_ha = result.get("total_stress_area_ha", 0.0)
            await record_raster_analysis(
                layer_id=args.layer_id,
                summary=(
                    f"Found {len(zones)} stress zone(s), total {total_ha} ha "
                    f"at NDVI threshold {ndvi_threshold}, min area {min_area_ha} ha."
                ),
                source="find_stress_zones",
                detail=json.dumps({
                    "ndvi_threshold": ndvi_threshold,
                    "min_area_ha": min_area_ha,
                    "zones": [
                        {k: z.get(k) for k in ("zone_id", "area_ha", "mean_ndvi",
                                               "severity", "center_lon", "center_lat")}
                        for z in zones[:20]
                    ],
                    "total_stress_area_ha": total_ha,
                    "zone_count": result.get("zone_count"),
                }, default=str)[:4000],
                owner_uuid=str(meta.user_uuid),
            )
        except Exception:
            logger.debug("Brain timeline write skipped for find_stress_zones", exc_info=True)
        return response
    except asyncio.TimeoutError:
        return {"error": "find_stress_zones timed out after 90 seconds."}
    except Exception as e:
        logger.exception("find_stress_zones failed for layer %s", args.layer_id)
        return {"error": f"Failed: {str(e)[:200]}"}


# ── compare_rasters (Method 3 change detection — KEYSTONE) ──────────────────


def _stage_midpoint_ndvi(crop: str, stage: str) -> float:
    """Look up the midpoint of the healthy NDVI range for a crop+stage."""
    crop_key = crop if crop in NDVI_HEALTH_RANGES else "_default"
    crop_ranges = NDVI_HEALTH_RANGES.get(crop_key, NDVI_HEALTH_RANGES["_default"])
    rng = crop_ranges.get(stage) or crop_ranges.get("_any") or (0.5, 0.7)
    return (rng[0] + rng[1]) / 2.0


class CompareRastersArgs(BaseModel):
    layer_id_a: str = Field(
        ...,
        description="The first user-uploaded NDVI raster (typically the EARLIER flight or 'before' timepoint).",
    )
    layer_id_b: str = Field(
        ...,
        description="The second user-uploaded NDVI raster (typically the LATER flight or 'after' timepoint). Must overlap with layer_id_a.",
    )
    band: int = Field(
        ...,
        description="Which band contains NDVI (1-indexed). For single-band NDVI use 1; for typical 4-band drone NDVI export use 2.",
    )
    crop: str = Field(
        ...,
        description="The crop in the field — used to look up the expected NDVI delta between the two captures' growth stages. Supported: 'maize', 'beans', 'rice', 'sorghum', 'wheat'. Pass 'unknown' if no crop information.",
    )


async def compare_rasters(
    args: CompareRastersArgs, meta: IngabeToolCallMetaArgs
) -> dict:
    """Compare two user-uploaded NDVI rasters of the same field at different times to detect change. Method 3: per-pixel NDVI diff at downsampled resolution + crop-stage-aware expected delta + CHIRPS rainfall context. Returns: time interval in days, observed mean NDVI delta, expected delta given crop stage transitions, anomaly (observed - expected), rainfall between captures, area declining significantly, and a verdict (no_significant_change / expected_growth / mild_decline / drought_signature / harvest_pattern / suspicious_pattern). Refuses if bounds don't overlap or either layer is RGB-only. The keystone tool for insurance change detection."""
    from src.structures import get_async_read_connection
    from src.utils import get_async_s3_client, get_bucket_name
    from src.tools.raster_query import (
        describe_user_raster,
        DescribeUserRasterArgs,
    )
    from src.services.dssat_service import _CROP_CALENDARS, detect_current_season

    # 1. Validate both layers + check both are NDVI-shaped
    for lid in (args.layer_id_a, args.layer_id_b):
        d = await describe_user_raster(DescribeUserRasterArgs(layer_id=lid), meta)
        if "error" in d:
            return {"error": f"Layer {lid}: {d.get('message', d['error'])}"}
        rt = d.get("raster_type")
        if rt in ("rgb_visual", "dem"):
            return {
                "error": "wrong_raster_type",
                "message": (
                    f"Layer {lid} is type '{rt}' — true NDVI cannot be derived. "
                    f"compare_rasters needs NDVI-shaped data on both sides."
                ),
                "raster_type": rt,
                "layer_id": lid,
            }

    # 2. Fetch both DB rows + verify ownership + sort by created_on
    async with get_async_read_connection() as conn:
        rows = await conn.fetch(
            """
            SELECT layer_id, name, type, s3_key, bounds, metadata, owner_uuid, created_on
            FROM map_layers
            WHERE layer_id = ANY($1::text[])
            """,
            [args.layer_id_a, args.layer_id_b],
        )
    if len(rows) != 2:
        return {"error": "One or both layers not found."}
    for r in rows:
        if str(r["owner_uuid"]) != str(meta.user_uuid):
            return {"error": f"Layer {r['layer_id']} is not owned by you."}

    by_id = {r["layer_id"]: r for r in rows}
    row_a = by_id[args.layer_id_a]
    row_b = by_id[args.layer_id_b]

    # Chronological sort: t1 (earlier) and t2 (later), regardless of arg order
    if row_a["created_on"] <= row_b["created_on"]:
        t1_row, t2_row = row_a, row_b
    else:
        t1_row, t2_row = row_b, row_a
    t1_date: datetime = t1_row["created_on"]
    t2_date: datetime = t2_row["created_on"]
    interval_days = (t2_date - t1_date).total_seconds() / 86400.0

    # 3. Bounds overlap + alignment check.
    # The downsampled-grid comparison (below) aligns the two rasters by
    # array index, NOT by geographic position. That's only safe when both
    # flights cover ~the same field with ~the same framing. We enforce this
    # here by requiring (a) bbox overlap exists and (b) bbox centers are
    # within ~100m of each other — beyond that, per-pixel deltas would
    # compare non-corresponding ground locations.
    b1 = list(t1_row["bounds"]) if t1_row["bounds"] else None
    b2 = list(t2_row["bounds"]) if t2_row["bounds"] else None
    if not b1 or not b2 or len(b1) != 4 or len(b2) != 4:
        return {"error": "Both layers must have bounds."}
    inter_west = max(b1[0], b2[0])
    inter_south = max(b1[1], b2[1])
    inter_east = min(b1[2], b2[2])
    inter_north = min(b1[3], b2[3])
    if inter_west >= inter_east or inter_south >= inter_north:
        return {
            "error": "bounds_no_overlap",
            "message": (
                f"Layer {t1_row['layer_id']} bounds {b1} do not overlap with "
                f"layer {t2_row['layer_id']} bounds {b2}. compare_rasters needs "
                f"two flights of the same field."
            ),
        }
    inter_bbox_wgs84 = [inter_west, inter_south, inter_east, inter_north]

    # Bounds-misalignment guard. Compute approximate distance between bbox
    # centers in meters (WGS84 → great-circle approximation). For two flights
    # of the same field this should be <100m.
    import math as _math
    c1_lon, c1_lat = (b1[0] + b1[2]) / 2.0, (b1[1] + b1[3]) / 2.0
    c2_lon, c2_lat = (b2[0] + b2[2]) / 2.0, (b2[1] + b2[3]) / 2.0
    avg_lat = (c1_lat + c2_lat) / 2.0
    dx_m = (c2_lon - c1_lon) * 111320.0 * abs(_math.cos(_math.radians(avg_lat)))
    dy_m = (c2_lat - c1_lat) * 111320.0
    center_offset_m = _math.sqrt(dx_m * dx_m + dy_m * dy_m)
    BOUNDS_MISALIGN_THRESHOLD_M = 100.0
    if center_offset_m > BOUNDS_MISALIGN_THRESHOLD_M:
        return {
            "error": "bounds_misaligned",
            "message": (
                f"Bounding-box centers are ~{center_offset_m:.0f}m apart "
                f"(threshold: {BOUNDS_MISALIGN_THRESHOLD_M:.0f}m). The two "
                f"flights cover different framings of the field, so a per-pixel "
                f"NDVI delta would compare non-corresponding ground locations. "
                f"Re-fly with consistent extents, or trim/reproject both rasters "
                f"onto a common grid before comparing."
            ),
            "center_offset_m": round(center_offset_m, 1),
            "layer_a_center": [round(c1_lon, 6), round(c1_lat, 6)],
            "layer_b_center": [round(c2_lon, 6), round(c2_lat, 6)],
        }

    meta1 = (
        json.loads(t1_row["metadata"]) if isinstance(t1_row["metadata"], str)
        else (dict(t1_row["metadata"]) if t1_row["metadata"] else {})
    )
    meta2 = (
        json.loads(t2_row["metadata"]) if isinstance(t2_row["metadata"], str)
        else (dict(t2_row["metadata"]) if t2_row["metadata"] else {})
    )
    cog1 = meta1.get("cog_key") or t1_row["s3_key"]
    cog2 = meta2.get("cog_key") or t2_row["s3_key"]

    s3_client = await get_async_s3_client()
    bucket = get_bucket_name()
    url1 = await s3_client.generate_presigned_url(
        "get_object", Params={"Bucket": bucket, "Key": cog1}, ExpiresIn=900,
    )
    url2 = await s3_client.generate_presigned_url(
        "get_object", Params={"Bucket": bucket, "Key": cog2}, ExpiresIn=900,
    )

    import os as _os
    _os.environ.setdefault("GDAL_HTTP_UNSAFESSL", "YES")
    band = args.band

    def _read_overlap_to_grid() -> Dict[str, Any]:
        import numpy as np
        import rasterio
        from rasterio.enums import Resampling

        common_size = 512  # downsample target — keeps it under 1s per side

        def _read_to_common_grid(url):
            # Read the entire raster downsampled to common_size×common_size.
            # For drone NDVI flights of the same field, the whole-raster extent
            # IS the field — windowed reads in WGS84 would mismatch the dataset's
            # native CRS transform, so we just downsample the whole thing.
            with rasterio.open(url) as ds:
                out_h, out_w = common_size, common_size
                arr = ds.read(
                    band, out_shape=(out_h, out_w),
                    resampling=Resampling.average, masked=True,
                )
                data = arr.data.astype("float32") if hasattr(arr, "data") else arr.astype("float32")
                # Auto-rescale uint8-packed NDVI [0..255] → [-1..1]
                valid_for_check = data[~arr.mask] if hasattr(arr, "mask") else data.flatten()
                valid_for_check = valid_for_check[~np.isnan(valid_for_check)]
                if (
                    valid_for_check.size > 0
                    and valid_for_check.max() > 5
                    and valid_for_check.max() <= 255
                    and valid_for_check.min() >= 0
                ):
                    data = (data / 127.5) - 1.0
                mask = arr.mask if hasattr(arr, "mask") else np.isnan(data)
                return data, mask

        a_data, a_mask = _read_to_common_grid(url1)
        b_data, b_mask = _read_to_common_grid(url2)

        # Match shapes — reduce to min common shape
        min_h = min(a_data.shape[0], b_data.shape[0])
        min_w = min(a_data.shape[1], b_data.shape[1])
        a_data = a_data[:min_h, :min_w]
        b_data = b_data[:min_h, :min_w]
        a_mask = a_mask[:min_h, :min_w]
        b_mask = b_mask[:min_h, :min_w]

        valid = ~(a_mask | b_mask) & ~np.isnan(a_data) & ~np.isnan(b_data)
        if not valid.any():
            return {"error": "No valid overlapping pixels between the two layers."}

        delta = b_data - a_data
        delta_valid = delta[valid]

        delta_mean = round(float(delta_valid.mean()), 4)
        delta_p10 = round(float(np.percentile(delta_valid, 10)), 4)
        delta_p50 = round(float(np.percentile(delta_valid, 50)), 4)
        delta_p90 = round(float(np.percentile(delta_valid, 90)), 4)
        valid_pct = round(float(valid.sum()) / float(valid.size) * 100.0, 1)

        # Approx pixel area in m² for declining-area calculation
        center_lat = (inter_south + inter_north) / 2
        bbox_w_m = (inter_east - inter_west) * 111320.0 * abs(np.cos(np.radians(center_lat)))
        bbox_h_m = (inter_north - inter_south) * 111320.0
        pixel_area_m2 = max(0.0001, abs((bbox_w_m / min_w) * (bbox_h_m / min_h)))

        # Significant decline = >0.10 NDVI drop
        declining_pixels = int(((delta < -0.10) & valid).sum())
        declining_area_ha = round(declining_pixels * pixel_area_m2 / 10000.0, 2)

        return {
            "delta_mean": delta_mean,
            "delta_p10": delta_p10,
            "delta_p50": delta_p50,
            "delta_p90": delta_p90,
            "valid_overlap_pct": valid_pct,
            "declining_area_ha": declining_area_ha,
            "declining_pixel_count": declining_pixels,
        }

    try:
        diff_result = await asyncio.wait_for(
            asyncio.get_running_loop().run_in_executor(None, _read_overlap_to_grid),
            timeout=90,
        )
    except asyncio.TimeoutError:
        return {"error": "compare_rasters timed out after 90 seconds."}
    except Exception as e:
        logger.exception("compare_rasters read failed")
        return {"error": f"Read failed: {str(e)[:200]}"}
    if "error" in diff_result:
        return diff_result

    # 4. Crop-stage-aware expected delta
    expected_delta = None
    stage_t1 = None
    stage_t2 = None
    season = None
    if args.crop and args.crop != "unknown" and args.crop in _CROP_CALENDARS:
        try:
            season = detect_current_season(args.crop, t2_date)
            cal = _CROP_CALENDARS[args.crop].get(season)
            if cal:
                planting_mmdd = cal["planting"]
                harvest_dap = int(cal["harvest_dap"])
                pm = int(planting_mmdd[:2])
                pd = int(planting_mmdd[3:5])
                planting_dt = datetime(t2_date.year, pm, pd, tzinfo=t2_date.tzinfo)
                if planting_dt > t2_date:
                    planting_dt = datetime(t2_date.year - 1, pm, pd, tzinfo=t2_date.tzinfo)
                dap_t1 = max(0, (t1_date - planting_dt).days)
                dap_t2 = max(0, (t2_date - planting_dt).days)
                stage_t1 = _stage_from_dap(dap_t1, harvest_dap, args.crop)
                stage_t2 = _stage_from_dap(dap_t2, harvest_dap, args.crop)
                ndvi_at_t1 = _stage_midpoint_ndvi(args.crop, stage_t1)
                ndvi_at_t2 = _stage_midpoint_ndvi(args.crop, stage_t2)
                expected_delta = round(ndvi_at_t2 - ndvi_at_t1, 3)
        except Exception:
            logger.exception("crop calendar lookup failed in compare_rasters")

    anomaly = None
    if expected_delta is not None:
        anomaly = round(diff_result["delta_mean"] - expected_delta, 3)

    # 5. CHIRPS rainfall between captures (best-effort, non-blocking on failure)
    rainfall_mm = None
    rainfall_days_with_data = None
    try:
        from src.services.forecast_fusion import _fetch_chirps_precip
        center_lon = (inter_west + inter_east) / 2
        center_lat = (inter_south + inter_north) / 2
        days = []
        cur = t1_date
        while cur < t2_date and len(days) < 60:
            days.append(cur.strftime("%Y-%m-%d"))
            cur = cur.replace(hour=0, minute=0, second=0, microsecond=0)
            from datetime import timedelta as _td
            cur = cur + _td(days=1)
        if days:
            chirps = await asyncio.get_running_loop().run_in_executor(
                None, _fetch_chirps_precip, center_lat, center_lon, days,
            )
            vals = [v for v in chirps.values() if v is not None]
            if vals:
                rainfall_mm = round(sum(vals), 1)
                rainfall_days_with_data = len(vals)
    except Exception:
        logger.warning("CHIRPS rainfall fetch failed in compare_rasters", exc_info=True)

    # 6. Verdict synthesizer
    delta_mean = diff_result["delta_mean"]
    if abs(delta_mean) < 0.05 and (anomaly is None or abs(anomaly) < 0.10):
        verdict = "no_significant_change"
        verdict_msg = "Field is essentially unchanged between captures."
    elif anomaly is not None and anomaly < -0.15:
        if rainfall_mm is not None and rainfall_mm < 20:
            verdict = "drought_signature"
            verdict_msg = (
                f"NDVI declined ~{abs(delta_mean):.2f} more than the {expected_delta:.2f} "
                f"expected for this crop+stage transition, with only {rainfall_mm}mm rainfall "
                f"between captures. Drought signature."
            )
        else:
            verdict = "stress_signature"
            verdict_msg = (
                f"NDVI declined ~{abs(anomaly):.2f} more than expected for "
                f"{stage_t1} → {stage_t2}. Cause not water-related (rainfall: "
                f"{rainfall_mm if rainfall_mm is not None else 'unknown'}mm). "
                f"Investigate disease, pest, or other stress."
            )
    elif delta_mean < -0.30:
        verdict = "harvest_or_tillage"
        verdict_msg = (
            f"Sharp NDVI decline ({delta_mean:.2f}) likely indicates harvest, "
            f"tillage, or major management event — not stress."
        )
    elif anomaly is not None and abs(anomaly) < 0.05:
        verdict = "expected_growth"
        verdict_msg = (
            f"NDVI changed {delta_mean:+.2f} as expected for {stage_t1} → {stage_t2}. "
            f"Field is on the expected trajectory."
        )
    elif delta_mean > 0.10:
        verdict = "vigorous_growth"
        verdict_msg = (
            f"NDVI rose {delta_mean:+.2f} — vigorous growth, possibly above expected."
        )
    else:
        verdict = "mild_decline"
        verdict_msg = (
            f"NDVI declined {delta_mean:.2f}, slightly more than expected for stage "
            f"{stage_t1 or 'unknown'} → {stage_t2 or 'unknown'}. Worth monitoring."
        )

    response = {
        "verdict": verdict,
        "message": verdict_msg,
        "evidence": {
            "layer_t1": {
                "layer_id": t1_row["layer_id"],
                "name": t1_row["name"],
                "captured_at": t1_date.isoformat(),
            },
            "layer_t2": {
                "layer_id": t2_row["layer_id"],
                "name": t2_row["name"],
                "captured_at": t2_date.isoformat(),
            },
            "interval_days": round(interval_days, 1),
            "bounds_a_b_overlap_wgs84": inter_bbox_wgs84,
            "bbox_center_offset_m": round(center_offset_m, 1),
            "comparison_method": "full_raster_downsampled_aligned_by_index",
            "comparison_method_note": (
                "Both rasters were downsampled to a common 512x512 grid and "
                "aligned by array index, not by geographic reprojection. This "
                "is reliable when both flights cover ~the same field with ~the "
                "same framing (enforced by bbox center offset <= 100m)."
            ),
            "valid_overlap_pct": diff_result["valid_overlap_pct"],
            "delta_mean_ndvi": delta_mean,
            "delta_p10": diff_result["delta_p10"],
            "delta_p50": diff_result["delta_p50"],
            "delta_p90": diff_result["delta_p90"],
            "declining_area_ha": diff_result["declining_area_ha"],
            "declining_pixels": diff_result["declining_pixel_count"],
            "crop": args.crop,
            "season": season,
            "stage_t1": stage_t1,
            "stage_t2": stage_t2,
            "expected_delta_ndvi_for_stage_transition": expected_delta,
            "anomaly_observed_minus_expected": anomaly,
            "rainfall_mm_between_captures": rainfall_mm,
            "rainfall_days_with_data": rainfall_days_with_data,
        },
    }
    # Append timeline entries to BOTH layers' brain pages so the comparison
    # event shows up in either flight's history.
    try:
        from src.services.raster_brain_link import record_raster_analysis
        compare_summary = (
            f"Compared with {t2_row['name']} ({t2_date.date()}): "
            f"verdict={verdict}, delta_NDVI={diff_result['delta_mean']:+.2f}, "
            f"interval={round(interval_days,1)}d."
        )
        for which, layer in (("t1", t1_row), ("t2", t2_row)):
            other = t2_row if which == "t1" else t1_row
            await record_raster_analysis(
                layer_id=layer["layer_id"],
                summary=(
                    f"compare_rasters vs {other['name']}: {verdict}, "
                    f"delta NDVI {diff_result['delta_mean']:+.2f}."
                ),
                source="compare_rasters",
                detail=json.dumps(response.get("evidence", {}), default=str)[:4000],
                owner_uuid=str(meta.user_uuid),
            )
    except Exception:
        logger.debug("Brain timeline write skipped for compare_rasters", exc_info=True)
    return response


# ── evaluate_insurance_trigger ──────────────────────────────────────────────


# Per-phase rainfall expectation for "drought context" check.
# Source: FAO crop water requirements + Rwanda Season A/B norms.
# Used as a soft signal — low rainfall + low NDVI = drought trigger.
_PHASE_RAINFALL_MIN_MM_PER_DAY: Dict[str, float] = {
    "planting":   3.0,
    "vegetative": 4.0,
    "flowering":  5.0,
    "grain_fill": 4.0,
    "pod_fill":   4.0,
    "maturity":   2.0,
    "_any":       3.5,
}


class EvaluateInsuranceTriggerArgs(BaseModel):
    layer_id_before: str = Field(
        ...,
        description="The layer_id of the EARLIER drone NDVI raster ('before' / baseline). For policy onboarding without a prior flight, pass the same id as layer_id_after — degenerate case will be reported.",
    )
    layer_id_after: str = Field(
        ...,
        description="The layer_id of the LATER drone NDVI raster ('after' / current claim flight).",
    )
    band: int = Field(
        ...,
        description="Which band contains NDVI (1-indexed). For single-band NDVI use 1; for typical 4-band drone NDVI export use 2.",
    )
    crop: str = Field(
        ...,
        description="Crop in the field. Supported: 'maize', 'beans', 'rice', 'sorghum', 'wheat'. Pass 'unknown' if not specified — accuracy will be reduced.",
    )
    polygon_geojson: str = Field(
        ...,
        description="GeoJSON Polygon string defining the insured field boundary, OR empty string '' to use the whole 'after' raster. Used to compute the current absolute-NDVI signal.",
    )


async def evaluate_insurance_trigger(
    args: EvaluateInsuranceTriggerArgs, meta: IngabeToolCallMetaArgs
) -> dict:
    """Evaluate parametric insurance trigger conditions on a user's drone NDVI flights. Composes compare_rasters (change detection) + zonal stats on the 'after' raster (current absolute health) + a per-crop-stage threshold table. Computes a 0-100 composite_score across 4 weighted signals (absolute health, NDVI decline vs expected, area declining significantly, drought context from rainfall) and returns triggered=True if score >= 60. Returns triggered (bool), composite_score (0-100), per-signal status with thresholds, payout_recommendation, plus full underlying compare_rasters evidence. ALWAYS use this when the user asks 'should this claim pay out?', 'is the trigger fired?', 'evaluate the insurance', or any parametric-trigger question on drone NDVI data. Source='drone' — for satellite-based triggers use get_insurance_intelligence."""
    from src.services.dssat_service import _CROP_CALENDARS, detect_current_season

    # 1. Run compare_rasters first — the keystone change-detection signal.
    cmp = await compare_rasters(
        CompareRastersArgs(
            layer_id_a=args.layer_id_before,
            layer_id_b=args.layer_id_after,
            band=args.band,
            crop=args.crop,
        ),
        meta,
    )
    if "error" in cmp:
        # Pass error through so Sage can explain to user — wrong raster type, no overlap, etc.
        return {**cmp, "tool": "evaluate_insurance_trigger"}

    ev = cmp.get("evidence", {})
    delta_mean = ev.get("delta_mean_ndvi")
    anomaly = ev.get("anomaly_observed_minus_expected")
    declining_area_ha = ev.get("declining_area_ha", 0.0)
    rainfall_mm = ev.get("rainfall_mm_between_captures")
    rainfall_days = ev.get("rainfall_days_with_data") or 0
    interval_days = ev.get("interval_days", 0)
    stage_t2 = ev.get("stage_t2") or "_any"
    season = ev.get("season")
    layer_t2 = ev.get("layer_t2", {})
    layer_t1 = ev.get("layer_t1", {})
    is_degenerate = layer_t1.get("layer_id") == layer_t2.get("layer_id")

    # 2. Compute absolute NDVI mean on the 'after' raster (current claim flight).
    after_layer_id = layer_t2.get("layer_id") or args.layer_id_after
    stats = await compute_zonal_stats(
        ComputeZonalStatsArgs(
            layer_id=after_layer_id,
            polygon_geojson=args.polygon_geojson,
            band=args.band,
        ),
        meta,
    )
    if "error" in stats:
        return {
            "error": "after_layer_stats_failed",
            "message": f"Could not compute zonal stats on after layer: {stats.get('error')}",
            "tool": "evaluate_insurance_trigger",
        }

    mean_after = stats.get("mean")
    # uint8-packed NDVI rescale, same heuristic as interpret_raster_health
    if (
        isinstance(mean_after, (int, float))
        and 50 <= mean_after <= 200
        and stats.get("max", 0) <= 255
        and stats.get("min", 0) >= 0
    ):
        mean_after = (mean_after / 127.5) - 1.0
    if mean_after is None or not (-1.5 <= mean_after <= 1.5):
        return {
            "error": "invalid_after_ndvi",
            "message": f"After-layer mean ({mean_after}) is outside plausible NDVI range. Band {args.band} probably isn't NDVI.",
            "tool": "evaluate_insurance_trigger",
        }
    mean_after = round(float(mean_after), 4)

    # 3. Look up the healthy NDVI band for current crop+stage.
    crop_key = args.crop if args.crop in NDVI_HEALTH_RANGES else "_default"
    crop_ranges = NDVI_HEALTH_RANGES.get(crop_key, NDVI_HEALTH_RANGES["_default"])
    if stage_t2 in crop_ranges:
        stage_key = stage_t2
    elif crop_key == "_default":
        stage_key = "_any"
    else:
        stage_key = "vegetative" if "vegetative" in crop_ranges else next(iter(crop_ranges))
    healthy_low, healthy_high = crop_ranges[stage_key]
    healthy_mid = (healthy_low + healthy_high) / 2.0

    # 4. Score 4 signals (each 0-100 contribution, then weighted).

    # SIGNAL 1: Absolute NDVI shortfall vs stage expectation. Weight 0.35.
    # Below healthy_low → linear ramp 0-100 across a 0.20 NDVI band.
    if mean_after >= healthy_low:
        s1_score = 0.0
        s1_status = "PASS"
        s1_msg = f"Current NDVI {mean_after:.2f} is within healthy {healthy_low:.2f}-{healthy_high:.2f} for {args.crop} at {stage_key}."
    else:
        shortfall = healthy_low - mean_after
        s1_score = float(min(100.0, (shortfall / 0.20) * 100.0))
        s1_status = "TRIGGERED" if s1_score >= 50 else "AT_RISK"
        s1_msg = f"Current NDVI {mean_after:.2f} is {shortfall:.2f} below healthy floor {healthy_low:.2f}."

    # SIGNAL 2: NDVI decline anomaly (worse than expected for stage transition). Weight 0.30.
    # Available only when both layers differ AND crop calendar is known.
    if is_degenerate or anomaly is None:
        s2_score = 0.0
        s2_status = "NOT_APPLICABLE"
        s2_msg = (
            "No baseline available — only one flight or no crop calendar. "
            "Decline check skipped." if is_degenerate
            else "Crop calendar unavailable — decline anomaly cannot be computed."
        )
    elif anomaly >= -0.05:
        s2_score = 0.0
        s2_status = "PASS"
        s2_msg = f"NDVI change anomaly {anomaly:+.2f} is within normal range for stage transition."
    else:
        # ramp 0-100 across anomaly -0.05 to -0.25
        s2_score = float(min(100.0, ((-anomaly) - 0.05) / 0.20 * 100.0))
        s2_status = "TRIGGERED" if s2_score >= 50 else "AT_RISK"
        s2_msg = f"NDVI declined {anomaly:+.2f} more than expected for {ev.get('stage_t1')} → {stage_t2}."

    # SIGNAL 3: Significantly-declining-area share. Weight 0.20.
    # Use t2 layer's actual field area when available, else a reasonable default.
    after_area_ha = stats.get("area_ha") or stats.get("polygon_area_ha")
    if not is_degenerate and after_area_ha and after_area_ha > 0 and declining_area_ha is not None:
        decline_pct = (declining_area_ha / after_area_ha) * 100.0
    else:
        decline_pct = None

    if decline_pct is None or is_degenerate:
        s3_score = 0.0
        s3_status = "NOT_APPLICABLE"
        s3_msg = "No baseline — cannot measure declining area."
    elif decline_pct < 10:
        s3_score = 0.0
        s3_status = "PASS"
        s3_msg = f"Only {decline_pct:.1f}% of field declined significantly."
    else:
        # ramp 10% → 0, 50% → 100
        s3_score = float(min(100.0, max(0.0, (decline_pct - 10.0) / 40.0 * 100.0)))
        s3_status = "TRIGGERED" if s3_score >= 50 else "AT_RISK"
        s3_msg = f"{decline_pct:.1f}% of field ({declining_area_ha} ha) declined NDVI > 0.10 between flights."

    # SIGNAL 4: Drought context from CHIRPS rainfall. Weight 0.15.
    # Adds confidence when low NDVI is paired with low rainfall.
    if rainfall_mm is None or rainfall_days < 3 or interval_days <= 0:
        s4_score = 0.0
        s4_status = "NOT_APPLICABLE"
        s4_msg = "Rainfall record unavailable for this interval."
    else:
        rain_per_day = rainfall_mm / max(1.0, interval_days)
        expected_per_day = _PHASE_RAINFALL_MIN_MM_PER_DAY.get(stage_key, 3.5)
        if rain_per_day >= expected_per_day:
            s4_score = 0.0
            s4_status = "PASS"
            s4_msg = f"Rainfall {rain_per_day:.1f}mm/day meets {expected_per_day:.1f}mm/day expectation for {stage_key}."
        else:
            shortfall_pct = (expected_per_day - rain_per_day) / expected_per_day
            s4_score = float(min(100.0, shortfall_pct * 100.0))
            s4_status = "DROUGHT_CONTEXT" if s4_score >= 50 else "DRY_BUT_NOT_DROUGHT"
            s4_msg = f"Rainfall {rain_per_day:.1f}mm/day fell short of {expected_per_day:.1f}mm/day expected for {stage_key}."

    # Weighted composite. Re-normalize over applicable signals only — so a
    # one-flight onboarding query is judged on signal 1 alone, fairly.
    weights = {
        "absolute_health": 0.35,
        "ndvi_decline_anomaly": 0.30,
        "declining_area_share": 0.20,
        "drought_context": 0.15,
    }
    applicable = []
    if s1_status != "NOT_APPLICABLE":
        applicable.append(("absolute_health", s1_score))
    if s2_status != "NOT_APPLICABLE":
        applicable.append(("ndvi_decline_anomaly", s2_score))
    if s3_status != "NOT_APPLICABLE":
        applicable.append(("declining_area_share", s3_score))
    if s4_status != "NOT_APPLICABLE":
        applicable.append(("drought_context", s4_score))

    if applicable:
        total_w = sum(weights[k] for k, _ in applicable)
        composite_score = round(
            sum(weights[k] * s for k, s in applicable) / total_w, 1
        )
    else:
        composite_score = 0.0

    triggered = composite_score >= 60.0

    if triggered:
        if composite_score >= 80:
            payout_rec = "FULL_PAYOUT — multiple strong stress signals confirmed."
        else:
            payout_rec = "PARTIAL_PAYOUT — trigger threshold met but signals are mixed; investigate before full payout."
    else:
        if composite_score >= 40:
            payout_rec = "MONITOR — below trigger but elevated; re-fly before claim closure."
        else:
            payout_rec = "NO_PAYOUT — signals do not indicate insurable damage."

    response = {
        "triggered": triggered,
        "composite_score": composite_score,
        "payout_recommendation": payout_rec,
        "source": "drone",
        "signals": {
            "absolute_health": {
                "score": round(s1_score, 1),
                "status": s1_status,
                "message": s1_msg,
                "weight": weights["absolute_health"],
                "observed_ndvi_mean": mean_after,
                "healthy_range": [healthy_low, healthy_high],
            },
            "ndvi_decline_anomaly": {
                "score": round(s2_score, 1),
                "status": s2_status,
                "message": s2_msg,
                "weight": weights["ndvi_decline_anomaly"],
                "anomaly": anomaly,
                "delta_mean": delta_mean,
            },
            "declining_area_share": {
                "score": round(s3_score, 1),
                "status": s3_status,
                "message": s3_msg,
                "weight": weights["declining_area_share"],
                "declining_area_ha": declining_area_ha,
                "field_area_ha": after_area_ha,
                "declining_pct": round(decline_pct, 1) if decline_pct is not None else None,
            },
            "drought_context": {
                "score": round(s4_score, 1),
                "status": s4_status,
                "message": s4_msg,
                "weight": weights["drought_context"],
                "rainfall_mm_between_captures": rainfall_mm,
                "rainfall_days_with_data": rainfall_days,
                "interval_days": interval_days,
            },
        },
        "context": {
            "crop": args.crop,
            "season": season,
            "growth_stage": stage_t2,
            "is_degenerate_baseline": is_degenerate,
            "layer_before": layer_t1,
            "layer_after": layer_t2,
        },
        "compare_rasters_verdict": cmp.get("verdict"),
        "compare_rasters_message": cmp.get("message"),
    }
    # Append timeline entries to BOTH layers' brain pages so the trigger
    # evaluation is recorded in either flight's history.
    try:
        from src.services.raster_brain_link import record_raster_analysis
        for lid in (args.layer_id_after, args.layer_id_before):
            if not lid:
                continue
            await record_raster_analysis(
                layer_id=lid,
                summary=(
                    f"Insurance trigger: {payout_rec.split(' — ')[0]}, "
                    f"composite_score={composite_score}, triggered={triggered}, "
                    f"crop={args.crop}, stage={stage_t2}."
                ),
                source="evaluate_insurance_trigger",
                detail=json.dumps({
                    "triggered": triggered,
                    "composite_score": composite_score,
                    "payout_recommendation": payout_rec,
                    "signals": {k: {"score": v["score"], "status": v["status"]}
                                for k, v in response["signals"].items()},
                }, default=str)[:4000],
                owner_uuid=str(meta.user_uuid),
            )
    except Exception:
        logger.debug("Brain timeline write skipped for evaluate_insurance_trigger", exc_info=True)

    # Build a displayable polygon: the insured field boundary, tagged with the
    # composite_score + verdict. The LLM passes this to display_geojson_layer
    # with style_hint="insurance_composite_score" to paint the parcel in
    # green/yellow/orange/red based on score. This is the visual answer to
    # "should this claim pay out?" — underwriter sees the colored parcel, not
    # just a paragraph.
    try:
        if args.polygon_geojson and args.polygon_geojson.strip():
            _polygon_obj = json.loads(args.polygon_geojson)
            # Normalize: accept Polygon geometry, Feature, or FeatureCollection
            if _polygon_obj.get("type") == "Polygon":
                _geom = _polygon_obj
            elif _polygon_obj.get("type") == "Feature":
                _geom = _polygon_obj.get("geometry")
            elif _polygon_obj.get("type") == "FeatureCollection":
                _feats = _polygon_obj.get("features", [])
                _geom = _feats[0].get("geometry") if _feats else None
            else:
                _geom = None

            if _geom is not None:
                feature = {
                    "type": "Feature",
                    "geometry": _geom,
                    "properties": {
                        "composite_score": composite_score,
                        "triggered": triggered,
                        "payout_recommendation": payout_rec.split(" — ")[0] if " — " in payout_rec else payout_rec,
                        "crop": args.crop,
                        "growth_stage": stage_t2,
                        "ndvi_mean": mean_after,
                    },
                }
                fc = {"type": "FeatureCollection", "features": [feature]}
                # Compute bbox from the polygon's coordinates
                try:
                    from shapely.geometry import shape as _shape
                    _b = _shape(_geom).bounds
                    _bbox_str = f"{_b[0]},{_b[1]},{_b[2]},{_b[3]}"
                except Exception:
                    _bbox_str = ""

                response["displayable_geojson"] = {
                    "geojson": fc,
                    "style_hint": "insurance_composite_score",
                    "title": (
                        f"Insurance Trigger — {args.crop} "
                        f"({payout_rec.split(' — ')[0] if ' — ' in payout_rec else 'Verdict'})"
                    ),
                    "bbox": _bbox_str,
                }
    except Exception:
        logger.debug("displayable_geojson build skipped for evaluate_insurance_trigger", exc_info=True)

    return response
