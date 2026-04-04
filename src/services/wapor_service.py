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

"""FAO WaPOR v3 evapotranspiration and water productivity service.

Queries Cloud-Optimized GeoTIFFs from FAO WaPOR v3 stored on Google Cloud Storage.
Data: 100m resolution, dekadal (10-day) updates for Africa.

Layers:
- L2-AETI-D: Actual evapotranspiration + interception (mm/day), dekadal, 100m
- L2-NPP-D: Net primary productivity (gC/m²/day), dekadal, 100m
- L2-T-D: Transpiration (mm/day), dekadal, 100m
- L2-RSM-D: Relative soil moisture (%), dekadal, 100m
"""

import logging
from concurrent.futures import ThreadPoolExecutor
from datetime import date, timedelta
from typing import Any

import httpx
import numpy as np
import rasterio

logger = logging.getLogger(__name__)

GCS_BASE = "https://storage.googleapis.com/fao-gismgr-wapor-3-data/DATA/WAPOR-3/MAPSET"
CATALOG_BASE = "https://data.apps.fao.org/gismgr/api/v2/catalog/workspaces/WAPOR-3/mapsets"

# Layer definitions: code -> (scale, offset, unit, description)
LAYERS = {
    "L2-AETI-D": (0.1, 0.0, "mm/day", "Actual evapotranspiration + interception"),
    "L2-NPP-D": (0.001, 0.0, "gC/m²/day", "Net primary productivity"),
    "L2-T-D": (0.1, 0.0, "mm/day", "Transpiration"),
    "L2-RSM-D": (0.001, 0.0, "%", "Relative soil moisture"),
}

NODATA = -9999


def _date_to_dekad(d: date) -> str:
    """Convert a date to WaPOR dekad string: YYYY-MM-D1/D2/D3."""
    if d.day <= 10:
        return f"{d.year}-{d.month:02d}-D1"
    elif d.day <= 20:
        return f"{d.year}-{d.month:02d}-D2"
    else:
        return f"{d.year}-{d.month:02d}-D3"


def _dekad_dates(date_from: date, date_to: date) -> list[str]:
    """Generate list of dekad codes between two dates."""
    dekads = []
    current = date_from
    seen = set()
    while current <= date_to:
        dk = _date_to_dekad(current)
        if dk not in seen:
            dekads.append(dk)
            seen.add(dk)
        current += timedelta(days=10)
    # Make sure the last dekad is included
    dk = _date_to_dekad(date_to)
    if dk not in seen:
        dekads.append(dk)
    return dekads


def _raster_url(layer_code: str, dekad: str) -> str:
    """Build GCS URL for a WaPOR raster."""
    return f"{GCS_BASE}/{layer_code}/WAPOR-3.{layer_code}.{dekad}.tif"


def _read_point(url: str, lat: float, lon: float, scale: float, offset: float) -> float | None:
    """Read a single pixel value from a COG at given coordinates."""
    try:
        with rasterio.open(url) as ds:
            row, col = ds.index(lon, lat)
            window = rasterio.windows.Window(col, row, 1, 1)
            data = ds.read(1, window=window)
            raw = int(data[0, 0])
            if raw == NODATA:
                return None
            return raw * scale + offset
    except Exception as e:
        logger.warning("WaPOR read failed for %s: %s", url, e)
        return None


