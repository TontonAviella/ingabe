"""On-the-fly metric computation for layer features (choropleth enrichment).

Computes per-feature statistics (land cover percentages, weather, vegetation
indices) and stores them in the app DB ``layer_enrichments`` table so they can
be injected into PostGIS tile queries as a VALUES CTE.
"""

from __future__ import annotations

import asyncio
import json
import logging
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional

import numpy as np

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Metric registry
# ---------------------------------------------------------------------------

@dataclass
class MetricDefinition:
    key: str
    label: str
    category: str
    description: str
    source: str


AVAILABLE_METRICS: Dict[str, MetricDefinition] = {
    "cropland_pct": MetricDefinition(
        key="cropland_pct",
        label="Cropland %",
        category="Land Cover",
        description="Percentage of area classified as cropland (ESRI 10m LULC)",
        source="ESRI 10m Annual LULC",
    ),
    "forest_pct": MetricDefinition(
        key="forest_pct",
        label="Forest %",
        category="Land Cover",
        description="Percentage of area classified as trees/forest (ESRI 10m LULC)",
        source="ESRI 10m Annual LULC",
    ),
    "built_pct": MetricDefinition(
        key="built_pct",
        label="Built Area %",
        category="Land Cover",
        description="Percentage of area classified as built-up (ESRI 10m LULC)",
        source="ESRI 10m Annual LULC",
    ),
    "rangeland_pct": MetricDefinition(
        key="rangeland_pct",
        label="Rangeland %",
        category="Land Cover",
        description="Percentage of area classified as rangeland (ESRI 10m LULC)",
        source="ESRI 10m Annual LULC",
    ),
    "ndvi_mean": MetricDefinition(
        key="ndvi_mean",
        label="NDVI Mean",
        category="Vegetation",
        description="Mean NDVI from Sentinel-2 (last 30 days)",
        source="Sentinel Hub Statistical API",
    ),
    "rainfall_mm": MetricDefinition(
        key="rainfall_mm",
        label="Rainfall (mm)",
        category="Weather",
        description="Total precipitation over last 10 days",
        source="Open-Meteo",
    ),
    "temp_mean": MetricDefinition(
        key="temp_mean",
        label="Temperature (C)",
        category="Weather",
        description="Mean temperature over last 10 days",
        source="Open-Meteo",
    ),
}

# ESRI LULC class values → metric key mapping
_LULC_CLASS_MAP = {
    "cropland_pct": 5,   # Crops
    "forest_pct": 2,     # Trees
    "built_pct": 7,      # Built Area
    "rangeland_pct": 11,  # Rangeland
}


# ---------------------------------------------------------------------------
# LULC computation (rasterio-based, reuses worldcover.py patterns)
# ---------------------------------------------------------------------------

def _compute_lulc_metrics(
    features: List[Dict[str, Any]],
    metric_key: str,
) -> Dict[int, float]:
    """Compute land cover percentage for each feature using ESRI 10m LULC COGs.

    Reads COGs once for the full layer extent, then masks each feature
    individually. Returns {feature_id: percentage}.
    """
    import rasterio
    from rasterio.enums import Resampling
    from rasterio.features import geometry_mask
    from rasterio.merge import merge
    from rasterio.transform import from_bounds
    from rasterio.vrt import WarpedVRT
    from shapely.geometry import shape

    from src.worldcover import get_rwanda_tile_urls

    target_class = _LULC_CLASS_MAP[metric_key]

    # Compute bounding box of all features
    all_bounds = []
    for feat in features:
        geom = shape(feat["geom"])
        all_bounds.append(geom.bounds)

    west = min(b[0] for b in all_bounds)
    south = min(b[1] for b in all_bounds)
    east = max(b[2] for b in all_bounds)
    north = max(b[3] for b in all_bounds)

    # Open COGs as WarpedVRT in EPSG:4326
    tile_urls = get_rwanda_tile_urls()
    datasets = []
    raw_datasets = []
    try:
        for url in tile_urls:
            ds = rasterio.open(url)
            vrt = WarpedVRT(ds, crs="EPSG:4326", resampling=Resampling.nearest)
            datasets.append(vrt)
            raw_datasets.append(ds)

        # Merge to layer extent
        mosaic, mosaic_transform = merge(
            datasets,
            bounds=(west, south, east, north),
            resampling=Resampling.nearest,
            nodata=0,
        )
    finally:
        for vrt in datasets:
            vrt.close()
        for ds in raw_datasets:
            ds.close()

    data = mosaic[0]  # (h, w) uint8
    h, w = data.shape
    transform = from_bounds(west, south, east, north, w, h)

    results: Dict[int, float] = {}

    for feat in features:
        fid = feat["id"]
        geom_dict = feat["geom"]
        try:
            mask = geometry_mask(
                [geom_dict],
                out_shape=(h, w),
                transform=transform,
                invert=True,  # True = inside polygon
            )
            masked_pixels = data[mask]
            total = int(np.count_nonzero(masked_pixels > 0))  # exclude nodata
            if total == 0:
                results[fid] = 0.0
            else:
                target_count = int(np.count_nonzero(masked_pixels == target_class))
                results[fid] = round(target_count / total * 100, 2)
        except Exception as e:
            logger.warning("LULC mask failed for feature %d: %s", fid, e)
            results[fid] = 0.0

    return results


