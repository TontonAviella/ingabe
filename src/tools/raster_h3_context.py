from __future__ import annotations

import asyncio
import json
import logging
import math
import os
import time
import uuid
from dataclasses import dataclass
from typing import Any

import h3
from pydantic import BaseModel, Field

from src.routes.websocket import kue_ephemeral_action
from src.services.h3_layer_persistence import persist_h3_spatial_insight_layer
from src.services.h3_spatial_insight import h3_cell_geojson_geometry
from src.tools.pyd import IngabeToolCallMetaArgs

logger = logging.getLogger(__name__)


class CreateRasterH3ContextLayerArgs(BaseModel):
    layer_id: str = Field(
        ...,
        description="The layer_id of the user-uploaded raster/orthophoto to analyze and render as internal analysis cells.",
    )
    domain: str = Field(
        ...,
        description=(
            "Question context: agriculture, housing, infrastructure, environment, or mixed. "
            "Use the user's intent, not a guess from the basemap."
        ),
    )
    analysis_goal: str = Field(
        ...,
        description=(
            "Short goal for the cell layer, e.g. 'screen vegetation stress', "
            "'identify housing-context attention zones', or 'environment runoff screening'."
        ),
    )
    h3_resolution: int = Field(
        ...,
        description=(
            "H3 resolution. Use 9-10 for a full drone ortho overview, 11-13 for small fields, "
            "14-15 only for tiny clips."
        ),
    )
    max_hexes: int = Field(
        ...,
        description=(
            "Safety cap for generated cells. Use 1000-3000 for quick previews, "
            "or 8000-12000 for zoom-adaptive drone map layers that should refine as users zoom in."
        ),
    )
    max_sample_pixels: int = Field(
        ...,
        description=(
            "Maximum raster pixels to sample before grouping into cells. Use 20000-60000 "
            "for live Sage responses; larger values are slower but smoother."
        ),
    )
    render_map: bool = Field(
        ...,
        description="Whether to immediately render the generated cell layer on the map. Usually true.",
    )
    render_3d: bool = Field(
        ...,
        description="Whether to render extruded 3D columns from the cell score. Use false for dense orthophotos.",
    )


@dataclass(frozen=True)
class RasterCellStats:
    count: int
    grvi_sum: float
    grvi_sq_sum: float
    brightness_sum: float


