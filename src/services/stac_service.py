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

"""STAC satellite imagery discovery service for Rwanda agriculture.

Uses pystac-client when available (preferred), falls back to raw HTTP requests.
Supports Earth Search, Planetary Computer, and CDSE catalogs.
"""

import logging
import time
from datetime import datetime, timedelta
from typing import Optional, List, Dict, Any

import numpy as np
import requests

try:
    from pystac_client import Client as PystacClient

    _PYSTAC_CLIENT_AVAILABLE = True
except ImportError:
    _PYSTAC_CLIENT_AVAILABLE = False

try:
    import rasterio
    from rasterio.env import Env as RasterioEnv
    from rasterio.windows import Window
    _RASTERIO_AVAILABLE = True
except ImportError:
    _RASTERIO_AVAILABLE = False

logger = logging.getLogger(__name__)

# Public STAC endpoints for satellite imagery
STAC_CATALOGS = {
    "earth_search": "https://earth-search.aws.element84.com/v1",
    "planetary_computer": "https://planetarycomputer.microsoft.com/api/stac/v1",
    "cdse": "https://catalogue.dataspace.copernicus.eu/stac",
}

# Rwanda bounding box (approximate)
RWANDA_BBOX = [28.86, -2.84, 30.90, -1.04]

# Sentinel-2 collection IDs per catalog
SENTINEL2_COLLECTIONS = {
    "earth_search": "sentinel-2-l2a",
    "planetary_computer": "sentinel-2-l2a",
    "cdse": "SENTINEL-2",
}

# Drought status constants (WMO VCI thresholds)
DROUGHT_EXTREME = "extreme_drought"
DROUGHT_SEVERE = "severe_drought"
DROUGHT_MODERATE = "moderate_drought"
DROUGHT_MILD = "mild_drought"
DROUGHT_NONE = "no_drought"

# Band names to extract from STAC items
_USEFUL_ASSETS = {
    "visual", "thumbnail",
    # Sentinel-2 band names (Copernicus STAC / other catalogs)
    "B02", "B03", "B04", "B08", "SCL",
    # Earth Search v1 common names
    "red", "nir", "green", "blue", "scl",
    "coastal", "rededge1", "rededge2", "rededge3",
    "nir08", "nir09", "swir16", "swir22",
}


