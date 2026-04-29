"""RGB-only visual analysis tools.

For drone orthophotos that are 3-band RGB (no NIR band, so true NDVI is not
computable). What we CAN do honestly:

  1. Visible coverage area: alpha-mask aware count of valid pixels → hectares.
  2. Greenness: GRVI = (Green - Red) / (Green + Red). Bounded -1..1, real
     correlation with vegetation vigor (~0.7 with NDVI for typical maize).
     Honest about being a weaker proxy than true NDVI.
  3. Spatial heterogeneity: GRVI std dev across the field.

We do NOT claim NDVI verdicts on RGB data. interpret_raster_health refuses
that case and points here instead.
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


# GRVI = (Green - Red) / (Green + Red). Values typically:
#   -0.05 to +0.05 = bare/dry
#   +0.05 to +0.15 = moderate canopy
#   +0.15 to +0.30 = healthy green canopy
#   > +0.30        = dense lush vegetation (or wet leaves at saturation)
# These are coarser thresholds than NDVI ranges. Stage-aware refinement is
# limited because GRVI saturates earlier than NDVI.
GRVI_VERDICT_BANDS = [
    (0.20, "lush_canopy", "Dense, vigorous green canopy."),
    (0.10, "healthy_canopy", "Healthy green canopy. Looks normal for an established crop."),
    (0.03, "moderate_canopy", "Moderate canopy. Could be early growth, partially senescent, or under stress."),
    (-0.05, "sparse_or_stressed", "Sparse vegetation or stress signature. Worth inspecting."),
    (-1.0, "bare_or_dry", "Mostly bare soil or dry/dormant vegetation."),
]


class AnalyzeRgbFieldArgs(BaseModel):
    layer_id: str = Field(
        ...,
        description="The layer_id of the user-uploaded RGB visual orthophoto (3-band ortho, no NIR).",
    )
    polygon_geojson: str = Field(
        ...,
        description=(
            "GeoJSON Polygon string defining the field area, OR empty string '' to "
            "analyze the whole raster."
        ),
    )
    red_band: int = Field(
        ...,
        description=(
            "Which band contains the Red channel (1-indexed). For typical RGB drone "
            "orthos, Red is band 1. For BGR-ordered exports it may be band 3."
        ),
    )
    green_band: int = Field(
        ...,
        description=(
            "Which band contains the Green channel (1-indexed). For typical RGB drone "
            "orthos, Green is band 2."
        ),
    )


async def analyze_rgb_field(
    args: AnalyzeRgbFieldArgs, meta: IngabeToolCallMetaArgs
) -> dict:
    """Analyze a user-uploaded RGB visual orthophoto for greenness and field coverage. Computes GRVI (Green-Red Vegetation Index) which is the only vegetation index derivable from RGB without a near-infrared band — about 70% as informative as true NDVI for distinguishing healthy from stressed canopy. Reports valid coverage in hectares (alpha-mask aware), GRVI mean / std / percentiles, and a coarse verdict (lush/healthy/moderate/sparse/bare). Use this for RGB-only drone orthos when the user asks about field coverage or visible greenness. ALWAYS prefer interpret_raster_health when the layer has a true NDVI band — true NDVI is more accurate. This tool is the honest fallback for RGB-only data."""
    from src.structures import get_async_read_connection
    from src.utils import get_async_s3_client, get_bucket_name

    async with get_async_read_connection() as conn:
        row = await conn.fetchrow(
            """
            SELECT layer_id, name, type, s3_key, bounds, metadata, owner_uuid
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
    band_count = metadata.get("band_count", 0) or 0
    if band_count < 2:
        return {"error": f"Layer has {band_count} band(s) — RGB analysis needs at least red + green."}
    if max(args.red_band, args.green_band) > band_count:
        return {
            "error": (
                f"Band index out of range. red_band={args.red_band}, "
                f"green_band={args.green_band}, layer has {band_count} bands."
            )
        }
    if args.red_band == args.green_band:
        return {"error": "red_band and green_band cannot be the same."}

    # Polygon handling
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

    # Fetch presigned URL
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
    red_band = args.red_band
    green_band = args.green_band

    def _compute() -> Dict[str, Any]:
        import numpy as np
        import rasterio
        from rasterio.enums import Resampling
        from rasterio.mask import mask as rio_mask
        from rasterio.warp import transform_geom

        with rasterio.open(cog_url) as ds:
            if is_whole_raster:
                # Read at downsampled resolution from overviews — same as zonal stats
                target_long = 1024
                long_native = max(ds.width, ds.height)
                ovr_factor = max(1, long_native // target_long)
                out_h = max(1, ds.height // ovr_factor)
                out_w = max(1, ds.width // ovr_factor)
                red = ds.read(
                    red_band, out_shape=(out_h, out_w),
                    resampling=Resampling.average, masked=True,
                )
                green = ds.read(
                    green_band, out_shape=(out_h, out_w),
                    resampling=Resampling.average, masked=True,
                )
                resolution_note = (
                    f"Computed at downsampled resolution ({out_w}x{out_h} from "
                    f"{ds.width}x{ds.height} native)."
                )
            else:
                geom = polygon
                target_crs = ds.crs
                if target_crs and target_crs.to_string() != "EPSG:4326":
                    geom = transform_geom("EPSG:4326", target_crs.to_string(), polygon)
                try:
                    masked, _ = rio_mask(
                        ds, [geom], crop=True,
                        indexes=[red_band, green_band], filled=False,
                    )
                except ValueError as e:
                    return {"error": f"Polygon does not overlap the raster: {e}"}
                red = masked[0]
                green = masked[1]
                resolution_note = "Computed at native resolution within polygon."

            # GRVI = (G - R) / (G + R), with NoData mask propagated
            r = red.astype("float32")
            g = green.astype("float32")
            denom = g + r
            with np.errstate(divide="ignore", invalid="ignore"):
                grvi = np.where(denom > 0, (g - r) / denom, np.nan)

            valid_mask = ~np.isnan(grvi)
            if hasattr(red, "mask"):
                valid_mask = valid_mask & (~red.mask) & (~green.mask)

            valid = grvi[valid_mask]
            valid_count = int(valid.size)
            total_count = int(grvi.size)
            valid_pct = (valid_count / total_count * 100.0) if total_count else 0.0

            if valid_count == 0:
                return {"error": "No valid pixels for GRVI computation."}

            grvi_mean = float(np.mean(valid))
            grvi_std = float(np.std(valid))
            grvi_p10 = float(np.percentile(valid, 10))
            grvi_p90 = float(np.percentile(valid, 90))

            return {
                "grvi_mean": round(grvi_mean, 4),
                "grvi_std": round(grvi_std, 4),
                "grvi_p10": round(grvi_p10, 4),
                "grvi_p50": round(float(np.percentile(valid, 50)), 4),
                "grvi_p90": round(grvi_p90, 4),
                "valid_pixel_count": valid_count,
                "valid_pixel_pct": round(valid_pct, 1),
                "total_pixel_count": total_count,
                "resolution_note": resolution_note,
            }

    try:
        result = await asyncio.wait_for(
            asyncio.get_running_loop().run_in_executor(None, _compute),
            timeout=60,
        )
        if "error" in result:
            return result

        # Map GRVI mean to verdict band
        grvi_mean = result["grvi_mean"]
        verdict = "bare_or_dry"
        verdict_message = GRVI_VERDICT_BANDS[-1][2]
        for threshold, level, msg in GRVI_VERDICT_BANDS:
            if grvi_mean >= threshold:
                verdict = level
                verdict_message = msg
                break

        # Compute valid area in hectares
        bounds = row["bounds"]
        valid_area_ha = None
        if bounds and len(bounds) == 4:
            west, south, east, north = bounds
            center_lat = (south + north) / 2
            dy_km = (north - south) * 111.32
            dx_km = (east - west) * 111.32 * math.cos(math.radians(center_lat))
            bbox_area_ha = dx_km * dy_km * 100 if dy_km > 0 and dx_km > 0 else None
            if bbox_area_ha and result["valid_pixel_pct"]:
                valid_area_ha = round(bbox_area_ha * result["valid_pixel_pct"] / 100.0, 1)

        heterogeneity = (
            "high (some patches noticeably greener than others)"
            if result["grvi_p90"] - result["grvi_p10"] > 0.15
            else "low (field looks fairly uniform)"
        )

        return {
            "verdict": verdict,
            "message": verdict_message,
            "interpretation": (
                f"GRVI mean {grvi_mean:.3f} (range {result['grvi_p10']:.3f} to "
                f"{result['grvi_p90']:.3f}). {verdict_message} "
                f"Spatial heterogeneity: {heterogeneity}."
            ),
            "honest_caveat": (
                "GRVI is computed from RGB only and is approximately 70% as informative "
                "as true NDVI for canopy stress. For higher-confidence health analysis, "
                "use a multispectral or NDVI-band drone export."
            ),
            "evidence": {
                "layer_name": row["name"],
                "valid_field_area_ha": valid_area_ha,
                "valid_coverage_pct": result["valid_pixel_pct"],
                "grvi_mean": grvi_mean,
                "grvi_std": result["grvi_std"],
                "grvi_p10": result["grvi_p10"],
                "grvi_p50": result["grvi_p50"],
                "grvi_p90": result["grvi_p90"],
                "polygon_used": polygon_used,
                "resolution_note": result["resolution_note"],
            },
        }
    except asyncio.TimeoutError:
        return {"error": "RGB analysis timed out after 60 seconds."}
    except Exception as e:
        logger.exception("analyze_rgb_field failed for layer %s", args.layer_id)
        return {"error": f"RGB analysis failed: {str(e)[:200]}"}
