# Copyright (C) 2025 Ingabe Ltd.
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU Affero General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.

"""ALOS PALSAR L-band SAR service — free L-band annual mosaics from DE Africa.

Reads ALOS PALSAR-2 annual mosaics (25m, HH+HV, L-band) from the public
DE Africa S3 bucket. L-band (24cm wavelength) penetrates canopy to
stalk/branch level, unlike C-band (Sentinel-1) which saturates on dense
vegetation. This service enables L-band discrimination testing for Rwanda
smallholder crop classification.

Collections used:
    alos_palsar_mosaic — ALOS/PALSAR (2007-2010) and ALOS-2/PALSAR-2 (2015-2022)
    Bands: hh, hv (gamma-naught backscatter), mask, date, linci

Environment:
    Nothing required. Uses AWS_NO_SIGN_REQUEST=YES (same as deafrica_stac.py).

Usage:
    from src.services.alos_palsar import get_alos_palsar_service
    svc = get_alos_palsar_service()

    # Get L-band stats for a district
    stats = svc.get_l_band_stats(bbox=[29.0, -2.5, 30.0, -1.5], years=[2020, 2021, 2022])

    # Get HH/HV ratio (key crop discrimination metric)
    ratio = svc.get_hh_hv_ratio(bbox=[29.0, -2.5, 30.0, -1.5], year=2022)

    # Full Rwanda mosaic stats by district
    national = svc.get_rwanda_national_stats(years=[2020, 2021, 2022])
"""

from __future__ import annotations

import logging
import math
import os
from typing import Any, Dict, List, Optional, Tuple

import httpx
import numpy as np

logger = logging.getLogger(__name__)

# Public bucket — anonymous access
os.environ.setdefault("AWS_NO_SIGN_REQUEST", "YES")
os.environ.setdefault("AWS_DEFAULT_REGION", "af-south-1")
os.environ.setdefault("GDAL_DISABLE_READDIR_ON_OPEN", "EMPTY_DIR")
os.environ.setdefault("CPL_VSIL_CURL_ALLOWED_EXTENSIONS", ".tif,.TIF,.tiff")

_STAC_ROOT = "https://explorer.digitalearth.africa/stac"
_COLLECTION = "alos_palsar_mosaic"

# Rwanda bounding box (geoBoundaries)
RWANDA_BBOX = (28.86, -2.84, 30.90, -1.05)

# ALOS PALSAR mask values (from DE Africa docs)
# 0=nodata, 50=water, 100=layover, 150=shadowing, 255=land
_VALID_MASK = {255}  # Only use land pixels


def _search_palsar_items(
    bbox: Tuple[float, float, float, float],
    year_from: int = 2015,
    year_to: int = 2022,
    limit: int = 50,
) -> List[Dict[str, Any]]:
    """Search DE Africa alos_palsar_mosaic collection for items.

    Note: ALOS PALSAR items use start_datetime (not datetime), and the
    STAC datetime filter can timeout on this collection. We fetch all items
    for the bbox and filter by year client-side using start_datetime or
    the year embedded in the S3 path.
    """
    url = f"{_STAC_ROOT}/collections/{_COLLECTION}/items"
    params = {
        "bbox": ",".join(str(x) for x in bbox),
        "limit": str(limit),
    }
    try:
        r = httpx.get(
            url,
            params=params,
            headers={"Accept": "application/json", "User-Agent": "curl/8"},
            timeout=60.0,
            follow_redirects=True,
        )
        r.raise_for_status()
        features = r.json().get("features", [])
    except Exception as e:
        logger.warning("ALOS PALSAR STAC search failed: %s", e)
        return []

    # Filter by year client-side (start_datetime or year in asset path)
    def _item_year(f: Dict[str, Any]) -> Optional[int]:
        sd = f.get("properties", {}).get("start_datetime", "")
        if sd and len(sd) >= 4:
            try:
                return int(sd[:4])
            except ValueError:
                pass
        # Fallback: extract year from HH asset href
        hh = f.get("assets", {}).get("hh", {}).get("href", "")
        for part in hh.split("/"):
            if part.isdigit() and len(part) == 4:
                return int(part)
        return None

    filtered = [
        f for f in features
        if (yr := _item_year(f)) is not None and year_from <= yr <= year_to
    ]
    filtered.sort(key=lambda f: f.get("properties", {}).get("start_datetime", ""))
    return filtered


