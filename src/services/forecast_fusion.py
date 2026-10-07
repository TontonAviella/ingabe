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

"""Forecast bias correction and weighted fusion.

Compares recent model hindcasts (from the Open-Meteo forecast API past_days)
against observed weather to:

  1. Compute per-model, per-variable rolling bias
  2. Apply additive bias correction to each model's forecast
  3. Weight models by recent accuracy (inverse-MAE weighting)

Ground truth sources (priority order):
  Precipitation: CHIRPS v2.0 (satellite+gauge blend, 5km, ~3 week lag)
                 → fallback: Open-Meteo archive ERA5
  Temperature:   Open-Meteo archive ERA5 (9km, 5-day lag)

Data flow:
  ┌─────────────────────────┐     ┌──────────────┐  ┌──────────────────┐
  │ Open-Meteo Forecast API │     │ CHIRPS v2.0  │  │ Open-Meteo ERA5  │
  │   past_days=7           │     │ (precip only)│  │ (temp + fallback)│
  │   → per-model hindcasts │     │ 5km gauge+sat│  │ 9km reanalysis   │
  └────────┬────────────────┘     └──────┬───────┘  └───────┬──────────┘
           │                             │                  │
           ▼                             ▼                  ▼
     ┌──────────────────────────────────────────────────────────┐
     │              compute_bias_corrections()                  │
     │  model_hindcast - observed = bias                        │
     │  abs(model_hindcast - observed) = MAE                    │
     │  weight = 1/MAE (inverse error)                          │
     └─────────────┬────────────────────────────────────────────┘
                   │
                   ▼
     ┌─────────────────────────────────────────┐
     │      apply_bias_correction()            │
     │  corrected = forecast - bias            │
     │  consensus = Σ(weight × corrected)      │
     └─────────────────────────────────────────┘
"""

from __future__ import annotations

import gzip
import io
import logging
import os
import threading
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor, wait
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

import numpy as np
from cachetools import TTLCache

logger = logging.getLogger(__name__)

_ARCHIVE_API = "https://archive-api.open-meteo.com/v1/archive"

# CHIRPS v2.0 Africa daily (0.05° ≈ 5km, satellite+gauge blend). The final
# product lags by weeks (e.g. on 2026-10-04 it ended on 2026-08-31); the
# preliminary global daily product (fewer gauges) lags by about 5 days and is
# used for days the final product does not have yet.
_CHIRPS_BASE = (
    "https://data.chc.ucsb.edu/products/CHIRPS-2.0/africa_daily/tifs/p05"
)
_CHIRPS_PRELIM_BASE = (
    "https://data.chc.ucsb.edu/products/CHIRPS-2.0/prelim/global_daily/tifs/p05"
)

# Variables we correct — mapped to their archive API names
_CORRECTABLE_VARS = {
    "temperature_max": "temperature_2m_max",
    "temperature_min": "temperature_2m_min",
    "precipitation_mm": "precipitation_sum",
}


# A report reads one pixel from each of up to ~100 CHIRPS files of 2-3 MB that
# take ~5 s each to download, ten at a time (2026-10-07: 61 s of a 384 s
# insurance report). Published days do not change, so the Rwanda part of every
# file read is kept on disk (~12 KB a day) and any later read in Rwanda comes
# from there. The default directory is the ingabe-cache volume, which outlives
# container restarts.
_CHIRPS_CACHE_DIR = Path(os.environ.get("CHIRPS_CACHE_DIR", "/tmp/ingabe_cache/chirps"))
_CHIRPS_KEPT_BOUNDS = (28.5, -3.2, 31.5, -0.7)  # west, south, east, north: Rwanda with a margin
_CHIRPS_PRELIM_MAX_AGE_S = 7 * 86400  # preliminary days are read again after a week, in case CHC revises one
# A day the server does not have yet (404) is asked for again after this long.
_chirps_unpublished: TTLCache[str, bool] = TTLCache(maxsize=4096, ttl=3 * 3600)
_chirps_unpublished_lock = threading.Lock()
# One download per file at a time: the season fetch and the forecast's bias
# correction ask for the same recent days at once.
_chirps_file_locks: dict[str, threading.Lock] = {}
_chirps_file_locks_guard = threading.Lock()
# The CHIRPS server answers 429 from about 30 parallel downloads (2026-10-07).
_CHIRPS_WORKERS = 10