async def create_raster_h3_context_layer(
    args: CreateRasterH3ContextLayerArgs,
    meta: IngabeToolCallMetaArgs,
) -> dict[str, Any]:
    """Create a raster-backed cell analysis layer over an uploaded drone TIFF/COG.

    Use when the user asks what is happening in an uploaded drone/orthophoto
    map, where problems are, what areas need attention, or whether the context
    is agriculture, housing, infrastructure, or environment. This tool samples
    the actual raster pixels first, then groups them into internal H3 cells for
    fast map rendering. It does not pretend RGB imagery is a building detector:
    for housing or infrastructure it returns visual attention cells and tells
    Sage to pair the result with Open Buildings/roads/drainage evidence when
    asset exposure matters. For simple metadata or hectares questions, use
    describe_user_raster instead.
    """

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
    source_key = metadata.get("cog_key") or row["s3_key"]
    if not source_key:
        return {"error": f"Layer {args.layer_id} has no readable raster object key."}

    s3_client = await get_async_s3_client()
    raster_url = await s3_client.generate_presigned_url(
        "get_object",
        Params={"Bucket": get_bucket_name(), "Key": source_key},
        ExpiresIn=900,
    )

    try:
        result = await asyncio.wait_for(
            asyncio.get_running_loop().run_in_executor(
                None,
                lambda: build_raster_h3_context_from_url(
                    raster_url,
                    layer_id=args.layer_id,
                    layer_name=row["name"],
                    bounds_wgs84=list(row["bounds"]) if row["bounds"] else None,
                    domain=args.domain,
                    analysis_goal=args.analysis_goal,
                    h3_resolution=args.h3_resolution,
                    max_hexes=args.max_hexes,
                    max_sample_pixels=args.max_sample_pixels,
                    original_filename=metadata.get("original_filename"),
                    source_storage="cog" if metadata.get("cog_key") else "raw_tiff",
                ),
            ),
            timeout=90,
        )
    except Exception as exc:
        logger.exception("raster H3 context failed for layer %s", args.layer_id)
        return {"error": f"Raster cell analysis failed: {exc}"}

    persisted_layer = None
    if args.render_map and result.get("status") == "success":
        try:
            persisted_layer = await persist_h3_spatial_insight_layer(
                result=result,
                user_uuid=meta.user_uuid,
                map_id=meta.map_id,
                project_id=meta.project_id,
                layer_name=f"Raster Context - {row['name']}",
                render_3d=args.render_3d,
            )
        except Exception as exc:
            logger.warning("Raster H3 layer persistence failed; using inline fallback: %s", exc, exc_info=True)

        engines = result.setdefault("engines", {})
        render_engine = engines.setdefault("render", {})
        transport = engines.setdefault("transport", {})
        if persisted_layer:
            async with kue_ephemeral_action(
                meta.conversation_id,
                f"Saving raster context layer: {row['name']}",
                layer_id=persisted_layer.layer_id,
                update_style_json=True,
                bounds=persisted_layer.bounds or result.get("bbox"),
            ) as payload:
                payload.updates["h3_layer_persisted"] = {
                    "layer_id": persisted_layer.layer_id,
                    "name": f"Raster Context - {row['name']}",
                    "pmtiles": True,
                    "geoparquet": bool(persisted_layer.geoparquet_key),
                    "feature_count": persisted_layer.feature_count,
                }
                await asyncio.sleep(0.2)
            render_engine["layer_id"] = persisted_layer.layer_id
            render_engine["rendered"] = True
            transport["current"] = "pmtiles_vector_layer"
            transport["browser"] = "PMTiles/MVT"
            transport["analytics_cache"] = (
                "GeoParquet" if persisted_layer.geoparquet_key else "pending"
            )
            result["layer_id"] = persisted_layer.layer_id
            result["pmtiles_key"] = persisted_layer.pmtiles_key
            result["geoparquet_key"] = persisted_layer.geoparquet_key
        else:
            source_id = f"sage-raster-h3-{uuid.uuid4().hex[:8]}"
            async with kue_ephemeral_action(
                meta.conversation_id,
                f"Rendering raster context preview: {row['name']}",
                bounds=result.get("bbox"),
            ) as payload:
                payload.updates["add_geojson_layer"] = {
                    "source_id": source_id,
                    "geojson": result["geojson"],
                    "name": f"Raster Context - {row['name']}",
                    "bounds": result.get("bbox"),
                    "style_hint": "h3_spatial_insight_risk",
                    "style": _inline_style(args.render_3d),
                }
                await asyncio.sleep(0.2)
            render_engine["source_id"] = source_id
            render_engine["rendered"] = True
            transport["current"] = "inline_geojson_preview_fallback"

    _capture_raster_h3_telemetry(result, args=args, meta=meta, persisted=bool(persisted_layer))

    if result.get("status") == "success":
        geojson = result["geojson"]
        result["geojson_feature_count"] = len(geojson.get("features", []))
        if persisted_layer:
            result["geojson"] = (
                "omitted from tool response; persisted as PMTiles/MVT layer "
                f"{persisted_layer.layer_id}"
            )
        else:
            result["geojson"] = json.dumps(geojson)

    return result


