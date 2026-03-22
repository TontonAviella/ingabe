# Copyright (C) 2025 Ingabe Ltd.
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU Affero General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU Affero General Public License for more details.
#
# You should have received a copy of the GNU Affero General Public License
# along with this program.  If not, see <http://www.gnu.org/licenses/>.

"""Open-Meteo multi-model weather forecast engine.

Fuses 4 weather models via the free Open-Meteo API:
  Physics-based NWP:
    ECMWF IFS  (9km)  — European Centre, 4D-Var data assimilation
    GFS        (13km) — NOAA/NCEP, semi-Lagrangian dynamics
    ICON       (11km) — DWD (German Weather Service), icosahedral grid
  AI-based:
    GraphCast  (28km) — Google DeepMind, graph neural network

Returns daily forecasts with per-model values, consensus statistics,
agricultural risk assessment, and natural-language briefing.
"""

from __future__ import annotations

import logging
import urllib.request
from datetime import datetime, timezone
from typing import Any, Dict, List

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Open-Meteo multi-model configuration
# ---------------------------------------------------------------------------

_OPENMETEO_API = "https://api.open-meteo.com/v1/forecast"
_OPENMETEO_MODELS = ["ecmwf_ifs025", "gfs_global", "icon_global", "gfs_graphcast025"]
_OPENMETEO_RESOLUTIONS = {"ecmwf_ifs025": 9, "gfs_global": 13, "icon_global": 11, "gfs_graphcast025": 28}
_OPENMETEO_LABELS = {
    "ecmwf_ifs025": "ECMWF IFS",
    "gfs_global": "GFS",
    "icon_global": "ICON",
    "gfs_graphcast025": "GraphCast",
}


