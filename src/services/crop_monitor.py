# Copyright (C) 2025 Ingabe Ltd.
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU Affero General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.

"""Crop field monitoring via satellite remote sensing.

Multi-signal stack for crop insurance, designed for ANY crop (no identification needed).

Signals:
  Optical (Sentinel-2, when cloud-free):
    NDVI   — vegetation presence/amount (primary)
    PSRI   — plant senescence, detects dying crops directly
    BSI    — bare soil index, confirms bare vs vegetated
    NDMI   — moisture stress in leaves
    MSI    — moisture stress index (SWIR1/NIR)
    S2REP  — red edge position, early stress before NDVI drops

  Radar (Sentinel-1, cloud-proof, always available):
    VH/VV cross-ratio — vegetation structure
    VV backscatter    — soil moisture proxy

  Soil moisture (Open-Meteo ERA5-Land, 9km, daily):
    Surface 0-7cm  — planting conditions
    Root zone 7-28cm — crop water supply
    Historical trend — drought detection

Data sources: Planetary Computer STAC (S1+S2), Open-Meteo (soil moisture).
All free, no API keys required.
"""

from __future__ import annotations

import logging
import warnings
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional, Tuple

import numpy as np
import requests
import rasterio
from rasterio.warp import transform as rio_transform
import pystac_client
import planetary_computer

warnings.filterwarnings("ignore")
logger = logging.getLogger(__name__)

STAC_URL = "https://planetarycomputer.microsoft.com/api/stac/v1"
_SOIL_MOISTURE_API = "https://archive-api.open-meteo.com/v1/archive"


# ---------------------------------------------------------------------------
# STAC + Band Reading
# ---------------------------------------------------------------------------

def _open_catalog():
    return pystac_client.Client.open(STAC_URL, modifier=planetary_computer.sign_inplace)


def _read_band(item, asset_key: str, lat: float, lon: float) -> float:
    """Read a single band value at a point via windowed COG read (3x3 mean)."""
    href = item.assets[asset_key].href
    with rasterio.open(href) as src:
        xs, ys = rio_transform("EPSG:4326", src.crs, [lon], [lat])
        row, col = src.index(xs[0], ys[0])
        window = rasterio.windows.Window(col - 1, row - 1, 3, 3)
        data = src.read(1, window=window).astype(float)
        if src.nodata is not None:
            data[data == src.nodata] = np.nan
        return float(np.nanmean(data))


def _clamp_bbox(lon: float, lat: float, buf: float = 0.005) -> List[float]:
    """Build bbox clamped to valid STAC range."""
    return [
        max(-180, lon - buf), max(-90, lat - buf),
        min(180, lon + buf), min(90, lat + buf),
    ]


def get_s2_scenes(lat: float, lon: float, days_back: int = 60,
                  max_cloud: int = 30, max_items: int = 5) -> list:
    """Query STAC for recent Sentinel-2 L2A scenes at a point."""
    catalog = _open_catalog()
    end = datetime.now()
    start = end - timedelta(days=days_back)
    bbox = _clamp_bbox(lon, lat)
    search = catalog.search(
        collections=["sentinel-2-l2a"],
        bbox=bbox,
        datetime=f"{start.strftime('%Y-%m-%d')}/{end.strftime('%Y-%m-%d')}",
        query={"eo:cloud_cover": {"lt": max_cloud}},
        sortby=[{"field": "datetime", "direction": "desc"}],
        max_items=max_items,
    )
    return list(search.items())


def get_s1_scenes(lat: float, lon: float, days_back: int = 60,
                  max_items: int = 3) -> list:
    """Query STAC for recent Sentinel-1 RTC scenes at a point."""
    catalog = _open_catalog()
    end = datetime.now()
    start = end - timedelta(days=days_back)
    bbox = _clamp_bbox(lon, lat)
    search = catalog.search(
        collections=["sentinel-1-rtc"],
        bbox=bbox,
        datetime=f"{start.strftime('%Y-%m-%d')}/{end.strftime('%Y-%m-%d')}",
        max_items=max_items,
    )
    return list(search.items())