def _read_window(
    href: str,
    bounds: Tuple[float, float, float, float],
) -> Optional[Tuple[np.ndarray, Any]]:
    """Read a COG window covering the given bounds."""
    import rasterio
    from rasterio.warp import transform_bounds
    from rasterio.windows import from_bounds

    try:
        with rasterio.open(href) as src:
            proj = transform_bounds("EPSG:4326", src.crs, *bounds)
            win = from_bounds(*proj, transform=src.transform)
            arr = src.read(1, window=win, boundless=True, fill_value=0)
            return arr, src.window_transform(win)
    except Exception as e:
        logger.warning("ALOS PALSAR COG read failed for %s: %s", href, e)
        return None


def _safe_round(v: float) -> float:
    f = float(v)
    return 0.0 if (math.isnan(f) or math.isinf(f)) else round(f, 4)


def _band_stats(arr: np.ndarray, valid: np.ndarray) -> Dict[str, Any]:
    """Compute statistics over valid pixels."""
    masked = arr[valid]
    if masked.size == 0:
        return {
            "mean": 0.0, "std": 0.0, "min": 0.0, "max": 0.0,
            "valid_pixels": 0, "no_data_pixels": int(arr.size),
        }
    return {
        "mean": _safe_round(masked.mean()),
        "std": _safe_round(masked.std()),
        "min": _safe_round(masked.min()),
        "max": _safe_round(masked.max()),
        "valid_pixels": int(masked.size),
        "no_data_pixels": int(arr.size - masked.size),
        "percentiles": {
            "10": _safe_round(np.percentile(masked, 10)),
            "50": _safe_round(np.percentile(masked, 50)),
            "90": _safe_round(np.percentile(masked, 90)),
        },
    }


def _to_gamma_naught_db(dn: np.ndarray) -> np.ndarray:
    """Convert ALOS PALSAR DN values to gamma-naught (dB).

    ALOS PALSAR mosaics store DN (uint16). Conversion:
        gamma0 = 10 * log10(DN^2) - 83.0  (for PALSAR-2, CF=-83.0)
    See: https://www.eorc.jaxa.jp/ALOS/en/palsar_fnf/DatasetDescription_PALSAR2_Mosaic_FNF_revN.pdf
    """
    dn_float = dn.astype("float64")
    dn_float[dn_float == 0] = np.nan  # nodata
    gamma0 = 10.0 * np.log10(dn_float ** 2) - 83.0
    return gamma0.astype("float32")