@dataclass(frozen=True)
class _ChirpsKept:
    """The pixels of one CHIRPS file over _CHIRPS_KEPT_BOUNDS, placed in the file's own grid."""

    values: np.ndarray  # float32, as stored in the file
    row_off: int
    col_off: int
    transform: tuple[float, ...]  # the whole file's affine transform (a, b, c, d, e, f)

    def raw_at(self, lat: float, lon: float) -> Optional[float]:
        """The stored value of the pixel holding (lat, lon), or None when it lies outside the kept part."""
        from affine import Affine  # lazy: rasterio's dependency, like the rasterio imports below
        from rasterio.transform import rowcol  # type: ignore[import-untyped]  # lazy: rasterio/GDAL stack

        row, col = rowcol(Affine(*self.transform), lon, lat)  # the pixel src.index(lon, lat) picks
        r, c = row - self.row_off, col - self.col_off
        if 0 <= r < self.values.shape[0] and 0 <= c < self.values.shape[1]:
            return float(self.values[r, c])
        return None


def _chirps_download(url: str) -> bytes:
    """The decompressed GeoTIFF bytes of one CHIRPS file; HTTPError propagates."""
    req = urllib.request.Request(url, headers={"User-Agent": "mundi.ai/1.0"})
    with urllib.request.urlopen(req, timeout=15) as resp:
        return gzip.decompress(resp.read())


def _chirps_cache_path(url: str) -> Path:
    product = "prelim" if url.startswith(_CHIRPS_PRELIM_BASE) else "final"
    return _CHIRPS_CACHE_DIR / f"{product}-{url.rsplit('/', 1)[-1].removesuffix('.tif.gz')}.npz"


def _chirps_kept_from_disk(url: str) -> Optional[_ChirpsKept]:
    path = _chirps_cache_path(url)
    try:
        if url.startswith(_CHIRPS_PRELIM_BASE) and time.time() - path.stat().st_mtime > _CHIRPS_PRELIM_MAX_AGE_S:
            return None
        with np.load(path) as kept:
            row_off, col_off = (int(v) for v in kept["offsets"])
            return _ChirpsKept(kept["values"], row_off, col_off, tuple(float(v) for v in kept["transform"]))
    except FileNotFoundError:
        return None
    except Exception:
        logger.warning("CHIRPS: unreadable kept file %s; downloading the day again", path, exc_info=True)
        return None


def _chirps_keep(url: str, tif: bytes) -> _ChirpsKept:
    """Cut the kept part out of a downloaded file and store it; a store that fails is logged and skipped."""
    import rasterio  # type: ignore[import-untyped]  # lazy: rasterio/GDAL stack

    west, south, east, north = _CHIRPS_KEPT_BOUNDS
    with rasterio.open(io.BytesIO(tif)) as src:
        top, left = src.index(west, north)
        bottom, right = src.index(east, south)
        top, left = max(top, 0), max(left, 0)
        bottom, right = min(bottom, src.height - 1), min(right, src.width - 1)
        kept = _ChirpsKept(src.read(1)[top:bottom + 1, left:right + 1].copy(), top, left, tuple(src.transform)[:6])
    path = _chirps_cache_path(url)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        part = path.with_name(f"{path.stem}.{os.getpid()}.{threading.get_ident()}.part.npz")
        np.savez(part, values=kept.values, offsets=np.array([kept.row_off, kept.col_off]),
                 transform=np.array(kept.transform))
        os.replace(part, path)
    except OSError:
        logger.warning("CHIRPS: could not keep %s on disk; it will be downloaded again", url, exc_info=True)
    return kept


def _chirps_kept(url: str) -> tuple[_ChirpsKept, Optional[bytes]]:
    """The kept part of one CHIRPS file, from disk or downloaded now (then also the file itself).

    A 404 is remembered for a while and raised again without asking; other
    HTTP errors and network failures are not remembered.
    """
    with _chirps_unpublished_lock:
        if url in _chirps_unpublished:
            raise urllib.error.HTTPError(url, 404, "Not Found (asked recently)", hdrs=None, fp=None)  # type: ignore[arg-type]
    with _chirps_file_locks_guard:
        file_lock = _chirps_file_locks.setdefault(url, threading.Lock())
    with file_lock:
        kept = _chirps_kept_from_disk(url)
        if kept is not None:
            return kept, None
        try:
            tif = _chirps_download(url)
        except urllib.error.HTTPError as e:
            if e.code == 404:
                with _chirps_unpublished_lock:
                    _chirps_unpublished[url] = True
            raise
        return _chirps_keep(url, tif), tif