# ---------------------------------------------------------------------------
# Index Extraction
# ---------------------------------------------------------------------------

def extract_optical(item, lat: float, lon: float) -> Dict[str, Any]:
    """Extract all monitoring indices from one Sentinel-2 scene."""
    bands = ["B02", "B03", "B04", "B05", "B06", "B07", "B08", "B8A", "B11", "B12"]
    v: Dict[str, float] = {}
    for b in bands:
        try:
            v[b] = _read_band(item, b, lat, lon) / 10000.0
        except Exception:
            v[b] = np.nan

    eps = 1e-8
    blue, green, red = v["B02"], v["B03"], v["B04"]
    re1, re2, re3 = v["B05"], v["B06"], v["B07"]
    nir, nir2 = v["B08"], v["B8A"]
    swir1, swir2 = v["B11"], v["B12"]

    return {
        "ndvi": round(float((nir - red) / (nir + red + eps)), 3),
        "psri": round(float((red - green) / (re1 + eps)), 3),
        "bsi": round(float(((swir1 + red) - (nir + blue)) / ((swir1 + red) + (nir + blue) + eps)), 3),
        "ndmi": round(float((nir - swir1) / (nir + swir1 + eps)), 3),
        "msi": round(float(swir1 / (nir + eps)), 3),
        "s2rep": round(float(705 + 35 * ((red + re3) / 2 - re1) / (re2 - re1 + eps)), 1),
        "ndre": round(float((nir - re1) / (nir + re1 + eps)), 3),
        "scene_date": item.datetime.strftime("%Y-%m-%d"),
        "cloud_cover": round(item.properties.get("eo:cloud_cover", -1), 1),
        "source": "sentinel-2",
    }


def extract_sar(item, lat: float, lon: float) -> Dict[str, Any]:
    """Extract SAR vegetation/moisture indices from one Sentinel-1 scene."""
    vv_raw, vh_raw = np.nan, np.nan
    for band in ["vv", "vh"]:
        try:
            val = _read_band(item, band, lat, lon)
            if band == "vv":
                vv_raw = val
            else:
                vh_raw = val
        except Exception:
            pass

    vv_db = 10 * np.log10(vv_raw + 1e-10) if vv_raw > 0 else np.nan
    vh_db = 10 * np.log10(vh_raw + 1e-10) if vh_raw > 0 else np.nan
    cr = vh_raw / (vv_raw + 1e-10) if vv_raw > 0 else np.nan

    return {
        "sar_vv_db": round(float(vv_db), 1),
        "sar_vh_db": round(float(vh_db), 1),
        "sar_cross_ratio": round(float(cr), 3),
        "sar_scene_date": item.datetime.strftime("%Y-%m-%d"),
        "source": "sentinel-1",
    }


# ---------------------------------------------------------------------------
# Soil Moisture
# ---------------------------------------------------------------------------

def get_soil_moisture(lat: float, lon: float, days_back: int = 30) -> Optional[Dict[str, Any]]:
    """Get recent soil moisture from Open-Meteo ERA5-Land."""
    end = datetime.now()
    start = end - timedelta(days=days_back)
    url = (
        f"{_SOIL_MOISTURE_API}?latitude={lat}&longitude={lon}"
        f"&start_date={start.strftime('%Y-%m-%d')}"
        f"&end_date={end.strftime('%Y-%m-%d')}"
        f"&daily=soil_moisture_0_to_7cm_mean,soil_moisture_7_to_28cm_mean"
        f"&models=era5_land"
        f"&timezone=auto"
    )
    try:
        r = requests.get(url, timeout=15)
        if r.status_code != 200:
            return None

        data = r.json().get("daily", {})
        dates = data.get("time", [])
        sm_surface = data.get("soil_moisture_0_to_7cm_mean", [])
        sm_root = data.get("soil_moisture_7_to_28cm_mean", [])

        valid_surface = [v for v in sm_surface if v is not None]
        valid_root = [v for v in sm_root if v is not None]

        if not valid_surface:
            return None

        current_surface = valid_surface[-1]
        current_root = valid_root[-1] if valid_root else np.nan
        mean_surface = np.mean(valid_surface)
        min_surface = min(valid_surface)

        if len(valid_surface) >= 7:
            recent = np.mean(valid_surface[-7:])
            earlier = np.mean(valid_surface[:7])
            trend = recent - earlier
        else:
            trend = 0.0

        return {
            "sm_surface": round(float(current_surface), 3),
            "sm_root_zone": round(float(current_root), 3),
            "sm_30d_mean": round(float(mean_surface), 3),
            "sm_30d_min": round(float(min_surface), 3),
            "sm_trend": round(float(trend), 3),
            "sm_period": f"{dates[0]} to {dates[-1]}" if dates else "unknown",
        }
    except Exception as e:
        logger.warning("Soil moisture fetch failed: %s", e)
        return None