def build_raster_h3_context_from_url(
    raster_url: str,
    *,
    layer_id: str,
    layer_name: str,
    bounds_wgs84: list[float] | None,
    domain: str,
    analysis_goal: str,
    h3_resolution: int,
    max_hexes: int,
    max_sample_pixels: int,
    original_filename: str | None,
    source_storage: str,
) -> dict[str, Any]:
    """Build a raster-backed H3 FeatureCollection from a local path or URL."""

    _validate_inputs(h3_resolution, max_hexes, max_sample_pixels)
    if os.environ.get("MUNDI_DEV_ALLOW_UNSAFE_SSL") == "1":
        os.environ.setdefault("GDAL_HTTP_UNSAFESSL", "YES")
    start = time.perf_counter()

    import numpy as np
    import rasterio
    from affine import Affine
    from rasterio.enums import Resampling
    from rasterio.warp import transform as rio_transform
    from rasterio.warp import transform_bounds

    with rasterio.open(raster_url) as ds:
        out_h, out_w = _target_shape(ds.width, ds.height, max_sample_pixels)
        band_count = ds.count
        if band_count < 2:
            return {
                "error": (
                    "Raster cell context needs at least red and green bands for visual "
                    "screening. Use compute_zonal_stats or a terrain/NDVI-specific tool "
                    "for single-band rasters."
                )
            }

        red_band = 1
        green_band = 2 if band_count >= 2 else 1
        blue_band = 3 if band_count >= 3 else green_band
        red = ds.read(red_band, out_shape=(out_h, out_w), resampling=Resampling.average, masked=True)
        green = ds.read(green_band, out_shape=(out_h, out_w), resampling=Resampling.average, masked=True)
        blue = ds.read(blue_band, out_shape=(out_h, out_w), resampling=Resampling.average, masked=True)

        raster_bounds = bounds_wgs84
        if not raster_bounds and ds.crs:
            raster_bounds = list(
                transform_bounds(ds.crs, "EPSG:4326", *ds.bounds, densify_pts=21)
            )

        transform = ds.transform * Affine.scale(ds.width / out_w, ds.height / out_h)
        xs, ys = _pixel_centers(transform, out_h, out_w)
        xs_flat = xs.reshape(-1)
        ys_flat = ys.reshape(-1)
        if ds.crs and ds.crs.to_string() != "EPSG:4326":
            lon, lat = rio_transform(ds.crs, "EPSG:4326", xs_flat.tolist(), ys_flat.tolist())
            lon_arr = np.asarray(lon, dtype="float64")
            lat_arr = np.asarray(lat, dtype="float64")
        else:
            lon_arr = xs_flat.astype("float64")
            lat_arr = ys_flat.astype("float64")

    r = red.astype("float32").filled(np.nan).reshape(-1)
    g = green.astype("float32").filled(np.nan).reshape(-1)
    b = blue.astype("float32").filled(np.nan).reshape(-1)
    valid_mask = _valid_rgb_mask(red, green, blue).reshape(-1)
    denom = r + g
    with np.errstate(divide="ignore", invalid="ignore"):
        grvi = np.where(denom > 0, (g - r) / denom, np.nan)
    valid_mask = valid_mask & np.isfinite(grvi) & np.isfinite(lon_arr) & np.isfinite(lat_arr)
    valid_mask = valid_mask & (lat_arr >= -90) & (lat_arr <= 90) & (lon_arr >= -180) & (lon_arr <= 180)

    valid_pixel_count = int(np.count_nonzero(valid_mask))
    if valid_pixel_count == 0:
        return {"error": "No valid pixels were available for raster cell analysis."}

    scale = _brightness_scale(r[valid_mask], g[valid_mask], b[valid_mask])
    brightness = np.clip((r + g + b) / (3.0 * scale), 0.0, 1.0)

    min_cell_pixels = max(3, int(valid_pixel_count * 0.00015))
    selected_levels: list[tuple[int, dict[str, RasterCellStats]]] = []
    selected_feature_count = 0
    for candidate_resolution in _candidate_h3_resolutions(h3_resolution):
        candidate_cells = _aggregate_pixels_to_cells(
            lat=lat_arr[valid_mask],
            lon=lon_arr[valid_mask],
            grvi=grvi[valid_mask],
            brightness=brightness[valid_mask],
            h3_resolution=candidate_resolution,
        )
        candidate_cells = {
            h3_index: stats
            for h3_index, stats in candidate_cells.items()
            if stats.count >= min_cell_pixels
        }
        if not candidate_cells:
            continue
        if selected_feature_count + len(candidate_cells) > max_hexes:
            if selected_levels:
                break
            return {
                "status": "error",
                "error": (
                    f"H3 resolution {candidate_resolution} produced {len(candidate_cells)} "
                    f"raster-backed cells after filtering, above max_hexes={max_hexes}. "
                    "Use a coarser resolution or smaller area."
                ),
                "suggested_action": "Lower h3_resolution by 1-2 levels for live map analysis.",
            }
        selected_levels.append((candidate_resolution, candidate_cells))
        selected_feature_count += len(candidate_cells)

    if not selected_levels:
        return {
            "error": (
                "Raster pixels did not produce any analysis cells with enough pixel "
                "support. Use a coarser H3 resolution or increase max_sample_pixels."
            )
        }

    zoom_resolution_map = _zoom_resolution_map([level[0] for level in selected_levels])
    features: list[dict[str, Any]] = []
    scores: list[float] = []
    resolution_cell_counts: dict[str, int] = {}
    for (candidate_resolution, candidate_cells), zoom_band in zip(selected_levels, zoom_resolution_map):
        level_features, level_scores = _cell_features(
            candidate_cells,
            domain=domain,
            analysis_goal=analysis_goal,
            h3_resolution=candidate_resolution,
            zoom_min=zoom_band["minzoom"],
            zoom_max=zoom_band["maxzoom"],
        )
        resolution_cell_counts[str(candidate_resolution)] = len(level_features)
        features.extend(level_features)
        scores.extend(level_scores)

    elapsed_ms = round((time.perf_counter() - start) * 1000.0, 1)
    scores = scores or [0.0]
    top_cells = sorted(
        (
            {
                "h3_index": f["properties"]["h3_index"],
                "score": f["properties"]["risk_score"],
                "level": f["properties"]["risk_level"],
                "grvi_mean": f["properties"]["grvi_mean"],
                "valid_pixel_count": f["properties"]["valid_pixel_count"],
                "likely_issue": f["properties"]["likely_issue"],
            }
            for f in features
        ),
        key=lambda row: row["score"],
        reverse=True,
    )[:8]

    normalized_domain = _normalize_domain(domain)
    evidence_note = _evidence_note(normalized_domain)
    return {
        "status": "success",
        "summary": {
            "source_layer_id": layer_id,
            "source_layer_name": layer_name,
            "domain": normalized_domain,
            "analysis_goal": analysis_goal,
            "h3_resolution": selected_levels[0][0],
            "h3_resolutions": [level[0] for level in selected_levels],
            "adaptive_resolution": len(selected_levels) > 1,
            "resolution_count": len(selected_levels),
            "resolution_cell_counts": resolution_cell_counts,
            "zoom_resolution_map": zoom_resolution_map,
            "cell_count": len(features),
            "sampled_raster_pixels": valid_pixel_count,
            "sample_shape": f"{out_w}x{out_h}",
            "minimum_cell_pixels": min_cell_pixels,
            "source_storage": source_storage,
            "original_filename": original_filename,
            "mean_score": round(sum(scores) / len(scores), 1),
            "max_score": round(max(scores), 1),
            "high_or_severe_cell_count": sum(1 for score in scores if score >= 60),
            "evidence_basis": evidence_note,
            "confidence": _overall_confidence(normalized_domain),
            "elapsed_ms": elapsed_ms,
            "top_cells": top_cells,
            "honesty_note": (
                "Cells are backed by sampled raster pixels. RGB orthophotos support GRVI/brightness "
                "screening only; confirmed buildings, roads, drainage, or crop damage require the "
                "matching evidence layer/model."
            ),
            "next_best_evidence": _next_best_evidence(normalized_domain),
        },
        "bbox": raster_bounds,
        "geojson": {"type": "FeatureCollection", "features": features},
        "screening_model": "raster_h3_context_v1",
        "engines": {
            "grid": {
                "name": "H3",
                "runtime": "python-h3-v4",
                "rust_target": "mundi-geokernel/h3o",
            },
            "analysis": {
                "raster_sampled": True,
                "metric": "GRVI plus brightness from uploaded raster pixels",
                "building_detector_used": False,
                "note": evidence_note,
            },
            "render": {
                "runtime": "MapLibre/deck.gl",
                "style_hint": "h3_spatial_insight_risk",
                "height_property": "risk_score",
                "height_scale": 35,
                "rendered": False,
            },
            "transport": {
                "internal": "geojson_feature_collection_for_conversion",
                "browser_target": "MVT/PMTiles",
                "analytics_target": "GeoParquet",
            },
        },
    }