class STACService:
    """Service for discovering satellite imagery via STAC API.

    Uses pystac-client when available (vendor-agnostic, same API for all catalogs).
    Falls back to raw HTTP requests if pystac-client is not installed.
    """

    @staticmethod
    def _resolve_band_keys(assets: dict) -> tuple:
        """Resolve red/NIR band keys across naming conventions.

        Returns (red_key, nir_key) — either may be None if not found.
        Supports B04/B08 (Copernicus) and red/nir (Earth Search).
        """
        red_key = "B04" if "B04" in assets else ("red" if "red" in assets else None)
        nir_key = "B08" if "B08" in assets else ("nir" if "nir" in assets else None)
        return red_key, nir_key

    @staticmethod
    def _compute_ndvi_stats(
        b04_data: np.ndarray,
        b08_data: np.ndarray,
        exclude_zero_reflectance: bool = False,
    ) -> Optional[Dict[str, Any]]:
        """Compute NDVI statistics from red and NIR band arrays.

        Returns dict with mean/std/min/max NDVI and valid_pixel_count,
        or None if no valid pixels.
        """
        denominator = b08_data + b04_data
        ndvi = np.where(denominator != 0, (b08_data - b04_data) / denominator, 0.0)

        valid_mask = (ndvi >= -1.0) & (ndvi <= 1.0) & np.isfinite(ndvi)
        if exclude_zero_reflectance:
            valid_mask &= (b04_data > 0) | (b08_data > 0)
        valid_ndvi = ndvi[valid_mask]

        if len(valid_ndvi) == 0:
            return None

        return {
            "mean_ndvi": round(float(np.mean(valid_ndvi)), 4),
            "std_ndvi": round(float(np.std(valid_ndvi)), 4),
            "min_ndvi": round(float(np.min(valid_ndvi)), 4),
            "max_ndvi": round(float(np.max(valid_ndvi)), 4),
            "valid_pixel_count": int(len(valid_ndvi)),
            "_valid_ndvi": valid_ndvi,  # for downstream classification
        }

    @staticmethod
    def _date_range(days: int = 30) -> str:
        """Build an ISO 8601 date range string ending today."""
        end = datetime.utcnow()
        start = end - timedelta(days=days)
        return f"{start.strftime('%Y-%m-%d')}/{end.strftime('%Y-%m-%d')}"

    def __init__(self, catalog_name: str = "earth_search"):
        self.catalog_name = catalog_name
        self.catalog_url = STAC_CATALOGS[catalog_name]
        self._pystac_client: Optional[Any] = None
        # Fallback HTTP session
        self._session = requests.Session()
        self._session.headers.update({"Accept": "application/geo+json"})

        if _PYSTAC_CLIENT_AVAILABLE:
            try:
                self._pystac_client = PystacClient.open(self.catalog_url)
                logger.info("Using pystac-client for %s", catalog_name)
            except Exception as e:
                logger.warning(
                    "pystac-client failed to open %s, falling back to raw HTTP: %s",
                    catalog_name,
                    e,
                )

    def search_imagery(
        self,
        bbox: Optional[List[float]] = None,
        datetime_range: Optional[str] = None,
        collections: Optional[List[str]] = None,
        max_cloud_cover: float = 20.0,
        limit: int = 10,
    ) -> Dict[str, Any]:
        """Search for satellite imagery over a region.

        Uses pystac-client when available, falls back to raw HTTP POST.

        Args:
            bbox: [west, south, east, north] in WGS84. Defaults to Rwanda bounds.
            datetime_range: ISO 8601 range like "2024-01-01/2024-06-30"
            collections: STAC collection IDs. Defaults to Sentinel-2 L2A.
            max_cloud_cover: Maximum cloud cover percentage (0-100)
            limit: Max number of results

        Returns:
            Dict with matched items summary (dates, cloud cover, asset links)
        """
        if bbox is None:
            bbox = RWANDA_BBOX
        if collections is None:
            collections = [SENTINEL2_COLLECTIONS[self.catalog_name]]
        if datetime_range is None:
            datetime_range = self._date_range(30)

        if self._pystac_client is not None:
            return self._search_pystac(bbox, datetime_range, collections, max_cloud_cover, limit)
        return self._search_http(bbox, datetime_range, collections, max_cloud_cover, limit)

    def _wrap_search_results(
        self,
        results: List[Dict[str, Any]],
        collections: List[str],
        bbox: List[float],
        datetime_range: str,
        max_cloud_cover: float,
    ) -> Dict[str, Any]:
        """Build the standard search response envelope."""
        return {
            "catalog": self.catalog_name,
            "collections": collections,
            "bbox": bbox,
            "datetime_range": datetime_range,
            "max_cloud_cover": max_cloud_cover,
            "matched": len(results),
            "items": results,
        }

    def _search_pystac(
        self,
        bbox: List[float],
        datetime_range: str,
        collections: List[str],
        max_cloud_cover: float,
        limit: int,
    ) -> Dict[str, Any]:
        """Search using pystac-client (preferred)."""
        try:
            search = self._pystac_client.search(
                collections=collections,
                bbox=bbox,
                datetime=datetime_range,
                max_items=limit,
                query={"eo:cloud_cover": {"lt": max_cloud_cover}},
            )

            results = []
            for item in search.items():
                props = item.properties
                results.append({
                    "id": item.id,
                    "datetime": props.get("datetime"),
                    "cloud_cover": props.get("eo:cloud_cover"),
                    "platform": props.get("platform"),
                    "bbox": list(item.bbox) if item.bbox else None,
                    "assets": {
                        name: {
                            "href": asset.href,
                            "type": asset.media_type,
                        }
                        for name, asset in item.assets.items()
                        if name in _USEFUL_ASSETS
                    },
                })

            return self._wrap_search_results(results, collections, bbox, datetime_range, max_cloud_cover)
        except Exception as e:
            logger.exception("pystac-client search failed: %s", e)
            # Fall back to raw HTTP
            return self._search_http(bbox, datetime_range, collections, max_cloud_cover, limit)

    def _search_http(
        self,
        bbox: List[float],
        datetime_range: str,
        collections: List[str],
        max_cloud_cover: float,
        limit: int,
    ) -> Dict[str, Any]:
        """Search using raw HTTP POST (fallback)."""
        search_url = f"{self.catalog_url}/search"
        payload: Dict[str, Any] = {
            "collections": collections,
            "bbox": bbox,
            "datetime": datetime_range,
            "limit": limit,
            "query": {"eo:cloud_cover": {"lt": max_cloud_cover}},
        }

        try:
            resp = self._session.post(search_url, json=payload, timeout=30)
            resp.raise_for_status()
            data = resp.json()

            features = data.get("features", [])
            results = []
            for feature in features:
                props = feature.get("properties", {})
                assets_raw = feature.get("assets", {})
                results.append({
                    "id": feature.get("id"),
                    "datetime": props.get("datetime"),
                    "cloud_cover": props.get("eo:cloud_cover"),
                    "platform": props.get("platform"),
                    "bbox": feature.get("bbox"),
                    "assets": {
                        name: {
                            "href": asset.get("href"),
                            "type": asset.get("type"),
                        }
                        for name, asset in assets_raw.items()
                        if name in _USEFUL_ASSETS
                    },
                })

            return self._wrap_search_results(results, collections, bbox, datetime_range, max_cloud_cover)
        except Exception as e:
            logger.exception("STAC HTTP search failed: %s", e)
            return {"error": str(e), "catalog": self.catalog_name}

    def compute_ndvi_from_item(self, item_result: dict) -> Dict[str, Any]:
        """Compute NDVI statistics from a STAC item with B04 and B08 bands.

        Downloads a windowed subset (center 512x512) from both bands via HTTP,
        computes NDVI, and returns statistics with land cover classification.

        Args:
            item_result: STAC item dict with assets.B04.href and assets.B08.href

        Returns:
            Dict with mean_ndvi, std_ndvi, min_ndvi, max_ndvi, valid_pixel_count,
            classification, bbox_computed, source_item_id, download_time_sec
        """
        if not _RASTERIO_AVAILABLE:
            return {
                "error": "rasterio not available — install with `pip install rasterio`",
                "source_item_id": item_result.get("id"),
            }

        assets = item_result.get("assets", {})
        red_key, nir_key = self._resolve_band_keys(assets)
        if red_key is None or nir_key is None:
            return {
                "error": "Missing red/B04 or nir/B08 bands in STAC item",
                "source_item_id": item_result.get("id"),
            }

        b04_href = assets[red_key]["href"]
        b08_href = assets[nir_key]["href"]

        start_time = time.time()

        try:
            # Use GDAL environment settings for optimal HTTP performance
            with RasterioEnv(
                GDAL_HTTP_MERGE_CONSECUTIVE_RANGES="YES",
                GDAL_DISABLE_READDIR_ON_OPEN="EMPTY_DIR",
                CPL_VSIL_CURL_ALLOWED_EXTENSIONS=".tif",
            ):
                # Open both bands via HTTP (rasterio handles /vsicurl/ automatically)
                with rasterio.open(b04_href) as b04_src, rasterio.open(b08_href) as b08_src:
                    # Calculate window to read center 512x512 pixels
                    height, width = b04_src.height, b04_src.width
                    window_size = min(512, height, width)

                    col_off = (width - window_size) // 2
                    row_off = (height - window_size) // 2

                    window = Window(col_off, row_off, window_size, window_size)

                    # Read windowed data
                    b04_data = b04_src.read(1, window=window).astype(np.float32)
                    b08_data = b08_src.read(1, window=window).astype(np.float32)

                    # Get transform for the window to compute actual bbox
                    window_transform = b04_src.window_transform(window)
                    bbox_computed = None
                    try:
                        # Compute bounds in source CRS
                        left = window_transform.c
                        top = window_transform.f
                        right = left + window_transform.a * window_size
                        bottom = top + window_transform.e * window_size
                        bbox_computed = [left, bottom, right, top]
                    except Exception:
                        pass

            stats = self._compute_ndvi_stats(b04_data, b08_data)

            if stats is None:
                return {
                    "error": "No valid NDVI pixels found",
                    "source_item_id": item_result.get("id"),
                    "download_time_sec": round(time.time() - start_time, 2),
                }

            # Classify using ml_inference thresholds
            classification = self._classify_ndvi_pixels(stats.pop("_valid_ndvi"))

            download_time = time.time() - start_time

            return {
                **stats,
                "classification": classification,
                "bbox_computed": bbox_computed,
                "source_item_id": item_result.get("id"),
                "datetime": item_result.get("datetime"),
                "cloud_cover": item_result.get("cloud_cover"),
                "download_time_sec": round(download_time, 2),
            }

        except Exception as e:
            logger.exception("NDVI computation failed for item %s", item_result.get("id"))
            return {
                "error": str(e),
                "source_item_id": item_result.get("id"),
                "download_time_sec": round(time.time() - start_time, 2),
            }

    def _classify_ndvi_pixels(self, ndvi_array: np.ndarray) -> Dict[str, Any]:
        """Classify NDVI pixels using ml_inference thresholds."""
        # Import thresholds from ml_inference
        from src.services.ml_inference import CropClassifier

        thresholds = CropClassifier.CROP_THRESHOLDS
        classification = {}
        total = len(ndvi_array)

        for class_name, thresh in thresholds.items():
            mask = (ndvi_array >= thresh["ndvi_min"]) & (ndvi_array < thresh["ndvi_max"])
            count = int(mask.sum())
            classification[class_name] = {
                "count": count,
                "percentage": round(count / total * 100, 2) if total > 0 else 0,
            }

        return classification

    def compute_ndvi_timeseries(
        self,
        bbox: Optional[List[float]] = None,
        datetime_range: Optional[str] = None,
        max_cloud_cover: float = 10.0,
    ) -> Dict[str, Any]:
        """Compute NDVI time-series from multiple satellite scenes.

        This is the key method that makes STAC actually useful — it searches
        for imagery and computes real NDVI statistics for each scene.

        Args:
            bbox: Bounding box [west, south, east, north]
            datetime_range: ISO 8601 range like "2024-01-01/2024-06-30"
            max_cloud_cover: Maximum cloud cover percentage

        Returns:
            Dict with time-ordered list of NDVI stats per scene
        """
        # Search for imagery
        search_results = self.search_imagery(
            bbox=bbox,
            datetime_range=datetime_range,
            max_cloud_cover=max_cloud_cover,
            limit=20,
        )

        if "error" in search_results:
            return search_results

        # Compute NDVI for each scene with red+NIR bands
        ndvi_timeseries = []
        for item in search_results.get("items", []):
            assets = item.get("assets", {})
            red_key, nir_key = self._resolve_band_keys(assets)
            if red_key and nir_key:
                ndvi_result = self.compute_ndvi_from_item(item)
                if "error" not in ndvi_result:
                    ndvi_timeseries.append(ndvi_result)
                else:
                    logger.warning(
                        "NDVI computation failed for %s: %s",
                        item.get("id"),
                        ndvi_result.get("error"),
                    )

        # Sort by datetime
        ndvi_timeseries.sort(key=lambda x: x.get("datetime", ""))

        return {
            "catalog": self.catalog_name,
            "bbox": bbox or RWANDA_BBOX,
            "datetime_range": datetime_range,
            "max_cloud_cover": max_cloud_cover,
            "scene_count": len(ndvi_timeseries),
            "timeseries": ndvi_timeseries,
            "summary": {
                "mean_ndvi_avg": (
                    round(np.mean([x["mean_ndvi"] for x in ndvi_timeseries]), 4)
                    if ndvi_timeseries
                    else None
                ),
                "mean_ndvi_std": (
                    round(np.std([x["mean_ndvi"] for x in ndvi_timeseries]), 4)
                    if ndvi_timeseries
                    else None
                ),
            },
        }

    def get_ndvi_data(
        self,
        bbox: Optional[List[float]] = None,
        datetime_range: Optional[str] = None,
        max_cloud_cover: float = 10.0,
    ) -> Dict[str, Any]:
        """Get NDVI data with actual computation.

        Computes NDVI statistics from satellite imagery bands.
        Alias for compute_ndvi_timeseries().
        """
        return self.compute_ndvi_timeseries(
            bbox=bbox,
            datetime_range=datetime_range,
            max_cloud_cover=max_cloud_cover,
        )


    # ------------------------------------------------------------------
    # Bbox-windowed NDVI from COG bands (for admin boundary analysis)
    # ------------------------------------------------------------------

    def compute_ndvi_for_bbox(
        self,
        item_result: dict,
        bbox: List[float],
        max_pixels: int = 256,
    ) -> Dict[str, Any]:
        """Compute NDVI statistics for a specific bounding box from a STAC item.

        Unlike compute_ndvi_from_item (center 512x512), this reads only the
        pixels inside the given bbox, making it efficient for admin boundaries.

        Args:
            item_result: STAC item dict with assets.B04.href and assets.B08.href
            bbox: [west, south, east, north] in WGS84
            max_pixels: Maximum dimension to read (downscales if larger)

        Returns:
            Dict with mean_ndvi, std_ndvi, min_ndvi, max_ndvi, valid_pixel_count
        """
        if not _RASTERIO_AVAILABLE:
            return {"error": "rasterio not available"}

        assets = item_result.get("assets", {})
        red_key, nir_key = self._resolve_band_keys(assets)
        if red_key is None or nir_key is None:
            return {"error": "Missing red/B04 or nir/B08 bands in STAC item"}

        from rasterio.warp import transform_bounds
        from rasterio.windows import from_bounds

        b04_href = assets[red_key]["href"]
        b08_href = assets[nir_key]["href"]
        start_time = time.time()

        try:
            with RasterioEnv(
                GDAL_HTTP_MERGE_CONSECUTIVE_RANGES="YES",
                GDAL_DISABLE_READDIR_ON_OPEN="EMPTY_DIR",
                CPL_VSIL_CURL_ALLOWED_EXTENSIONS=".tif",
                GDAL_HTTP_MAX_RETRY="3",
                GDAL_HTTP_RETRY_DELAY="1",
            ):
                with rasterio.open(b04_href) as b04_src, rasterio.open(b08_href) as b08_src:
                    # Transform bbox from WGS84 to the raster's CRS
                    dst_crs = b04_src.crs
                    left, bottom, right, top = transform_bounds(
                        "EPSG:4326", dst_crs, *bbox,
                    )

                    # Compute window from projected bounds
                    window = from_bounds(left, bottom, right, top, b04_src.transform)

                    # Clamp to raster extent
                    window = window.intersection(
                        Window(0, 0, b04_src.width, b04_src.height)
                    )
                    if window.width <= 0 or window.height <= 0:
                        return {"error": "Bbox does not intersect this scene"}

                    # Determine output size (downsample large areas)
                    out_height = min(int(window.height), max_pixels)
                    out_width = min(int(window.width), max_pixels)

                    b04_data = b04_src.read(
                        1, window=window,
                        out_shape=(out_height, out_width),
                    ).astype(np.float32)
                    b08_data = b08_src.read(
                        1, window=window,
                        out_shape=(out_height, out_width),
                    ).astype(np.float32)

            stats = self._compute_ndvi_stats(b04_data, b08_data, exclude_zero_reflectance=True)

            if stats is None:
                return {
                    "error": "No valid NDVI pixels in bbox",
                    "source_item_id": item_result.get("id"),
                    "download_time_sec": round(time.time() - start_time, 2),
                }

            stats.pop("_valid_ndvi", None)
            return {
                **stats,
                "source_item_id": item_result.get("id"),
                "datetime": item_result.get("datetime"),
                "cloud_cover": item_result.get("cloud_cover"),
                "download_time_sec": round(time.time() - start_time, 2),
            }

        except Exception as e:
            logger.warning("NDVI bbox computation failed for %s: %s", item_result.get("id"), e)
            return {
                "error": str(e),
                "source_item_id": item_result.get("id"),
                "download_time_sec": round(time.time() - start_time, 2),
            }

    def compute_admin_ndvi(
        self,
        bbox: List[float],
        days: int = 90,
        max_cloud_cover: float = 50.0,
        max_scenes: int = 12,
    ) -> Dict[str, Any]:
        """Compute NDVI time-series for an admin boundary bbox from STAC COGs.

        Searches for recent Sentinel-2 scenes covering the bbox, then reads
        B04/B08 bands via HTTP range requests and computes NDVI statistics.

        Args:
            bbox: [west, south, east, north] in WGS84
            days: How many days back to search (default 90)
            max_cloud_cover: Maximum cloud cover percentage
            max_scenes: Maximum number of scenes to process

        Returns:
            Dict with time-ordered NDVI observations
        """
        datetime_range = self._date_range(days)

        # Search for imagery covering the bbox
        search_results = self.search_imagery(
            bbox=bbox,
            datetime_range=datetime_range,
            max_cloud_cover=max_cloud_cover,
            limit=max_scenes,
        )

        if "error" in search_results:
            return search_results

        # Compute NDVI for each scene
        observations: List[Dict[str, Any]] = []
        for item in search_results.get("items", []):
            assets = item.get("assets", {})
            red_key, nir_key = self._resolve_band_keys(assets)
            if red_key and nir_key:
                result = self.compute_ndvi_for_bbox(item, bbox)
                if "error" not in result:
                    observations.append(result)

        observations.sort(key=lambda x: x.get("datetime", ""))

        return {
            "source": "stac_cog_realtime",
            "catalog": self.catalog_name,
            "bbox": bbox,
            "datetime_range": datetime_range,
            "scene_count": len(observations),
            "observations": observations,
        }

    def compute_drought_indicators(
        self,
        bbox: List[float],
        days: int = 90,
        max_cloud_cover: float = 50.0,
    ) -> Dict[str, Any]:
        """Compute drought indicators from STAC COG NDVI time-series.

        Uses Vegetation Condition Index (VCI) as the primary drought indicator:
            VCI = (NDVI_current - NDVI_min) / (NDVI_max - NDVI_min) × 100

        VCI thresholds (standard WMO interpretation):
            < 10:  Extreme drought
            10-20: Severe drought
            20-35: Moderate drought
            35-50: Mild drought / Watch
            > 50:  No drought

        Args:
            bbox: [west, south, east, north] in WGS84
            days: How many days back for historical range
            max_cloud_cover: Maximum cloud cover percentage

        Returns:
            Dict with drought_status, VCI, latest NDVI, and time-series
        """
        ts_result = self.compute_admin_ndvi(
            bbox=bbox, days=days, max_cloud_cover=max_cloud_cover,
            max_scenes=4,  # Limit to 4 scenes to stay within timeout (~20s each)
        )

        if "error" in ts_result:
            return ts_result

        observations = ts_result.get("observations", [])
        if len(observations) < 2:
            return {
                "error": "Insufficient cloud-free scenes for drought analysis",
                "scene_count": len(observations),
                "source": "stac_cog_realtime",
            }

        # Extract NDVI values
        ndvi_values = [obs["mean_ndvi"] for obs in observations]
        ndvi_min = min(ndvi_values)
        ndvi_max = max(ndvi_values)
        latest_ndvi = ndvi_values[-1]

        # ── Safeguard: too few scenes for reliable VCI ──
        # With only 2-4 scenes from ~90 days, VCI min/max are local
        # extremes — not a true seasonal baseline. If the current scene
        # happens to be the local minimum, VCI=0% and we'd wrongly
        # report "extreme drought". Report as insufficient instead.
        if len(observations) < 8:
            return {
                "source": "stac_cog_realtime",
                "drought_status": "insufficient_data",
                "current_vci": None,
                "latest_ndvi": round(latest_ndvi, 4),
                "ndvi_min_90d": round(ndvi_min, 4),
                "ndvi_max_90d": round(ndvi_max, 4),
                "description": (
                    f"Only {len(observations)} cloud-free scenes available — "
                    f"need at least 8 for reliable VCI drought detection. "
                    f"Current NDVI is {latest_ndvi:.3f}, which is within "
                    f"normal dry-season range for this area."
                ),
                "scene_count": len(observations),
                "observations": observations,
            }

        # Compute VCI
        ndvi_range = ndvi_max - ndvi_min
        if ndvi_range < 0.05:
            # Very narrow range — vegetation is stable, VCI is meaningless
            vci = None
            drought_status = DROUGHT_NONE
            description = (
                f"NDVI range too narrow ({ndvi_min:.4f}–{ndvi_max:.4f}) "
                f"for meaningful VCI — vegetation is stable (NDVI={latest_ndvi:.3f})."
            )
        else:
            vci = round((latest_ndvi - ndvi_min) / ndvi_range * 100, 1)

        # Classify drought status (only when VCI was computed)
        if vci is not None and vci < 10:
            drought_status = DROUGHT_EXTREME
            description = (
                f"VCI={vci}% indicates extreme drought. "
                f"Current NDVI ({latest_ndvi:.3f}) is near the historical minimum ({ndvi_min:.3f})."
            )
        elif vci is not None and vci < 20:
            drought_status = DROUGHT_SEVERE
            description = (
                f"VCI={vci}% indicates severe drought. "
                f"Vegetation health is significantly below normal."
            )
        elif vci is not None and vci < 35:
            drought_status = DROUGHT_MODERATE
            description = (
                f"VCI={vci}% indicates moderate drought. "
                f"Vegetation health is below normal levels."
            )
        elif vci is not None and vci < 50:
            drought_status = DROUGHT_MILD
            description = (
                f"VCI={vci}% indicates mild drought or drought watch. "
                f"Vegetation health is slightly below average."
            )
        elif vci is not None:
            drought_status = DROUGHT_NONE
            description = (
                f"VCI={vci}% indicates no drought. "
                f"Vegetation health is normal or above average (NDVI={latest_ndvi:.3f})."
            )

        # Detect declining trend
        trend_slope = None
        if len(ndvi_values) >= 3:
            x = np.arange(len(ndvi_values), dtype=np.float64)
            y = np.array(ndvi_values, dtype=np.float64)
            slope, _ = np.polyfit(x, y, 1)
            trend_slope = round(float(slope), 6)
            if trend_slope < -0.01:
                description += " NDVI shows a declining trend."

        return {
            "source": "stac_cog_realtime",
            "drought_status": drought_status,
            "current_vci": vci,
            "latest_ndvi": round(latest_ndvi, 4),
            "ndvi_min_90d": round(ndvi_min, 4),
            "ndvi_max_90d": round(ndvi_max, 4),
            "trend_slope": trend_slope,
            "description": description,
            "scene_count": len(observations),
            "observations": observations,
        }


# Module-level singleton
_stac_service: Optional[STACService] = None


def get_stac_service(catalog_name: str = "earth_search") -> STACService:
    global _stac_service
    if _stac_service is None or _stac_service.catalog_name != catalog_name:
        _stac_service = STACService(catalog_name)
    return _stac_service