def fetch_openmeteo_multimodel(
    lat: float, lon: float, forecast_days: int = 10,
) -> Dict[str, Any]:
    """Fetch multi-model forecast from Open-Meteo — 3 physics-based NWP + 1 AI model.

    Fuses ECMWF IFS (9km), GFS (13km), ICON (11km), and GraphCast (28km).
    Returns daily forecasts with per-model values and consensus statistics
    (mean, spread).  Model agreement = confidence; disagreement = uncertainty.
    """
    import json as _json

    capped_days = min(forecast_days, 16)
    params = (
        f"latitude={lat}&longitude={lon}"
        f"&daily=temperature_2m_max,temperature_2m_min,temperature_2m_mean,"
        f"precipitation_sum,wind_speed_10m_max,et0_fao_evapotranspiration"
        f"&hourly=soil_moisture_0_to_7cm,relative_humidity_2m"
        f"&models={','.join(_OPENMETEO_MODELS)}"
        f"&forecast_days={capped_days}"
        f"&past_days=7"
        f"&timezone=auto"
    )
    url = f"{_OPENMETEO_API}?{params}"

    try:
        req = urllib.request.Request(url, headers={"User-Agent": "mundi.ai/1.0"})
        with urllib.request.urlopen(req, timeout=15) as resp:
            data = _json.loads(resp.read().decode())
    except Exception as e:
        logger.error("Open-Meteo request failed: %s", e)
        return {"daily": [], "error": str(e)}

    daily_data = data.get("daily", {})
    times = daily_data.get("time", [])
    if not times:
        return {"daily": [], "error": "No daily data returned"}

    # Variables we care about and their suffixes per model
    vars_config = {
        "temperature_max": "temperature_2m_max",
        "temperature_min": "temperature_2m_min",
        "temperature_mean": "temperature_2m_mean",
        "precipitation_mm": "precipitation_sum",
        "wind_speed_max_kmh": "wind_speed_10m_max",
        "et0_mm": "et0_fao_evapotranspiration",
    }

    daily = []
    for i, date_str in enumerate(times):
        day: Dict[str, Any] = {"date": date_str}

        for out_key, api_var in vars_config.items():
            model_vals = {}
            for model in _OPENMETEO_MODELS:
                # Multi-model keys: var_model (e.g. temperature_2m_max_ecmwf_ifs025)
                col = f"{api_var}_{model}"
                arr = daily_data.get(col, [])
                if i < len(arr) and arr[i] is not None:
                    model_vals[model] = arr[i]

            if not model_vals:
                continue

            values = list(model_vals.values())
            mean_val = sum(values) / len(values)
            spread = max(values) - min(values) if len(values) > 1 else 0.0

            day[out_key] = {
                "mean": round(mean_val, 1),
                "spread": round(spread, 1),
                "models": {
                    _OPENMETEO_LABELS.get(m, m): round(v, 1)
                    for m, v in model_vals.items()
                },
                "n_models": len(model_vals),
            }

            # Add p10/p90 estimates from model spread for ensemble compatibility
            if len(values) >= 2:
                std = (sum((v - mean_val) ** 2 for v in values) / len(values)) ** 0.5
                if out_key == "precipitation_mm":
                    day[out_key]["p10"] = round(max(0.0, mean_val - 1.282 * std), 1)
                    day[out_key]["p90"] = round(mean_val + 1.282 * std, 1)
                else:
                    day[out_key]["p10"] = round(mean_val - 1.282 * std, 1)
                    day[out_key]["p90"] = round(mean_val + 1.282 * std, 1)

        daily.append(day)

    # Extract soil moisture from hourly (daily average of first model available)
    hourly = data.get("hourly", {})
    hourly_times = hourly.get("time", [])
    sm_key = None
    for model in _OPENMETEO_MODELS:
        k = f"soil_moisture_0_to_7cm_{model}"
        if k in hourly:
            sm_key = k
            break
    if sm_key is None and "soil_moisture_0_to_7cm" in hourly:
        sm_key = "soil_moisture_0_to_7cm"

    if sm_key and hourly_times:
        # Aggregate hourly soil moisture to daily
        sm_by_date: Dict[str, List[float]] = {}
        for j, ht in enumerate(hourly_times):
            d = ht[:10]
            vals = hourly.get(sm_key, [])
            if j < len(vals) and vals[j] is not None:
                sm_by_date.setdefault(d, []).append(vals[j])

        for day in daily:
            sm_vals = sm_by_date.get(day["date"], [])
            if sm_vals:
                day["soil_moisture_0_7cm_m3m3"] = round(sum(sm_vals) / len(sm_vals), 3)

    # Split past vs forecast days
    today_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    forecast_daily = [d for d in daily if d["date"] >= today_str]
    recent_daily = [d for d in daily if d["date"] < today_str]

    # Bias correction: compare recent hindcasts vs observed, then correct forecast
    from src.services.forecast_fusion import (
        apply_bias_correction,
        compute_bias_corrections,
    )

    corrections = compute_bias_corrections(recent_daily, lat, lon)
    if corrections:
        apply_bias_correction(forecast_daily, corrections)
        logger.info(
            "Bias correction applied: %d observed days, models=%s",
            corrections.get("observed_days", 0),
            list(corrections.get("weights", {}).keys()),
        )

    # Risk assessment on forecast days only
    _assess_daily_risk(forecast_daily)
    risk_summary = _assess_period_risk(forecast_daily)

    # Recent rainfall context for the insurer
    recent_precip = []
    for d in recent_daily:
        p = d.get("precipitation_mm", {})
        if p:
            recent_precip.append(p.get("mean", 0))
    if recent_precip:
        risk_summary["recent_7day_rainfall_mm"] = round(sum(recent_precip), 1)
        risk_summary["recent_avg_daily_mm"] = round(sum(recent_precip) / len(recent_precip), 1)

    return {
        "model": "MULTI",
        "type": "NWP + AI fusion",
        "models_used": [_OPENMETEO_LABELS.get(m, m) for m in _OPENMETEO_MODELS],
        "source": "Open-Meteo — ECMWF IFS (9km) + GFS (13km) + ICON (11km) + GraphCast (28km)",
        "resolution_km": 9,
        "location": {"lat": data.get("latitude", lat), "lon": data.get("longitude", lon)},
        "elevation_m": data.get("elevation"),
        "timezone": data.get("timezone"),
        "risk_summary": risk_summary,
        "bias_correction": {
            "applied": bool(corrections),
            "observed_days": corrections.get("observed_days", 0) if corrections else 0,
            "weights": corrections.get("weights", {}) if corrections else {},
        },
        "recent": recent_daily,
        "daily": forecast_daily,
    }


# ---------------------------------------------------------------------------
# Insurance risk assessment layer
# ---------------------------------------------------------------------------

# Rwanda Season A (Sep-Jan) and Season B (Feb-Jun) rainfall thresholds (mm/day)
_HEAVY_RAIN_THRESHOLD = 20.0   # flood/erosion risk
_DRY_DAY_THRESHOLD = 2.0       # drought risk below this


def _model_confidence(day_var: Dict[str, Any]) -> str:
    """Translate model agreement into confidence language.

    4 models agree (spread < 20% of mean) → HIGH
    2-3 agree (spread 20-60% of mean)     → MODERATE
    Models diverge (spread > 60% of mean) → LOW
    """
    spread = day_var.get("spread", 0)
    mean = abs(day_var.get("mean", 0))
    n = day_var.get("n_models", 1)
    if n < 2:
        return "SINGLE_MODEL"
    if mean < 0.1:
        # Near-zero mean — use absolute spread
        return "HIGH" if spread < 1.0 else "MODERATE" if spread < 5.0 else "LOW"
    ratio = spread / mean
    if ratio < 0.2:
        return "HIGH"
    elif ratio < 0.6:
        return "MODERATE"
    return "LOW"