def _validate_inputs(h3_resolution: int, max_hexes: int, max_sample_pixels: int) -> None:
    if not 0 <= h3_resolution <= 15:
        raise ValueError("h3_resolution must be between 0 and 15")
    if max_hexes < 1:
        raise ValueError("max_hexes must be at least 1")
    if max_sample_pixels < 100:
        raise ValueError("max_sample_pixels must be at least 100")


def _target_shape(width: int, height: int, max_sample_pixels: int) -> tuple[int, int]:
    total = max(1, width * height)
    cap = min(max_sample_pixels, 350_000)
    if total <= cap:
        return height, width
    scale = math.sqrt(cap / total)
    return max(1, int(height * scale)), max(1, int(width * scale))


def _candidate_h3_resolutions(base_resolution: int) -> list[int]:
    if base_resolution >= 14:
        return [base_resolution, min(15, base_resolution + 1)]
    return list(range(base_resolution, min(15, base_resolution + 2) + 1))


def _zoom_resolution_map(resolutions: list[int]) -> list[dict[str, float | int]]:
    if not resolutions:
        return []
    if len(resolutions) == 1:
        return [{"h3_resolution": resolutions[0], "minzoom": 0, "maxzoom": 24}]

    first_switch_zoom = max(8, min(18, resolutions[0] + 5))
    zoom_map: list[dict[str, float | int]] = []
    for idx, resolution in enumerate(resolutions):
        if idx == 0:
            minzoom = 0
            maxzoom = first_switch_zoom
        elif idx == len(resolutions) - 1:
            minzoom = first_switch_zoom + (idx - 1) * 2
            maxzoom = 24
        else:
            minzoom = first_switch_zoom + (idx - 1) * 2
            maxzoom = first_switch_zoom + idx * 2
        zoom_map.append(
            {
                "h3_resolution": resolution,
                "minzoom": minzoom,
                "maxzoom": maxzoom,
            }
        )
    return zoom_map


