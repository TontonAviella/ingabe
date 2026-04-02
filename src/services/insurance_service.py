# Copyright (C) 2025 Ingabe Ltd.
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU Affero General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.

"""Insurance crop monitoring intelligence.

Combines weather + satellite data into a complete evidence package
answering the three insurance questions:

  Q1: "Is there a crop?"
      Satellite: NDVI + BSI + PSRI + SAR
      Weather:   Recent rainfall vs seasonal normal

  Q2: "Is it on track?"
      Satellite: NDVI/PSRI/NDMI trend
      Weather:   10-day forecast, drought/flood risk

  Q3: "Did the crop fail? Should I pay the claim?"
      Satellite: Multi-signal evidence score 0-8
      Weather:   Drought days, rainfall deficit, temperature stress
      Combined:  APPROVE / INVESTIGATE / REJECT with evidence
"""

from __future__ import annotations

import logging
import time
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Optional

import requests

from src.services.crop_monitor import verify_field, compare_field

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Weather APIs
# ---------------------------------------------------------------------------

_FORECAST_API = "https://api.open-meteo.com/v1/forecast"
_ARCHIVE_API = "https://archive-api.open-meteo.com/v1/archive"
_ELEVATION_API = "https://api.open-meteo.com/v1/elevation"

_MODELS = ["ecmwf_ifs025", "gfs_global", "icon_global", "gfs_graphcast025"]


# ---------------------------------------------------------------------------
# CHIRPS pixel-level rainfall normals (replaces country-average lookup table)
# ---------------------------------------------------------------------------

# Fallback country-average normals if pixel-level fetch fails.
# Source: CHIRPS 1981-2020 climatology, Rwanda country-average.
_FALLBACK_MONTHLY_NORMALS = {
    1: 80, 2: 90, 3: 115, 4: 145, 5: 85,
    6: 20, 7: 10, 8: 25, 9: 70, 10: 105,
    11: 115, 12: 95,
}


def _get_monthly_normal(lat: float, lon: float, month: int) -> float:
    """Get pixel-level monthly precipitation normal from Open-Meteo climate API.

    Uses ERA5-Land 1991-2020 climatology at the actual field location,
    accounting for Rwanda's massive rainfall gradients (900mm east to
    1800mm northwest). Falls back to country-average if API fails.
    """
    try:
        # Open-Meteo climate API provides monthly normals from ERA5-Land
        url = (
            f"https://climate-api.open-meteo.com/v1/climate"
            f"?latitude={lat}&longitude={lon}"
            f"&models=ERA5"
            f"&monthly=precipitation_sum"
            f"&start_date=1991-01-01&end_date=2020-12-31"
            f"&timezone=auto"
        )
        r = requests.get(url, timeout=10)
        if r.status_code != 200:
            return _FALLBACK_MONTHLY_NORMALS.get(month, 80)

        data = r.json()
        monthly = data.get("monthly", {})
        times = monthly.get("time", [])
        precip = monthly.get("precipitation_sum", [])

        if not times or not precip:
            return _FALLBACK_MONTHLY_NORMALS.get(month, 80)

        # Average across all years for the requested month
        month_values = []
        for t, p in zip(times, precip):
            if p is not None and t.endswith(f"-{month:02d}"):
                month_values.append(p)

        if month_values:
            return round(sum(month_values) / len(month_values), 1)

        return _FALLBACK_MONTHLY_NORMALS.get(month, 80)
    except Exception:
        return _FALLBACK_MONTHLY_NORMALS.get(month, 80)


# ---------------------------------------------------------------------------
# Weather data fetchers
# ---------------------------------------------------------------------------

def _fetch_elevation(lat: float, lon: float) -> Optional[float]:
    """SRTM 90m elevation from Open-Meteo."""
    url = f"{_ELEVATION_API}?latitude={lat}&longitude={lon}"
    try:
        r = requests.get(url, timeout=5, headers={"User-Agent": "mundi.ai/1.0"})
        data = r.json()
        elevs = data.get("elevation", [])
        return elevs[0] if elevs else None
    except Exception:
        return None