# ---------------------------------------------------------------------------
# Weather computation (Open-Meteo, reuses weather_service pattern)
# ---------------------------------------------------------------------------

def _compute_weather_metric(
    features: List[Dict[str, Any]],
    metric_key: str,
) -> Dict[int, float]:
    """Compute weather metric for each feature centroid via Open-Meteo.

    Uses centroids of feature geometries for bulk Open-Meteo request.
    Returns {feature_id: value}.
    """
    from shapely.geometry import shape

    centroids = []
    for feat in features:
        geom = shape(feat["geom"])
        c = geom.centroid
        centroids.append({"id": feat["id"], "lat": round(c.y, 4), "lon": round(c.x, 4)})

    if not centroids:
        return {}

    lats = ",".join(str(c["lat"]) for c in centroids)
    lons = ",".join(str(c["lon"]) for c in centroids)

    past_days = 10
    url = (
        f"https://api.open-meteo.com/v1/forecast?"
        f"latitude={lats}&longitude={lons}"
        f"&daily=temperature_2m_mean,precipitation_sum"
        f"&past_days={past_days}"
        f"&timezone=Africa/Kigali"
        f"&forecast_days=1"
    )

    try:
        req = urllib.request.Request(url, headers={"User-Agent": "mundi.ai/1.0"})
        with urllib.request.urlopen(req, timeout=30) as resp:
            data = json.loads(resp.read().decode())
    except Exception as e:
        logger.error("Open-Meteo request failed: %s", e)
        return {}

    # Single location returns dict, multi returns list
    if isinstance(data, dict) and "daily" in data:
        data = [data]

    results: Dict[int, float] = {}
    for idx, centroid in enumerate(centroids):
        fid = centroid["id"]
        if idx >= len(data):
            break

        daily = data[idx].get("daily", {})

        if metric_key == "rainfall_mm":
            precip = daily.get("precipitation_sum", [])
            vals = [v for v in precip if v is not None]
            results[fid] = round(sum(vals), 1) if vals else 0.0
        elif metric_key == "temp_mean":
            temps = daily.get("temperature_2m_mean", [])
            vals = [v for v in temps if v is not None]
            results[fid] = round(sum(vals) / len(vals), 1) if vals else 0.0

    return results


# ---------------------------------------------------------------------------
# NDVI computation (Sentinel Hub, reuses sentinel_hub_service pattern)
# ---------------------------------------------------------------------------

async def _compute_ndvi_metric(
    features: List[Dict[str, Any]],
) -> Dict[int, float]:
    """Compute mean NDVI for each feature via Sentinel Hub Statistical API.

    Uses asyncio.Semaphore to limit concurrency to 3 concurrent requests.
    Returns {feature_id: ndvi_mean}.
    """
    from src.services.sentinel_hub_service import (
        get_sentinel_hub_service,
        AGRI_INDEX_NAMES,
    )

    sh = get_sentinel_hub_service()
    if sh is None:
        logger.warning("Sentinel Hub service not available, skipping NDVI")
        return {}

    date_to = datetime.utcnow().strftime("%Y-%m-%d")
    date_from = (datetime.utcnow() - timedelta(days=30)).strftime("%Y-%m-%d")

    semaphore = asyncio.Semaphore(3)
    results: Dict[int, float] = {}

    async def _fetch_one(feat: Dict[str, Any]) -> None:
        fid = feat["id"]
        async with semaphore:
            try:
                # get_agri_stats is synchronous — run in thread
                stats = await asyncio.get_event_loop().run_in_executor(
                    None,
                    lambda: sh.get_agri_stats(
                        geometry=feat["geom"],
                        date_from=date_from,
                        date_to=date_to,
                    ),
                )
                intervals = stats.get("intervals", [])
                ndvi_means = [
                    iv["ndvi"]["mean"]
                    for iv in intervals
                    if "ndvi" in iv and iv["ndvi"].get("valid_pixels", 0) > 0
                ]
                if ndvi_means:
                    results[fid] = round(float(np.mean(ndvi_means)), 4)
                else:
                    results[fid] = 0.0
            except Exception as e:
                logger.warning("NDVI computation failed for feature %d: %s", fid, e)
                results[fid] = 0.0

    tasks = [_fetch_one(feat) for feat in features]
    await asyncio.gather(*tasks)

    return results


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

async def compute_metric(
    metric_key: str,
    features: List[Dict[str, Any]],
) -> Dict[int, float]:
    """Compute a metric for a list of features.

    Args:
        metric_key: Key from AVAILABLE_METRICS
        features: List of dicts with 'id' (int) and 'geom' (GeoJSON dict)

    Returns:
        Dict mapping feature_id → computed value
    """
    if metric_key not in AVAILABLE_METRICS:
        raise ValueError(f"Unknown metric: {metric_key}")

    if not features:
        return {}

    if metric_key in _LULC_CLASS_MAP:
        # LULC metrics are CPU-bound — run in executor
        loop = asyncio.get_event_loop()
        return await loop.run_in_executor(
            None, _compute_lulc_metrics, features, metric_key
        )
    elif metric_key in ("rainfall_mm", "temp_mean"):
        loop = asyncio.get_event_loop()
        return await loop.run_in_executor(
            None, _compute_weather_metric, features, metric_key
        )
    elif metric_key == "ndvi_mean":
        return await _compute_ndvi_metric(features)
    else:
        raise ValueError(f"No compute function for metric: {metric_key}")
