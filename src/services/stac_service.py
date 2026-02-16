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

# Band names to extract from STAC items
_USEFUL_ASSETS = {"visual", "B04", "B08", "B03", "B02", "SCL", "thumbnail"}


class STACService:
    """Service for discovering satellite imagery via STAC API.

    Uses pystac-client when available (vendor-agnostic, same API for all catalogs).
    Falls back to raw HTTP requests if pystac-client is not installed.
    """

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
            end = datetime.utcnow()
            start = end - timedelta(days=30)
            datetime_range = f"{start.strftime('%Y-%m-%d')}/{end.strftime('%Y-%m-%d')}"

        if self._pystac_client is not None:
            return self._search_pystac(bbox, datetime_range, collections, max_cloud_cover, limit)
        return self._search_http(bbox, datetime_range, collections, max_cloud_cover, limit)

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

            return {
                "catalog": self.catalog_name,
                "collections": collections,
                "bbox": bbox,
                "datetime_range": datetime_range,
                "max_cloud_cover": max_cloud_cover,
                "matched": len(results),
                "items": results,
            }
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

            return {
                "catalog": self.catalog_name,
                "collections": collections,
                "bbox": bbox,
                "datetime_range": datetime_range,
                "max_cloud_cover": max_cloud_cover,
                "matched": len(results),
                "items": results,
            }
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
        if "B04" not in assets or "B08" not in assets:
            return {
                "error": "Missing B04 or B08 bands in STAC item",
                "source_item_id": item_result.get("id"),
            }

        b04_href = assets["B04"]["href"]
        b08_href = assets["B08"]["href"]

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

            # Compute NDVI with proper handling of division by zero
            denominator = b08_data + b04_data
            ndvi = np.where(
                denominator != 0,
                (b08_data - b04_data) / denominator,
                0.0,
            )

            # Mask invalid values (sentinel values, no-data)
            valid_mask = (ndvi >= -1.0) & (ndvi <= 1.0) & np.isfinite(ndvi)
            valid_ndvi = ndvi[valid_mask]

            if len(valid_ndvi) == 0:
                return {
                    "error": "No valid NDVI pixels found",
                    "source_item_id": item_result.get("id"),
                    "download_time_sec": round(time.time() - start_time, 2),
                }

            # Compute statistics
            mean_ndvi = float(np.mean(valid_ndvi))
            std_ndvi = float(np.std(valid_ndvi))
            min_ndvi = float(np.min(valid_ndvi))
            max_ndvi = float(np.max(valid_ndvi))

            # Classify using ml_inference thresholds
            classification = self._classify_ndvi_pixels(valid_ndvi)

            download_time = time.time() - start_time

            return {
                "mean_ndvi": round(mean_ndvi, 4),
                "std_ndvi": round(std_ndvi, 4),
                "min_ndvi": round(min_ndvi, 4),
                "max_ndvi": round(max_ndvi, 4),
                "valid_pixel_count": int(len(valid_ndvi)),
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

        # Compute NDVI for each scene with B04+B08
        ndvi_timeseries = []
        for item in search_results.get("items", []):
            assets = item.get("assets", {})
            if "B04" in assets and "B08" in assets:
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


# Module-level singleton
_stac_service: Optional[STACService] = None


def get_stac_service(catalog_name: str = "earth_search") -> STACService:
    global _stac_service
    if _stac_service is None or _stac_service.catalog_name != catalog_name:
        _stac_service = STACService(catalog_name)
    return _stac_service