def _fetch_recent_rainfall(lat: float, lon: float, days: int = 30) -> Dict[str, Any]:
    """Recent observed rainfall from Open-Meteo ERA5 archive."""
    end = datetime.now(timezone.utc) - timedelta(days=1)
    start = end - timedelta(days=days - 1)

    url = (
        f"{_ARCHIVE_API}?latitude={lat}&longitude={lon}"
        f"&start_date={start.strftime('%Y-%m-%d')}"
        f"&end_date={end.strftime('%Y-%m-%d')}"
        f"&daily=precipitation_sum,temperature_2m_max,temperature_2m_min"
        f"&timezone=auto"
    )

    try:
        r = requests.get(url, timeout=15, headers={"User-Agent": "mundi.ai/1.0"})
        data = r.json()
    except Exception as e:
        return {"error": str(e), "total_mm": None}

    daily = data.get("daily", {})
    precip = daily.get("precipitation_sum", [])
    tmax = daily.get("temperature_2m_max", [])
    tmin = daily.get("temperature_2m_min", [])

    if not precip:
        return {"error": "no data", "total_mm": None}

    total_mm = sum(p for p in precip if p is not None)
    rain_days = sum(1 for p in precip if p is not None and p >= 2.0)
    dry_days = sum(1 for p in precip if p is not None and p < 2.0)
    heavy_days = sum(1 for p in precip if p is not None and p >= 20.0)

    # Consecutive dry days (longest streak)
    max_consec_dry = 0
    current_streak = 0
    for p in precip:
        if p is not None and p < 2.0:
            current_streak += 1
            max_consec_dry = max(max_consec_dry, current_streak)
        else:
            current_streak = 0

    # Pixel-level monthly normal instead of country average
    month = end.month
    monthly_normal = _get_monthly_normal(lat, lon, month)
    daily_normal = monthly_normal / 30.0
    expected_mm = daily_normal * days
    pct_of_normal = round(100 * total_mm / expected_mm, 0) if expected_mm > 0 else None

    valid_tmax = [t for t in tmax if t is not None]
    valid_tmin = [t for t in tmin if t is not None]

    return {
        "period_days": days,
        "total_rainfall_mm": round(total_mm, 1),
        "rain_days": rain_days,
        "dry_days": dry_days,
        "heavy_rain_days": heavy_days,
        "consecutive_dry_days_max": max_consec_dry,
        "monthly_normal_mm": monthly_normal,
        "expected_mm": round(expected_mm, 1),
        "pct_of_normal": pct_of_normal,
        "avg_tmax_c": round(sum(valid_tmax) / len(valid_tmax), 1) if valid_tmax else None,
        "avg_tmin_c": round(sum(valid_tmin) / len(valid_tmin), 1) if valid_tmin else None,
        "max_tmax_c": round(max(valid_tmax), 1) if valid_tmax else None,
    }


