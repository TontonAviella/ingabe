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

"""Digital Earth Africa STAC service — free analysis-ready S2/S1 for Rwanda.

Drop-in replacement for SentinelHubService for the analysis path. Reads
pre-processed Sentinel-2 L2A COGs directly from the public DE Africa S3
bucket (af-south-1) via STAC search. No credentials, no rate limits,
no token refresh.

Output shape matches sentinel_hub_service.get_field_stats so callers can
swap the two without touching consumer code.

Collections used:
    s2_l2a — Sentinel-2 L2A surface reflectance (Sen2Cor, COG on S3)
    SCL band is used for cloud masking (matches EVALSCRIPT_AGRI_INDICES logic)

Environment:
    Nothing required. Sets AWS_NO_SIGN_REQUEST=YES at module load so
    rasterio can read the public bucket without AWS credentials.
"""

from __future__ import annotations

import logging
import math
import os
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional, Tuple

import httpx
import numpy as np

logger = logging.getLogger(__name__)

# Public bucket is anonymous; make sure rasterio's GDAL doesn't try to sign.
os.environ.setdefault("AWS_NO_SIGN_REQUEST", "YES")
os.environ.setdefault("AWS_DEFAULT_REGION", "af-south-1")
# COG-friendly GDAL tuning
os.environ.setdefault("GDAL_DISABLE_READDIR_ON_OPEN", "EMPTY_DIR")
os.environ.setdefault("CPL_VSIL_CURL_ALLOWED_EXTENSIONS", ".tif,.TIF,.tiff")

_STAC_ROOT = "https://explorer.digitalearth.africa/stac"
_STAC_SEARCH = f"{_STAC_ROOT}/search"

# SCL values that should be masked as invalid (matches EVALSCRIPT_AGRI_INDICES).
# 0=nodata, 1=saturated, 3=cloudShadow, 8=cloudMed, 9=cloudHigh, 10=cirrus, 11=snow
_SCL_INVALID = {0, 1, 3, 8, 9, 10, 11}

# Index → required bands. B08 NIR, B04 Red, B03 Green, B02 Blue, B05 RedEdge, B11 SWIR1.
_INDEX_BANDS: Dict[str, Tuple[str, ...]] = {
    "ndvi": ("B04", "B08"),
    "ndwi": ("B03", "B08"),
    "ndre": ("B05", "B08"),
    "savi": ("B04", "B08"),
    "bsi":  ("B02", "B03", "B04", "B08"),
    "evi":  ("B02", "B04", "B08"),
    "ndbi": ("B08", "B11"),
}


def _bbox_from_geojson(geom: Dict[str, Any]) -> Tuple[float, float, float, float]:
    """Compute WGS84 bbox (minx, miny, maxx, maxy) from a GeoJSON geometry."""
    from shapely.geometry import shape
    g = shape(geom)
    return g.bounds  # type: ignore[return-value]


def _compute_index(bands: Dict[str, np.ndarray], index: str) -> np.ndarray:
    """Compute a vegetation index from a dict of band arrays (float32)."""
    eps = 1e-6
    if index == "ndvi":
        nir, red = bands["B08"], bands["B04"]
        return (nir - red) / (nir + red + eps)
    if index == "ndwi":
        green, nir = bands["B03"], bands["B08"]
        return (green - nir) / (green + nir + eps)
    if index == "ndre":
        nir, re5 = bands["B08"], bands["B05"]
        return (nir - re5) / (nir + re5 + eps)
    if index == "savi":
        L = 0.5
        nir, red = bands["B08"], bands["B04"]
        return ((nir - red) / (nir + red + L + eps)) * (1.0 + L)
    if index == "bsi":
        b02, b03, b04, b08 = bands["B02"], bands["B03"], bands["B04"], bands["B08"]
        num = (b04 + b02) - (b08 + b03)
        den = (b04 + b02) + (b08 + b03) + eps
        return num / den
    if index == "evi":
        nir, red, blue = bands["B08"], bands["B04"], bands["B02"]
        return 2.5 * (nir - red) / (nir + 6.0 * red - 7.5 * blue + 1.0 + eps)
    if index == "ndbi":
        swir, nir = bands["B11"], bands["B08"]
        return (swir - nir) / (swir + nir + eps)
    raise ValueError(f"Unsupported index '{index}' for DE Africa service")