class ALOSPALSARService:
    """ALOS PALSAR L-band annual mosaic access via DE Africa STAC + COG."""

    def get_l_band_stats(
        self,
        bbox: Tuple[float, float, float, float] = RWANDA_BBOX,
        years: Optional[List[int]] = None,
    ) -> Dict[str, Any]:
        """Get L-band HH/HV backscatter statistics for a bounding box.

        Returns per-year stats for HH, HV, and HH/HV ratio (in dB).
        The HH/HV ratio is the primary metric for vegetation type discrimination:
        - Forest: ratio < -5 dB (strong HV from volume scattering)
        - Crops: ratio -5 to -10 dB (moderate HV, depends on crop structure)
        - Bare soil/water: ratio > -3 dB (weak HV, dominated by surface scatter)
        """
        if years is None:
            years = [2020, 2021, 2022]

        results_by_year: List[Dict[str, Any]] = []

        for year in years:
            items = _search_palsar_items(bbox, year_from=year, year_to=year)
            if not items:
                results_by_year.append({
                    "year": year, "status": "no_data",
                    "note": f"No ALOS PALSAR tiles for {year}",
                })
                continue

            # Accumulate stats across tiles for this year
            all_hh_db: List[np.ndarray] = []
            all_hv_db: List[np.ndarray] = []
            all_valid: List[np.ndarray] = []
            tiles_used = 0

            for item in items:
                assets = item.get("assets", {})
                if "hh" not in assets or "hv" not in assets:
                    continue

                hh_res = _read_window(assets["hh"]["href"], bbox)
                hv_res = _read_window(assets["hv"]["href"], bbox)
                if hh_res is None or hv_res is None:
                    continue

                hh_dn, _ = hh_res
                hv_dn, _ = hv_res

                # Read mask if available
                if "mask" in assets:
                    mask_res = _read_window(assets["mask"]["href"], bbox)
                    if mask_res is not None:
                        mask_arr, _ = mask_res
                        # Resample mask to match HH shape if needed
                        if mask_arr.shape != hh_dn.shape:
                            yf = hh_dn.shape[0] / mask_arr.shape[0]
                            xf = hh_dn.shape[1] / mask_arr.shape[1]
                            yy = (np.arange(hh_dn.shape[0]) / yf).astype(int).clip(0, mask_arr.shape[0] - 1)
                            xx = (np.arange(hh_dn.shape[1]) / xf).astype(int).clip(0, mask_arr.shape[1] - 1)
                            mask_arr = mask_arr[yy[:, None], xx[None, :]]
                        valid = np.isin(mask_arr, list(_VALID_MASK))
                    else:
                        valid = (hh_dn > 0) & (hv_dn > 0)
                else:
                    valid = (hh_dn > 0) & (hv_dn > 0)

                hh_db = _to_gamma_naught_db(hh_dn)
                hv_db = _to_gamma_naught_db(hv_dn)

                # Exclude NaN from conversion
                valid = valid & ~np.isnan(hh_db) & ~np.isnan(hv_db)

                all_hh_db.append(hh_db)
                all_hv_db.append(hv_db)
                all_valid.append(valid)
                tiles_used += 1

            if tiles_used == 0:
                results_by_year.append({
                    "year": year, "status": "no_valid_tiles",
                    "tiles_found": len(items), "tiles_used": 0,
                })
                continue

            # Combine tiles (simple concat for stats)
            combined_hh = np.concatenate([a.ravel() for a in all_hh_db])
            combined_hv = np.concatenate([a.ravel() for a in all_hv_db])
            combined_valid = np.concatenate([v.ravel() for v in all_valid])

            # HH/HV ratio in dB (= HH_dB - HV_dB)
            ratio_db = combined_hh - combined_hv

            results_by_year.append({
                "year": year,
                "status": "success",
                "tiles_found": len(items),
                "tiles_used": tiles_used,
                "total_pixels": int(combined_hh.size),
                "valid_pixels": int(combined_valid.sum()),
                "hh_db": _band_stats(combined_hh, combined_valid),
                "hv_db": _band_stats(combined_hv, combined_valid),
                "hh_hv_ratio_db": _band_stats(ratio_db, combined_valid),
                "interpretation": {
                    "ratio_ranges": {
                        "forest": "< -5 dB (strong volume scattering)",
                        "crops": "-5 to -10 dB (moderate canopy interaction)",
                        "bare_soil_water": "> -3 dB (surface scattering dominates)",
                    },
                    "note": "HH/HV ratio discriminates vegetation structure. "
                            "Different crops have distinct ratios due to stalk geometry. "
                            "L-band penetrates canopy unlike C-band (Sentinel-1).",
                },
            })

        return {
            "source": "deafrica_stac",
            "collection": _COLLECTION,
            "sensor": "ALOS-2/PALSAR-2",
            "band": "L-band (24cm wavelength)",
            "resolution_m": 25,
            "bbox": list(bbox),
            "years": results_by_year,
        }

    def get_hh_hv_ratio(
        self,
        bbox: Tuple[float, float, float, float] = RWANDA_BBOX,
        year: int = 2022,
    ) -> Dict[str, Any]:
        """Get HH/HV backscatter ratio for vegetation discrimination.

        The HH/HV ratio (in dB) is the single most discriminating L-band
        metric for separating vegetation types. This is because:
        - HV backscatter increases with vegetation volume (canopy density, biomass)
        - HH backscatter is more sensitive to surface roughness and moisture
        - The ratio cancels out incidence angle and moisture effects

        For crop classification, temporal CHANGE in HH/HV ratio across
        seasons is more useful than absolute values.
        """
        result = self.get_l_band_stats(bbox=bbox, years=[year])
        if not result["years"]:
            return {"status": "no_data", "year": year}

        year_data = result["years"][0]
        if year_data.get("status") != "success":
            return year_data

        return {
            "source": "deafrica_stac",
            "sensor": "ALOS-2/PALSAR-2 L-band",
            "year": year,
            "resolution_m": 25,
            "bbox": list(bbox),
            "hh_hv_ratio_db": year_data["hh_hv_ratio_db"],
            "hh_db": year_data["hh_db"],
            "hv_db": year_data["hv_db"],
            "tiles_used": year_data["tiles_used"],
            "valid_pixels": year_data["valid_pixels"],
        }

    def get_temporal_variation(
        self,
        bbox: Tuple[float, float, float, float] = RWANDA_BBOX,
        years: Optional[List[int]] = None,
    ) -> Dict[str, Any]:
        """Compute year-over-year L-band backscatter variation.

        Temporal variation in HH/HV ratio reveals agricultural activity:
        - Consistent ratio across years → perennial crops (banana, coffee) or forest
        - Variable ratio → annual crops (maize, beans) with different seasonal biomass
        - High HV std → heterogeneous landscape (mixed fields, smallholder mosaics)
        """
        if years is None:
            years = [2018, 2019, 2020, 2021, 2022]

        stats = self.get_l_band_stats(bbox=bbox, years=years)
        if not stats["years"]:
            return {"status": "no_data"}

        # Extract ratio means across years
        yearly_means = []
        for yr in stats["years"]:
            if yr.get("status") == "success":
                yearly_means.append({
                    "year": yr["year"],
                    "hh_mean_db": yr["hh_db"]["mean"],
                    "hv_mean_db": yr["hv_db"]["mean"],
                    "ratio_mean_db": yr["hh_hv_ratio_db"]["mean"],
                    "ratio_std_db": yr["hh_hv_ratio_db"]["std"],
                })

        if len(yearly_means) < 2:
            return {
                "status": "insufficient_years",
                "note": "Need at least 2 years for temporal analysis",
                "available": yearly_means,
            }

        # Compute inter-annual variation
        ratio_means = [y["ratio_mean_db"] for y in yearly_means]
        return {
            "source": "deafrica_stac",
            "sensor": "ALOS-2/PALSAR-2 L-band",
            "resolution_m": 25,
            "bbox": list(bbox),
            "yearly_stats": yearly_means,
            "inter_annual_variation": {
                "ratio_mean_across_years": _safe_round(np.mean(ratio_means)),
                "ratio_std_across_years": _safe_round(np.std(ratio_means)),
                "ratio_range_db": _safe_round(max(ratio_means) - min(ratio_means)),
            },
            "interpretation": {
                "low_variation": "< 1 dB range → stable land cover (forest, perennial crops)",
                "moderate_variation": "1-3 dB range → annual crop rotation",
                "high_variation": "> 3 dB range → land use change or highly variable agriculture",
            },
        }


_singleton: Optional[ALOSPALSARService] = None


def get_alos_palsar_service() -> ALOSPALSARService:
    """Return the shared ALOSPALSARService instance."""
    global _singleton
    if _singleton is None:
        _singleton = ALOSPALSARService()
    return _singleton
