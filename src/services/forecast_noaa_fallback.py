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

"""Legacy NOAA GEFS fallback — AWS S3 ensemble forecasts.

Used as fallback when the primary Open-Meteo multi-model API is unavailable.
GEFS provides a 31-member traditional ensemble at 28km resolution via AWS S3.

This module also contains the NOMADS AIGFS/AIGEFS/HGEFS code, which is no
longer used in the primary forecast path but preserved for reference.
"""

from __future__ import annotations

import json
import logging
import os
import pathlib
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Literal, Optional, Tuple

import numpy as np

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# NOMADS base URLs
# ---------------------------------------------------------------------------
_NOMADS_AIGFS = "https://nomads.ncep.noaa.gov/pub/data/nccf/com/aigfs/prod"
_NOMADS_AIGEFS = "https://nomads.ncep.noaa.gov/pub/data/nccf/com/aigefs/prod"
_NOMADS_HGEFS = "https://nomads.ncep.noaa.gov/pub/data/nccf/com/hgefs/prod"

# ---------------------------------------------------------------------------
# AWS S3 — traditional GEFS (no rate limits, 20+ day retention)
# ---------------------------------------------------------------------------
_S3_GEFS = "https://noaa-gefs-pds.s3.amazonaws.com"

ModelName = Literal["AIGFS", "AIGEFS", "HGEFS", "GEFS"]

# ---------------------------------------------------------------------------
# Surface variables — shared across all models
# Ensemble models: (pattern, output_key, mean_converter, spread_converter)
# Deterministic: spread_converter is unused but kept for uniform tuple shape.
# ---------------------------------------------------------------------------
_SURFACE_VARIABLES = [
    ("TMP:2 m above ground", "temperature_2m",
     lambda k: round(k - 273.15, 1),       # mean: K → °C
     lambda k: round(abs(float(k)), 1)),    # spread: K diff = °C diff
    ("APCP:surface", "precipitation_mm",
     lambda v: round(max(0.0, float(v)), 1),
     lambda v: round(max(0.0, float(v)), 1)),
    ("UGRD:10 m above ground", "wind_u_10m",
     lambda v: round(float(v), 1),
     lambda v: round(abs(float(v)), 1)),
    ("VGRD:10 m above ground", "wind_v_10m",
     lambda v: round(float(v), 1),
     lambda v: round(abs(float(v)), 1)),
    ("PRMSL:mean sea level", "pressure_msl",
     lambda v: round(float(v) / 100, 1),        # Pa → hPa
     lambda v: round(abs(float(v)) / 100, 1)),
]

# Normal distribution z-scores for percentile reconstruction
_Z_P10 = -1.282
_Z_P25 = -0.674
_Z_P75 = 0.674
_Z_P90 = 1.282

_MAX_WORKERS = 6

# ---------------------------------------------------------------------------
# Low-level helpers
# ---------------------------------------------------------------------------


class _RateLimitedError(Exception):
    """Raised when NOMADS returns a rate-limit response."""


def _http_get(
    url: str,
    headers: Optional[Dict[str, str]] = None,
    timeout: int = 15,
    retries: int = 2,
) -> bytes:
    """HTTP GET with retry + exponential backoff.

    Retries on transient failures (503, 429, timeouts, connection resets).
    Raises on permanent failures (404, 400) immediately.
    Detects NOMADS rate-limit HTML responses and backs off.
    """
    hdrs = {"User-Agent": "mundi.ai/1.0"}
    if headers:
        hdrs.update(headers)
    req = urllib.request.Request(url, headers=hdrs)

    last_err: Optional[Exception] = None
    for attempt in range(retries):
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                data = resp.read()
                # NOMADS returns 200 + HTML "Over Rate Limit" page instead of 429
                if b"Over Rate Limit" in data[:500]:
                    last_err = _RateLimitedError("NOMADS rate limit")
                    time.sleep(2.0 * (2 ** attempt))  # 2s, 4s — longer backoff
                    continue
                return data
        except urllib.error.HTTPError as e:
            if e.code in (503, 429, 500, 502):
                last_err = e
                time.sleep(1.0 * (2 ** attempt))  # 1s, 2s
                continue
            raise  # 404, 400 etc — permanent, don't retry
        except (urllib.error.URLError, OSError, TimeoutError) as e:
            last_err = e
            time.sleep(1.0 * (2 ** attempt))
            continue
    raise last_err  # type: ignore[misc]


def _parse_idx(idx_content: str) -> List[Dict[str, Any]]:
    """Parse a GRIB2 .idx file into variable entries with byte offsets."""
    entries = []
    lines = idx_content.strip().split("\n")
    for i, line in enumerate(lines):
        parts = line.split(":")
        if len(parts) < 6:
            continue
        byte_start = int(parts[1])
        byte_end = int(lines[i + 1].split(":")[1]) - 1 if i + 1 < len(lines) else None
        entries.append({
            "byte_start": byte_start,
            "byte_end": byte_end,
            "pattern": f"{parts[3]}:{parts[4]}",
        })
    return entries


def _download_byte_range(url: str, start: int, end: Optional[int]) -> bytes:
    """Download a byte range from a URL."""
    range_hdr = f"bytes={start}-{end}" if end is not None else f"bytes={start}-"
    return _http_get(url, headers={"Range": range_hdr}, timeout=30)