# ---------------------------------------------------------------------------
# Classification / Decision Logic
# ---------------------------------------------------------------------------

def classify_vegetation(optical: Dict[str, Any]) -> str:
    """Classify vegetation state from optical indices."""
    ndvi = optical["ndvi"]
    psri = optical["psri"]
    bsi = optical["bsi"]

    if ndvi < 0.20 and bsi > 0.05:
        return "BARE_SOIL"
    elif ndvi < 0.20:
        return "BARE_SOIL"
    elif ndvi < 0.35:
        if psri > 0.1:
            return "SENESCING"
        return "SPARSE"
    elif ndvi < 0.50:
        if psri > 0.05:
            return "STRESSED"
        return "ACTIVE"
    else:
        return "DENSE"


def classify_health(optical: Dict[str, Any]) -> Tuple[str, List[str]]:
    """Assess crop health from multi-index signals."""
    issues: List[str] = []

    if optical["psri"] > 0.10:
        issues.append("SENESCING — chlorophyll breakdown detected")
    elif optical["psri"] > 0.02:
        issues.append("EARLY_STRESS — slight chlorophyll decline")

    if optical["ndmi"] < -0.15:
        issues.append("SEVERE_WATER_STRESS — very low leaf moisture")
    elif optical["ndmi"] < -0.05:
        issues.append("MODERATE_WATER_STRESS — leaf moisture declining")

    if optical["msi"] > 1.2:
        issues.append("DROUGHT_SIGNAL — high SWIR/NIR ratio")

    if optical["s2rep"] < 715 and optical["ndvi"] > 0.30:
        issues.append("RED_EDGE_SHIFT — early stress before visible damage")

    if not issues:
        return "HEALTHY", []
    elif any("SEVERE" in i or "SENESCING" in i for i in issues):
        return "CRITICAL", issues
    else:
        return "WARNING", issues


def classify_sar_vegetation(sar: Dict[str, Any]) -> str:
    """Classify vegetation from SAR when optical is unavailable."""
    cr = sar["sar_cross_ratio"]
    if cr < 0.15:
        return "LIKELY_BARE"
    elif cr < 0.25:
        return "LIKELY_VEGETATED"
    elif cr < 0.40:
        return "LIKELY_CROP"
    else:
        return "LIKELY_DENSE"


def assess_soil_moisture(sm: Optional[Dict[str, Any]]) -> Tuple[str, str]:
    """Assess soil moisture conditions for insurance."""
    if sm is None:
        return "UNKNOWN", "No soil moisture data available"

    surface = sm["sm_surface"]
    trend = sm["sm_trend"]

    if surface < 0.15:
        status = "CRITICALLY_DRY"
        msg = f"Surface moisture {surface:.3f} m³/m³ — drought conditions"
    elif surface < 0.25:
        status = "DRY"
        msg = f"Surface moisture {surface:.3f} m³/m³ — below normal"
    elif surface < 0.40:
        status = "ADEQUATE"
        msg = f"Surface moisture {surface:.3f} m³/m³ — normal"
    else:
        status = "WET"
        msg = f"Surface moisture {surface:.3f} m³/m³ — well-watered"

    if trend < -0.05:
        msg += f". DRYING TREND ({trend:+.3f} over 30d)."
    elif trend > 0.05:
        msg += f". Wetting trend ({trend:+.3f} over 30d)."

    return status, msg