def _fetch_forecast(lat: float, lon: float, days: int = 10) -> Dict[str, Any]:
    """10-day multi-model forecast with terrain correction."""
    capped = min(days, 16)
    params = (
        f"latitude={lat}&longitude={lon}"
        f"&daily=temperature_2m_max,temperature_2m_min,precipitation_sum"
        f"&models={','.join(_MODELS)}"
        f"&forecast_days={capped}"
        f"&timezone=auto"
    )
    url = f"{_FORECAST_API}?{params}"

    try:
        r = requests.get(url, timeout=15, headers={"User-Agent": "mundi.ai/1.0"})
        data = r.json()
    except Exception as e:
        return {"error": str(e)}

    daily_data = data.get("daily", {})
    times = daily_data.get("time", [])
    if not times:
        return {"error": "no forecast data"}

    model_elev = data.get("elevation")
    field_elev = _fetch_elevation(lat, lon)

    total_precip = 0.0
    dry_days = 0
    heavy_days = 0
    daily_precip = []

    for i, date_str in enumerate(times):
        model_vals = {}
        for model in _MODELS:
            col = f"precipitation_sum_{model}"
            arr = daily_data.get(col, [])
            if i < len(arr) and arr[i] is not None:
                model_vals[model] = arr[i]

        if model_vals:
            mean_p = sum(model_vals.values()) / len(model_vals)

            # Terrain correction for precipitation
            if model_elev is not None and field_elev is not None:
                elev_diff = field_elev - model_elev
                if elev_diff > 50:
                    factor = min(1.4, 1.0 + 0.08 * (elev_diff / 500.0))
                    mean_p *= factor
                elif elev_diff < -50:
                    factor = max(0.7, 1.0 + 0.05 * (elev_diff / 500.0))
                    mean_p *= factor

            total_precip += mean_p
            daily_precip.append(round(mean_p, 1))
            if mean_p < 2.0:
                dry_days += 1
            if mean_p >= 20.0:
                heavy_days += 1
        else:
            daily_precip.append(None)

    max_consec_dry = 0
    streak = 0
    for p in daily_precip:
        if p is not None and p < 2.0:
            streak += 1
            max_consec_dry = max(max_consec_dry, streak)
        else:
            streak = 0

    # Drought/flood risk
    if max_consec_dry >= 5:
        drought_risk = "HIGH"
    elif max_consec_dry >= 3 or dry_days > len(times) * 0.5:
        drought_risk = "MODERATE"
    else:
        drought_risk = "LOW"

    if heavy_days >= 3:
        flood_risk = "HIGH"
    elif heavy_days >= 1:
        flood_risk = "MODERATE"
    else:
        flood_risk = "LOW"

    return {
        "forecast_days": len(times),
        "total_rainfall_mm": round(total_precip, 1),
        "dry_days": dry_days,
        "heavy_rain_days": heavy_days,
        "consecutive_dry_days_max": max_consec_dry,
        "drought_risk": drought_risk,
        "flood_risk": flood_risk,
    }


# ---------------------------------------------------------------------------
# Weather evidence scoring (for claim support)
# ---------------------------------------------------------------------------

def score_weather_evidence(recent: Dict[str, Any], forecast: Dict[str, Any]) -> Dict[str, Any]:
    """Score weather evidence for/against crop failure claim.

    Satellite evidence score (0-8):
      Rainfall deficit > 30%:         +2
      Consecutive dry days > 7:        +2
      Consecutive dry days 4-7:        +1
      Heavy rain days > 3 (flood):     +2
      Temperature stress (>32C):       +1
      Ongoing drought forecast:        +1
    """
    score = 0
    evidence = []

    pct = recent.get("pct_of_normal")
    if pct is not None:
        if pct < 50:
            score += 2
            evidence.append(f"Severe rainfall deficit: {pct:.0f}% of normal")
        elif pct < 70:
            score += 1
            evidence.append(f"Below-normal rainfall: {pct:.0f}% of normal")

    consec = recent.get("consecutive_dry_days_max", 0)
    if consec >= 7:
        score += 2
        evidence.append(f"Extended dry spell: {consec} consecutive dry days")
    elif consec >= 4:
        score += 1
        evidence.append(f"Dry spell: {consec} consecutive dry days")

    heavy = recent.get("heavy_rain_days", 0)
    if heavy >= 3:
        score += 2
        evidence.append(f"Flood risk: {heavy} days with >20mm rainfall")
    elif heavy >= 2:
        score += 1
        evidence.append(f"Heavy rainfall: {heavy} days with >20mm")

    tmax = recent.get("max_tmax_c")
    if tmax is not None and tmax > 32:
        score += 1
        evidence.append(f"Heat stress: max temperature reached {tmax:.0f}C")

    fc_drought = forecast.get("drought_risk", "LOW")
    if fc_drought in ("HIGH", "MODERATE"):
        score += 1
        evidence.append(f"Forecast: drought risk {fc_drought} for next {forecast.get('forecast_days', 10)} days")

    if score >= 5:
        support = "STRONG"
    elif score >= 3:
        support = "MODERATE"
    elif score >= 1:
        support = "WEAK"
    else:
        support = "NONE"

    return {
        "score": score,
        "max_score": 8,
        "support": support,
        "evidence": evidence,
    }