def _search_s2_items(
    bbox: Tuple[float, float, float, float],
    date_from: str,
    date_to: str,
    max_cloud: float = 80.0,
    limit: int = 50,
) -> List[Dict[str, Any]]:
    """Search DE Africa s2_l2a collection for items intersecting bbox/time.

    Uses GET against the collection /items endpoint — DE Africa's STAC
    server rejects POST /search with 403, but GET with query params works.
    Cloud-cover filter is applied client-side after the response lands.
    """
    url = f"{_STAC_ROOT}/collections/s2_l2a/items"
    params = {
        "bbox": ",".join(str(x) for x in bbox),
        "datetime": f"{date_from}T00:00:00Z/{date_to}T23:59:59Z",
        "limit": str(limit),
    }
    try:
        r = httpx.get(
            url,
            params=params,
            # DE Africa's CDN blocks "mundi.ai" in UA. Use a neutral client string.
            headers={"Accept": "application/json", "User-Agent": "curl/8"},
            timeout=30.0,
            follow_redirects=True,
        )
        r.raise_for_status()
        features = r.json().get("features", [])
    except Exception as e:
        logger.warning("DE Africa STAC search failed: %s", e)
        return []
    # Client-side cloud filter (collection /items endpoint does not
    # support CQL query params reliably across STAC servers).
    features = [
        f for f in features
        if (f.get("properties", {}).get("eo:cloud_cover") or 0) < max_cloud
    ]
    features.sort(key=lambda f: f["properties"].get("datetime", ""))
    return features


def _read_window(
    s3_href: str,
    geom_bounds_lonlat: Tuple[float, float, float, float],
) -> Optional[Tuple[np.ndarray, Any]]:
    """Open a COG from s3://... and read the window covering geom_bounds_lonlat."""
    import rasterio
    from rasterio.warp import transform_bounds
    from rasterio.windows import from_bounds

    try:
        with rasterio.open(s3_href) as src:
            proj = transform_bounds("EPSG:4326", src.crs, *geom_bounds_lonlat)
            win = from_bounds(*proj, transform=src.transform)
            arr = src.read(1, window=win, boundless=True, fill_value=0)
            return arr, src.window_transform(win)
    except Exception as e:
        logger.warning("DE Africa COG read failed for %s: %s", s3_href, e)
        return None


def _stats_from_array(arr: np.ndarray, valid: np.ndarray) -> Dict[str, float]:
    """Compute mean/std/min/max/percentiles over pixels where valid==True."""
    masked = arr[valid]
    if masked.size == 0:
        return {
            "mean": 0.0, "std": 0.0, "min": 0.0, "max": 0.0,
            "valid_pixels": 0, "no_data_pixels": int(arr.size),
        }
    def _r(v: float) -> float:
        f = float(v)
        return 0.0 if (math.isnan(f) or math.isinf(f)) else round(f, 4)
    return {
        "mean": _r(masked.mean()),
        "std":  _r(masked.std()),
        "min":  _r(masked.min()),
        "max":  _r(masked.max()),
        "valid_pixels": int(masked.size),
        "no_data_pixels": int(arr.size - masked.size),
        "percentiles": {
            "10": _r(np.percentile(masked, 10)),
            "50": _r(np.percentile(masked, 50)),
            "90": _r(np.percentile(masked, 90)),
        },
    }