def _pixel_centers(transform: Any, height: int, width: int) -> tuple[Any, Any]:
    import numpy as np

    rows, cols = np.indices((height, width), dtype="float64")
    xs = transform.c + (cols + 0.5) * transform.a + (rows + 0.5) * transform.b
    ys = transform.f + (cols + 0.5) * transform.d + (rows + 0.5) * transform.e
    return xs, ys


def _valid_rgb_mask(red: Any, green: Any, blue: Any) -> Any:
    import numpy as np

    return (
        ~np.ma.getmaskarray(red)
        & ~np.ma.getmaskarray(green)
        & ~np.ma.getmaskarray(blue)
    )


def _brightness_scale(r: Any, g: Any, b: Any) -> float:
    import numpy as np

    values = np.concatenate([r, g, b])
    finite = values[np.isfinite(values)]
    if finite.size == 0:
        return 1.0
    scale = float(np.percentile(finite, 98))
    return scale if scale > 0 else 1.0


def _aggregate_pixels_to_cells(
    *,
    lat: Any,
    lon: Any,
    grvi: Any,
    brightness: Any,
    h3_resolution: int,
) -> dict[str, RasterCellStats]:
    accum: dict[str, list[float]] = {}
    for lat_value, lon_value, grvi_value, brightness_value in zip(lat, lon, grvi, brightness):
        cell = h3.latlng_to_cell(float(lat_value), float(lon_value), h3_resolution)
        row = accum.setdefault(cell, [0.0, 0.0, 0.0, 0.0])
        row[0] += 1.0
        row[1] += float(grvi_value)
        row[2] += float(grvi_value) * float(grvi_value)
        row[3] += float(brightness_value)
    return {
        cell: RasterCellStats(
            count=int(values[0]),
            grvi_sum=values[1],
            grvi_sq_sum=values[2],
            brightness_sum=values[3],
        )
        for cell, values in accum.items()
        if values[0] > 0
    }