# ---------------------------------------------------------------------------
# Combined insurance report
# ---------------------------------------------------------------------------

def get_insurance_report(
    lat: float,
    lon: float,
    include_forecast: bool = True,
) -> Dict[str, Any]:
    """Full insurance report: weather + satellite combined.

    Returns a complete evidence package:
    - Q1: Is there a crop? (satellite + weather context)
    - Q2: Is it on track? (satellite trend + weather forecast)
    - Q3: Did it fail? (satellite evidence + weather evidence = combined verdict)
    """
    t0 = time.time()
    report_date = datetime.now().strftime("%Y-%m-%d")

    # Fetch all data
    sat_verify = verify_field(lat, lon)
    sat_compare = compare_field(lat, lon, before_days=(90, 30), after_days=30)
    weather_recent = _fetch_recent_rainfall(lat, lon, days=30)
    weather_forecast = _fetch_forecast(lat, lon, days=10) if include_forecast else {}

    elapsed = time.time() - t0

    # --- Q1: Is there a crop? ---
    q1_satellite: Dict[str, Any] = {}
    if sat_verify.get("status") == "OK":
        q1_satellite = {
            "answer": "YES" if sat_verify.get("has_vegetation") else "NO",
            "health": sat_verify.get("health_status", "UNKNOWN"),
            "vegetation_state": sat_verify.get("vegetation_state", "UNKNOWN"),
            "ndvi": sat_verify.get("optical", {}).get("ndvi"),
            "data_source": sat_verify.get("data_source"),
        }
        if sat_verify.get("health_issues"):
            q1_satellite["health_issues"] = sat_verify["health_issues"]
    else:
        q1_satellite = {"answer": "NO_DATA", "detail": sat_verify.get("message", "No satellite data")}

    q1_weather: Dict[str, Any] = {}
    if weather_recent.get("total_rainfall_mm") is not None:
        pct = weather_recent.get("pct_of_normal")
        if pct is not None and pct < 60:
            q1_weather["rainfall_assessment"] = "DEFICIT"
            q1_weather["detail"] = f"Only {pct:.0f}% of normal rainfall — planting at risk"
        elif pct is not None and pct < 80:
            q1_weather["rainfall_assessment"] = "BELOW_NORMAL"
            q1_weather["detail"] = f"{pct:.0f}% of normal rainfall"
        elif pct is not None and pct > 130:
            q1_weather["rainfall_assessment"] = "EXCESS"
            q1_weather["detail"] = f"{pct:.0f}% of normal — possible waterlogging"
        else:
            q1_weather["rainfall_assessment"] = "ADEQUATE"
            q1_weather["detail"] = f"{pct:.0f}% of normal rainfall — on track"
        q1_weather["recent_30d_mm"] = weather_recent["total_rainfall_mm"]
        q1_weather["normal_mm"] = weather_recent["expected_mm"]
        q1_weather["pct_of_normal"] = pct

    # Q1 combined assessment
    sat_ans = q1_satellite.get("answer", "NO_DATA")
    rain_assess = q1_weather.get("rainfall_assessment", "UNKNOWN")

    if sat_ans == "YES" and rain_assess == "ADEQUATE":
        q1_combined = "Crop is growing and rainfall is on track. No concerns."
    elif sat_ans == "YES" and rain_assess in ("BELOW_NORMAL", "DEFICIT"):
        pct_val = weather_recent.get("pct_of_normal", 0)
        q1_combined = f"Crop present but rainfall is {pct_val:.0f}% of normal. Monitor closely."
    elif sat_ans == "NO" and rain_assess == "DEFICIT":
        q1_combined = "No crop detected and severe rainfall deficit. Field may not have been planted."
    elif sat_ans == "NO":
        q1_combined = "No crop detected by satellite. Could be fallow, harvested, or failed planting."
    elif sat_ans == "NO_DATA":
        q1_combined = "Satellite data unavailable (clouds). Rainfall data suggests " + (
            "adequate conditions." if rain_assess == "ADEQUATE" else "potential stress."
        )
    else:
        health = q1_satellite.get("health", "UNKNOWN")
        if health in ("CRITICAL", "WARNING"):
            pct_val = weather_recent.get("pct_of_normal", 0)
            q1_combined = f"Crop exists but showing {health} stress. Rainfall at {pct_val:.0f}% of normal."
        else:
            q1_combined = "Crop is present."

    # --- Q2: Is it on track? ---
    q2_satellite: Dict[str, Any] = {}
    if sat_compare.get("status") == "OK":
        ndvi_chg = sat_compare.get("ndvi_change", 0)
        psri_chg = sat_compare.get("psri_change", 0)
        ndmi_chg = sat_compare.get("ndmi_after", 0) - sat_compare.get("ndmi_before", 0)

        signals_up = sum([ndvi_chg > 0.05, psri_chg < -0.02, ndmi_chg > 0.05])
        signals_down = sum([ndvi_chg < -0.05, psri_chg > 0.02, ndmi_chg < -0.05])

        if signals_up > signals_down:
            trend = "GROWING"
        elif signals_down > signals_up:
            trend = "DECLINING"
        else:
            trend = "STABLE"

        q2_satellite = {
            "trend": trend,
            "ndvi_change": round(ndvi_chg, 3),
            "psri_change": round(psri_chg, 3),
            "ndmi_change": round(ndmi_chg, 3),
            "period": f"{sat_compare.get('before_date', '?')} to {sat_compare.get('after_date', '?')}",
        }
    else:
        q2_satellite = {"trend": "UNKNOWN", "detail": sat_compare.get("message", "")}

    q2_weather: Dict[str, Any] = {}
    if weather_forecast and not weather_forecast.get("error"):
        q2_weather = {
            "forecast_days": weather_forecast.get("forecast_days"),
            "total_rainfall_mm": weather_forecast.get("total_rainfall_mm"),
            "drought_risk": weather_forecast.get("drought_risk"),
            "flood_risk": weather_forecast.get("flood_risk"),
            "consecutive_dry_days_max": weather_forecast.get("consecutive_dry_days_max"),
        }

    sat_trend = q2_satellite.get("trend", "UNKNOWN")
    fc_drought = q2_weather.get("drought_risk", "LOW")

    if sat_trend == "GROWING" and fc_drought == "LOW":
        q2_combined = "Crop is growing well. No weather threats in the 10-day forecast."
        q2_risk = "LOW"
    elif sat_trend == "GROWING" and fc_drought in ("MODERATE", "HIGH"):
        q2_combined = f"Crop is currently growing but {fc_drought} drought risk ahead. Watch closely."
        q2_risk = "MODERATE"
    elif sat_trend == "DECLINING" and fc_drought in ("MODERATE", "HIGH"):
        q2_combined = f"Crop is declining AND drought risk is {fc_drought}. Recommend field visit."
        q2_risk = "HIGH"
    elif sat_trend == "DECLINING":
        q2_combined = "Crop is declining. Weather shows adequate rainfall, so cause may be disease or pests."
        q2_risk = "MODERATE"
    elif sat_trend == "STABLE":
        q2_combined = "Crop is stable. " + (
            "Weather looks fine." if fc_drought == "LOW" else f"But {fc_drought} drought risk ahead."
        )
        q2_risk = "LOW" if fc_drought == "LOW" else "MODERATE"
    else:
        q2_combined = "Satellite data insufficient for trend. Rely on weather outlook."
        q2_risk = "MODERATE" if fc_drought != "LOW" else "LOW"

    # --- Q3: Did the crop fail? ---
    q3_satellite: Dict[str, Any] = {}
    if sat_compare.get("status") == "OK":
        q3_satellite = {
            "evidence_score": sat_compare.get("evidence_score", 0),
            "max_score": 8,
            "claim_support": sat_compare.get("claim_support", "UNKNOWN"),
            "evidence": sat_compare.get("evidence", []),
        }
    else:
        q3_satellite = {"claim_support": "UNKNOWN", "detail": "Insufficient satellite data"}

    q3_weather = score_weather_evidence(weather_recent, weather_forecast or {})

    # Combined verdict (4x4 decision matrix)
    sat_support = q3_satellite.get("claim_support", "UNKNOWN")
    wx_support = q3_weather.get("support", "NONE")

    if sat_support == "STRONG" and wx_support in ("STRONG", "MODERATE"):
        verdict = "APPROVE"
        confidence = "HIGH"
        detail = "Both satellite and weather confirm crop failure. Strong evidence for claim."
    elif sat_support == "STRONG" and wx_support in ("WEAK", "NONE"):
        verdict = "APPROVE"
        confidence = "MODERATE"
        detail = "Satellite confirms crop failure. Weather does not show severe event, possible localized issue."
    elif sat_support == "MODERATE" and wx_support in ("STRONG", "MODERATE"):
        verdict = "APPROVE"
        confidence = "MODERATE"
        detail = "Weather confirms stress event. Satellite shows moderate damage. Evidence supports claim."
    elif sat_support == "MODERATE" and wx_support in ("WEAK", "NONE"):
        verdict = "INVESTIGATE"
        confidence = "LOW"
        detail = "Some satellite damage signals but weather was normal. Recommend field verification."
    elif sat_support in ("WEAK", "NONE") and wx_support in ("STRONG", "MODERATE"):
        verdict = "INVESTIGATE"
        confidence = "LOW"
        detail = "Weather shows stress event but satellite does not confirm crop damage yet. May be early stage."
    elif sat_support == "NONE" and sat_compare.get("ndvi_change", 0) > 0.05:
        verdict = "REJECT"
        confidence = "HIGH"
        detail = "Satellite shows vegetation is growing. No evidence of crop failure."
    else:
        verdict = "INSUFFICIENT"
        confidence = "LOW"
        detail = "Not enough evidence from either satellite or weather to make a determination."

    # Overall risk level
    if verdict == "APPROVE":
        overall_risk = "CONFIRMED_LOSS"
        action = "Process claim for payment."
    elif verdict == "INVESTIGATE":
        overall_risk = "HIGH"
        action = "Send field agent for ground verification within 5 days."
    elif q2_risk == "HIGH":
        overall_risk = "HIGH"
        action = "Monitor closely. Consider early intervention."
    elif q2_risk == "MODERATE":
        overall_risk = "MODERATE"
        action = "Schedule follow-up check in 7-10 days."
    else:
        overall_risk = "LOW"
        action = "No action needed. Field is on track."

    return {
        "field": {"lat": lat, "lon": lon},
        "report_date": report_date,
        "processing_time_s": round(elapsed, 1),

        "q1_crop_present": {
            "satellite": q1_satellite,
            "weather": q1_weather,
            "combined": q1_combined,
        },

        "q2_crop_trend": {
            "satellite": q2_satellite,
            "weather": q2_weather,
            "combined": q2_combined,
            "risk_level": q2_risk,
        },

        "q3_claim_verdict": {
            "satellite_evidence": q3_satellite,
            "weather_evidence": q3_weather,
            "combined_verdict": verdict,
            "confidence": confidence,
            "detail": detail,
        },

        "risk_summary": {
            "overall_risk": overall_risk,
            "action": action,
        },

        "data_sources": {
            "satellite": "Sentinel-2 (optical) + Sentinel-1 (SAR) via Planetary Computer",
            "weather_observed": "ERA5-Land reanalysis via Open-Meteo",
            "weather_forecast": "Multi-model ensemble: ECMWF IFS + GFS + ICON + GraphCast",
            "soil_moisture": "ERA5-Land via Open-Meteo",
        },
    }