def _download_variables_for_hour(
    grib_url: str,
    idx_url: str,
    var_patterns: List[Tuple[str, str, Any, Any]],
) -> Dict[str, bytes]:
    """Download requested variables for one forecast hour via byte-range.

    Returns dict mapping output_key -> GRIB2 bytes for that variable.
    """
    try:
        idx_content = _http_get(idx_url, timeout=10).decode()
    except Exception as e:
        logger.debug("idx fetch failed: %s — %s", idx_url, e)
        return {}

    idx_entries = _parse_idx(idx_content)

    download_tasks = []
    for var_tuple in var_patterns:
        var_pattern, out_key = var_tuple[0], var_tuple[1]
        matches = [e for e in idx_entries if e["pattern"] == var_pattern]
        if matches:
            download_tasks.append((out_key, matches[0]))

    if not download_tasks:
        return {}

    result = {}
    with ThreadPoolExecutor(max_workers=len(download_tasks)) as var_pool:
        var_futures = {
            var_pool.submit(
                _download_byte_range, grib_url, entry["byte_start"], entry["byte_end"]
            ): out_key
            for out_key, entry in download_tasks
        }
        for future in as_completed(var_futures):
            out_key = var_futures[future]
            try:
                result[out_key] = future.result()
            except Exception as e:
                logger.debug("byte-range download failed for %s: %s", out_key, e)

    return result


def _extract_point(grib_bytes: bytes, lat: float, lon: float) -> Optional[float]:
    """Extract bilinearly interpolated value from a GRIB2 message via eccodes.

    Uses the 4 nearest grid points weighted by inverse-distance for smooth
    interpolation.  Every unique (lat, lon) now returns a distinct value
    instead of snapping to the nearest 0.25° grid node.
    """
    import eccodes

    try:
        msgid = eccodes.codes_new_from_message(grib_bytes)
    except Exception:
        return None

    try:
        neighbours = eccodes.codes_grib_find_nearest(msgid, lat, lon, npoints=4)
    except Exception:
        eccodes.codes_release(msgid)
        return None

    eccodes.codes_release(msgid)

    # Filter out missing / sentinel values
    valid = [
        n for n in neighbours
        if not (np.isnan(n.value) or n.value > 1e10)
    ]
    if not valid:
        return None
    if len(valid) == 1:
        return valid[0].value

    # Inverse-distance weighting (distance in km from eccodes)
    weights = []
    for n in valid:
        d = max(n.distance, 0.001)  # avoid division by zero
        weights.append(1.0 / d)

    total_w = sum(weights)
    return sum(n.value * w for n, w in zip(valid, weights)) / total_w


def _convert_raw_values(
    raw_bytes: Dict[str, bytes],
    converters: Dict[str, Any],
    lat: float,
    lon: float,
) -> Dict[str, float]:
    """Extract point values from GRIB bytes and apply converters."""
    vals: Dict[str, float] = {}
    for out_key, grib_data in raw_bytes.items():
        raw = _extract_point(grib_data, lat, lon)
        if raw is not None:
            vals[out_key] = converters[out_key](raw)
    return vals


def _combine_wind(vals: Dict[str, float], key_u: str = "wind_u_10m", key_v: str = "wind_v_10m") -> None:
    """Combine U/V wind into speed in-place, removing U/V keys."""
    u = vals.pop(key_u, None)
    v = vals.pop(key_v, None)
    if u is not None and v is not None:
        vals["wind_speed_ms"] = round(np.sqrt(u**2 + v**2), 1)


def _rename_pressure(vals: Dict[str, float]) -> None:
    """Rename pressure_msl → pressure_hpa in-place."""
    if "pressure_msl" in vals:
        vals["pressure_hpa"] = vals.pop("pressure_msl")


def _build_distributions(
    avg_vals: Dict[str, float],
    spr_vals: Dict[str, float],
) -> Dict[str, Dict[str, float]]:
    """Build probability distributions from mean + spread."""
    result = {}
    for var_key, mean_val in avg_vals.items():
        spread = spr_vals.get(var_key, 0.0)
        p10 = round(mean_val + _Z_P10 * spread, 1)
        p25 = round(mean_val + _Z_P25 * spread, 1)
        p75 = round(mean_val + _Z_P75 * spread, 1)
        p90 = round(mean_val + _Z_P90 * spread, 1)

        if var_key == "precipitation_mm":
            p10 = max(0.0, p10)
            p25 = max(0.0, p25)

        result[var_key] = {
            "mean": round(mean_val, 1),
            "spread": round(spread, 1),
            "p10": p10,
            "p25": p25,
            "p50": round(mean_val, 1),
            "p75": p75,
            "p90": p90,
        }
    return result


# ---------------------------------------------------------------------------
# Run date detection — shared pattern
# ---------------------------------------------------------------------------