def _chirps_pixel(url: str, lat: float, lon: float) -> Optional[float]:
    """The pixel value at (lat, lon) of one gzipped CHIRPS GeoTIFF; HTTPError propagates."""
    kept, tif = _chirps_kept(url)
    val = kept.raw_at(lat, lon)
    if val is None:  # outside Rwanda: read the file itself
        import rasterio  # type: ignore[import-untyped]  # lazy: rasterio/GDAL stack

        with rasterio.open(io.BytesIO(tif if tif is not None else _chirps_download(url))) as src:
            row, col = src.index(lon, lat)
            val = float(src.read(1)[row, col])
    return None if val < -9000 else round(max(0.0, val), 1)


def _fetch_chirps_one(
    lat: float, lon: float, date_str: str,
) -> tuple[str, Optional[float], bool]:
    """(date, mm, preliminary) for one day: the final product, else the preliminary one."""
    dt = datetime.strptime(date_str, "%Y-%m-%d")
    fname = f"chirps-v2.0.{dt.strftime('%Y.%m.%d')}.tif.gz"
    try:
        return date_str, _chirps_pixel(f"{_CHIRPS_BASE}/{dt.year}/{fname}", lat, lon), False
    except urllib.error.HTTPError as e:
        if e.code != 404:
            return date_str, None, False
    except Exception:
        return date_str, None, False
    try:
        return date_str, _chirps_pixel(f"{_CHIRPS_PRELIM_BASE}/{dt.year}/{fname}", lat, lon), True
    except Exception:
        return date_str, None, False


def fetch_chirps_daily(
    lat: float, lon: float, dates: List[str], timeout_s: Optional[float] = None,
) -> tuple[Dict[str, Optional[float]], set[str]]:
    """Daily CHIRPS rainfall at (lat, lon) for ``dates``, and which days are preliminary.

    Returns ({date_str: mm or None}, {dates read from the preliminary product}).
    A day neither product has yet is None, never 0. With ``timeout_s``, the
    days not read by then are None too (missing, not dry): their downloads
    finish in the background and are kept for the next read.
    """
    try:
        import rasterio  # type: ignore[import-untyped]  # noqa: F401
    except ImportError:
        logger.info("rasterio not available — skipping CHIRPS")
        return {}, set()

    pool = ThreadPoolExecutor(max_workers=_CHIRPS_WORKERS)
    futures = [pool.submit(_fetch_chirps_one, lat, lon, d) for d in dates]
    done, not_done = wait(futures, timeout=timeout_s)
    pool.shutdown(wait=False, cancel_futures=True)

    result: Dict[str, Optional[float]] = {}
    prelim: set[str] = set()
    for d, f in zip(dates, futures):
        date_str, val, is_prelim = f.result() if f in done else (d, None, False)
        result[date_str] = val
        if is_prelim and val is not None:
            prelim.add(date_str)

    fetched = sum(1 for v in result.values() if v is not None)
    if fetched:
        logger.info("CHIRPS: got %d/%d days of precip data (%d preliminary)",
                    fetched, len(dates), len(prelim))
    if not_done:
        logger.warning("CHIRPS: %d of %d days not read within %.0f s; they count as missing",
                       len(not_done), len(dates), timeout_s)
    return result, prelim


def _fetch_chirps_precip(
    lat: float, lon: float, dates: List[str],
) -> Dict[str, Optional[float]]:
    """Daily CHIRPS rainfall {date_str: mm or None}; see fetch_chirps_daily."""
    return fetch_chirps_daily(lat, lon, dates)[0]


