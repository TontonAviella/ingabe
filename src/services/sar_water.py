# Copyright (C) 2025 Ingabe Ltd.
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU Affero General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.

"""SAR-based water detection and flood delineation.

Detects water bodies from Sentinel-1 VV backscatter using adaptive local
thresholding (quadtree tiling + coefficient of variation). Delineates
flood extent by comparing pre/post SAR imagery.

Algorithms are clean-room implementations inspired by published SAR
textbook techniques (quadtree local thresholding, multilook averaging),
adapted for Sentinel-1 C-band RTC data and Rwanda's hilly terrain.

Usage:
    from src.services.sar_water import get_sar_water_service
    svc = get_sar_water_service()
    water = svc.detect_water(bbox=(29.0, -2.5, 30.0, -1.5))
    flood = svc.detect_flood(bbox=(29.0, -2.5, 30.0, -1.5),
                             date_before="2025-01-01", date_after="2025-02-01")
"""

from __future__ import annotations

import logging
import math
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional, Tuple

import numpy as np

logger = logging.getLogger(__name__)


def _multilook(arr: np.ndarray, factor: int = 2) -> np.ndarray:
    """Reduce speckle noise via block averaging (multilook).

    Trims array to multiple of factor, then block-averages.
    """
    h, w = arr.shape
    h_trim = (h // factor) * factor
    w_trim = (w // factor) * factor
    trimmed = arr[:h_trim, :w_trim]
    return np.nanmean(
        trimmed.reshape(h_trim // factor, factor, w_trim // factor, factor),
        axis=(1, 3),
    )


def _compute_water_threshold(
    arr: np.ndarray,
    tile_size: int = 8,
    sub_tile_size: int = 4,
    cv_percentile: float = 90.0,
) -> float:
    """Compute adaptive water/land threshold using quadtree local method.

    1. Divide image into L+ tiles (tile_size x tile_size pixels)
    2. Subdivide each L+ tile into L- sub-tiles (sub_tile_size x sub_tile_size)
    3. Compute coefficient of variation: CV = std(L- means) / mean(L+ tile)
    4. Select high-CV tiles (> cv_percentile) with below-average backscatter
    5. Threshold = mean of selected tile pixels
    """
    h, w = arr.shape
    if h < tile_size or w < tile_size:
        # Too small for tiling, use global Otsu-like threshold
        valid = arr[np.isfinite(arr)]
        if valid.size == 0:
            return -15.0  # sensible default for water in dB
        return float(np.nanmean(valid) - np.nanstd(valid))

    tile_cvs: List[float] = []
    tile_means: List[float] = []
    tile_pixels: List[np.ndarray] = []

    n_tiles_y = h // tile_size
    n_tiles_x = w // tile_size

    for ty in range(n_tiles_y):
        for tx in range(n_tiles_x):
            y0, x0 = ty * tile_size, tx * tile_size
            tile = arr[y0:y0 + tile_size, x0:x0 + tile_size]
            tile_valid = tile[np.isfinite(tile)]
            if tile_valid.size < sub_tile_size * sub_tile_size:
                continue

            tile_mean = float(np.nanmean(tile_valid))
            if abs(tile_mean) < 1e-10:
                continue

            # Compute sub-tile means
            sub_means: List[float] = []
            n_sub_y = tile_size // sub_tile_size
            n_sub_x = tile_size // sub_tile_size
            for sy in range(n_sub_y):
                for sx in range(n_sub_x):
                    sy0, sx0 = sy * sub_tile_size, sx * sub_tile_size
                    sub = tile[sy0:sy0 + sub_tile_size, sx0:sx0 + sub_tile_size]
                    sub_valid = sub[np.isfinite(sub)]
                    if sub_valid.size > 0:
                        sub_means.append(float(np.nanmean(sub_valid)))

            if len(sub_means) < 2:
                continue

            cv = float(np.std(sub_means)) / abs(tile_mean)
            tile_cvs.append(cv)
            tile_means.append(tile_mean)
            tile_pixels.append(tile_valid)

    if not tile_cvs:
        valid = arr[np.isfinite(arr)]
        return float(np.nanmean(valid) - np.nanstd(valid)) if valid.size > 0 else -15.0

    cv_arr = np.array(tile_cvs)
    mean_arr = np.array(tile_means)
    cv_threshold = float(np.percentile(cv_arr, cv_percentile))
    global_mean = float(np.nanmean(arr[np.isfinite(arr)]))

    # Select high-CV tiles with below-average backscatter (water candidates)
    selected_pixels: List[np.ndarray] = []
    for i, (cv, tmean, tpix) in enumerate(zip(tile_cvs, tile_means, tile_pixels)):
        if cv >= cv_threshold and tmean < global_mean:
            selected_pixels.append(tpix)

    if not selected_pixels:
        # No water candidates found, use conservative threshold
        return float(global_mean - 2.0 * np.nanstd(arr[np.isfinite(arr)]))

    all_selected = np.concatenate(selected_pixels)
    return float(np.nanmean(all_selected))


def _water_mask(
    arr: np.ndarray,
    tile_size: int = 8,
    sub_tile_size: int = 4,
    cv_percentile: float = 90.0,
    min_area_pixels: int = 9,
) -> Tuple[np.ndarray, float]:
    """Detect water pixels from SAR backscatter array.

    Returns (binary_mask, threshold_value).
    """
    from scipy.ndimage import binary_opening, label

    threshold = _compute_water_threshold(arr, tile_size, sub_tile_size, cv_percentile)

    # Water = pixels below threshold
    valid = np.isfinite(arr)
    mask = valid & (arr < threshold)

    # Morphological cleanup: remove isolated pixels
    structure = np.ones((3, 3), dtype=bool)
    mask = binary_opening(mask, structure=structure)

    # Remove small components
    if min_area_pixels > 1:
        labeled, n_features = label(mask)
        for i in range(1, n_features + 1):
            component = labeled == i
            if component.sum() < min_area_pixels:
                mask[component] = False

    return mask, threshold


def _mask_to_geojson(
    mask: np.ndarray,
    transform: Any,
    crs: Any,
    max_features: int = 500,
) -> Dict[str, Any]:
    """Convert binary mask to GeoJSON FeatureCollection."""
    import rasterio.features
    from shapely.geometry import shape, mapping

    features: List[Dict[str, Any]] = []
    mask_uint8 = mask.astype(np.uint8)

    try:
        for geom, val in rasterio.features.shapes(mask_uint8, transform=transform):
            if val == 1:
                poly = shape(geom)
                if poly.is_valid and poly.area > 0:
                    simplified = poly.simplify(tolerance=0.0001, preserve_topology=True)
                    features.append({
                        "type": "Feature",
                        "geometry": mapping(simplified),
                        "properties": {"class": "water"},
                    })
                    if len(features) >= max_features:
                        logger.warning("GeoJSON capped at %d features", max_features)
                        break
    except Exception as e:
        logger.warning("Failed to vectorize water mask: %s", e)

    return {
        "type": "FeatureCollection",
        "features": features,
    }


def _compute_area_ha(
    mask: np.ndarray,
    transform: Any,
) -> float:
    """Compute area of True pixels in hectares."""
    n_pixels = int(mask.sum())
    if n_pixels == 0:
        return 0.0
    # Pixel area from transform (assumes projected CRS or approximate)
    pixel_w = abs(transform.a)  # degrees or meters
    pixel_h = abs(transform.e)

    # If in degrees, approximate meters at equator latitude
    # For Rwanda (~-2 lat), 1 degree ≈ 111km
    if pixel_w < 1.0:  # likely degrees
        pixel_w_m = pixel_w * 111_000 * math.cos(math.radians(2.0))
        pixel_h_m = pixel_h * 111_000
    else:
        pixel_w_m = pixel_w
        pixel_h_m = pixel_h

    pixel_area_m2 = pixel_w_m * pixel_h_m
    return round(n_pixels * pixel_area_m2 / 10_000, 2)


class SARWaterService:
    """SAR water body detection and flood delineation."""

    def detect_water(
        self,
        bbox: Tuple[float, float, float, float],
        date: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Detect water bodies from latest S1 VV image.

        Args:
            bbox: (lon_min, lat_min, lon_max, lat_max)
            date: Target date YYYY-MM-DD. Defaults to last 12 days.

        Returns:
            Dict with water_fraction, water_area_ha, threshold_db,
            geojson FeatureCollection of water polygons.
        """
        from src.services.sentinel1_service import get_sentinel1_service
        s1 = get_sentinel1_service()

        if date:
            target = datetime.strptime(date, "%Y-%m-%d")
        else:
            target = datetime.utcnow()

        start = (target - timedelta(days=12)).strftime("%Y-%m-%d")
        end = target.strftime("%Y-%m-%d")
        date_range = f"{start}/{end}"

        data = s1.get_backscatter(bbox, date_range, polarization="vv", limit=3)
        if not data["arrays"]:
            return {
                "status": "no_data",
                "error": f"No Sentinel-1 scenes found for {date_range}",
            }

        # Use most recent scene
        vv = data["arrays"][-1]
        scene_date = data["dates"][-1]
        transform = data["transforms"][-1]
        crs = data["crs"]

        # Multilook for speckle reduction
        vv_ml = _multilook(vv, factor=2)

        # Detect water
        mask, threshold = _water_mask(vv_ml, tile_size=8, sub_tile_size=4, cv_percentile=90.0)

        total_valid = np.isfinite(vv_ml).sum()
        water_pixels = int(mask.sum())
        water_fraction = round(water_pixels / total_valid, 4) if total_valid > 0 else 0.0

        # Note: transform needs scaling for multilooked array
        from rasterio.transform import Affine
        ml_transform = Affine(
            transform.a * 2, transform.b, transform.c,
            transform.d, transform.e * 2, transform.f,
        )

        water_area = _compute_area_ha(mask, ml_transform)
        geojson = _mask_to_geojson(mask, ml_transform, crs)

        return {
            "status": "success",
            "scene_date": scene_date,
            "water_fraction": water_fraction,
            "water_area_ha": water_area,
            "water_pixels": water_pixels,
            "total_pixels": int(total_valid),
            "threshold_db": round(threshold, 2),
            "geojson": geojson,
            "source": "Sentinel-1 RTC (Planetary Computer)",
        }

    def detect_flood(
        self,
        bbox: Tuple[float, float, float, float],
        date_before: str,
        date_after: str,
    ) -> Dict[str, Any]:
        """Delineate flood extent by comparing pre/post S1 imagery.

        Args:
            bbox: (lon_min, lat_min, lon_max, lat_max)
            date_before: Pre-flood date YYYY-MM-DD
            date_after: Post-flood date YYYY-MM-DD

        Returns:
            Dict with flood_area_ha, permanent_water_ha, new_flood_ha,
            geojson FeatureCollection of flood polygons.
        """
        from src.services.sentinel1_service import get_sentinel1_service
        s1 = get_sentinel1_service()

        pair = s1.get_pair(bbox, date_before, date_after)
        if pair["status"] != "success":
            return pair

        before_vv = pair["before"]["vv"]
        after_vv = pair["after"]["vv"]
        transform = pair["before"]["transform"]
        crs = pair["before"]["crs"]

        # Multilook both images
        before_ml = _multilook(before_vv, factor=2)
        after_ml = _multilook(after_vv, factor=2)

        # Ensure same shape (crop to smaller)
        min_h = min(before_ml.shape[0], after_ml.shape[0])
        min_w = min(before_ml.shape[1], after_ml.shape[1])
        before_ml = before_ml[:min_h, :min_w]
        after_ml = after_ml[:min_h, :min_w]

        # Before: strict threshold (90th percentile) → permanent water
        before_mask, before_thresh = _water_mask(
            before_ml, tile_size=8, sub_tile_size=4, cv_percentile=90.0
        )

        # After: permissive threshold (80th percentile) → all water including flood
        after_mask, after_thresh = _water_mask(
            after_ml, tile_size=8, sub_tile_size=4, cv_percentile=80.0
        )

        # Refinement: re-detect before with smaller tiles
        before_refined, _ = _water_mask(
            before_ml, tile_size=4, sub_tile_size=2, cv_percentile=90.0
        )

        # Flood = new water (after water that wasn't permanent water before)
        flood_mask = after_mask & ~before_refined

        # Scaled transform for multilook
        from rasterio.transform import Affine
        ml_transform = Affine(
            transform.a * 2, transform.b, transform.c,
            transform.d, transform.e * 2, transform.f,
        )

        permanent_water_ha = _compute_area_ha(before_refined, ml_transform)
        total_water_after_ha = _compute_area_ha(after_mask, ml_transform)
        flood_area_ha = _compute_area_ha(flood_mask, ml_transform)

        total_valid = np.isfinite(after_ml).sum()
        flood_fraction = round(int(flood_mask.sum()) / total_valid, 4) if total_valid > 0 else 0.0

        geojson = _mask_to_geojson(flood_mask, ml_transform, crs)

        return {
            "status": "success",
            "before_date": pair["before"]["date"],
            "after_date": pair["after"]["date"],
            "permanent_water_ha": permanent_water_ha,
            "total_water_after_ha": total_water_after_ha,
            "new_flood_ha": flood_area_ha,
            "flood_fraction": flood_fraction,
            "before_threshold_db": round(before_thresh, 2),
            "after_threshold_db": round(after_thresh, 2),
            "geojson": geojson,
            "source": "Sentinel-1 RTC (Planetary Computer)",
        }


_singleton: Optional[SARWaterService] = None


def get_sar_water_service() -> SARWaterService:
    """Return the shared SARWaterService instance."""
    global _singleton
    if _singleton is None:
        _singleton = SARWaterService()
    return _singleton
