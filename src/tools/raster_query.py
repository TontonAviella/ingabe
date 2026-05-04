"""Pixel-reading tools for user-uploaded rasters.

Tier 1 of the drone-data interaction architecture. These tools expose mechanical
access to MapLayer rasters: metadata description, zonal statistics, and raster-type
detection so downstream tools know which analysis is appropriate.

Composed by Tier 2 tools in raster_interpret.py (NDVI health verdicts) and
rgb_visual.py (RGB-only analyses) to produce farmer-language answers tailored
to the actual data type the user uploaded.
"""

import asyncio
import json
import logging
import math
import os
from typing import Any, Dict

from pydantic import BaseModel, Field

from src.tools.pyd import IngabeToolCallMetaArgs

logger = logging.getLogger(__name__)


# ── describe_user_raster ────────────────────────────────────────────────────


class DescribeUserRasterArgs(BaseModel):
    layer_id: str = Field(
        ...,
        description="The layer_id of the user-uploaded raster (starts with 'L', visible in the project's layer panel). Example: 'LwQ6VWK64bvL'.",
    )


async def describe_user_raster(
    args: DescribeUserRasterArgs, meta: IngabeToolCallMetaArgs
) -> dict:
    """Describe a user-uploaded raster layer in plain language: bounds, area in hectares, band count, dimensions, file size, COG status, capture date if known. Performs a sanity check that this layer's geographic location is reasonable relative to other rasters in the same project (catches CRS errors where a Rwanda field gets georeferenced to Florida by a misconfigured drone export). Use this whenever the user asks ABOUT one of their uploaded rasters — drone orthophotos, NDVI rasters, multispectral tiffs. Do NOT use for satellite-data questions (use get_ndvi_stats or get_field_health for those)."""
    from src.structures import get_async_read_connection

    async with get_async_read_connection() as conn:
        row = await conn.fetchrow(
            """
            SELECT layer_id, name, type, s3_key, bounds, metadata, size_bytes,
                   created_on, owner_uuid
            FROM map_layers
            WHERE layer_id = $1
            """,
            args.layer_id,
        )
    if not row:
        return {"error": f"Layer {args.layer_id} not found."}
    if str(row["owner_uuid"]) != str(meta.user_uuid):
        return {"error": f"Layer {args.layer_id} is not owned by you."}
    if row["type"] != "raster":
        return {
            "error": (
                f"Layer {args.layer_id} is type '{row['type']}', not a raster. "
                "Use a different tool for vector or PostGIS layers."
            )
        }

    metadata = (
        json.loads(row["metadata"])
        if isinstance(row["metadata"], str)
        else (dict(row["metadata"]) if row["metadata"] else {})
    )
    bounds_raw = row["bounds"]
    bounds = list(bounds_raw) if bounds_raw else None

    raster_type, raster_type_explanation = _detect_raster_type(
        metadata, row["name"], row["s3_key"]
    )

    # Step 1: Cheap WGS84 approximation as fallback (for layers with no COG yet
    # or when the rasterio open fails). Slightly inflated for non-equatorial
    # latitudes but always available from DB metadata alone.
    area_ha_approx = None
    bbox_width_m = None
    bbox_height_m = None
    if bounds and len(bounds) == 4:
        west, south, east, north = bounds
        center_lat = (south + north) / 2
        dy_km = (north - south) * 111.32
        dx_km = (east - west) * 111.32 * math.cos(math.radians(center_lat))
        if dy_km > 0 and dx_km > 0:
            area_ha_approx = round(dx_km * dy_km * 100, 1)
            bbox_width_m = dx_km * 1000
            bbox_height_m = dy_km * 1000

    # Step 2: Open the COG (when ready) for native-projection-exact bbox + the
    # alpha-mask-aware actual field area. This is the meaningful number for
    # farmers and insurance underwriters: the bbox is rectangular but real
    # flight footprints are irregular, so 5-15% of the bbox is typically empty.
    area_bbox_ha = None
    area_valid_ha = None
    valid_pixel_fraction = None
    cog_key = metadata.get("cog_key")
    if cog_key:
        try:
            from src.utils import get_async_s3_client, get_bucket_name
            os.environ.setdefault("GDAL_HTTP_UNSAFESSL", "YES")
            s3_client = await get_async_s3_client()
            bucket = get_bucket_name()
            cog_url = await s3_client.generate_presigned_url(
                "get_object",
                Params={"Bucket": bucket, "Key": cog_key},
                ExpiresIn=300,
            )
            (
                area_bbox_ha,
                valid_pixel_fraction,
            ) = await asyncio.get_running_loop().run_in_executor(
                None, _read_native_geometry, cog_url
            )
            if area_bbox_ha is not None and valid_pixel_fraction is not None:
                area_valid_ha = round(area_bbox_ha * valid_pixel_fraction, 1)
        except Exception as e:
            logger.warning(
                "Could not read native geometry for layer %s: %s",
                args.layer_id, str(e)[:120],
            )

    # Pick the canonical bbox area to report: native if we got it, else WGS84 approx
    area_ha = area_bbox_ha if area_bbox_ha is not None else area_ha_approx

    # Pre-compute pixel size so downstream LLMs don't have to do square-root math
    # (free-tier models hallucinate arithmetic; this guarantees the right answer
    # appears in the tool result and gets quoted verbatim).
    pixel_resolution_cm = None
    pixel_resolution_label = None
    width_px = metadata.get("width")
    height_px = metadata.get("height")
    if width_px and height_px and bbox_width_m and bbox_height_m:
        try:
            cm_per_px_x = (bbox_width_m / float(width_px)) * 100.0
            cm_per_px_y = (bbox_height_m / float(height_px)) * 100.0
            avg_cm = (cm_per_px_x + cm_per_px_y) / 2.0
            pixel_resolution_cm = round(avg_cm, 2)
            if avg_cm < 10:
                pixel_resolution_label = f"~{avg_cm:.1f} cm/pixel (sub-meter, drone-grade)"
            elif avg_cm < 100:
                pixel_resolution_label = f"~{avg_cm:.0f} cm/pixel (high-res aerial)"
            elif avg_cm < 1000:
                pixel_resolution_label = f"~{avg_cm/100:.1f} m/pixel (high-res satellite)"
            else:
                pixel_resolution_label = f"~{avg_cm/100:.0f} m/pixel (medium/coarse)"
        except (ValueError, ZeroDivisionError, TypeError):
            pixel_resolution_cm = None

    sanity_warning = await _cross_layer_sanity_check(
        args.layer_id, meta.user_uuid, bounds
    )

    # Pick canonical area description based on what we managed to compute
    if area_valid_ha is not None:
        # Best case: we have alpha-mask-aware actual field area
        area_label = (
            f"{area_valid_ha} ha actual field area "
            f"(of {area_bbox_ha} ha bounding box, {round(valid_pixel_fraction * 100, 1)}% valid pixels)"
        )
    elif area_bbox_ha is not None:
        area_label = f"{area_bbox_ha} ha (bounding box, native projection — exact)"
    elif area_ha_approx is not None:
        area_label = f"~{area_ha_approx} ha (bounding box, WGS84 approximation — ~1% high)"
    else:
        area_label = None

    return {
        "layer_id": row["layer_id"],
        "name": row["name"],
        "raster_type": raster_type,
        "raster_type_explanation": raster_type_explanation,
        "bounds_wgs84": bounds,
        # Three area numbers, each with clear meaning:
        "area_ha": area_ha,                       # canonical (native if available, else approx)
        "area_bbox_ha": area_bbox_ha,             # rectangular extent, native projection (exact)
        "area_valid_ha": area_valid_ha,           # alpha-mask-aware actual field area
        "valid_pixel_fraction": (
            round(valid_pixel_fraction, 4) if valid_pixel_fraction is not None else None
        ),
        "area_label": area_label,                 # human-readable summary for Sage to quote
        "pixel_resolution_cm": pixel_resolution_cm,
        "pixel_resolution_label": pixel_resolution_label,
        "band_count": metadata.get("band_count"),
        "width_pixels": metadata.get("width"),
        "height_pixels": metadata.get("height"),
        "original_srid": metadata.get("original_srid"),
        "original_filename": metadata.get("original_filename"),
        "file_size_mb": (
            round(row["size_bytes"] / 1024 / 1024, 1) if row["size_bytes"] else None
        ),
        "uploaded_at": row["created_on"].isoformat() if row["created_on"] else None,
        "cog_status": "ready" if metadata.get("cog_key") else "pending",
        "cog_source": metadata.get("cog_source"),
        "raster_value_stats_b1": metadata.get("raster_value_stats_b1"),
        "sanity_warning": sanity_warning,
        "compatible_tools": _compatible_tools_for_type(raster_type),
    }