# ---------------------------------------------------------------------------
# Main API Functions
# ---------------------------------------------------------------------------

def verify_field(lat: float, lon: float, days_back: int = 60) -> Dict[str, Any]:
    """Full field verification for insurance. Works for ANY crop.

    Returns a report combining optical (S2), SAR (S1), and soil moisture.
    Always returns a result, falls back to SAR when clouds block optical.
    """
    report: Dict[str, Any] = {
        "lat": lat, "lon": lon,
        "query_date": datetime.now().strftime("%Y-%m-%d"),
    }

    # 1. Try optical (S2)
    s2_items = get_s2_scenes(lat, lon, days_back, max_cloud=30)
    has_optical = False

    if s2_items:
        optical = extract_optical(s2_items[0], lat, lon)
        if not np.isnan(optical["ndvi"]):
            has_optical = True
            report["optical"] = optical
            report["vegetation_state"] = classify_vegetation(optical)
            health, issues = classify_health(optical)
            report["health_status"] = health
            if issues:
                report["health_issues"] = issues
            report["ndvi"] = optical["ndvi"]
            report["has_vegetation"] = optical["ndvi"] >= 0.20
            report["n_s2_scenes"] = len(s2_items)

    # 2. Always try SAR (S1) — works through clouds
    s1_items = get_s1_scenes(lat, lon, days_back)
    if s1_items:
        sar = extract_sar(s1_items[0], lat, lon)
        report["sar"] = sar

        if not has_optical:
            report["vegetation_state"] = classify_sar_vegetation(sar)
            report["data_source"] = "SAR_ONLY (optical blocked by clouds)"
            report["has_vegetation"] = sar["sar_cross_ratio"] >= 0.15
        else:
            report["data_source"] = "OPTICAL + SAR"
    elif has_optical:
        report["data_source"] = "OPTICAL_ONLY"
    else:
        report["status"] = "NO_DATA"
        report["message"] = f"No S1 or S2 scenes in last {days_back} days"
        return report

    # 3. Soil moisture
    sm = get_soil_moisture(lat, lon, days_back=30)
    if sm:
        report["soil_moisture"] = sm
        sm_status, sm_msg = assess_soil_moisture(sm)
        report["soil_moisture_status"] = sm_status
        report["soil_moisture_summary"] = sm_msg

    # 4. Build recommendation
    report["status"] = "OK"
    report["recommendation"] = _build_recommendation(report)
    return report