def _cell_features(
    cells: dict[str, RasterCellStats],
    *,
    domain: str,
    analysis_goal: str,
    h3_resolution: int,
    zoom_min: float | int,
    zoom_max: float | int,
) -> tuple[list[dict[str, Any]], list[float]]:
    features: list[dict[str, Any]] = []
    scores: list[float] = []
    normalized_domain = _normalize_domain(domain)
    for h3_index, stats in sorted(cells.items()):
        grvi_mean = stats.grvi_sum / stats.count
        variance = max((stats.grvi_sq_sum / stats.count) - grvi_mean * grvi_mean, 0.0)
        grvi_std = math.sqrt(variance)
        brightness_mean = stats.brightness_sum / stats.count
        score = _score_cell(normalized_domain, grvi_mean, brightness_mean)
        scores.append(score)
        likely_issue = _likely_issue(normalized_domain, score)
        features.append(
            {
                "type": "Feature",
                "geometry": h3_cell_geojson_geometry(h3_index),
                "properties": {
                    "h3_index": h3_index,
                    "h3_resolution": h3_resolution,
                    "zoom_min": zoom_min,
                    "zoom_max": zoom_max,
                    "domain": normalized_domain,
                    "analysis_goal": analysis_goal,
                    "risk_score": round(score, 1),
                    "risk_level": _risk_level(score),
                    "score_kind": _score_kind(normalized_domain),
                    "grvi_mean": round(grvi_mean, 4),
                    "grvi_std": round(grvi_std, 4),
                    "brightness_mean": round(brightness_mean, 4),
                    "valid_pixel_count": stats.count,
                    "likely_issue": likely_issue,
                    "recommended_action": _recommended_action(normalized_domain, score),
                    "evidence_basis": _evidence_note(normalized_domain),
                    "confidence": _cell_confidence(normalized_domain),
                    "screening_model": "raster_h3_context_v1",
                },
            }
        )
    return features, scores


def _normalize_domain(domain: str) -> str:
    value = (domain or "").strip().lower()
    if value in {"farm", "field", "crop", "drone"}:
        return "agriculture"
    if value in {"house", "houses", "building", "buildings", "settlement", "urban", "city"}:
        return "housing"
    if value in {"road", "roads", "drainage", "culvert", "bridge"}:
        return "infrastructure"
    if value in {"environmental", "erosion", "runoff", "pollution"}:
        return "environment"
    if value not in {"agriculture", "housing", "infrastructure", "environment", "mixed"}:
        return "mixed"
    return value


def _score_cell(domain: str, grvi_mean: float, brightness_mean: float) -> float:
    if domain == "agriculture":
        return _clamp(((0.16 - grvi_mean) / 0.28) * 100.0, 0.0, 100.0)
    if domain == "environment":
        return _clamp(((0.10 - grvi_mean) / 0.24) * 85.0 + brightness_mean * 12.0, 0.0, 100.0)
    if domain in {"housing", "infrastructure"}:
        low_vegetation = _clamp((0.08 - grvi_mean) / 0.20, 0.0, 1.0)
        bright_or_bare = _clamp((brightness_mean - 0.32) / 0.42, 0.0, 1.0)
        return _clamp(25.0 + (low_vegetation * 45.0) + (bright_or_bare * 30.0), 0.0, 100.0)
    low_greenness = _clamp((0.10 - grvi_mean) / 0.24, 0.0, 1.0)
    return _clamp(20.0 + low_greenness * 65.0 + brightness_mean * 15.0, 0.0, 100.0)


def _clamp(value: float, minimum: float, maximum: float) -> float:
    return max(minimum, min(maximum, value))


def _risk_level(score: float) -> str:
    if score >= 80:
        return "severe"
    if score >= 60:
        return "high"
    if score >= 40:
        return "moderate"
    return "low"


def _score_kind(domain: str) -> str:
    if domain == "agriculture":
        return "visible_vegetation_stress_proxy"
    if domain == "environment":
        return "vegetation_or_exposed_surface_proxy"
    if domain in {"housing", "infrastructure"}:
        return "non_vegetated_or_bright_surface_attention_proxy"
    return "mixed_visual_attention_proxy"


def _likely_issue(domain: str, score: float) -> str:
    if domain == "agriculture":
        if score >= 60:
            return "low visible greenness or exposed soil; inspect crop condition"
        if score >= 40:
            return "mixed vegetation cover; compare with crop boundaries and field notes"
        return "green vegetation signal; keep as baseline context"
    if domain in {"housing", "infrastructure"}:
        if score >= 60:
            return "non-vegetated or bright-surface candidate; verify with buildings, roads, and drainage evidence"
        if score >= 40:
            return "mixed surface context; do not infer housing without exposure data"
        return "mostly vegetated visual context; no confirmed infrastructure inference"
    if domain == "environment":
        if score >= 60:
            return "low vegetation or exposed/wet surface proxy; check erosion, drainage, or pollution evidence"
        if score >= 40:
            return "mixed environmental surface condition"
        return "green/covered surface context"
    return "visual attention cell; needs domain evidence for a firm interpretation"


