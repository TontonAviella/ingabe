"""NASA POWER daily weather API client for DSSAT crop simulation.

Fetches daily agrometeorological data from NASA POWER (free, no API key,
0.25 degree resolution, global coverage) and returns values in
DSSAT-compatible units.

API: https://power.larc.nasa.gov/api/temporal/daily/point
Community: AG (Agroclimatology)
"""

from __future__ import annotations

import json
import logging
import urllib.request
from functools import lru_cache
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

_POWER_BASE = "https://power.larc.nasa.gov/api/temporal/daily/point"

# NASA POWER parameter names → DSSAT equivalents
# T2M_MAX  → TMAX (°C)
# T2M_MIN  → TMIN (°C)
# PRECTOTCORR → RAIN (mm/day)
# ALLSKY_SFC_SW_DWN → SRAD (MJ/m²/day)
_POWER_PARAMS = "T2M_MAX,T2M_MIN,PRECTOTCORR,ALLSKY_SFC_SW_DWN"


def _grid_key(lat: float, lon: float) -> str:
    """Snap to 0.25-degree grid cell for caching."""
    return f"{round(lat * 4) / 4:.2f},{round(lon * 4) / 4:.2f}"


@lru_cache(maxsize=500)
def _cached_fetch(grid_key: str, date_from: str, date_to: str) -> Optional[Dict[str, Any]]:
    """Cached NASA POWER fetch per 0.25-degree cell and date range."""
    lat_str, lon_str = grid_key.split(",")
    lat, lon = float(lat_str), float(lon_str)

    url = (
        f"{_POWER_BASE}"
        f"?parameters={_POWER_PARAMS}"
        f"&community=AG"
        f"&longitude={lon}"
        f"&latitude={lat}"
        f"&start={date_from.replace('-', '')}"
        f"&end={date_to.replace('-', '')}"
        f"&format=JSON"
    )

    try:
        req = urllib.request.Request(url, headers={"User-Agent": "mundi.ai/1.0"})
        with urllib.request.urlopen(req, timeout=30) as resp:
            data = json.loads(resp.read().decode())
    except Exception as e:
        logger.error("NASA POWER request failed: %s", e)
        return None

    params = data.get("properties", {}).get("parameter", {})
    if not params:
        logger.warning("NASA POWER returned no parameter data")
        return None

    return params


def fetch_power_daily(
    lat: float,
    lon: float,
    date_from: str,
    date_to: str,
) -> Dict[str, Any]:
    """Fetch daily weather from NASA POWER Agroclimatology.

    Args:
        lat: Latitude (WGS84)
        lon: Longitude (WGS84)
        date_from: Start date (YYYY-MM-DD)
        date_to: End date (YYYY-MM-DD)

    Returns:
        Dict with keys: dates, TMAX, TMIN, RAIN, SRAD
        (all as lists of daily values in DSSAT-compatible units).
        Returns empty dict on failure.
    """
    gk = _grid_key(lat, lon)
    params = _cached_fetch(gk, date_from, date_to)

    if params is None:
        return {}

    tmax_raw = params.get("T2M_MAX", {})
    tmin_raw = params.get("T2M_MIN", {})
    rain_raw = params.get("PRECTOTCORR", {})
    srad_raw = params.get("ALLSKY_SFC_SW_DWN", {})

    # All parameters share the same date keys (YYYYMMDD format)
    dates_raw = sorted(tmax_raw.keys())

    dates: List[str] = []
    tmax: List[float] = []
    tmin: List[float] = []
    rain: List[float] = []
    srad: List[float] = []

    for d in dates_raw:
        t_max_val = tmax_raw.get(d, -999)
        t_min_val = tmin_raw.get(d, -999)
        rain_val = rain_raw.get(d, -999)
        srad_val = srad_raw.get(d, -999)

        # NASA POWER uses -999 as missing value sentinel
        if any(v == -999 for v in [t_max_val, t_min_val, rain_val, srad_val]):
            continue

        # Convert date format: YYYYMMDD → YYYY-MM-DD
        date_str = f"{d[:4]}-{d[4:6]}-{d[6:8]}"
        dates.append(date_str)
        tmax.append(float(t_max_val))
        tmin.append(float(t_min_val))
        rain.append(max(0.0, float(rain_val)))  # No negative precip
        srad.append(float(srad_val))

    return {
        "dates": dates,
        "TMAX": tmax,
        "TMIN": tmin,
        "RAIN": rain,
        "SRAD": srad,
    }


def fetch_power_daily_with_fallback(
    lat: float,
    lon: float,
    date_from: str,
    date_to: str,
) -> Dict[str, Any]:
    """Fetch NASA POWER data, falling back to Open-Meteo if POWER fails.

    Returns same dict format as fetch_power_daily().
    """
    result = fetch_power_daily(lat, lon, date_from, date_to)
    if result and result.get("dates"):
        return result

    logger.info("NASA POWER unavailable, falling back to Open-Meteo for %.4f, %.4f", lat, lon)
    return _fetch_openmeteo_fallback(lat, lon, date_from, date_to)


def _fetch_openmeteo_fallback(
    lat: float,
    lon: float,
    date_from: str,
    date_to: str,
) -> Dict[str, Any]:
    """Fetch weather from Open-Meteo as fallback for NASA POWER."""
    url = (
        f"https://archive-api.open-meteo.com/v1/archive?"
        f"latitude={lat}&longitude={lon}"
        f"&start_date={date_from}&end_date={date_to}"
        f"&daily=temperature_2m_max,temperature_2m_min,"
        f"precipitation_sum,shortwave_radiation_sum"
        f"&timezone=auto"
    )

    try:
        req = urllib.request.Request(url, headers={"User-Agent": "mundi.ai/1.0"})
        with urllib.request.urlopen(req, timeout=30) as resp:
            data = json.loads(resp.read().decode())
    except Exception as e:
        logger.error("Open-Meteo fallback failed: %s", e)
        return {}

    daily = data.get("daily", {})
    raw_dates = daily.get("time", [])
    raw_tmax = daily.get("temperature_2m_max", [])
    raw_tmin = daily.get("temperature_2m_min", [])
    raw_rain = daily.get("precipitation_sum", [])
    raw_srad = daily.get("shortwave_radiation_sum", [])

    dates: List[str] = []
    tmax: List[float] = []
    tmin: List[float] = []
    rain: List[float] = []
    srad: List[float] = []

    for i, d in enumerate(raw_dates):
        t_hi = raw_tmax[i] if i < len(raw_tmax) else None
        t_lo = raw_tmin[i] if i < len(raw_tmin) else None
        r = raw_rain[i] if i < len(raw_rain) else None
        s = raw_srad[i] if i < len(raw_srad) else None

        if any(v is None for v in [t_hi, t_lo, r, s]):
            continue

        dates.append(d)
        tmax.append(float(t_hi))
        tmin.append(float(t_lo))
        rain.append(max(0.0, float(r)))
        # Open-Meteo returns Wh/m², convert to MJ/m²/day: Wh * 3600 / 1e6
        srad.append(float(s) * 3600.0 / 1e6)

    return {
        "dates": dates,
        "TMAX": tmax,
        "TMIN": tmin,
        "RAIN": rain,
        "SRAD": srad,
    }