def _latest_complete_run(
    nomads_base: str,
    model_prefix: str,
    file_subpath_template: str,
    required_fhr: int = 240,
) -> Tuple[str, str]:
    """Find latest available model run on NOMADS.

    Strategy:
      1. Try to find a run where f{required_fhr} exists (fully complete).
      2. If none found, fall back to any run where at least f006 exists
         (partial run — better than nothing, code handles missing hours).

    NOMADS keeps ~2 days of data and purges old runs. During transitions
    the newest run may still be publishing while the oldest is being purged.

    Conservative with requests: checks at most 5 candidates (30h back)
    to avoid NOMADS rate limits. Probes sequentially and stops at first hit.
    """
    now = datetime.now(timezone.utc)

    # Build candidate list: newest first, 5 cycles back (~30h)
    # Offset by 4h (not 8h) so we can find today's runs sooner.
    # A cycle initiated 4h ago has at least f006-f024 published.
    candidates = []
    seen = set()
    for hours_back in range(0, 36, 6):
        candidate = now - timedelta(hours=hours_back + 4)
        date_str = candidate.strftime("%Y%m%d")
        cycle = f"{(candidate.hour // 6) * 6:02d}"
        key = f"{date_str}/{cycle}"
        if key not in seen:
            seen.add(key)
            candidates.append((date_str, cycle))

    def _probe(date_str: str, cycle: str, fhr: int) -> bool:
        fhr_str = f"f{fhr:03d}"
        test_path = file_subpath_template.format(
            prefix=model_prefix, cycle=cycle, fhr=fhr_str,
        )
        idx_url = f"{nomads_base}/{model_prefix}.{date_str}/{cycle}/{test_path}.idx"
        try:
            req = urllib.request.Request(idx_url, method="HEAD")
            req.add_header("User-Agent", "mundi.ai/1.0")
            with urllib.request.urlopen(req, timeout=8) as resp:
                # Check for rate-limit HTML served as 200
                if resp.headers.get("Content-Type", "").startswith("text/html"):
                    return False
                return True
        except Exception:
            return False

    # Pass 1: find newest complete run (sequential, stop-early)
    for date_str, cycle in candidates:
        if _probe(date_str, cycle, required_fhr):
            logger.info(
                "Found complete %s run: %s/%s (verified f%03d)",
                model_prefix.upper(), date_str, cycle, required_fhr,
            )
            return date_str, cycle

    # Pass 2: fallback to newest partial run (at least f006 exists)
    for date_str, cycle in candidates:
        if _probe(date_str, cycle, 6):
            logger.warning(
                "No complete %s run found — using partial %s/%s (f006 exists, f%03d missing)",
                model_prefix.upper(), date_str, cycle, required_fhr,
            )
            return date_str, cycle

    yesterday = (now - timedelta(days=1)).strftime("%Y%m%d")
    logger.error("No %s data found on NOMADS at all — returning fallback %s/12", model_prefix.upper(), yesterday)
    return yesterday, "12"


def _latest_aigfs_run(forecast_days: int = 16) -> Tuple[str, str]:
    return _latest_complete_run(
        _NOMADS_AIGFS, "aigfs",
        "model/atmos/grib2/{prefix}.t{cycle}z.sfc.{fhr}.grib2",
        required_fhr=min(forecast_days * 24, 384),
    )


def _latest_aigefs_run(forecast_days: int = 10) -> Tuple[str, str]:
    return _latest_complete_run(
        _NOMADS_AIGEFS, "aigefs",
        "ensstat/products/atmos/grib2/{prefix}.t{cycle}z.sfc.avg.{fhr}.grib2",
        required_fhr=min(forecast_days * 24, 240),
    )


def _latest_hgefs_run(forecast_days: int = 10) -> Tuple[str, str]:
    return _latest_complete_run(
        _NOMADS_HGEFS, "hgefs",
        "ensstat/products/atmos/grib2/{prefix}.t{cycle}z.sfc.avg.{fhr}.grib2",
        required_fhr=min(forecast_days * 24, 240),
    )


# ---------------------------------------------------------------------------
# AIGFS — deterministic forecast (single run, no ensemble)
# ---------------------------------------------------------------------------


def _fetch_aigfs_hour(
    date_str: str,
    cycle: str,
    fhr: int,
    lat: float,
    lon: float,
) -> Optional[Dict[str, Any]]:
    """Fetch AIGFS deterministic values for one forecast hour."""
    fhr_str = f"f{fhr:03d}"
    grib_url = (
        f"{_NOMADS_AIGFS}/aigfs.{date_str}/{cycle}/"
        f"model/atmos/grib2/aigfs.t{cycle}z.sfc.{fhr_str}.grib2"
    )

    raw_bytes = _download_variables_for_hour(grib_url, f"{grib_url}.idx", _SURFACE_VARIABLES)
    if not raw_bytes:
        return None

    mean_conv = {out_key: conv for _, out_key, conv, _ in _SURFACE_VARIABLES}
    vals = _convert_raw_values(raw_bytes, mean_conv, lat, lon)
    _combine_wind(vals)
    _rename_pressure(vals)

    if not vals:
        return None

    init_dt = datetime.strptime(f"{date_str}{cycle}", "%Y%m%d%H").replace(tzinfo=timezone.utc)
    valid_dt = init_dt + timedelta(hours=fhr)

    result: Dict[str, Any] = {
        "valid_time": valid_dt.strftime("%Y-%m-%dT%H:%MZ"),
        "forecast_hour": fhr,
    }
    result.update(vals)
    return result