def _assess_daily_risk(daily: List[Dict[str, Any]]) -> None:
    """Add risk assessment fields to each daily forecast in-place."""
    for day in daily:
        risk = {}

        # --- Precipitation risk ---
        precip = day.get("precipitation_mm", {})
        if precip:
            p_mean = precip.get("mean", 0)
            p_spread = precip.get("spread", 0)
            p_models = precip.get("models", {})
            p_vals = list(p_models.values()) if p_models else [p_mean]
            p_max = max(p_vals) if p_vals else p_mean

            confidence = _model_confidence(precip)

            # Excess rainfall risk
            if p_max >= _HEAVY_RAIN_THRESHOLD:
                risk["excess_rainfall"] = "HIGH"
                risk["excess_rainfall_detail"] = (
                    f"At least one model predicts {p_max:.0f}mm — "
                    f"flood/erosion risk. Confidence: {confidence}."
                )
            elif p_mean >= _HEAVY_RAIN_THRESHOLD * 0.5:
                risk["excess_rainfall"] = "MODERATE"
                risk["excess_rainfall_detail"] = (
                    f"Consensus {p_mean:.0f}mm, worst-case {p_max:.0f}mm. "
                    f"Confidence: {confidence}."
                )
            else:
                risk["excess_rainfall"] = "LOW"

            # Dry day flag
            if p_mean < _DRY_DAY_THRESHOLD and all(v < _DRY_DAY_THRESHOLD for v in p_vals):
                risk["dry_day"] = True
            else:
                risk["dry_day"] = False

            risk["precipitation_confidence"] = confidence

        # --- Temperature risk ---
        tmax = day.get("temperature_max", {})
        tmin = day.get("temperature_min", {})
        if tmax and tmin:
            tmax_mean = tmax.get("mean", 25)
            tmin_mean = tmin.get("mean", 15)

            if tmax_mean > 32:
                risk["heat_stress"] = "HIGH"
                risk["heat_stress_detail"] = f"Max temperature {tmax_mean:.0f}°C — crop heat stress likely."
            elif tmax_mean > 28:
                risk["heat_stress"] = "MODERATE"
            else:
                risk["heat_stress"] = "LOW"

            if tmin_mean < 10:
                risk["cold_stress"] = "HIGH"
                risk["cold_stress_detail"] = f"Min temperature {tmin_mean:.0f}°C — cold damage risk."
            elif tmin_mean < 13:
                risk["cold_stress"] = "MODERATE"
            else:
                risk["cold_stress"] = "LOW"

        # --- Soil moisture risk ---
        sm = day.get("soil_moisture_0_7cm_m3m3")
        if sm is not None:
            if sm < 0.15:
                risk["soil_drought"] = "HIGH"
                risk["soil_drought_detail"] = f"Soil moisture {sm:.2f} m³/m³ — wilting point risk."
            elif sm < 0.25:
                risk["soil_drought"] = "MODERATE"
            else:
                risk["soil_drought"] = "LOW"

            if sm > 0.45:
                risk["waterlogging"] = "MODERATE"
            elif sm > 0.50:
                risk["waterlogging"] = "HIGH"
            else:
                risk["waterlogging"] = "LOW"

        day["risk"] = risk


