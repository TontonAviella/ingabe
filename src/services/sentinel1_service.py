# Copyright (C) 2025 Ingabe Ltd.
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU Affero General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.

"""Sentinel-1 RTC access via Planetary Computer STAC.

Provides analysis-ready gamma-naught backscatter (VV/VH) from the
Planetary Computer sentinel-1-rtc collection. Data is radiometrically
terrain-corrected in COG format, no calibration needed.

Used by:
    - sar_water.py (water detection + flood delineation)
    - sar_ndvi.py  (SAR → NDVI cloud gap filler)

Usage:
    from src.services.sentinel1_service import get_sentinel1_service
    svc = get_sentinel1_service()
    data = svc.get_backscatter(bbox=(29.0, -2.5, 30.0, -1.5), date_range="2025-01-01/2025-01-31")
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional, Tuple

import httpx
import numpy as np

logger = logging.getLogger(__name__)

_STAC_ENDPOINT = "https://planetarycomputer.microsoft.com/api/stac/v1"
_COLLECTION = "sentinel-1-rtc"


def _sign_href(href: str) -> str:
    """Sign a Planetary Computer asset URL for access."""
    try:
        import planetary_computer
        return planetary_computer.sign(href)
    except ImportError:
        logger.warning("planetary-computer package not available, using unsigned URL")
        return href
    except Exception as e:
        logger.warning("Failed to sign URL %s: %s", href, e)
        return href


def _search_items(
    bbox: Tuple[float, float, float, float],
    date_range: str,
    limit: int = 50,
) -> List[Dict[str, Any]]:
    """Search Planetary Computer for Sentinel-1 RTC items."""
    url = f"{_STAC_ENDPOINT}/search"
    payload = {
        "collections": [_COLLECTION],
        "bbox": list(bbox),
        "datetime": date_range,
        "limit": limit,
        "sortby": [{"field": "datetime", "direction": "asc"}],
    }
    try:
        r = httpx.post(
            url,
            json=payload,
            headers={"Accept": "application/geo+json", "User-Agent": "mundi.ai/1.0"},
            timeout=60.0,
        )
        r.raise_for_status()
        return r.json().get("features", [])
    except Exception as e:
        logger.warning("S1 RTC STAC search failed: %s", e)
        return []


def _read_band_window(
    href: str,
    bounds: Tuple[float, float, float, float],
    max_pixels: int = 512,
) -> Optional[Tuple[np.ndarray, Any, Any]]:
    """Read a COG band window covering the given WGS84 bounds.

    Returns (array, transform, crs) or None on failure.
    """
    import rasterio
    from rasterio.warp import transform_bounds
    from rasterio.windows import from_bounds, Window

    signed = _sign_href(href)
    try:
        with rasterio.open(signed) as src:
            proj_bounds = transform_bounds("EPSG:4326", src.crs, *bounds)
            win = from_bounds(*proj_bounds, transform=src.transform)
            # Clamp to raster extent
            win = win.intersection(Window(0, 0, src.width, src.height))
            if win.width <= 0 or win.height <= 0:
                return None

            out_h = min(int(win.height), max_pixels)
            out_w = min(int(win.width), max_pixels)
            arr = src.read(1, window=win, out_shape=(out_h, out_w)).astype(np.float32)
            win_transform = src.window_transform(win)
            return arr, win_transform, src.crs
    except Exception as e:
        logger.warning("S1 RTC COG read failed for %s: %s", href[:80], e)
        return None


class Sentinel1Service:
    """Sentinel-1 RTC gamma-naught access via Planetary Computer STAC."""

    def get_backscatter(
        self,
        bbox: Tuple[float, float, float, float],
        date_range: str,
        polarization: str = "vv",
        limit: int = 20,
    ) -> Dict[str, Any]:
        """Fetch S1 RTC gamma-naught arrays for bbox and date range.

        Args:
            bbox: (lon_min, lat_min, lon_max, lat_max)
            date_range: ISO 8601 range like "2025-01-01/2025-01-31"
            polarization: "vv" or "vh"
            limit: Max scenes to return

        Returns:
            {dates, arrays, transforms, crs, scene_count}
        """
        items = _search_items(bbox, date_range, limit=limit)
        if not items:
            return {"status": "no_data", "scene_count": 0, "dates": [], "arrays": []}

        dates: List[str] = []
        arrays: List[np.ndarray] = []
        transforms: List[Any] = []
        crs = None

        for item in items:
            assets = item.get("assets", {})
            band_key = polarization.lower()
            if band_key not in assets:
                continue

            dt = item.get("properties", {}).get("datetime", "")
            result = _read_band_window(assets[band_key]["href"], bbox)
            if result is None:
                continue

            arr, tfm, item_crs = result
            # Replace nodata (0 or very small values) with NaN
            arr[arr <= 0] = np.nan
            # Convert linear power to dB (PC stores gamma-naught as linear)
            arr = 10.0 * np.log10(arr)

            dates.append(dt)
            arrays.append(arr)
            transforms.append(tfm)
            if crs is None:
                crs = str(item_crs)

        return {
            "status": "success" if arrays else "no_valid_data",
            "scene_count": len(arrays),
            "dates": dates,
            "arrays": arrays,
            "transforms": transforms,
            "crs": crs,
        }

    def get_time_series(
        self,
        bbox: Tuple[float, float, float, float],
        date_range: str,
        limit: int = 20,
    ) -> Dict[str, Any]:
        """Fetch VV+VH time series statistics for bbox.

        Uses a single STAC search and reads both bands from each item
        to ensure VV/VH lists are always aligned.
        """
        items = _search_items(bbox, date_range, limit=limit)
        if not items:
            return {"status": "no_data", "dates": []}

        dates: List[str] = []
        vv_means: List[float] = []
        vv_stds: List[float] = []
        vh_means: List[float] = []
        vh_stds: List[float] = []

        for item in items:
            assets = item.get("assets", {})
            if "vv" not in assets or "vh" not in assets:
                continue

            vv_result = _read_band_window(assets["vv"]["href"], bbox)
            vh_result = _read_band_window(assets["vh"]["href"], bbox)
            if vv_result is None or vh_result is None:
                continue

            vv_arr, _, _ = vv_result
            vh_arr, _, _ = vh_result

            # Replace nodata and convert to dB
            vv_arr[vv_arr <= 0] = np.nan
            vv_arr = 10.0 * np.log10(vv_arr)
            vh_arr[vh_arr <= 0] = np.nan
            vh_arr = 10.0 * np.log10(vh_arr)

            dt = item.get("properties", {}).get("datetime", "")
            dates.append(dt)
            vv_means.append(float(np.nanmean(vv_arr)))
            vv_stds.append(float(np.nanstd(vv_arr)))
            vh_means.append(float(np.nanmean(vh_arr)))
            vh_stds.append(float(np.nanstd(vh_arr)))

        if not dates:
            return {"status": "no_data", "dates": []}

        return {
            "status": "success",
            "dates": dates,
            "vv_means": vv_means,
            "vv_stds": vv_stds,
            "vh_means": vh_means,
            "vh_stds": vh_stds,
            "scene_count": len(dates),
        }

    def get_pair(
        self,
        bbox: Tuple[float, float, float, float],
        date_before: str,
        date_after: str,
        search_window_days: int = 12,
    ) -> Dict[str, Any]:
        """Fetch two S1 scenes (before/after) for change detection.

        Searches +/- search_window_days around each target date
        and returns the closest match.
        """
        def _find_nearest(target_date_str: str) -> Optional[Dict[str, Any]]:
            target = datetime.strptime(target_date_str, "%Y-%m-%d")
            start = (target - timedelta(days=search_window_days)).strftime("%Y-%m-%d")
            end = (target + timedelta(days=search_window_days)).strftime("%Y-%m-%d")
            dr = f"{start}/{end}"

            # Single STAC search, read both VV+VH from each item
            # (same alignment pattern as get_time_series)
            items = _search_items(bbox, dr, limit=5)
            if not items:
                return None

            scenes: List[Dict[str, Any]] = []
            for item in items:
                assets = item.get("assets", {})
                if "vv" not in assets:
                    continue
                vv_result = _read_band_window(assets["vv"]["href"], bbox)
                if vv_result is None:
                    continue
                vv_arr, tfm, item_crs = vv_result
                vv_arr[vv_arr <= 0] = np.nan
                vv_arr = 10.0 * np.log10(vv_arr)

                vh_arr = None
                if "vh" in assets:
                    vh_result = _read_band_window(assets["vh"]["href"], bbox)
                    if vh_result is not None:
                        vh_arr = vh_result[0]
                        vh_arr[vh_arr <= 0] = np.nan
                        vh_arr = 10.0 * np.log10(vh_arr)

                dt = item.get("properties", {}).get("datetime", "")
                scenes.append({
                    "vv": vv_arr, "vh": vh_arr, "date": dt,
                    "transform": tfm, "crs": str(item_crs),
                })

            if not scenes:
                return None

            # Find closest to target date
            best_idx = 0
            best_diff = float("inf")
            for i, s in enumerate(scenes):
                try:
                    scene_dt = datetime.fromisoformat(s["date"].replace("Z", "+00:00"))
                    diff = abs((scene_dt.replace(tzinfo=None) - target).total_seconds())
                    if diff < best_diff:
                        best_diff = diff
                        best_idx = i
                except (ValueError, TypeError):
                    pass

            best = scenes[best_idx]
            return {
                "vv": best["vv"],
                "vh": best["vh"],
                "date": best["date"],
                "transform": best["transform"],
                "crs": best["crs"],
            }

        before = _find_nearest(date_before)
        after = _find_nearest(date_after)

        if before is None or after is None:
            return {
                "status": "insufficient_data",
                "error": f"Could not find S1 scenes near {'before' if before is None else 'after'} date",
                "before": before,
                "after": after,
            }

        return {
            "status": "success",
            "before": before,
            "after": after,
        }


_singleton: Optional[Sentinel1Service] = None


def get_sentinel1_service() -> Sentinel1Service:
    """Return the shared Sentinel1Service instance."""
    global _singleton
    if _singleton is None:
        _singleton = Sentinel1Service()
    return _singleton
