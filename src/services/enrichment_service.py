"""On-the-fly metric computation for layer features (choropleth enrichment).

Computes per-feature statistics (land cover percentages, weather, vegetation
indices) and stores them in the app DB ``layer_enrichments`` table so they can
be injected into PostGIS tile queries as a VALUES CTE.
"""

from __future__ import annotations

import asyncio
import gc
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
    "evi_mean": MetricDefinition(
        key="evi_mean",
        label="EVI Mean",
        category="Vegetation",
        description="Enhanced Vegetation Index from Sentinel-2 (last 30 days)",
        source="Sentinel Hub Statistical API",
    ),
    "ndwi_mean": MetricDefinition(
        key="ndwi_mean",
        label="NDWI Mean",
        category="Vegetation",
        description="Normalized Difference Water Index from Sentinel-2 (last 30 days)",
        source="Sentinel Hub Statistical API",
    ),
    "savi_mean": MetricDefinition(
        key="savi_mean",
        label="SAVI Mean",
        category="Vegetation",
        description="Soil-Adjusted Vegetation Index from Sentinel-2 (last 30 days)",
        source="Sentinel Hub Statistical API",
    ),
    "ndre_mean": MetricDefinition(
        key="ndre_mean",
        label="NDRE Mean",
        category="Vegetation",
        description="Normalized Difference Red Edge Index from Sentinel-2 (last 30 days)",
        source="Sentinel Hub Statistical API",
    ),
    "ndbi_mean": MetricDefinition(
        key="ndbi_mean",
        label="NDBI Mean",
        category="Vegetation",
        description="Normalized Difference Built-up Index from Sentinel-2 (last 30 days)",
        source="Sentinel Hub Statistical API",
    ),
    "ch4_emissions": MetricDefinition(
        key="ch4_emissions",
        label="CH4 (tonnes/yr)",
        category="Emissions",
        description="Methane emissions from agriculture (EDGAR v8.0)",
        source="EDGAR",
    ),
    "n2o_emissions": MetricDefinition(
        key="n2o_emissions",
        label="N2O (tonnes/yr)",
        category="Emissions",
        description="Nitrous oxide emissions from agriculture (EDGAR v8.0)",
        source="EDGAR",
    ),
    "co2_emissions": MetricDefinition(
        key="co2_emissions",
        label="CO2 (tonnes/yr)",
        category="Emissions",
        description="Carbon dioxide emissions from agriculture (EDGAR v8.0)",
        source="EDGAR",
    ),
    "soil_ph": MetricDefinition(
        key="soil_ph",
        label="Soil pH",
        category="Soil",
        description="Soil acidity/alkalinity (iSDAsoil 30m, 0-20cm)",
        source="iSDAsoil",
    ),
    "soil_nitrogen": MetricDefinition(
        key="soil_nitrogen",
        label="Nitrogen (g/kg)",
        category="Soil",
        description="Total nitrogen content (iSDAsoil 30m, 0-20cm)",
        source="iSDAsoil",
    ),
    "soil_phosphorus": MetricDefinition(
        key="soil_phosphorus",
        label="Phosphorus (ppm)",
        category="Soil",
        description="Extractable phosphorus (iSDAsoil 30m, 0-20cm)",
        source="iSDAsoil",
    ),
    "soil_potassium": MetricDefinition(
        key="soil_potassium",
        label="Potassium (ppm)",
        category="Soil",
        description="Extractable potassium (iSDAsoil 30m, 0-20cm)",
        source="iSDAsoil",
    ),
    "soil_organic_carbon": MetricDefinition(
        key="soil_organic_carbon",
        label="Organic Carbon (g/kg)",
        category="Soil",
        description="Soil organic carbon content (iSDAsoil 30m, 0-20cm)",
        source="iSDAsoil",
    ),
    "soil_clay": MetricDefinition(
        key="soil_clay",
        label="Clay Content (%)",
        category="Soil",
        description="Clay fraction of soil (iSDAsoil 30m, 0-20cm)",
        source="iSDAsoil",
    ),
    "rainfall_mm": MetricDefinition(
        key="rainfall_mm",
        label="Rainfall (mm)",
        category="Weather",
        description="Forecast precipitation over next 3 days (NOAA HGEFS ensemble mean)",
        source="NOAA HGEFS",
    ),
    "temp_mean": MetricDefinition(
        key="temp_mean",
        label="Temperature (C)",
        category="Weather",
        description="Forecast mean temperature over next 3 days (NOAA HGEFS ensemble mean)",
        source="NOAA HGEFS",
    ),
    "wind_speed_ms": MetricDefinition(
        key="wind_speed_ms",
        label="Wind Speed (m/s)",
        category="Weather",
        description="Forecast mean wind speed over next 3 days (NOAA HGEFS ensemble mean)",
        source="NOAA HGEFS",
    ),
    "yield_forecast_tha": MetricDefinition(
        key="yield_forecast_tha",
        label="Yield Forecast (t/ha)",
        category="Agriculture",
        description="DSSAT crop yield forecast with Sentinel-2 data assimilation (current season)",
        source="DSSAT + Sentinel-2",
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

    # Filter out features with NULL/empty geometries
    valid_features = [f for f in features if f.get("geom")]
    if not valid_features:
        return {f["id"]: 0.0 for f in features}

    # Compute bounding box of all features
    all_bounds = []
    for feat in valid_features:
        geom = shape(feat["geom"])
        if geom.is_empty:
            continue
        all_bounds.append(geom.bounds)

    if not all_bounds:
        return {f["id"]: 0.0 for f in features}

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
    del mosaic  # Free the full mosaic array immediately
    h, w = data.shape
    transform = from_bounds(west, south, east, north, w, h)

    # Pre-fill results with 0.0 for features with NULL/empty geometry
    results: Dict[int, float] = {f["id"]: 0.0 for f in features if f not in valid_features}

    for feat in valid_features:
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

    del data  # Free raster array
    gc.collect()  # Reclaim memory from large numpy arrays
    return results


def compute_all_lulc_metrics(
    features: List[Dict[str, Any]],
) -> Dict[str, Dict[int, float]]:
    """Compute all 4 land cover percentages in one COG read.

    Reads COGs once for the full layer extent, then masks each feature
    individually, computing cropland/forest/built/rangeland in a single pass.

    Returns:
        Dict mapping metric_key → {feature_id: percentage}
    """
    import rasterio
    from rasterio.enums import Resampling
    from rasterio.features import geometry_mask
    from rasterio.merge import merge
    from rasterio.transform import from_bounds
    from rasterio.vrt import WarpedVRT
    from shapely.geometry import shape

    from src.worldcover import get_rwanda_tile_urls

    # Filter out features with NULL/empty geometries
    valid_features = [f for f in features if f.get("geom")]

    zero_results: Dict[str, Dict[int, float]] = {key: {} for key in _LULC_CLASS_MAP}
    if not valid_features:
        for f in features:
            for key in _LULC_CLASS_MAP:
                zero_results[key][f["id"]] = 0.0
        return zero_results

    # Compute bounding box of all features
    all_bounds = []
    for feat in valid_features:
        geom = shape(feat["geom"])
        if geom.is_empty:
            continue
        all_bounds.append(geom.bounds)

    if not all_bounds:
        for f in features:
            for key in _LULC_CLASS_MAP:
                zero_results[key][f["id"]] = 0.0
        return zero_results

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
    del mosaic  # Free the full mosaic array immediately
    h, w = data.shape
    transform = from_bounds(west, south, east, north, w, h)

    # Pre-fill 0.0 for features with NULL/empty geometry
    results: Dict[str, Dict[int, float]] = {key: {} for key in _LULC_CLASS_MAP}
    for f in features:
        if f not in valid_features:
            for key in _LULC_CLASS_MAP:
                results[key][f["id"]] = 0.0

    for feat in valid_features:
        fid = feat["id"]
        geom_dict = feat["geom"]
        try:
            mask = geometry_mask(
                [geom_dict],
                out_shape=(h, w),
                transform=transform,
                invert=True,
            )
            masked_pixels = data[mask]
            total = int(np.count_nonzero(masked_pixels > 0))
            for key, class_val in _LULC_CLASS_MAP.items():
                if total == 0:
                    results[key][fid] = 0.0
                else:
                    count = int(np.count_nonzero(masked_pixels == class_val))
                    results[key][fid] = round(count / total * 100, 2)
        except Exception as e:
            logger.warning("LULC mask failed for feature %d: %s", fid, e)
            for key in _LULC_CLASS_MAP:
                results[key][fid] = 0.0

    del data  # Free raster array
    gc.collect()  # Reclaim memory from large numpy arrays
    return results


# ---------------------------------------------------------------------------
# Weather computation (NOAA HGEFS ensemble via NOMADS)
# ---------------------------------------------------------------------------

def _compute_weather_metric(
    features: List[Dict[str, Any]],
    metric_key: str,
) -> Dict[int, float]:
    """Compute weather metric for each feature centroid via NOAA HGEFS.

    Uses the NOAA forecast service with grid-cell caching — nearby features
    sharing the same 0.25° grid cell reuse the same forecast (one NOMADS
    fetch per unique grid cell, not per feature).
    Returns {feature_id: value}.
    """
    from shapely.geometry import shape
    from src.services.forecast_service import get_farm_forecast

    centroids = []
    for feat in features:
        if not feat.get("geom"):
            continue
        geom = shape(feat["geom"])
        if geom.is_empty:
            continue
        c = geom.centroid
        centroids.append({"id": feat["id"], "lat": round(c.y, 4), "lon": round(c.x, 4)})

    if not centroids:
        return {f["id"]: 0.0 for f in features}

    results: Dict[int, float] = {}

    for centroid in centroids:
        fid = centroid["id"]
        try:
            forecast = get_farm_forecast(
                centroid["lat"], centroid["lon"],
                forecast_days=3, model="HGEFS",
            )
            daily = forecast.get("daily", [])
            if not daily:
                results[fid] = 0.0
                continue

            if metric_key == "rainfall_mm":
                vals = []
                for d in daily:
                    p = d.get("precipitation_mm")
                    if p is not None:
                        vals.append(p["mean"] if isinstance(p, dict) else p)
                results[fid] = round(sum(vals), 1) if vals else 0.0
            elif metric_key == "temp_mean":
                vals = []
                for d in daily:
                    t = d.get("temperature_mean")
                    if t is not None:
                        vals.append(t["mean"] if isinstance(t, dict) else t)
                results[fid] = round(sum(vals) / len(vals), 1) if vals else 0.0
            elif metric_key == "wind_speed_ms":
                vals = []
                for d in daily:
                    w = d.get("wind_speed_ms")
                    if w is not None:
                        vals.append(w["mean"] if isinstance(w, dict) else w)
                results[fid] = round(sum(vals) / len(vals), 1) if vals else 0.0
        except Exception as e:
            logger.warning("NOAA forecast failed for feature %d: %s", fid, e)
            results[fid] = 0.0

    return results


# ---------------------------------------------------------------------------
# Vegetation index computation (Sentinel Hub, reuses sentinel_hub_service)
# ---------------------------------------------------------------------------

# Map metric keys to the index name returned by get_agri_stats()
_AGRI_INDEX_MAP = {
    "ndvi_mean": "ndvi",
    "evi_mean": "evi",
    "ndwi_mean": "ndwi",
    "savi_mean": "savi",
    "ndre_mean": "ndre",
    "ndbi_mean": "ndbi",
}


async def _compute_agri_index_metric(
    features: List[Dict[str, Any]],
    index_name: str,
) -> Dict[int, float]:
    """Compute mean of a vegetation index for each feature via Sentinel Hub.

    Uses asyncio.Semaphore to limit concurrency to 3 concurrent requests.
    The get_agri_stats() call returns all 6 indices in one request — we
    just extract the one we need.

    Args:
        features: Feature dicts with 'id' and 'geom'.
        index_name: One of ndvi, evi, ndwi, savi, ndre, ndbi.

    Returns:
        {feature_id: mean_value}
    """
    from src.services.sentinel_hub_service import get_sentinel_hub_service

    sh = get_sentinel_hub_service()
    if sh is None:
        logger.warning("Sentinel Hub service not available, skipping %s", index_name)
        return {}

    date_to = datetime.utcnow().strftime("%Y-%m-%d")
    date_from = (datetime.utcnow() - timedelta(days=30)).strftime("%Y-%m-%d")

    semaphore = asyncio.Semaphore(3)
    results: Dict[int, float] = {}

    async def _fetch_one(feat: Dict[str, Any]) -> None:
        fid = feat["id"]
        async with semaphore:
            try:
                stats = await asyncio.get_event_loop().run_in_executor(
                    None,
                    lambda: sh.get_agri_stats(
                        geometry=feat["geom"],
                        date_from=date_from,
                        date_to=date_to,
                    ),
                )
                intervals = stats.get("intervals", [])
                means = [
                    iv[index_name]["mean"]
                    for iv in intervals
                    if index_name in iv and iv[index_name].get("valid_pixels", 0) > 0
                ]
                if means:
                    results[fid] = round(float(np.mean(means)), 4)
                else:
                    results[fid] = 0.0
            except Exception as e:
                logger.warning("%s computation failed for feature %d: %s", index_name, fid, e)
                results[fid] = 0.0

    tasks = [_fetch_one(feat) for feat in features]
    await asyncio.gather(*tasks)

    return results


# ---------------------------------------------------------------------------
# Emissions computation (EDGAR)
# ---------------------------------------------------------------------------

# Map metric keys to EDGAR emission types
_EMISSIONS_MAP = {
    "ch4_emissions": "CH4",
    "n2o_emissions": "N2O",
    "co2_emissions": "CO2",
}


def _compute_emissions_metric(
    features: List[Dict[str, Any]],
    emission_type: str,
) -> Dict[int, float]:
    """Compute total emissions for each feature centroid via EDGAR grid lookup.

    Downloads the EDGAR grid for the latest available year (2022) and sums
    across all agriculture sectors for the given emission type.

    Args:
        features: Feature dicts with 'id' and 'geom'.
        emission_type: One of CH4, N2O, CO2.

    Returns:
        {feature_id: total_tonnes_per_year}
    """
    from shapely.geometry import shape

    from src.services.emissions_service import VALID_COMBOS, get_emissions_service

    svc = get_emissions_service()
    if svc is None:
        logger.warning("Emissions service not available")
        return {}

    year = 2022  # latest EDGAR year
    sectors = VALID_COMBOS.get(emission_type, [])

    # Download grids for all valid sectors concurrently and sum them
    from concurrent.futures import ThreadPoolExecutor

    combined_grid = None
    grid_lats = None
    grid_lons = None

    def _download_sector(sector: str):
        return svc.download_edgar_gridmap(emission_type, sector, year)

    with ThreadPoolExecutor(max_workers=min(len(sectors), 4)) as pool:
        grid_results = list(pool.map(_download_sector, sectors))

    for i, grid_data in enumerate(grid_results):
        if "error" in grid_data:
            logger.warning("EDGAR grid %s/%s: %s", emission_type, sectors[i], grid_data["error"])
            continue
        values = grid_data.get("values")
        if values is None:
            continue
        if combined_grid is None:
            combined_grid = np.copy(values).astype(np.float64)
            grid_lats = grid_data["lats"]
            grid_lons = grid_data["lons"]
        else:
            combined_grid += values.astype(np.float64)

    if combined_grid is None or grid_lats is None or grid_lons is None:
        logger.warning("No EDGAR data available for %s", emission_type)
        return {feat["id"]: 0.0 for feat in features}

    results: Dict[int, float] = {}
    for feat in features:
        fid = feat["id"]
        try:
            geom = shape(feat["geom"])
            c = geom.centroid
            # Find nearest grid cell
            lat_idx = int(np.argmin(np.abs(grid_lats - c.y)))
            lon_idx = int(np.argmin(np.abs(grid_lons - c.x)))
            val = float(combined_grid[lat_idx, lon_idx])
            results[fid] = round(val, 2) if np.isfinite(val) else 0.0
        except Exception as e:
            logger.warning("Emissions lookup failed for feature %d: %s", fid, e)
            results[fid] = 0.0

    # Free large grid arrays
    del combined_grid, grid_lats, grid_lons
    gc.collect()
    return results


# ---------------------------------------------------------------------------
# Soil property computation (iSDAsoil)
# ---------------------------------------------------------------------------

# Map metric keys to iSDAsoil property names
_SOIL_PROPERTY_MAP = {
    "soil_ph": "ph",
    "soil_nitrogen": "nitrogen_total",
    "soil_phosphorus": "phosphorous_extractable",
    "soil_potassium": "potassium_extractable",
    "soil_organic_carbon": "carbon_organic",
    "soil_clay": "clay_content",
}


def _compute_soil_metric(
    features: List[Dict[str, Any]],
    soil_property: str,
) -> Dict[int, float]:
    """Compute a soil property value for each feature centroid via iSDAsoil COGs.

    Reads the COG **once** for the bounding box of all centroids, then samples
    each centroid from the in-memory array. This turns N HTTP range-requests
    into a single read — critical for large layers (14K+ features).

    Args:
        features: Feature dicts with 'id' and 'geom'.
        soil_property: iSDAsoil property name (e.g. 'ph', 'nitrogen_total').

    Returns:
        {feature_id: value}
    """
    import rasterio
    from pyproj import Transformer
    from rasterio.env import Env as RasterioEnv
    from rasterio.windows import from_bounds
    from shapely.geometry import shape

    from src.services.isdasoil_service import SOIL_PROPERTIES, _cog_url

    prop_info = SOIL_PROPERTIES.get(soil_property)
    if prop_info is None:
        logger.warning("Unknown soil property: %s", soil_property)
        return {feat["id"]: 0.0 for feat in features}

    url = _cog_url(soil_property)
    transformer = Transformer.from_crs("EPSG:4326", "EPSG:3857", always_xy=True)
    transform_fn = prop_info["transform"]
    depth_band = 0  # 0-20 cm
    buffer_m = 300.0  # padding around bounding box

    # Pre-compute centroids in EPSG:3857 (COG native CRS)
    centroids: List[tuple] = []  # (fid, cx_3857, cy_3857)
    for feat in features:
        try:
            geom = shape(feat["geom"])
            c = geom.centroid
            cx, cy = transformer.transform(c.x, c.y)
            centroids.append((feat["id"], cx, cy))
        except Exception as e:
            logger.warning("Bad geometry for feature %d: %s", feat["id"], e)
            centroids.append((feat["id"], None, None))

    valid_centroids = [(fid, cx, cy) for fid, cx, cy in centroids if cx is not None]
    if not valid_centroids:
        return {feat["id"]: 0.0 for feat in features}

    # Bounding box in EPSG:3857
    all_cx = [cx for _, cx, _ in valid_centroids]
    all_cy = [cy for _, _, cy in valid_centroids]
    bbox_west = min(all_cx) - buffer_m
    bbox_south = min(all_cy) - buffer_m
    bbox_east = max(all_cx) + buffer_m
    bbox_north = max(all_cy) + buffer_m

    results: Dict[int, float] = {}

    try:
        with RasterioEnv(
            GDAL_DISABLE_READDIR_ON_OPEN="EMPTY_DIR",
            CPL_VSIL_CURL_ALLOWED_EXTENSIONS=".tif",
            GDAL_HTTP_MERGE_CONSECUTIVE_RANGES="YES",
        ):
            with rasterio.open(url) as src:
                # Single read for the entire bounding box
                window = from_bounds(bbox_west, bbox_south, bbox_east, bbox_north, src.transform)
                data = src.read(window=window)  # (bands, h, w)
                win_transform = src.window_transform(window)

                band = data[depth_band].astype(np.float64)

                # Sample each centroid from the in-memory array
                for fid, cx, cy in valid_centroids:
                    try:
                        # Convert 3857 coords to pixel coords within the window
                        col, row = ~win_transform * (cx, cy)
                        col, row = int(round(col)), int(round(row))
                        if 0 <= row < band.shape[0] and 0 <= col < band.shape[1]:
                            raw_val = band[row, col]
                            if raw_val > 0:
                                results[fid] = round(float(transform_fn(raw_val)), 2)
                            else:
                                results[fid] = 0.0
                        else:
                            results[fid] = 0.0
                    except Exception as e:
                        logger.warning("Soil sample failed for feature %d: %s", fid, e)
                        results[fid] = 0.0

        del data, band
        gc.collect()

    except Exception as e:
        logger.error("Failed to read soil COG %s: %s", url, e)
        return {feat["id"]: 0.0 for feat in features}

    # Fill in features with bad geometry
    for fid, cx, cy in centroids:
        if fid not in results:
            results[fid] = 0.0

    return results


# ---------------------------------------------------------------------------
# Yield forecast computation (DSSAT + Sentinel-2 assimilation)
# ---------------------------------------------------------------------------

def _compute_yield_forecast(
    features: List[Dict[str, Any]],
    crop_type: str = "maize",
    season: Optional[str] = None,
) -> Dict[int, float]:
    """Compute DSSAT yield forecast for each feature centroid.

    Auto-detects current season from date if not specified.
    Returns {feature_id: yield_tha}.
    """
    from shapely.geometry import shape

    from src.services.dssat_service import run_dssat_with_assimilation

    results: Dict[int, float] = {}
    for feat in features:
        fid = feat["id"]
        try:
            geom = shape(feat["geom"])
            c = geom.centroid
            result = run_dssat_with_assimilation(
                lat=c.y,
                lon=c.x,
                crop_type=crop_type,
                season=season,
                geom=feat["geom"],
            )
            results[fid] = result.get("yield_tha", 0.0)
        except Exception as e:
            logger.warning("Yield forecast failed for feature %d: %s", fid, e)
            results[fid] = 0.0

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
    elif metric_key in ("rainfall_mm", "temp_mean", "wind_speed_ms"):
        loop = asyncio.get_event_loop()
        return await loop.run_in_executor(
            None, _compute_weather_metric, features, metric_key
        )
    elif metric_key in _AGRI_INDEX_MAP:
        index_name = _AGRI_INDEX_MAP[metric_key]
        return await _compute_agri_index_metric(features, index_name)
    elif metric_key in _EMISSIONS_MAP:
        emission_type = _EMISSIONS_MAP[metric_key]
        loop = asyncio.get_event_loop()
        return await loop.run_in_executor(
            None, _compute_emissions_metric, features, emission_type
        )
    elif metric_key in _SOIL_PROPERTY_MAP:
        soil_property = _SOIL_PROPERTY_MAP[metric_key]
        loop = asyncio.get_event_loop()
        return await loop.run_in_executor(
            None, _compute_soil_metric, features, soil_property
        )
    elif metric_key == "yield_forecast_tha":
        loop = asyncio.get_event_loop()
        return await loop.run_in_executor(
            None, _compute_yield_forecast, features
        )
    else:
        raise ValueError(f"No compute function for metric: {metric_key}")
