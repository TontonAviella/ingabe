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

"""Farm-level weather forecast service — main entry point.

Provides get_farm_forecast() which fuses 4 weather models (3 physics-based
NWP + 1 AI) via Open-Meteo, with AWS S3 GEFS fallback.

This is the function called by the LLM tool handler in message_routes.py.
"""

from __future__ import annotations

import logging
import time
from datetime import datetime, timezone
from typing import Any, Dict, Optional, Tuple

from src.services.forecast_noaa_fallback import _fetch_single_model_cached
from src.services.forecast_openmeteo import _has_data, fetch_openmeteo_multimodel

logger = logging.getLogger(__name__)


def _snap_to_grid(lat: float, lon: float) -> str:
    """Snap coordinates to ~0.01° (~1km) grid for cache keys.

    The underlying GRIB data is 0.25° resolution, but bilinear interpolation
    produces unique values for each location.  Snapping to 0.01° keeps cache
    keys stable (prevents float noise) while giving distinct forecasts for
    different sectors/cells within a district.

    ~0.01° ≈ 1.1 km — roughly village-scale in Rwanda.
    """
    grid_lat = round(lat, 2)
    grid_lon = round(lon, 2)
    return f"{grid_lat},{grid_lon}"


# ---------------------------------------------------------------------------
# TTL memory cache — short TTL for failures, long TTL for successes
# ---------------------------------------------------------------------------

_MEM_CACHE: Dict[str, Tuple[float, Dict[str, Any]]] = {}
_MEM_TTL_SUCCESS = 300   # 5 min — NOMADS updates every 6h, no need to re-fetch constantly
_MEM_TTL_FAILURE = 60    # 1 min — retry quickly after NOMADS gap clears
_MEM_CACHE_MAX = 200     # max entries before eviction


def _mem_cache_get(key: str) -> Optional[Dict[str, Any]]:
    """Get from memory cache if entry exists and hasn't expired."""
    entry = _MEM_CACHE.get(key)
    if entry is None:
        return None
    ts, data = entry
    ttl = _MEM_TTL_SUCCESS if _has_data(data) else _MEM_TTL_FAILURE
    if time.time() - ts > ttl:
        return None
    return data


def _mem_cache_set(key: str, data: Dict[str, Any]) -> None:
    """Store in memory cache, evicting oldest entries if full."""
    if len(_MEM_CACHE) >= _MEM_CACHE_MAX:
        oldest_key = min(_MEM_CACHE, key=lambda k: _MEM_CACHE[k][0])
        del _MEM_CACHE[oldest_key]
    _MEM_CACHE[key] = (time.time(), data)


def get_farm_forecast(
    lat: float,
    lon: float,
    forecast_days: int = 10,
) -> Dict[str, Any]:
    """Get forecast for a farm location.

    Uses 4 weather models via Open-Meteo:
      ECMWF IFS (9km) + GFS (13km) + ICON (11km) + GraphCast (28km)
    Falls back to AWS S3 GEFS (28km) if Open-Meteo is unavailable.

    Args:
        lat: Farm latitude
        lon: Farm longitude
        forecast_days: Days to forecast (1-16)
    """
    grid_key = _snap_to_grid(lat, lon)
    today = datetime.now(timezone.utc).strftime("%Y%m%d")

    # Primary: Open-Meteo multi-model (ECMWF + GFS + ICON + GraphCast)
    cache_key = f"{grid_key}:{today}:MULTI:{forecast_days}"
    cached = _mem_cache_get(cache_key)
    if cached is not None:
        return {**cached, "location": {"lat": lat, "lon": lon, "grid_cell": grid_key}}

    result = fetch_openmeteo_multimodel(lat, lon, forecast_days)
    if _has_data(result):
        _mem_cache_set(cache_key, result)
        return {**result, "location": {"lat": lat, "lon": lon, "grid_cell": grid_key}}

    # Fallback to S3 GEFS if Open-Meteo fails
    logger.warning("Open-Meteo failed — falling back to S3 GEFS for %s", grid_key)
    result = _fetch_single_model_cached(grid_key, today, "GEFS", forecast_days)
    return {**result, "location": {"lat": lat, "lon": lon, "grid_cell": grid_key}}