def fetch_aigfs_forecast(
    lat: float,
    lon: float,
    forecast_days: int = 16,
) -> Dict[str, Any]:
    """Fetch AIGFS deterministic forecast — 6-hourly point values."""
    forecast_days = min(max(1, forecast_days), 16)
    max_hour = forecast_days * 24

    date_str, cycle = _latest_aigfs_run(forecast_days)
    init_time = f"{date_str[:4]}-{date_str[4:6]}-{date_str[6:8]}T{cycle}:00Z"

    logger.info("AIGFS forecast %.4f,%.4f — init %s, %dd", lat, lon, init_time, forecast_days)

    forecast_hours = [fhr for fhr in range(0, 385, 6) if fhr <= max_hour]

    forecasts = []
    with ThreadPoolExecutor(max_workers=_MAX_WORKERS) as pool:
        futures = {
            pool.submit(_fetch_aigfs_hour, date_str, cycle, fhr, lat, lon): fhr
            for fhr in forecast_hours
        }
        for future in as_completed(futures):
            try:
                result = future.result()
                if result:
                    forecasts.append(result)
            except Exception as e:
                logger.debug("AIGFS hour failed: %s", e)

    forecasts.sort(key=lambda f: f["forecast_hour"])

    return {
        "model": "AIGFS",
        "init_time": init_time,
        "location": {"lat": lat, "lon": lon},
        "resolution_km": 28,
        "forecast_count": len(forecasts),
        "forecasts": forecasts,
    }


def fetch_aigfs_daily(
    lat: float,
    lon: float,
    forecast_days: int = 16,
) -> Dict[str, Any]:
    """Fetch AIGFS forecast aggregated to daily summaries (deterministic — no distributions)."""
    raw = fetch_aigfs_forecast(lat, lon, forecast_days)

    by_date: Dict[str, List[Dict[str, Any]]] = {}
    for fc in raw.get("forecasts", []):
        date_key = fc["valid_time"][:10]
        by_date.setdefault(date_key, []).append(fc)

    daily = []
    for date_str in sorted(by_date.keys()):
        steps = by_date[date_str]
        day: Dict[str, Any] = {"date": date_str}

        # Temperature: max, min, mean
        temps = [s["temperature_2m"] for s in steps if "temperature_2m" in s]
        if temps:
            day["temperature_max"] = round(max(temps), 1)
            day["temperature_min"] = round(min(temps), 1)
            day["temperature_mean"] = round(sum(temps) / len(temps), 1)

        # Precipitation: sum
        precips = [s["precipitation_mm"] for s in steps if "precipitation_mm" in s]
        if precips:
            day["precipitation_mm"] = round(sum(precips), 1)

        # Wind: average
        winds = [s["wind_speed_ms"] for s in steps if "wind_speed_ms" in s]
        if winds:
            day["wind_speed_ms"] = round(sum(winds) / len(winds), 1)

        # Pressure: average
        pressures = [s["pressure_hpa"] for s in steps if "pressure_hpa" in s]
        if pressures:
            day["pressure_hpa"] = round(sum(pressures) / len(pressures), 1)

        daily.append(day)

    return {
        "model": "AIGFS",
        "init_time": raw.get("init_time"),
        "location": raw.get("location"),
        "resolution_km": 28,
        "daily": daily,
    }


# ---------------------------------------------------------------------------
# Ensemble forecast — shared for AIGEFS (31 members) and HGEFS (62 members)
# ---------------------------------------------------------------------------


def _fetch_ensemble_hour(
    nomads_base: str,
    model_prefix: str,
    members: int,
    date_str: str,
    cycle: str,
    fhr: int,
    lat: float,
    lon: float,
) -> Optional[Dict[str, Any]]:
    """Fetch ensemble mean + spread for one forecast hour, derive distributions."""
    fhr_str = f"f{fhr:03d}"
    base_path = (
        f"{nomads_base}/{model_prefix}.{date_str}/{cycle}/"
        f"ensstat/products/atmos/grib2"
    )
    avg_url = f"{base_path}/{model_prefix}.t{cycle}z.sfc.avg.{fhr_str}.grib2"
    spr_url = f"{base_path}/{model_prefix}.t{cycle}z.sfc.spr.{fhr_str}.grib2"

    avg_bytes = _download_variables_for_hour(avg_url, f"{avg_url}.idx", _SURFACE_VARIABLES)
    spr_bytes = _download_variables_for_hour(spr_url, f"{spr_url}.idx", _SURFACE_VARIABLES)

    if not avg_bytes:
        return None

    mean_conv = {out_key: conv for _, out_key, conv, _ in _SURFACE_VARIABLES}
    spr_conv = {out_key: conv for _, out_key, _, conv in _SURFACE_VARIABLES}

    avg_vals = _convert_raw_values(avg_bytes, mean_conv, lat, lon)
    spr_vals = _convert_raw_values(spr_bytes, spr_conv, lat, lon)

    _combine_wind(avg_vals)
    _combine_wind(spr_vals)
    _rename_pressure(avg_vals)
    _rename_pressure(spr_vals)

    distributions = _build_distributions(avg_vals, spr_vals)
    if not distributions:
        return None

    init_dt = datetime.strptime(f"{date_str}{cycle}", "%Y%m%d%H").replace(tzinfo=timezone.utc)
    valid_dt = init_dt + timedelta(hours=fhr)

    result: Dict[str, Any] = {
        "valid_time": valid_dt.strftime("%Y-%m-%dT%H:%MZ"),
        "forecast_hour": fhr,
        "members": members,
    }
    result.update(distributions)
    return result