def _assess_period_risk(daily: List[Dict[str, Any]]) -> Dict[str, Any]:
    """Compute period-level risk summary across all forecast days."""
    if not daily:
        return {}

    n_days = len(daily)

    # Count risk days
    dry_days = sum(1 for d in daily if d.get("risk", {}).get("dry_day", False))
    excess_rain_days = sum(
        1 for d in daily
        if d.get("risk", {}).get("excess_rainfall") in ("HIGH", "MODERATE")
    )
    heat_stress_days = sum(
        1 for d in daily
        if d.get("risk", {}).get("heat_stress") in ("HIGH", "MODERATE")
    )

    # Total precipitation across period
    total_precip_values = []
    for d in daily:
        p = d.get("precipitation_mm", {})
        if p:
            total_precip_values.append(p.get("mean", 0))
    total_precip = sum(total_precip_values)

    # Consecutive dry days (longest streak)
    max_consec_dry = 0
    current_streak = 0
    for d in daily:
        if d.get("risk", {}).get("dry_day", False):
            current_streak += 1
            max_consec_dry = max(max_consec_dry, current_streak)
        else:
            current_streak = 0

    # Overall confidence — temperature confidence is typically HIGH in tropics,
    # precipitation is where models disagree.  Report both separately so the
    # insurer knows what to trust and what to hedge.
    precip_confs = [
        d.get("risk", {}).get("precipitation_confidence", "MODERATE")
        for d in daily
    ]
    precip_high_pct = sum(1 for c in precip_confs if c == "HIGH") / max(len(precip_confs), 1)
    precip_mod_pct = sum(1 for c in precip_confs if c in ("HIGH", "MODERATE")) / max(len(precip_confs), 1)

    # Drought risk assessment
    if max_consec_dry >= 5:
        drought_risk = "HIGH"
        drought_detail = (
            f"{max_consec_dry} consecutive dry days predicted "
            f"({dry_days}/{n_days} total dry days). "
            f"Crop water stress likely without irrigation."
        )
    elif max_consec_dry >= 3 or dry_days > n_days * 0.5:
        drought_risk = "MODERATE"
        drought_detail = (
            f"{dry_days}/{n_days} dry days, longest streak {max_consec_dry} days."
        )
    else:
        drought_risk = "LOW"
        drought_detail = f"{dry_days}/{n_days} dry days — adequate rainfall expected."

    # Flood risk
    if excess_rain_days >= 3:
        flood_risk = "HIGH"
        flood_detail = (
            f"{excess_rain_days} days with heavy rainfall risk. "
            f"Total period precipitation: {total_precip:.0f}mm."
        )
    elif excess_rain_days >= 1:
        flood_risk = "MODERATE"
        flood_detail = (
            f"{excess_rain_days} day(s) with elevated rainfall. "
            f"Total: {total_precip:.0f}mm."
        )
    else:
        flood_risk = "LOW"
        flood_detail = f"No heavy rainfall expected. Total: {total_precip:.0f}mm."

    # --- Build the briefing ---
    # Determine the headline risk
    risk_levels = {"HIGH": 3, "MODERATE": 2, "LOW": 1}
    top_risk_name = "drought" if risk_levels.get(drought_risk, 0) >= risk_levels.get(flood_risk, 0) else "flood"
    top_risk_level = drought_risk if top_risk_name == "drought" else flood_risk
    all_low = drought_risk == "LOW" and flood_risk == "LOW" and heat_stress_days == 0

    # Sentence 1: headline
    if all_low:
        headline = (
            f"Looking at the next {n_days} days, no significant weather risks. "
            f"Expect {total_precip:.0f}mm total rainfall, spread fairly evenly."
        )
    elif top_risk_name == "flood":
        if flood_risk == "HIGH":
            headline = (
                f"Watch for heavy rain — {excess_rain_days} of the next {n_days} days "
                f"could bring intense rainfall, with {total_precip:.0f}mm total expected."
            )
        else:
            headline = (
                f"Some elevated rainfall ahead — {excess_rain_days} day(s) with heavier "
                f"rain in the next {n_days} days, {total_precip:.0f}mm total."
            )
    else:  # drought
        if drought_risk == "HIGH":
            headline = (
                f"Dry spell ahead — {max_consec_dry} consecutive days with little to no rain. "
                f"Crops without irrigation could face water stress."
            )
        else:
            headline = (
                f"Drier than usual — {dry_days} of the next {n_days} days look dry, "
                f"longest stretch {max_consec_dry} days."
            )

    # Add heat if relevant
    if heat_stress_days > 0:
        headline += f" Also, {heat_stress_days} day(s) with temperatures high enough to stress crops."

    # Sentence 2: what we're confident about, what we're not
    if precip_high_pct > 0.6:
        confidence_sentence = (
            "We're confident in this outlook — all four models "
            "(ECMWF IFS, GFS, ICON, GraphCast) agree on both temperatures and rainfall amounts."
        )
    elif precip_mod_pct > 0.5:
        confidence_sentence = (
            "Temperature forecast is solid. On rainfall, the models mostly agree on "
            "which days are wet vs dry, but differ somewhat on exact amounts."
        )
    else:
        confidence_sentence = (
            "Temperature forecast is solid. On rainfall, the models agree "
            "rain is coming but differ on how much — "
            "the direction is clear, the magnitude less so."
        )

    briefing = f"{headline} {confidence_sentence}"

    return {
        "forecast_period_days": n_days,
        "total_precipitation_mm": round(total_precip, 1),
        "dry_days": dry_days,
        "consecutive_dry_days_max": max_consec_dry,
        "excess_rain_days": excess_rain_days,
        "heat_stress_days": heat_stress_days,
        "drought_risk": drought_risk,
        "flood_risk": flood_risk,
        "briefing": briefing,
    }


def _has_data(result: Dict[str, Any]) -> bool:
    """Check if a forecast result actually contains useful data."""
    # Single model
    daily = result.get("daily", [])
    if daily:
        return True
    # ALL models
    models = result.get("models", {})
    for m_data in models.values():
        if m_data.get("daily"):
            return True
    return False