def _fetch_observed(
    lat: float, lon: float, lookback_days: int = 7,
) -> Dict[str, List[Optional[float]]]:
    """Fetch observed weather from multiple sources.

    Precipitation: CHIRPS v2.0 (primary) → Open-Meteo ERA5 (fallback)
    Temperature: Open-Meteo ERA5 (only source)
    """
    import json as _json

    end = datetime.now(timezone.utc) - timedelta(days=1)  # yesterday
    start = end - timedelta(days=lookback_days - 1)

    date_list = [
        (start + timedelta(days=i)).strftime("%Y-%m-%d")
        for i in range(lookback_days)
    ]

    # --- Step 1: try CHIRPS for precipitation ---
    chirps = _fetch_chirps_precip(lat, lon, date_list)
    chirps_hits = sum(1 for v in chirps.values() if v is not None)

    # --- Step 2: always fetch ERA5 for temperature (and precip fallback) ---
    archive_vars = ",".join(_CORRECTABLE_VARS.values())
    url = (
        f"{_ARCHIVE_API}?latitude={lat}&longitude={lon}"
        f"&start_date={start.strftime('%Y-%m-%d')}"
        f"&end_date={end.strftime('%Y-%m-%d')}"
        f"&daily={archive_vars}&timezone=auto"
    )

    era5_daily: Dict[str, Any] = {}
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "mundi.ai/1.0"})
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = _json.loads(resp.read().decode())
        era5_daily = data.get("daily", {})
    except Exception as e:
        logger.warning("Archive API failed: %s", e)

    dates = era5_daily.get("time", date_list)
    if not dates and not chirps_hits:
        return {}

    # --- Step 3: merge — CHIRPS precip preferred over ERA5 precip ---
    result: Dict[str, Any] = {"dates": dates}

    # Temperature: always from ERA5
    result["temperature_max"] = era5_daily.get("temperature_2m_max", [])
    result["temperature_min"] = era5_daily.get("temperature_2m_min", [])

    # Precipitation: CHIRPS primary, ERA5 gap-fill
    era5_precip = era5_daily.get("precipitation_sum", [])
    merged_precip: List[Optional[float]] = []
    precip_source = {"chirps": 0, "era5": 0, "missing": 0}

    for i, d in enumerate(dates):
        chirps_val = chirps.get(d)
        era5_val = era5_precip[i] if i < len(era5_precip) else None

        if chirps_val is not None:
            merged_precip.append(chirps_val)
            precip_source["chirps"] += 1
        elif era5_val is not None:
            merged_precip.append(round(era5_val, 1))
            precip_source["era5"] += 1
        else:
            merged_precip.append(None)
            precip_source["missing"] += 1

    result["precipitation_mm"] = merged_precip

    logger.info(
        "Observed data: precip sources chirps=%d era5=%d missing=%d, temp from ERA5",
        precip_source["chirps"],
        precip_source["era5"],
        precip_source["missing"],
    )

    return result


def compute_bias_corrections(
    recent_daily: List[Dict[str, Any]],
    lat: float,
    lon: float,
    lookback_days: int = 14,
) -> Dict[str, Dict[str, float]]:
    """Compute per-model bias and MAE from hindcasts vs observations.

    Args:
        recent_daily: Past days from fetch_openmeteo_multimodel() — each day
                      has per-variable dicts with "models" sub-dict.
        lat, lon: Location for archive API query.
        lookback_days: How many past days to use.

    Returns:
        {
            "bias": {
                "ECMWF IFS": {"temperature_max": -0.2, "precipitation_mm": +6.0, ...},
                "GFS": {...},
                ...
            },
            "mae": {
                "ECMWF IFS": {"temperature_max": 1.1, "precipitation_mm": 6.2, ...},
                ...
            },
            "weights": {
                "ECMWF IFS": {"temperature_max": 0.35, "precipitation_mm": 0.10, ...},
                ...
            },
            "observed_days": 7,
        }
    """
    observed = _fetch_observed(lat, lon, lookback_days)
    if not observed or not observed.get("dates"):
        logger.info("No observed data — skipping bias correction")
        return {}

    obs_dates = observed["dates"]
    obs_by_date = {}
    for i, d in enumerate(obs_dates):
        obs_by_date[d] = {
            var: observed[var][i]
            for var in _CORRECTABLE_VARS
            if i < len(observed.get(var, []))
        }

    # Build per-model error lists with exponential decay weighting
    # Recent observations matter more than older ones (half-life = 5 days)
    # model_errors[model_label][var] = list of (error, weight) tuples
    model_errors: Dict[str, Dict[str, List[tuple]]] = {}
    _DECAY_HALFLIFE = 5.0  # days

    sorted_dates = sorted(obs_by_date.keys(), reverse=True)
    date_weights = {}
    for i, d in enumerate(sorted_dates):
        # Exponential decay: weight = 2^(-age/halflife)
        date_weights[d] = 2.0 ** (-i / _DECAY_HALFLIFE)

    for day in recent_daily:
        date = day.get("date")
        if date not in obs_by_date:
            continue
        obs = obs_by_date[date]
        w = date_weights.get(date, 1.0)

        for var in _CORRECTABLE_VARS:
            var_data = day.get(var, {})
            models = var_data.get("models", {})
            obs_val = obs.get(var)
            if obs_val is None:
                continue

            for model_label, hindcast_val in models.items():
                if hindcast_val is None:
                    continue
                model_errors.setdefault(model_label, {}).setdefault(var, [])
                model_errors[model_label][var].append((hindcast_val - obs_val, w))

    if not model_errors:
        return {}

    # Compute weighted bias and MAE per model per variable
    # Recent observations get higher weight via exponential decay
    bias: Dict[str, Dict[str, float]] = {}
    mae: Dict[str, Dict[str, float]] = {}

    for model_label, var_errors in model_errors.items():
        bias[model_label] = {}
        mae[model_label] = {}
        for var, error_weights in var_errors.items():
            if not error_weights:
                continue
            total_w = sum(w for _, w in error_weights)
            if total_w < 0.01:
                continue
            bias[model_label][var] = round(
                sum(e * w for e, w in error_weights) / total_w, 2
            )
            mae[model_label][var] = round(
                sum(abs(e) * w for e, w in error_weights) / total_w, 2
            )

    # Compute inverse-MAE weights per variable
    # w_i = (1/MAE_i) / Σ(1/MAE_j) — models with lower error get higher weight
    weights: Dict[str, Dict[str, float]] = {}
    all_vars = set()
    for m_mae in mae.values():
        all_vars.update(m_mae.keys())

    for var in all_vars:
        inv_maes = {}
        for model_label in mae:
            m = mae[model_label].get(var)
            if m is not None and m > 0.01:  # avoid division by near-zero
                inv_maes[model_label] = 1.0 / m
            elif m is not None:
                inv_maes[model_label] = 100.0  # near-perfect → high weight

        total_inv = sum(inv_maes.values()) if inv_maes else 1.0
        for model_label, inv_m in inv_maes.items():
            weights.setdefault(model_label, {})[var] = round(inv_m / total_inv, 3)

    matched_days = len(set(d["date"] for d in recent_daily if d["date"] in obs_by_date))

    return {
        "bias": bias,
        "mae": mae,
        "weights": weights,
        "observed_days": matched_days,
    }