def _fetch_ensemble_forecast(
    nomads_base: str,
    model_prefix: str,
    model_name: str,
    members: int,
    latest_run_fn: Any,
    lat: float,
    lon: float,
    forecast_days: int,
    max_fhr: int = 240,
) -> Dict[str, Any]:
    """Fetch ensemble forecast with parallel hour downloads."""
    forecast_days = min(max(1, forecast_days), max_fhr // 24)
    max_hour = forecast_days * 24

    date_str, cycle = latest_run_fn(forecast_days)
    init_time = f"{date_str[:4]}-{date_str[4:6]}-{date_str[6:8]}T{cycle}:00Z"

    logger.info("%s forecast %.4f,%.4f — init %s, %dd", model_name, lat, lon, init_time, forecast_days)

    forecast_hours = [fhr for fhr in range(0, max_fhr + 1, 6) if fhr <= max_hour]

    forecasts = []
    with ThreadPoolExecutor(max_workers=_MAX_WORKERS) as pool:
        futures = {
            pool.submit(
                _fetch_ensemble_hour, nomads_base, model_prefix, members,
                date_str, cycle, fhr, lat, lon,
            ): fhr
            for fhr in forecast_hours
        }
        for future in as_completed(futures):
            try:
                result = future.result()
                if result:
                    forecasts.append(result)
            except Exception as e:
                logger.debug("%s hour failed: %s", model_name, e)

    forecasts.sort(key=lambda f: f["forecast_hour"])

    return {
        "model": model_name,
        "init_time": init_time,
        "location": {"lat": lat, "lon": lon},
        "resolution_km": 28,
        "members": members,
        "forecast_count": len(forecasts),
        "forecasts": forecasts,
    }


def _aggregate_ensemble_daily(raw: Dict[str, Any]) -> Dict[str, Any]:
    """Aggregate 6-hourly ensemble forecast to daily summaries with distributions."""
    by_date: Dict[str, List[Dict[str, Any]]] = {}
    for fc in raw.get("forecasts", []):
        date_key = fc["valid_time"][:10]
        by_date.setdefault(date_key, []).append(fc)

    daily = []
    for date_str in sorted(by_date.keys()):
        steps = by_date[date_str]
        day: Dict[str, Any] = {"date": date_str}

        for var_key in ["temperature_2m", "precipitation_mm", "wind_speed_ms", "pressure_hpa"]:
            step_stats = [s[var_key] for s in steps if var_key in s]
            if not step_stats:
                continue

            if var_key == "precipitation_mm":
                day[var_key] = {
                    "mean": round(sum(s["mean"] for s in step_stats), 1),
                    "p10": round(max(0.0, sum(s["p10"] for s in step_stats)), 1),
                    "p25": round(max(0.0, sum(s["p25"] for s in step_stats)), 1),
                    "p50": round(sum(s["p50"] for s in step_stats), 1),
                    "p75": round(sum(s["p75"] for s in step_stats), 1),
                    "p90": round(sum(s["p90"] for s in step_stats), 1),
                    "spread": round(sum(s["spread"] for s in step_stats), 1),
                }
            elif var_key == "temperature_2m":
                day["temperature_max"] = {
                    "mean": round(max(s["mean"] for s in step_stats), 1),
                    "p10": round(max(s["p10"] for s in step_stats), 1),
                    "p90": round(max(s["p90"] for s in step_stats), 1),
                    "spread": round(max(s["spread"] for s in step_stats), 1),
                }
                day["temperature_min"] = {
                    "mean": round(min(s["mean"] for s in step_stats), 1),
                    "p10": round(min(s["p10"] for s in step_stats), 1),
                    "p90": round(min(s["p90"] for s in step_stats), 1),
                    "spread": round(min(s["spread"] for s in step_stats), 1),
                }
                n = len(step_stats)
                day["temperature_mean"] = {
                    "mean": round(sum(s["mean"] for s in step_stats) / n, 1),
                    "spread": round(sum(s["spread"] for s in step_stats) / n, 1),
                }
            else:
                n = len(step_stats)
                day[var_key] = {
                    "mean": round(sum(s["mean"] for s in step_stats) / n, 1),
                    "p10": round(sum(s["p10"] for s in step_stats) / n, 1),
                    "p90": round(sum(s["p90"] for s in step_stats) / n, 1),
                    "spread": round(sum(s["spread"] for s in step_stats) / n, 1),
                }

        daily.append(day)

    result = {
        "model": raw.get("model"),
        "init_time": raw.get("init_time"),
        "location": raw.get("location"),
        "resolution_km": 28,
        "members": raw.get("members"),
        "daily": daily,
    }
    # Pass through extra metadata (source, fallback_source, etc.)
    for key in ("source", "fallback_source"):
        if key in raw:
            result[key] = raw[key]
    return result


# ---------------------------------------------------------------------------
# AIGEFS public API
# ---------------------------------------------------------------------------


def fetch_aigefs_forecast(lat: float, lon: float, forecast_days: int = 10) -> Dict[str, Any]:
    """Fetch AIGEFS 31-member AI ensemble forecast — 6-hourly with distributions."""
    return _fetch_ensemble_forecast(
        _NOMADS_AIGEFS, "aigefs", "AIGEFS", 31,
        _latest_aigefs_run, lat, lon, forecast_days, max_fhr=240,
    )


def fetch_aigefs_daily(lat: float, lon: float, forecast_days: int = 10) -> Dict[str, Any]:
    """Fetch AIGEFS forecast aggregated to daily summaries with distributions."""
    return _aggregate_ensemble_daily(fetch_aigefs_forecast(lat, lon, forecast_days))


# ---------------------------------------------------------------------------
# HGEFS public API
# ---------------------------------------------------------------------------


def fetch_hgefs_forecast(lat: float, lon: float, forecast_days: int = 10) -> Dict[str, Any]:
    """Fetch HGEFS 62-member hybrid ensemble forecast — 6-hourly with distributions."""
    return _fetch_ensemble_forecast(
        _NOMADS_HGEFS, "hgefs", "HGEFS", 62,
        _latest_hgefs_run, lat, lon, forecast_days, max_fhr=240,
    )


def fetch_hgefs_daily(lat: float, lon: float, forecast_days: int = 10) -> Dict[str, Any]:
    """Fetch HGEFS forecast aggregated to daily summaries with distributions."""
    return _aggregate_ensemble_daily(fetch_hgefs_forecast(lat, lon, forecast_days))


# ---------------------------------------------------------------------------
# AWS S3 GEFS — 31-member traditional ensemble (always available, no rate limits)
# Uses same avg+spr+idx pattern as NOMADS but from S3.
# 3-hourly steps (f000-f384), but we fetch 6-hourly for consistency.
# ---------------------------------------------------------------------------


def _latest_gefs_s3_run(forecast_days: int = 16) -> Tuple[str, str]:
    """Find latest GEFS run on AWS S3.

    S3 has 20+ day retention and no rate limits, so we can probe
    more aggressively. Checks newest cycle first, falls back quickly.
    """
    now = datetime.now(timezone.utc)

    candidates = []
    seen = set()
    for hours_back in range(0, 48, 6):
        candidate = now - timedelta(hours=hours_back + 4)
        date_str = candidate.strftime("%Y%m%d")
        cycle = f"{(candidate.hour // 6) * 6:02d}"
        key = f"{date_str}/{cycle}"
        if key not in seen:
            seen.add(key)
            candidates.append((date_str, cycle))

    required_fhr = min(forecast_days * 24, 384)

    def _probe_s3(date_str: str, cycle: str, fhr: int) -> bool:
        fhr_str = f"f{fhr:03d}"
        idx_url = (
            f"{_S3_GEFS}/gefs.{date_str}/{cycle}/atmos/pgrb2sp25/"
            f"geavg.t{cycle}z.pgrb2s.0p25.{fhr_str}.idx"
        )
        try:
            req = urllib.request.Request(idx_url, method="HEAD")
            req.add_header("User-Agent", "mundi.ai/1.0")
            with urllib.request.urlopen(req, timeout=8) as resp:
                return resp.status == 200
        except Exception:
            return False

    # S3 is reliable — check complete run first
    for date_str, cycle in candidates:
        if _probe_s3(date_str, cycle, required_fhr):
            logger.info("Found complete GEFS S3 run: %s/%s (f%03d)", date_str, cycle, required_fhr)
            return date_str, cycle

    # Fallback to partial run
    for date_str, cycle in candidates:
        if _probe_s3(date_str, cycle, 6):
            logger.warning("No complete GEFS S3 run — using partial %s/%s", date_str, cycle)
            return date_str, cycle

    yesterday = (now - timedelta(days=1)).strftime("%Y%m%d")
    logger.error("No GEFS data on S3 — returning fallback %s/00", yesterday)
    return yesterday, "00"


def _fetch_gefs_s3_hour(
    date_str: str,
    cycle: str,
    fhr: int,
    lat: float,
    lon: float,
    members: int = 31,
) -> Optional[Dict[str, Any]]:
    """Fetch GEFS ensemble mean + spread for one forecast hour from AWS S3."""
    fhr_str = f"f{fhr:03d}"
    base = f"{_S3_GEFS}/gefs.{date_str}/{cycle}/atmos/pgrb2sp25"
    avg_url = f"{base}/geavg.t{cycle}z.pgrb2s.0p25.{fhr_str}"
    spr_url = f"{base}/gespr.t{cycle}z.pgrb2s.0p25.{fhr_str}"

    avg_bytes = _download_variables_for_hour(avg_url, f"{avg_url}.idx", _SURFACE_VARIABLES)
    spr_bytes = _download_variables_for_hour(spr_url, f"{spr_url}.idx", _SURFACE_VARIABLES)

    if not avg_bytes:
        return None

    mean_conv = {out_key: conv for _, out_key, conv, _ in _SURFACE_VARIABLES}
    spr_conv = {out_key: conv for _, out_key, _, conv in _SURFACE_VARIABLES}

    avg_vals = _convert_raw_values(avg_bytes, mean_conv, lat, lon)
    spr_vals = _convert_raw_values(spr_bytes, spr_conv, lat, lon)

    _combine_wind(avg_vals)
    _combine_wind(spr_vals)
    _rename_pressure(avg_vals)
    _rename_pressure(spr_vals)

    distributions = _build_distributions(avg_vals, spr_vals)
    if not distributions:
        return None

    init_dt = datetime.strptime(f"{date_str}{cycle}", "%Y%m%d%H").replace(tzinfo=timezone.utc)
    valid_dt = init_dt + timedelta(hours=fhr)

    result: Dict[str, Any] = {
        "valid_time": valid_dt.strftime("%Y-%m-%dT%H:%MZ"),
        "forecast_hour": fhr,
        "members": members,
    }
    result.update(distributions)
    return result


def fetch_gefs_s3_forecast(
    lat: float,
    lon: float,
    forecast_days: int = 16,
) -> Dict[str, Any]:
    """Fetch GEFS 31-member ensemble from AWS S3 — 6-hourly with distributions.

    Always available, no rate limits, 20+ day data retention.
    Uses pre-computed geavg (mean) + gespr (spread) files.
    """
    forecast_days = min(max(1, forecast_days), 16)
    max_hour = forecast_days * 24

    date_str, cycle = _latest_gefs_s3_run(forecast_days)
    init_time = f"{date_str[:4]}-{date_str[4:6]}-{date_str[6:8]}T{cycle}:00Z"

    logger.info("GEFS S3 forecast %.4f,%.4f — init %s, %dd", lat, lon, init_time, forecast_days)

    # GEFS has 3h steps but we fetch every 6h for consistency with other models
    forecast_hours = [fhr for fhr in range(0, max_hour + 1, 6)]

    forecasts = []
    with ThreadPoolExecutor(max_workers=_MAX_WORKERS) as pool:
        futures = {
            pool.submit(_fetch_gefs_s3_hour, date_str, cycle, fhr, lat, lon): fhr
            for fhr in forecast_hours
        }
        for future in as_completed(futures):
            try:
                result = future.result()
                if result:
                    forecasts.append(result)
            except Exception as e:
                logger.debug("GEFS S3 hour failed: %s", e)

    forecasts.sort(key=lambda f: f["forecast_hour"])

    return {
        "model": "GEFS",
        "init_time": init_time,
        "location": {"lat": lat, "lon": lon},
        "resolution_km": 28,
        "members": 31,
        "source": "AWS S3",
        "forecast_count": len(forecasts),
        "forecasts": forecasts,
    }


def fetch_gefs_s3_daily(
    lat: float,
    lon: float,
    forecast_days: int = 16,
) -> Dict[str, Any]:
    """Fetch GEFS S3 forecast aggregated to daily summaries with distributions."""
    return _aggregate_ensemble_daily(fetch_gefs_s3_forecast(lat, lon, forecast_days))


# ---------------------------------------------------------------------------
# All-models comparison
# ---------------------------------------------------------------------------


def fetch_all_models_daily(
    lat: float,
    lon: float,
    forecast_days: int = 10,
) -> Dict[str, Any]:
    """Fetch all three models in parallel and return combined daily output."""
    results: Dict[str, Any] = {}

    with ThreadPoolExecutor(max_workers=3) as pool:
        futures = {
            pool.submit(fetch_aigfs_daily, lat, lon, forecast_days): "AIGFS",
            pool.submit(fetch_aigefs_daily, lat, lon, forecast_days): "AIGEFS",
            pool.submit(fetch_hgefs_daily, lat, lon, forecast_days): "HGEFS",
        }
        for future in as_completed(futures):
            model = futures[future]
            try:
                results[model] = future.result()
            except Exception as e:
                logger.warning("Model %s failed: %s", model, e)
                results[model] = {"model": model, "error": str(e), "daily": []}

    return {
        "location": {"lat": lat, "lon": lon},
        "models": results,
    }


# ---------------------------------------------------------------------------
# Persistent forecast cache — survives NOMADS data gaps
# ---------------------------------------------------------------------------

_CACHE_DIR = pathlib.Path(os.environ.get(
    "FORECAST_CACHE_DIR",
    "/tmp/noaa_forecast_cache",
))


def _cache_path(grid_key: str, model: str, forecast_days: int) -> pathlib.Path:
    """File path for cached forecast JSON."""
    safe_key = grid_key.replace(",", "_").replace("-", "n")
    return _CACHE_DIR / f"{safe_key}_{model}_{forecast_days}d.json"


def _save_cache(grid_key: str, model: str, forecast_days: int, data: Dict[str, Any]) -> None:
    """Persist forecast to disk so it survives NOMADS gaps."""
    try:
        _CACHE_DIR.mkdir(parents=True, exist_ok=True)
        path = _cache_path(grid_key, model, forecast_days)
        payload = {
            "cached_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%MZ"),
            "data": data,
        }
        path.write_text(json.dumps(payload))
    except Exception as e:
        logger.debug("Cache write failed: %s", e)