def compare_field(lat: float, lon: float,
                  before_days: Tuple[int, int] = (90, 30),
                  after_days: int = 30) -> Dict[str, Any]:
    """Compare field condition across two time periods for claim verification.

    Uses optical NDVI change as primary signal, with SAR and soil moisture
    as supporting evidence.
    """
    all_items = get_s2_scenes(
        lat, lon,
        days_back=max(before_days[0], after_days + 60),
        max_cloud=40, max_items=10,
    )

    if len(all_items) < 2:
        return {"status": "INSUFFICIENT_DATA", "message": "Need at least 2 scenes"}

    now = datetime.now()
    before_items = [
        i for i in all_items
        if before_days[1] <= (now - i.datetime.replace(tzinfo=None)).days <= before_days[0]
    ]
    after_items = [
        i for i in all_items
        if (now - i.datetime.replace(tzinfo=None)).days <= after_days
    ]

    if not before_items or not after_items:
        return {"status": "NO_PAIR", "message": "Could not find scenes for both periods"}

    before = extract_optical(before_items[0], lat, lon)
    after = extract_optical(after_items[0], lat, lon)

    if np.isnan(before["ndvi"]) or np.isnan(after["ndvi"]):
        return {"status": "BAD_DATA", "message": "NaN in optical bands — scene may be cloud-contaminated"}

    ndvi_change = after["ndvi"] - before["ndvi"]
    psri_change = after["psri"] - before["psri"]

    report: Dict[str, Any] = {
        "status": "OK",
        "before_date": before["scene_date"],
        "after_date": after["scene_date"],
        "ndvi_before": before["ndvi"],
        "ndvi_after": after["ndvi"],
        "ndvi_change": round(ndvi_change, 3),
        "psri_before": before["psri"],
        "psri_after": after["psri"],
        "psri_change": round(psri_change, 3),
        "ndmi_before": before["ndmi"],
        "ndmi_after": after["ndmi"],
    }

    # Multi-signal evidence scoring (0-8 points)
    evidence_points = 0
    evidence: List[str] = []

    if ndvi_change < -0.15:
        evidence_points += 3
        evidence.append(f"NDVI severe decline ({ndvi_change:+.3f})")
    elif ndvi_change < -0.08:
        evidence_points += 2
        evidence.append(f"NDVI moderate decline ({ndvi_change:+.3f})")

    if psri_change > 0.08:
        evidence_points += 2
        evidence.append(f"PSRI senescence increase ({psri_change:+.3f})")
    elif psri_change > 0.03:
        evidence_points += 1
        evidence.append(f"PSRI slight stress increase ({psri_change:+.3f})")

    ndmi_change = after["ndmi"] - before["ndmi"]
    if ndmi_change < -0.10:
        evidence_points += 1
        evidence.append(f"NDMI moisture decline ({ndmi_change:+.3f})")

    sm = get_soil_moisture(lat, lon, days_back=60)
    if sm:
        report["soil_moisture"] = sm
        if sm["sm_surface"] < 0.15:
            evidence_points += 1
            evidence.append(f"Soil critically dry ({sm['sm_surface']:.3f} m³/m³)")
        if sm["sm_trend"] < -0.05:
            evidence_points += 1
            evidence.append(f"Soil drying trend ({sm['sm_trend']:+.3f})")

    report["evidence"] = evidence
    report["evidence_score"] = evidence_points

    if evidence_points >= 4:
        report["change_type"] = "SEVERE_DECLINE"
        report["claim_support"] = "STRONG"
        report["recommendation"] = f"Multiple signals confirm crop failure. {'; '.join(evidence)}."
    elif evidence_points >= 2:
        report["change_type"] = "MODERATE_DECLINE"
        report["claim_support"] = "MODERATE"
        report["recommendation"] = f"Some evidence of crop stress. {'; '.join(evidence)}."
    elif ndvi_change > 0.05:
        report["change_type"] = "GROWTH"
        report["claim_support"] = "NONE"
        report["recommendation"] = "Vegetation increased. No evidence of crop damage."
    else:
        report["change_type"] = "STABLE"
        report["claim_support"] = "WEAK"
        report["recommendation"] = "Vegetation stable. Claim may not be supported by satellite evidence."

    return report


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _build_recommendation(report: Dict[str, Any]) -> str:
    """Build human-readable recommendation from verify_field report."""
    parts: List[str] = []
    state = report.get("vegetation_state", "UNKNOWN")
    source = report.get("data_source", "")

    if state in ("BARE_SOIL", "LIKELY_BARE"):
        parts.append("No active vegetation detected. Field appears bare or fallow.")
    elif state == "SENESCING":
        parts.append("Vegetation is senescing (dying). Chlorophyll breakdown detected via PSRI.")
    elif state == "STRESSED":
        parts.append("Vegetation present but showing stress signals.")
    elif state in ("SPARSE", "LIKELY_VEGETATED"):
        parts.append("Sparse vegetation. Could be early growth stage or stressed crop.")
    elif state in ("ACTIVE", "LIKELY_CROP"):
        parts.append("Active crop detected.")
    elif state in ("DENSE", "LIKELY_DENSE"):
        parts.append("Dense vegetation detected.")

    health = report.get("health_status")
    if health == "CRITICAL":
        parts.append("ALERT: Critical health issues detected.")
    elif health == "WARNING":
        parts.append("Warning: Some stress indicators elevated.")

    sm_status = report.get("soil_moisture_status")
    if sm_status == "CRITICALLY_DRY":
        parts.append("DROUGHT RISK: Soil moisture critically low.")
    elif sm_status == "DRY":
        parts.append("Soil moisture below normal.")

    if "SAR_ONLY" in source:
        parts.append("Note: Optical blocked by clouds. Using SAR radar only.")

    return " ".join(parts)