def apply_bias_correction(
    forecast_daily: List[Dict[str, Any]],
    corrections: Dict[str, Any],
) -> None:
    """Apply bias corrections to forecast days in-place.

    For each correctable variable in each forecast day:
      1. Subtract bias from each model's value
      2. Recompute consensus mean using accuracy-based weights
      3. Update spread based on corrected values
    """
    if not corrections:
        return

    bias = corrections.get("bias", {})
    weights = corrections.get("weights", {})

    if not bias:
        return

    for day in forecast_daily:
        for var in _CORRECTABLE_VARS:
            var_data = day.get(var)
            if not var_data or "models" not in var_data:
                continue

            models = var_data["models"]
            corrected = {}
            weighted_sum = 0.0
            weight_total = 0.0

            for model_label, raw_val in models.items():
                if raw_val is None:
                    continue

                # Step 1: subtract bias
                model_bias = bias.get(model_label, {}).get(var, 0.0)
                corrected_val = raw_val - model_bias

                # Precipitation can't go negative
                if var == "precipitation_mm":
                    corrected_val = max(0.0, corrected_val)

                corrected[model_label] = round(corrected_val, 1)

                # Step 2: accumulate weighted sum
                w = weights.get(model_label, {}).get(var)
                if w is not None:
                    weighted_sum += corrected_val * w
                    weight_total += w

            if not corrected:
                continue

            # Update the day's data
            corrected_values = list(corrected.values())

            if weight_total > 0:
                new_mean = weighted_sum / weight_total
            else:
                new_mean = sum(corrected_values) / len(corrected_values)

            new_spread = max(corrected_values) - min(corrected_values)

            var_data["mean"] = round(new_mean, 1)
            var_data["spread"] = round(new_spread, 1)
            var_data["models"] = corrected
            var_data["bias_corrected"] = True

            # Update p10/p90 if they exist
            if len(corrected_values) >= 2:
                std = (
                    sum((v - new_mean) ** 2 for v in corrected_values)
                    / len(corrected_values)
                ) ** 0.5
                if var == "precipitation_mm":
                    var_data["p10"] = round(max(0.0, new_mean - 1.282 * std), 1)
                else:
                    var_data["p10"] = round(new_mean - 1.282 * std, 1)
                var_data["p90"] = round(new_mean + 1.282 * std, 1)