def _load_cache(
    grid_key: str,
    model: str,
    forecast_days: int,
    max_age_hours: int = 24,
    allow_stale: bool = False,
) -> Optional[Dict[str, Any]]:
    """Load cached forecast from disk.

    Args:
        max_age_hours: Preferred max age. Cache younger than this is returned immediately.
        allow_stale: If True, return cache even if older than max_age_hours.
                     Stale data is always better than no data.
    """
    path = _cache_path(grid_key, model, forecast_days)
    if not path.exists():
        return None
    try:
        payload = json.loads(path.read_text())
        cached_at = datetime.strptime(payload["cached_at"], "%Y-%m-%dT%H:%MZ").replace(tzinfo=timezone.utc)
        age_hours = (datetime.now(timezone.utc) - cached_at).total_seconds() / 3600
        if age_hours > max_age_hours and not allow_stale:
            return None
        label = "stale" if age_hours > max_age_hours else "cached"
        logger.info("Using %s forecast (%.1fh old) for %s/%s", label, age_hours, grid_key, model)
        return payload["data"]
    except Exception:
        return None


def _fetch_from_nomads(grid_key: str, model: str, forecast_days: int) -> Dict[str, Any]:
    """Fetch a single model from NOMADS (no caching)."""
    lat_str, lon_str = grid_key.split(",")
    lat, lon = float(lat_str), float(lon_str)
    if model == "AIGFS":
        return fetch_aigfs_daily(lat, lon, forecast_days)
    elif model == "AIGEFS":
        return fetch_aigefs_daily(lat, lon, forecast_days)
    elif model == "HGEFS":
        return fetch_hgefs_daily(lat, lon, forecast_days)
    elif model == "GEFS":
        return fetch_gefs_s3_daily(lat, lon, forecast_days)
    else:
        return fetch_hgefs_daily(lat, lon, forecast_days)