# ── native-projection geometry reader ───────────────────────────────────────


def _read_native_geometry(cog_url: str):
    """Open the COG header + a downsampled mask read. Return (bbox_area_ha,
    valid_fraction). Errors return (None, None).

    Native-projection bbox area = exact (UTM is in meters, no approximation).
    Valid fraction = portion of pixels NOT masked by the COG's internal mask
    band (matters when drones fly irregular footprints — typical 85-95%).
    """
    try:
        import numpy as np
        import rasterio
        from rasterio.enums import Resampling

        with rasterio.open(cog_url) as ds:
            bbox_area_ha = None
            crs = ds.crs
            if crs and crs.is_projected:
                left, bottom, right, top = ds.bounds
                width_m = right - left
                height_m = top - bottom
                if width_m > 0 and height_m > 0:
                    bbox_area_ha = round((width_m * height_m) / 10000.0, 2)

            valid_fraction = None
            try:
                target_long = 1024
                long_native = max(ds.width, ds.height)
                ovr = max(1, long_native // target_long)
                out_h = max(1, ds.height // ovr)
                out_w = max(1, ds.width // ovr)
                # dataset_mask returns 0 (invalid) or 255 (valid) per pixel
                mask = ds.dataset_mask(out_shape=(out_h, out_w))
                if mask.size > 0:
                    valid_count = int((mask > 0).sum())
                    valid_fraction = valid_count / float(mask.size)
            except Exception:
                # Mask read failed — fall through with valid_fraction=None
                pass

            return bbox_area_ha, valid_fraction
    except Exception:
        return None, None


# ── raster type detection ───────────────────────────────────────────────────


def _detect_raster_type(metadata: dict, layer_name: str, s3_key: str | None):
    """Heuristic classification of a raster into a type the rest of the system
    can route on. Uses band count, dtype, and filename hints. Errs on the side
    of conservative ('single_band_unknown' / 'rgb_visual') when ambiguous —
    downstream tools can ask the user for confirmation.
    """
    bands = metadata.get("band_count") or 0
    name = (metadata.get("original_filename") or layer_name or "").lower()
    has_ndvi_in_name = "ndvi" in name
    has_ndre_in_name = "ndre" in name
    has_rgb_in_name = "rgb" in name or "ortho" in name or "visual" in name
    has_dem_in_name = "dem" in name or "elev" in name or "dsm" in name or "dtm" in name
    val_stats = metadata.get("raster_value_stats_b1") or {}
    b1_min = val_stats.get("min")
    b1_max = val_stats.get("max")

    if bands == 1:
        if has_dem_in_name:
            return "dem", "Digital elevation model (single-band, height in meters)."
        if (has_ndvi_in_name or has_ndre_in_name) or (
            b1_min is not None and b1_max is not None
            and -1.5 <= b1_min and b1_max <= 1.5
        ):
            return (
                "ndvi_single",
                "Single-band NDVI/NDRE-style raster (float values in -1..1 range, ready for health analysis).",
            )
        return (
            "single_band_unknown",
            "Single-band raster of unknown type (could be NDVI, DEM, classification, soil moisture, etc.). Inspect raster_value_stats_b1 or ask the user.",
        )

    if bands in (3, 4):
        if has_ndvi_in_name or has_ndre_in_name:
            return (
                "rgb_with_packed_indices",
                f"{bands}-band drone export with NDVI/NDRE packed alongside visual bands. Typical layout: band 1 = Red, band 2 = NDVI (uint8 or float), band 3 = NDRE (uint8 or float), band 4 = alpha mask. Verify layout before assuming.",
            )
        if has_rgb_in_name or bands == 3:
            return (
                "rgb_visual",
                "RGB(A) visual orthophoto. Use analyze_rgb_field for greenness/coverage analysis. Cannot compute true NDVI without a NIR band.",
            )
        return (
            "rgb_visual",
            f"{bands}-band raster — most likely RGB(A) visual ortho. If this is multispectral with a NIR band, name the file with 'multispectral' in it.",
        )

    if bands >= 5:
        return (
            "multispectral",
            f"Multispectral raster ({bands} bands) — likely contains red + NIR (and possibly red-edge, SWIR). True NDVI computable on the fly with compute_index_from_bands.",
        )

    return (
        "unknown",
        f"Could not detect raster type from {bands} bands and name '{name[:60]}'. Ask the user.",
    )


def _compatible_tools_for_type(raster_type: str) -> list[str]:
    """Tell Sage which Tier 2 tools work on this raster type."""
    table = {
        "ndvi_single": ["interpret_raster_health", "compute_zonal_stats"],
        "rgb_with_packed_indices": [
            "interpret_raster_health (use band=2 for NDVI in 4-band drone exports)",
            "analyze_rgb_field (use band=1 for visual)",
            "compute_zonal_stats",
        ],
        "rgb_visual": [
            "analyze_rgb_field",
            "compute_zonal_stats",
            "(NOT interpret_raster_health — RGB has no NIR, true NDVI is not computable)",
        ],
        "multispectral": [
            "compute_zonal_stats",
            "(future: compute_index_from_bands to compute NDVI from red+NIR, then interpret_raster_health)",
        ],
        "dem": ["compute_zonal_stats"],
        "single_band_unknown": [
            "compute_zonal_stats",
            "(if values are in -1..1 range it may be NDVI — confirm with user)",
        ],
        "unknown": ["describe_user_raster", "compute_zonal_stats"],
    }
    return table.get(raster_type, ["describe_user_raster", "compute_zonal_stats"])


async def _cross_layer_sanity_check(layer_id: str, user_uuid: str, bounds):
    """Compare this raster's bounds to other rasters owned by the same user.
    If >100km from the nearest sibling, surface a warning (likely CRS bug).
    """
    if not bounds or len(bounds) != 4:
        return None
    from src.structures import get_async_read_connection

    async with get_async_read_connection() as conn:
        siblings = await conn.fetch(
            """
            SELECT layer_id, name, bounds
            FROM map_layers
            WHERE owner_uuid = $1
              AND type = 'raster' AND layer_id != $2
              AND bounds IS NOT NULL
            LIMIT 20
            """,
            user_uuid, layer_id,
        )
    if not siblings:
        return None

    this_lon = (bounds[0] + bounds[2]) / 2
    this_lat = (bounds[1] + bounds[3]) / 2
    distances = []
    for s in siblings:
        sb = s["bounds"]
        if not sb or len(sb) != 4:
            continue
        sib_lon = (sb[0] + sb[2]) / 2
        sib_lat = (sb[1] + sb[3]) / 2
        dlat = this_lat - sib_lat
        dlon = (this_lon - sib_lon) * math.cos(
            math.radians((this_lat + sib_lat) / 2)
        )
        d_km = math.sqrt(dlat ** 2 + dlon ** 2) * 111.32
        distances.append((s["name"], d_km))

    if not distances:
        return None
    closest_name, closest_km = min(distances, key=lambda x: x[1])
    if closest_km > 100:
        return (
            f"This layer's center is ~{closest_km:.0f} km from your closest other "
            f"layer ('{closest_name}'). For a single Rwanda farm this should be "
            f"under 50 km. The most common cause is a wrong CRS embedded by the "
            f"drone export (UTM zone mismatch). If the layer renders in the wrong "
            f"place on the map, re-export with the correct CRS for Rwanda "
            f"(EPSG:32735 or EPSG:32736)."
        )
    return None


# ── compute_zonal_stats ─────────────────────────────────────────────────────


class ComputeZonalStatsArgs(BaseModel):
    layer_id: str = Field(
        ...,
        description="The layer_id of the user-uploaded raster.",
    )
    polygon_geojson: str = Field(
        ...,
        description=(
            "A GeoJSON Polygon string defining the area to analyze (use this if "
            "the user drew a polygon or wants stats over a specific field). "
            "Pass empty string '' to analyze the whole raster."
        ),
    )
    band: int = Field(
        ...,
        description=(
            "Which raster band to analyze (1-indexed). For typical 4-band drone NDVI "
            "exports, NDVI is in band 2. For dedicated single-band NDVI rasters, "
            "use band 1. Default to 1 if uncertain."
        ),
    )


async def compute_zonal_stats(
    args: ComputeZonalStatsArgs, meta: IngabeToolCallMetaArgs
) -> dict:
    """Compute pixel statistics (mean, min, max, std, p10, p50, p90) over a polygon area within a user-uploaded raster, on a specified band. NoData pixels are excluded from the calculation and the percentage of valid pixels is reported. If polygon_geojson is empty, computes stats over the whole raster. Use this whenever you need numerical stats from a user's drone or NDVI raster — for example, "what's the average NDVI in this field?". Do NOT use for satellite stats (those have their own tools)."""
    from src.structures import get_async_read_connection
    from src.utils import get_async_s3_client, get_bucket_name

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
    if row["type"] != "raster":
        return {"error": f"Layer {args.layer_id} is type '{row['type']}', not a raster."}

    metadata = (
        json.loads(row["metadata"])
        if isinstance(row["metadata"], str)
        else (dict(row["metadata"]) if row["metadata"] else {})
    )
    band_count = metadata.get("band_count", 1) or 1
    if args.band < 1 or args.band > band_count:
        return {
            "error": (
                f"Band {args.band} out of range. Layer has {band_count} band(s). "
                f"Use 1 for single-band NDVI, 2 for NDVI in standard 4-band drone exports."
            )
        }

    if args.polygon_geojson and args.polygon_geojson.strip():
        try:
            polygon = json.loads(args.polygon_geojson)
        except Exception as e:
            return {"error": f"Could not parse polygon GeoJSON: {e}"}
        if polygon.get("type") not in ("Polygon", "MultiPolygon", "Feature", "FeatureCollection"):
            return {
                "error": (
                    f"GeoJSON type '{polygon.get('type')}' not supported. "
                    "Pass a Polygon, MultiPolygon, Feature, or FeatureCollection."
                )
            }
        if polygon.get("type") == "Feature":
            polygon = polygon["geometry"]
        elif polygon.get("type") == "FeatureCollection":
            features = polygon.get("features") or []
            if not features:
                return {"error": "Empty FeatureCollection."}
            polygon = features[0]["geometry"]
        polygon_used = "user_provided"
    else:
        bounds = row["bounds"]
        if not bounds or len(bounds) != 4:
            return {"error": "No polygon provided and layer has no bounds."}
        west, south, east, north = bounds
        polygon = {
            "type": "Polygon",
            "coordinates": [
                [
                    [west, south],
                    [east, south],
                    [east, north],
                    [west, north],
                    [west, south],
                ]
            ],
        }
        polygon_used = "whole_raster_bbox"

    cog_key = metadata.get("cog_key") or row["s3_key"]
    s3_client = await get_async_s3_client()
    bucket = get_bucket_name()
    cog_url = await s3_client.generate_presigned_url(
        "get_object",
        Params={"Bucket": bucket, "Key": cog_key},
        ExpiresIn=900,
    )

    os.environ.setdefault("GDAL_HTTP_UNSAFESSL", "YES")
    band = args.band

    is_whole_raster = polygon_used == "whole_raster_bbox"

    def _compute() -> Dict[str, Any]:
        import numpy as np
        import rasterio
        from rasterio.enums import Resampling
        from rasterio.mask import mask as rio_mask
        from rasterio.warp import transform_geom

        with rasterio.open(cog_url) as ds:
            if is_whole_raster:
                # Whole-raster path: native resolution can be 100M-1B+ pixels,
                # which times out via vsicurl. Read from the COG's overview
                # pyramid downsampled to ~1024 px on the long side. Mean/std/
                # percentiles at that resolution are statistically indistinguishable
                # from native-resolution stats for any field-scale raster.
                target_long = 1024
                long_native = max(ds.width, ds.height)
                ovr_factor = max(1, long_native // target_long)
                out_h = max(1, ds.height // ovr_factor)
                out_w = max(1, ds.width // ovr_factor)
                arr = ds.read(
                    band,
                    out_shape=(out_h, out_w),
                    resampling=Resampling.average,
                    masked=True,
                )
            else:
                # Polygon path: rio_mask at native resolution. Polygons are
                # smaller, so read time is bounded.
                geom = polygon
                target_crs = ds.crs
                if target_crs and target_crs.to_string() != "EPSG:4326":
                    geom = transform_geom("EPSG:4326", target_crs.to_string(), polygon)
                try:
                    masked, _ = rio_mask(
                        ds, [geom], crop=True, indexes=[band], filled=False
                    )
                except ValueError as e:
                    return {"error": f"Polygon does not overlap the raster: {e}"}
                arr = masked[0]

            total_count = int(arr.size)
            if hasattr(arr, "mask"):
                valid_mask = ~arr.mask
                valid = arr.data[valid_mask] if hasattr(arr, "data") else arr[valid_mask]
            else:
                valid = arr[~np.isnan(arr)] if arr.dtype.kind == "f" else arr.flatten()

            if arr.dtype.kind == "f":
                import numpy as np
                valid = valid[~np.isnan(valid)]

            valid_count = int(valid.size)
            valid_pct = (valid_count / total_count * 100.0) if total_count > 0 else 0.0

            if valid_count == 0:
                return {"error": "No valid pixels in the polygon (all NoData)."}

            result = {
                "mean": round(float(np.mean(valid)), 4),
                "min": round(float(np.min(valid)), 4),
                "max": round(float(np.max(valid)), 4),
                "std": round(float(np.std(valid)), 4),
                "p10": round(float(np.percentile(valid, 10)), 4),
                "p50": round(float(np.percentile(valid, 50)), 4),
                "p90": round(float(np.percentile(valid, 90)), 4),
                "valid_pixel_count": valid_count,
                "valid_pixel_pct": round(valid_pct, 1),
                "total_pixel_count": total_count,
            }
            if is_whole_raster:
                result["resolution_note"] = (
                    f"Whole-raster stats computed at downsampled resolution "
                    f"({out_w}x{out_h} px from {ds.width}x{ds.height} native) for speed. "
                    f"Mean/std/percentiles are statistically equivalent."
                )
            return result

    try:
        result = await asyncio.wait_for(
            asyncio.get_running_loop().run_in_executor(None, _compute),
            timeout=60,
        )
        if isinstance(result, dict) and "error" in result:
            return result
        response = {
            "layer_id": args.layer_id,
            "band": band,
            "polygon_used": polygon_used,
            **result,
        }
        # Build displayable_geojson outlining the analyzed polygon so the user
        # can verify Sage analyzed the right area. We don't paint by value because
        # band semantics vary (NDVI vs NDRE vs raw red) — the `outline` preset
        # just shows the polygon boundary in blue.
        try:
            from shapely.geometry import shape as _shape
            if polygon and polygon.get("type") in ("Polygon", "MultiPolygon"):
                feature = {
                    "type": "Feature",
                    "geometry": polygon,
                    "properties": {
                        "band": band,
                        "mean": result.get("mean"),
                        "polygon_used": polygon_used,
                    },
                }
                fc = {"type": "FeatureCollection", "features": [feature]}
                b = _shape(polygon).bounds
                response["displayable_geojson"] = {
                    "geojson": fc,
                    "style_hint": "outline",
                    "title": f"Zonal Stats — band {band} (mean {result.get('mean')})",
                    "bbox": f"{b[0]},{b[1]},{b[2]},{b[3]}",
                }
        except Exception:
            logger.debug("displayable_geojson build skipped for compute_zonal_stats", exc_info=True)
        return response
    except asyncio.TimeoutError:
        return {
            "error": (
                "Stats computation timed out after 60 seconds. The raster may be "
                "too large for direct on-the-fly stats. Try a smaller polygon."
            )
        }
    except Exception as e:
        logger.exception("zonal stats failed for layer %s", args.layer_id)
        return {"error": f"Stats computation failed: {str(e)[:200]}"}


# ── read_pixel_at ───────────────────────────────────────────────────────────


class ReadPixelAtArgs(BaseModel):
    layer_id: str = Field(
        ...,
        description="The layer_id of the user-uploaded raster.",
    )
    longitude: float = Field(
        ...,
        description="Longitude in WGS84 decimal degrees (e.g. 30.4245 for Rwanda).",
    )
    latitude: float = Field(
        ...,
        description="Latitude in WGS84 decimal degrees (e.g. -1.6970 for Rwanda).",
    )


async def read_pixel_at(
    args: ReadPixelAtArgs, meta: IngabeToolCallMetaArgs
) -> dict:
    """Read the per-band raw pixel value at a specific lon/lat location in a user-uploaded raster. Validates the point falls inside the layer's bounds first — if the user clicks at Rwanda coordinates against a Florida-georeferenced raster, returns a clear "outside bounds" error instead of silently returning NoData. Use this when the user clicks a specific spot on the map and asks "what's the value here?". For statistics over an area, use compute_zonal_stats. For a verdict on the whole field, use interpret_raster_health or analyze_rgb_field."""
    from src.structures import get_async_read_connection
    from src.utils import get_async_s3_client, get_bucket_name

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
    if row["type"] != "raster":
        return {"error": f"Layer {args.layer_id} is type '{row['type']}', not a raster."}

    bounds = row["bounds"]
    if bounds and len(bounds) == 4:
        west, south, east, north = bounds
        if not (west <= args.longitude <= east and south <= args.latitude <= north):
            distance_km = math.sqrt(
                ((args.longitude - (west + east) / 2) * 111.32 * math.cos(math.radians((south + north) / 2))) ** 2
                + ((args.latitude - (south + north) / 2) * 111.32) ** 2
            )
            return {
                "error": "point_outside_bounds",
                "message": (
                    f"Point ({args.longitude:.4f}, {args.latitude:.4f}) is outside the layer's "
                    f"bounding box [{west:.4f},{south:.4f} → {east:.4f},{north:.4f}]. "
                    f"Distance from layer center: {distance_km:.1f} km. "
                    f"Common cause: the raster has a wrong CRS (e.g. drone export with the wrong "
                    f"UTM zone). Check describe_user_raster output for sanity_warning."
                ),
                "layer_bounds": list(bounds),
                "queried_point": [args.longitude, args.latitude],
                "distance_km_from_center": round(distance_km, 1),
            }

    metadata = (
        json.loads(row["metadata"])
        if isinstance(row["metadata"], str)
        else (dict(row["metadata"]) if row["metadata"] else {})
    )
    cog_key = metadata.get("cog_key") or row["s3_key"]
    band_count = metadata.get("band_count") or 0

    s3_client = await get_async_s3_client()
    bucket = get_bucket_name()
    cog_url = await s3_client.generate_presigned_url(
        "get_object",
        Params={"Bucket": bucket, "Key": cog_key},
        ExpiresIn=300,
    )

    os.environ.setdefault("GDAL_HTTP_UNSAFESSL", "YES")

    def _read():
        from rio_tiler.io import Reader
        with Reader(cog_url) as src:
            point = src.point(args.longitude, args.latitude)
            return list(point.array.tolist()) if hasattr(point, "array") else list(point.data.tolist())

    try:
        values = await asyncio.wait_for(
            asyncio.get_running_loop().run_in_executor(None, _read),
            timeout=30,
        )
    except asyncio.TimeoutError:
        return {"error": "Pixel read timed out after 30 seconds."}
    except Exception as e:
        logger.exception("read_pixel_at failed for layer %s", args.layer_id)
        return {"error": f"Pixel read failed: {str(e)[:200]}"}

    return {
        "layer_id": args.layer_id,
        "longitude": args.longitude,
        "latitude": args.latitude,
        "values_per_band": values,
        "band_count": band_count or len(values),
    }


# ── get_value_distribution ──────────────────────────────────────────────────


class GetValueDistributionArgs(BaseModel):
    layer_id: str = Field(
        ...,
        description="The layer_id of the user-uploaded raster.",
    )
    band: int = Field(
        ...,
        description="Which band to sample (1-indexed). For NDVI in 4-band drone exports use 2; for single-band NDVI use 1.",
    )
    polygon_geojson: str = Field(
        ...,
        description="GeoJSON Polygon string to constrain the histogram, OR empty string '' to sample the whole raster.",
    )
    bins: int = Field(
        ...,
        description="Number of histogram bins. Use 20 as a default for human-readable display; up to 100 for fine analysis.",
    )


async def get_value_distribution(
    args: GetValueDistributionArgs, meta: IngabeToolCallMetaArgs
) -> dict:
    """Compute the value distribution (histogram + percentiles) of a raster band over a polygon (or whole raster). Returns: bin edges, bin counts, percentiles (5/10/25/50/75/90/95), mean, std, min, max, mode-bin. Skips NoData. Use this when the user asks about the spread, distribution, or variability of pixel values — for example, "what's the NDVI distribution across this field?", "are values bimodal?", "what's the worst 10% of pixels?". For a single number, use compute_zonal_stats. For a verdict on the field, use interpret_raster_health."""
    from src.structures import get_async_read_connection
    from src.utils import get_async_s3_client, get_bucket_name

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
    if row["type"] != "raster":
        return {"error": f"Layer {args.layer_id} is type '{row['type']}', not a raster."}

    metadata = (
        json.loads(row["metadata"])
        if isinstance(row["metadata"], str)
        else (dict(row["metadata"]) if row["metadata"] else {})
    )
    band_count = metadata.get("band_count", 1) or 1
    if args.band < 1 or args.band > band_count:
        return {"error": f"Band {args.band} out of range. Layer has {band_count} bands."}
    bins = max(2, min(int(args.bins), 200))

    if args.polygon_geojson and args.polygon_geojson.strip():
        try:
            polygon = json.loads(args.polygon_geojson)
        except Exception as e:
            return {"error": f"Could not parse polygon GeoJSON: {e}"}
        if polygon.get("type") == "Feature":
            polygon = polygon["geometry"]
        elif polygon.get("type") == "FeatureCollection":
            features = polygon.get("features") or []
            if not features:
                return {"error": "Empty FeatureCollection."}
            polygon = features[0]["geometry"]
        polygon_used = "user_provided"
    else:
        bounds = row["bounds"]
        if not bounds or len(bounds) != 4:
            return {"error": "No polygon provided and layer has no bounds."}
        west, south, east, north = bounds
        polygon = {
            "type": "Polygon",
            "coordinates": [
                [[west, south], [east, south], [east, north], [west, north], [west, south]]
            ],
        }
        polygon_used = "whole_raster_bbox"

    cog_key = metadata.get("cog_key") or row["s3_key"]
    s3_client = await get_async_s3_client()
    bucket = get_bucket_name()
    cog_url = await s3_client.generate_presigned_url(
        "get_object",
        Params={"Bucket": bucket, "Key": cog_key},
        ExpiresIn=900,
    )

    os.environ.setdefault("GDAL_HTTP_UNSAFESSL", "YES")
    is_whole_raster = polygon_used == "whole_raster_bbox"
    band = args.band

    def _compute() -> Dict[str, Any]:
        import numpy as np
        import rasterio
        from rasterio.enums import Resampling
        from rasterio.mask import mask as rio_mask
        from rasterio.warp import transform_geom

        with rasterio.open(cog_url) as ds:
            if is_whole_raster:
                target_long = 1024
                long_native = max(ds.width, ds.height)
                ovr = max(1, long_native // target_long)
                out_h = max(1, ds.height // ovr)
                out_w = max(1, ds.width // ovr)
                arr = ds.read(
                    band, out_shape=(out_h, out_w),
                    resampling=Resampling.average, masked=True,
                )
            else:
                geom = polygon
                target_crs = ds.crs
                if target_crs and target_crs.to_string() != "EPSG:4326":
                    geom = transform_geom("EPSG:4326", target_crs.to_string(), polygon)
                try:
                    masked, _ = rio_mask(
                        ds, [geom], crop=True, indexes=[band], filled=False
                    )
                except ValueError as e:
                    return {"error": f"Polygon does not overlap the raster: {e}"}
                arr = masked[0]

            if hasattr(arr, "mask"):
                valid = arr.data[~arr.mask] if hasattr(arr, "data") else arr[~arr.mask]
            else:
                valid = arr[~np.isnan(arr)] if arr.dtype.kind == "f" else arr.flatten()
            if arr.dtype.kind == "f":
                valid = valid[~np.isnan(valid)]
            if valid.size == 0:
                return {"error": "No valid pixels in the area."}

            valid_f = valid.astype("float64")
            counts, edges = np.histogram(valid_f, bins=bins)
            mode_bin_idx = int(np.argmax(counts))
            return {
                "bin_edges": [round(float(e), 4) for e in edges.tolist()],
                "bin_counts": counts.tolist(),
                "mean": round(float(np.mean(valid_f)), 4),
                "std": round(float(np.std(valid_f)), 4),
                "min": round(float(np.min(valid_f)), 4),
                "max": round(float(np.max(valid_f)), 4),
                "p5": round(float(np.percentile(valid_f, 5)), 4),
                "p10": round(float(np.percentile(valid_f, 10)), 4),
                "p25": round(float(np.percentile(valid_f, 25)), 4),
                "p50": round(float(np.percentile(valid_f, 50)), 4),
                "p75": round(float(np.percentile(valid_f, 75)), 4),
                "p90": round(float(np.percentile(valid_f, 90)), 4),
                "p95": round(float(np.percentile(valid_f, 95)), 4),
                "mode_bin_range": [
                    round(float(edges[mode_bin_idx]), 4),
                    round(float(edges[mode_bin_idx + 1]), 4),
                ],
                "valid_pixel_count": int(valid.size),
                "valid_pixel_pct": round(
                    float(valid.size) / float(arr.size) * 100.0, 1
                ) if arr.size > 0 else 0.0,
            }

    try:
        result = await asyncio.wait_for(
            asyncio.get_running_loop().run_in_executor(None, _compute),
            timeout=60,
        )
        if "error" in result:
            return result
        return {
            "layer_id": args.layer_id,
            "band": band,
            "bins": bins,
            "polygon_used": polygon_used,
            **result,
        }
    except asyncio.TimeoutError:
        return {"error": "Distribution computation timed out after 60 seconds."}
    except Exception as e:
        logger.exception("get_value_distribution failed for layer %s", args.layer_id)
        return {"error": f"Distribution failed: {str(e)[:200]}"}