def query_et(
    lat: float,
    lon: float,
    date_from: date | None = None,
    date_to: date | None = None,
    include_components: bool = False,
) -> dict[str, Any]:
    """Query evapotranspiration time series for a point.

    Args:
        lat: Latitude (WGS84).
        lon: Longitude (WGS84).
        date_from: Start date (default: 3 dekads ago).
        date_to: End date (default: latest available).
        include_components: If True, also fetch transpiration and NPP.

    Returns:
        Dict with status, time series, and metadata.
    """
    if date_to is None:
        date_to = date.today() - timedelta(days=5)  # latest likely available
    if date_from is None:
        date_from = date_to - timedelta(days=30)  # ~3 dekads

    dekads = _dekad_dates(date_from, date_to)
    if not dekads:
        return {"status": "error", "error": "No dekads in date range"}

    # Limit to 12 dekads (~4 months) to keep response time reasonable
    if len(dekads) > 12:
        dekads = dekads[-12:]

    # Layers to query
    layers_to_query = ["L2-AETI-D"]
    if include_components:
        layers_to_query.extend(["L2-T-D", "L2-NPP-D"])

    # Build all (layer, dekad) pairs for parallel fetch
    tasks = []
    for layer_code in layers_to_query:
        scale, offset, unit, desc = LAYERS[layer_code]
        for dk in dekads:
            url = _raster_url(layer_code, dk)
            tasks.append((layer_code, dk, url, scale, offset, unit))

    # Parallel COG reads (each is a single HTTP range request, fast)
    results: dict[str, dict[str, float | None]] = {lc: {} for lc in layers_to_query}
    with ThreadPoolExecutor(max_workers=6) as executor:
        futures = {}
        for layer_code, dk, url, scale, offset, unit in tasks:
            f = executor.submit(_read_point, url, lat, lon, scale, offset)
            futures[f] = (layer_code, dk)
        for f in futures:
            layer_code, dk = futures[f]
            results[layer_code][dk] = f.result()

    # Build time series
    time_series = []
    for dk in dekads:
        entry: dict[str, Any] = {"dekad": dk}
        et_val = results["L2-AETI-D"].get(dk)
        if et_val is not None:
            entry["et_mm_per_day"] = round(et_val, 2)
        else:
            entry["et_mm_per_day"] = None
        if include_components:
            t_val = results.get("L2-T-D", {}).get(dk)
            entry["transpiration_mm_per_day"] = round(t_val, 2) if t_val is not None else None
            npp_val = results.get("L2-NPP-D", {}).get(dk)
            entry["npp_gC_per_m2_per_day"] = round(npp_val, 4) if npp_val is not None else None
        time_series.append(entry)

    # Summary stats for ET
    et_values = [e["et_mm_per_day"] for e in time_series if e["et_mm_per_day"] is not None]
    summary = {}
    if et_values:
        arr = np.array(et_values)
        summary = {
            "mean_et_mm_per_day": round(float(arr.mean()), 2),
            "min_et_mm_per_day": round(float(arr.min()), 2),
            "max_et_mm_per_day": round(float(arr.max()), 2),
            "total_dekads": len(et_values),
        }

    return {
        "status": "success",
        "coordinates": {"lat": lat, "lon": lon},
        "date_range": f"{date_from.isoformat()} to {date_to.isoformat()}",
        "resolution": "100m (dekadal, 10-day intervals)",
        "source": "FAO WaPOR v3 (100m dekadal)",
        "time_series": time_series,
        "summary": summary,
    }


def query_soil_moisture(
    lat: float,
    lon: float,
    date_from: date | None = None,
    date_to: date | None = None,
) -> dict[str, Any]:
    """Query WaPOR relative soil moisture for a point.

    Args:
        lat: Latitude.
        lon: Longitude.
        date_from: Start date.
        date_to: End date.

    Returns:
        Dict with soil moisture time series.
    """
    if date_to is None:
        date_to = date.today() - timedelta(days=5)
    if date_from is None:
        date_from = date_to - timedelta(days=30)

    dekads = _dekad_dates(date_from, date_to)
    if len(dekads) > 12:
        dekads = dekads[-12:]

    scale, offset, unit, desc = LAYERS["L2-RSM-D"]

    with ThreadPoolExecutor(max_workers=6) as executor:
        futures = {}
        for dk in dekads:
            url = _raster_url("L2-RSM-D", dk)
            f = executor.submit(_read_point, url, lat, lon, scale, offset)
            futures[f] = dk

        rsm_data = {}
        for f in futures:
            rsm_data[futures[f]] = f.result()

    time_series = []
    for dk in dekads:
        val = rsm_data.get(dk)
        time_series.append({
            "dekad": dk,
            "relative_soil_moisture_pct": round(val * 100, 1) if val is not None else None,
        })

    values = [e["relative_soil_moisture_pct"] for e in time_series if e["relative_soil_moisture_pct"] is not None]
    summary = {}
    if values:
        arr = np.array(values)
        summary = {
            "mean_rsm_pct": round(float(arr.mean()), 1),
            "min_rsm_pct": round(float(arr.min()), 1),
            "max_rsm_pct": round(float(arr.max()), 1),
        }

    return {
        "status": "success",
        "coordinates": {"lat": lat, "lon": lon},
        "date_range": f"{date_from.isoformat()} to {date_to.isoformat()}",
        "resolution": "100m (dekadal)",
        "source": "FAO WaPOR v3 (100m dekadal)",
        "time_series": time_series,
        "summary": summary,
    }


def get_latest_available_dekad() -> str | None:
    """Check the WaPOR catalog for the most recent AETI dekad."""
    try:
        resp = httpx.get(
            f"{CATALOG_BASE}/L2-AETI-D/stats",
            timeout=15,
        )
        resp.raise_for_status()
        data = resp.json()
        last = data.get("response", {}).get("rasters", {}).get("lastIngested", {})
        code = last.get("code", "")
        # Extract dekad from code like "WAPOR-3.L2-AETI-D.2026-03-D3"
        parts = code.split(".")
        if len(parts) >= 3:
            return parts[2]
        return None
    except Exception as e:
        logger.warning("Failed to check WaPOR latest dekad: %s", e)
        return None