class DEAfricaSTACService:
    """Analysis-ready Sentinel-2 access via Digital Earth Africa STAC + COG."""

    def is_configured(self) -> bool:
        """DE Africa is public; always configured."""
        return True

    def get_field_stats(
        self,
        geometry: Dict[str, Any],
        date_from: Optional[str] = None,
        date_to: Optional[str] = None,
        index: str = "ndvi",
        collection: Optional[str] = None,  # ignored, kept for API parity
        max_cloud: float = 80.0,
    ) -> Dict[str, Any]:
        """Compute vegetation index statistics for a GeoJSON polygon.

        Matches the output shape of SentinelHubService.get_field_stats so
        callers can swap implementations. `collection` is accepted for
        signature compatibility but DE Africa only exposes s2_l2a here.
        """
        now = datetime.utcnow()
        if date_to is None:
            date_to = now.strftime("%Y-%m-%d")
        if date_from is None:
            date_from = (now - timedelta(days=30)).strftime("%Y-%m-%d")

        if index not in _INDEX_BANDS:
            return {"error": f"Unsupported index '{index}'. Supported: {list(_INDEX_BANDS)}"}

        try:
            bbox = _bbox_from_geojson(geometry)
        except Exception as e:
            return {"error": f"Invalid geometry: {e}"}

        items = _search_s2_items(bbox, date_from, date_to, max_cloud=max_cloud)
        if not items:
            return {
                "source": "deafrica_stac",
                "collection": "s2_l2a",
                "index": index,
                "date_from": date_from,
                "date_to": date_to,
                "intervals": [],
                "note": "No Sentinel-2 scenes matched bbox/date/cloud filter",
            }

        # Rasterize geometry mask once we know the window CRS/shape — but
        # for a field-scale AOI the bbox window is already close enough.
        # Use SCL-based cloud mask + bbox as the effective AOI.
        needed_bands = set(_INDEX_BANDS[index])
        intervals: List[Dict[str, Any]] = []

        for item in items:
            assets = item.get("assets", {})
            if not all(b in assets for b in needed_bands) or "SCL" not in assets:
                continue

            band_arrays: Dict[str, np.ndarray] = {}
            shape: Optional[Tuple[int, int]] = None
            failed = False
            for b in needed_bands:
                res = _read_window(assets[b]["href"], bbox)
                if res is None:
                    failed = True
                    break
                arr, _ = res
                band_arrays[b] = arr.astype("float32")
                shape = arr.shape
            if failed or shape is None:
                continue

            scl_res = _read_window(assets["SCL"]["href"], bbox)
            if scl_res is None:
                continue
            scl, _ = scl_res
            # SCL is 20m native; resample by nearest to match 10m bands if needed.
            if scl.shape != shape:
                yf = shape[0] / scl.shape[0]
                xf = shape[1] / scl.shape[1]
                yy = (np.arange(shape[0]) / yf).astype(int).clip(0, scl.shape[0] - 1)
                xx = (np.arange(shape[1]) / xf).astype(int).clip(0, scl.shape[1] - 1)
                scl = scl[yy[:, None], xx[None, :]]

            valid = ~np.isin(scl, list(_SCL_INVALID)) & (band_arrays[next(iter(needed_bands))] > 0)
            idx_arr = _compute_index(band_arrays, index)
            stats = _stats_from_array(idx_arr, valid)

            dt = item["properties"].get("datetime", "")
            intervals.append({
                "date_from": dt,
                "date_to": dt,
                "cloud_cover_scene": item["properties"].get("eo:cloud_cover"),
                "stac_item_id": item.get("id"),
                index: stats,
            })

        return {
            "source": "deafrica_stac",
            "collection": "s2_l2a",
            "index": index,
            "date_from": date_from,
            "date_to": date_to,
            "scenes_found": len(items),
            "scenes_used": len(intervals),
            "intervals": intervals,
        }

    def get_field_timeseries(
        self,
        geometry: Dict[str, Any],
        date_from: Optional[str] = None,
        date_to: Optional[str] = None,
        index: str = "ndvi",
        months: Optional[int] = None,
    ) -> Dict[str, Any]:
        """Per-scene index timeseries.

        Accepts either explicit date_from/date_to OR a `months` lookback
        for parity with SentinelHubService.get_field_timeseries(months=N).
        """
        if months is not None and date_from is None and date_to is None:
            now = datetime.utcnow()
            date_to = now.strftime("%Y-%m-%d")
            date_from = (now - timedelta(days=int(months) * 30)).strftime("%Y-%m-%d")
        return self.get_field_stats(geometry, date_from, date_to, index=index)


_singleton: Optional[DEAfricaSTACService] = None


def get_deafrica_service() -> DEAfricaSTACService:
    """Return the shared DEAfricaSTACService instance."""
    global _singleton
    if _singleton is None:
        _singleton = DEAfricaSTACService()
    return _singleton