def _has_data_fallback(result: Dict[str, Any]) -> bool:
    """Check if a forecast result actually contains useful data (for fallback logic)."""
    daily = result.get("daily", [])
    if daily:
        return True
    models = result.get("models", {})
    for m_data in models.values():
        if m_data.get("daily"):
            return True
    return False


def _fetch_gefs_s3_fallback(grid_key: str, forecast_days: int) -> Optional[Dict[str, Any]]:
    """Fetch GEFS from AWS S3 as fallback when NOMADS fails.

    Returns data with model name adjusted to indicate it's a fallback source.
    Returns None if S3 also fails (should be extremely rare).
    """
    lat_str, lon_str = grid_key.split(",")
    lat, lon = float(lat_str), float(lon_str)
    try:
        result = fetch_gefs_s3_daily(lat, lon, min(forecast_days, 16))
        if _has_data_fallback(result):
            result["fallback_source"] = "AWS S3 GEFS"
            logger.info("S3 GEFS fallback succeeded for %s", grid_key)
            return result
    except Exception as e:
        logger.warning("S3 GEFS fallback failed for %s: %s", grid_key, e)
    return None


def _fetch_single_model_cached(
    grid_key: str,
    today: str,
    model: str,
    forecast_days: int,
) -> Dict[str, Any]:
    """Fetch a single model with 5-layer fallback.

    Priority:
      1. Memory cache (5 min TTL good data, 1 min TTL empty)
      2. Live NOMADS fetch (HGEFS/AIGEFS/AIGFS — best AI models)
      3. AWS S3 GEFS (always available, no rate limits, live data)
      4. Fresh disk cache (<24h old)
      5. Stale disk cache (any age — better than nothing)

    Layer 3 (S3 GEFS) means we almost never serve stale disk data.
    GEFS is on S3 so we skip this layer if the requested model is already GEFS.
    """
    cache_key = f"{grid_key}:{today}:{model}:{forecast_days}"

    # Layer 1: memory cache
    entry = _FALLBACK_MEM_CACHE.get(cache_key)
    if entry is not None:
        ts, data = entry
        ttl = 300 if _has_data_fallback(data) else 60
        if time.time() - ts <= ttl:
            return data

    # Layer 2: fetch requested model (NOMADS for AI models, S3 for GEFS)
    result = _fetch_from_nomads(grid_key, model, forecast_days)

    if _has_data_fallback(result):
        _FALLBACK_MEM_CACHE[cache_key] = (time.time(), result)
        _save_cache(grid_key, model, forecast_days, result)
        return result

    # Layer 3: AWS S3 GEFS fallback (skip if already fetching GEFS)
    if model != "GEFS":
        s3_result = _fetch_gefs_s3_fallback(grid_key, forecast_days)
        if s3_result is not None:
            logger.warning("NOMADS %s gap — falling back to S3 GEFS for %s", model, grid_key)
            _FALLBACK_MEM_CACHE[cache_key] = (time.time(), s3_result)
            _save_cache(grid_key, model, forecast_days, s3_result)
            return s3_result

    # Layer 4: fresh disk cache (<24h)
    cached_disk = _load_cache(grid_key, model, forecast_days)
    if cached_disk is not None:
        logger.warning("NOMADS+S3 gap — serving cached %s for %s", model, grid_key)
        _FALLBACK_MEM_CACHE[cache_key] = (time.time(), cached_disk)
        return cached_disk

    # Layer 5: stale disk cache (any age — stale data > no data)
    stale_disk = _load_cache(grid_key, model, forecast_days, allow_stale=True)
    if stale_disk is not None:
        logger.warning("NOMADS+S3 gap — serving STALE %s for %s (>24h old)", model, grid_key)
        _FALLBACK_MEM_CACHE[cache_key] = (time.time(), stale_disk)
        return stale_disk

    logger.error("All sources failed for %s/%s — no data available", grid_key, model)
    _FALLBACK_MEM_CACHE[cache_key] = (time.time(), result)
    return result


# Module-level memory cache for the NOAA fallback path
_FALLBACK_MEM_CACHE: Dict[str, Tuple[float, Dict[str, Any]]] = {}