def _recommended_action(domain: str, score: float) -> str:
    if domain == "agriculture":
        if score >= 60:
            return "Prioritize these cells for field walk, soil wetness check, crop-stage check, and boundary confirmation."
        return "Use as raster-backed context; compare with crop condition, soil wetness, and field boundaries."
    if domain in {"housing", "infrastructure"}:
        if score >= 60:
            return "Overlay building footprints, roads, drainage lines, or inspection points before calling this confirmed exposure."
        return "Keep as visual context; use Open Buildings/OSM/field survey data for asset-level decisions."
    if domain == "environment":
        if score >= 60:
            return "Inspect drainage, runoff paths, erosion signs, or wetness indicators in these cells."
        return "Use as environmental context and combine with terrain/hydrology evidence when available."
    return "Treat as a screening cell and ask for the domain evidence needed to decide action."


def _evidence_note(domain: str) -> str:
    if domain == "agriculture":
        return "uploaded raster pixels grouped into internal cells using GRVI/brightness; RGB-only proxy, not true NDVI"
    if domain in {"housing", "infrastructure"}:
        return "uploaded raster pixels grouped into internal cells; visual proxy only, not confirmed building/road detection"
    if domain == "environment":
        return "uploaded raster pixels grouped into internal cells using greenness and exposed-surface proxy"
    return "uploaded raster pixels grouped into internal cells; domain-specific evidence still needed"


def _cell_confidence(domain: str) -> str:
    if domain == "agriculture":
        return "medium"
    if domain == "environment":
        return "low_medium"
    return "low"


def _overall_confidence(domain: str) -> str:
    return _cell_confidence(domain)


def _next_best_evidence(domain: str) -> list[str]:
    if domain == "agriculture":
        return ["crop/field boundary", "true NDVI/NDRE band if available", "soil wetness or recent rain"]
    if domain == "housing":
        return ["Open Buildings footprints", "local building/parcel survey", "flood depth or drainage exposure"]
    if domain == "infrastructure":
        return ["roads/drainage/culverts layer", "Whitebox terrain/hydrology metrics", "field inspection points"]
    if domain == "environment":
        return ["Whitebox terrain/hydrology metrics", "water/drainage layer", "pollution/erosion observations"]
    return ["domain evidence such as buildings, roads, farms, drainage, rain, or terrain metrics"]


def _inline_style(render_3d: bool) -> dict[str, Any]:
    return {
        "color_property": "risk_score",
        "stops": [
            {"max": 40, "color": "#22c55e"},
            {"max": 60, "color": "#facc15"},
            {"max": 80, "color": "#f97316"},
            {"max": 101, "color": "#dc2626"},
        ],
        "fill_opacity": 0.58,
        "stroke_color": "#111827",
        "stroke_width": 1.2,
        "extrude_3d": render_3d,
        "extrusion_property": "risk_score",
        "extrusion_scale": 35,
    }


def _capture_raster_h3_telemetry(
    result: dict[str, Any],
    *,
    args: CreateRasterH3ContextLayerArgs,
    meta: IngabeToolCallMetaArgs,
    persisted: bool,
) -> None:
    try:
        from src.services.posthog_analytics import capture_backend_event

        summary = result.get("summary") if isinstance(result, dict) else {}
        capture_backend_event(
            "backend_raster_h3_context_completed",
            distinct_id=str(meta.user_uuid),
            properties={
                "layer_id": args.layer_id,
                "domain": args.domain,
                "h3_resolution": args.h3_resolution,
                "h3_resolutions_csv": ",".join(
                    str(resolution)
                    for resolution in (summary.get("h3_resolutions") or [args.h3_resolution])
                )
                if isinstance(summary, dict)
                else str(args.h3_resolution),
                "adaptive_resolution": summary.get("adaptive_resolution") if isinstance(summary, dict) else False,
                "resolution_count": summary.get("resolution_count") if isinstance(summary, dict) else 1,
                "max_sample_pixels": args.max_sample_pixels,
                "cell_count": summary.get("cell_count") if isinstance(summary, dict) else None,
                "elapsed_ms": summary.get("elapsed_ms") if isinstance(summary, dict) else None,
                "status": result.get("status"),
                "persisted": persisted,
            },
        )
    except Exception:
        logger.debug("PostHog telemetry skipped for raster H3 context", exc_info=True)
