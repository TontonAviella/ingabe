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

"""Sentinel Hub Statistical API service for real-time field monitoring.

Uses CDSE (Copernicus Data Space Ecosystem) Sentinel Hub to compute
per-polygon vegetation indices without downloading imagery. Free tier
provides 10,000 processing units/month.

Environment variables:
    SH_CLIENT_ID:      Sentinel Hub OAuth client ID (from CDSE dashboard)
    SH_CLIENT_SECRET:  Sentinel Hub OAuth client secret
"""

import logging
import math
import os
from datetime import datetime, timedelta
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)

_SH_AVAILABLE = False

try:
    from sentinelhub import (
        CRS,
        BBox,
        DataCollection,
        Geometry,
        SentinelHubStatistical,
        SentinelHubStatisticalDownloadClient,
        SHConfig,
    )

    _SH_AVAILABLE = True
except ImportError:
    logger.info("sentinelhub not installed — Sentinel Hub features disabled")


# Evalscripts for server-side index computation
EVALSCRIPT_NDVI = """
//VERSION=3
function setup() {
  return {
    input: [{ bands: ["B04", "B08", "dataMask"] }],
    output: [
      { id: "ndvi", bands: 1, sampleType: "FLOAT32" },
      { id: "dataMask", bands: 1 }
    ]
  };
}
function evaluatePixel(samples) {
  let ndvi = (samples.B08 - samples.B04) / (samples.B08 + samples.B04);
  return {
    ndvi: [isFinite(ndvi) ? ndvi : 0],
    dataMask: [samples.dataMask]
  };
}
"""

EVALSCRIPT_MULTI_INDEX = """
//VERSION=3
function setup() {
  return {
    input: [{ bands: ["B02", "B03", "B04", "B08", "dataMask"] }],
    output: [
      { id: "ndvi", bands: 1, sampleType: "FLOAT32" },
      { id: "ndwi", bands: 1, sampleType: "FLOAT32" },
      { id: "bsi", bands: 1, sampleType: "FLOAT32" },
      { id: "dataMask", bands: 1 }
    ]
  };
}
function evaluatePixel(s) {
  let ndvi = (s.B08 - s.B04) / (s.B08 + s.B04);
  let ndwi = (s.B03 - s.B08) / (s.B03 + s.B08);
  let bsi = ((s.B04 + s.B02) - (s.B08 + s.B03)) / ((s.B04 + s.B02) + (s.B08 + s.B03));
  return {
    ndvi: [isFinite(ndvi) ? ndvi : 0],
    ndwi: [isFinite(ndwi) ? ndwi : 0],
    bsi: [isFinite(bsi) ? bsi : 0],
    dataMask: [s.dataMask]
  };
}
"""

# Comprehensive agricultural index evalscript — computes 6 indices in ONE API call.
# Uses Sentinel-2 L2A bands: B02(Blue), B03(Green), B04(Red), B05(RedEdge),
#   B08(NIR), B11(SWIR1), B12(SWIR2), SCL (scene classification for cloud mask).
EVALSCRIPT_AGRI_INDICES = """
//VERSION=3
function setup() {
  return {
    input: [{ bands: ["B02","B03","B04","B05","B08","B11","B12","SCL","dataMask"] }],
    output: [
      { id: "ndvi",  bands: 1, sampleType: "FLOAT32" },
      { id: "evi",   bands: 1, sampleType: "FLOAT32" },
      { id: "ndwi",  bands: 1, sampleType: "FLOAT32" },
      { id: "savi",  bands: 1, sampleType: "FLOAT32" },
      { id: "ndre",  bands: 1, sampleType: "FLOAT32" },
      { id: "ndbi",  bands: 1, sampleType: "FLOAT32" },
      { id: "dataMask", bands: 1 }
    ]
  };
}
function evaluatePixel(s) {
  // Cloud mask via Scene Classification Layer (SCL)
  // SCL 0=nodata 1=saturated 2=darkArea 3=cloudShadow 4=vegetation
  // 5=baresoil 6=water 7=cloudLowProb 8=cloudMedProb 9=cloudHighProb 10=thinCirrus 11=snow
  var validPixel = s.dataMask;
  if (s.SCL == 0 || s.SCL == 1 || s.SCL == 3 || s.SCL == 8 || s.SCL == 9 || s.SCL == 10 || s.SCL == 11) {
    validPixel = 0;
  }

  var f = function(v) { return isFinite(v) ? v : 0; };

  // NDVI = (NIR - Red) / (NIR + Red)
  var ndvi = f((s.B08 - s.B04) / (s.B08 + s.B04));

  // EVI = 2.5 * (NIR - Red) / (NIR + 6*Red - 7.5*Blue + 1)
  var evi_denom = s.B08 + 6.0*s.B04 - 7.5*s.B02 + 1.0;
  var evi = f(2.5 * (s.B08 - s.B04) / evi_denom);

  // NDWI = (Green - NIR) / (Green + NIR)  — water content
  var ndwi = f((s.B03 - s.B08) / (s.B03 + s.B08));

  // SAVI = ((NIR - Red) / (NIR + Red + L)) * (1 + L),  L = 0.5
  var L = 0.5;
  var savi = f(((s.B08 - s.B04) / (s.B08 + s.B04 + L)) * (1.0 + L));

  // NDRE = (NIR - RedEdge) / (NIR + RedEdge) — nitrogen/chlorophyll
  var ndre = f((s.B08 - s.B05) / (s.B08 + s.B05));

  // NDBI = (SWIR1 - NIR) / (SWIR1 + NIR) — built-up index
  var ndbi = f((s.B11 - s.B08) / (s.B11 + s.B08));

  return {
    ndvi: [ndvi], evi: [evi], ndwi: [ndwi],
    savi: [savi], ndre: [ndre], ndbi: [ndbi],
    dataMask: [validPixel]
  };
}
"""

# All agricultural indices computed by EVALSCRIPT_AGRI_INDICES
AGRI_INDEX_NAMES = ["ndvi", "evi", "ndwi", "savi", "ndre", "ndbi"]

# Supported satellite collections for switching between data sources
SUPPORTED_COLLECTIONS = {
    "SENTINEL2_L2A": "SENTINEL2_L2A",
    "SENTINEL2_L1C": "SENTINEL2_L1C",
    "SENTINEL1_GRD": "SENTINEL1",
    # Aliases for user convenience
    "sentinel-2": "SENTINEL2_L2A",
    "sentinel-2-l2a": "SENTINEL2_L2A",
    "sentinel-2-l1c": "SENTINEL2_L1C",
    "sentinel-1": "SENTINEL1",
    "s2": "SENTINEL2_L2A",
    "s1": "SENTINEL1",
}


def _resolve_collection(collection: Optional[str] = None) -> "DataCollection":
    """Resolve a collection name string to a sentinelhub DataCollection.

    Args:
        collection: Collection name (e.g. "SENTINEL2_L2A", "sentinel-2", "s2").
                    Defaults to SENTINEL2_L2A.

    Returns:
        DataCollection enum value.

    Raises:
        ValueError: If collection name is not recognized.
    """
    if collection is None:
        return DataCollection.SENTINEL2_L2A

    normalized = SUPPORTED_COLLECTIONS.get(collection) or SUPPORTED_COLLECTIONS.get(collection.upper())
    if normalized is None:
        supported = sorted(set(SUPPORTED_COLLECTIONS.keys()))
        raise ValueError(
            f"Unknown satellite collection '{collection}'. "
            f"Supported: {supported}"
        )

    return getattr(DataCollection, normalized)


def _get_sh_config() -> "SHConfig":
    """Create Sentinel Hub config from environment variables.

    Supports both original Sentinel Hub (services.sentinel-hub.com) and
    CDSE (sh.dataspace.copernicus.eu) via SH_BASE_URL env var.
    """
    config = SHConfig()
    config.sh_client_id = os.environ.get("SH_CLIENT_ID", "")
    config.sh_client_secret = os.environ.get("SH_CLIENT_SECRET", "")

    base_url = os.environ.get("SH_BASE_URL", "https://services.sentinel-hub.com")
    config.sh_base_url = base_url

    if "dataspace.copernicus.eu" in base_url:
        # CDSE endpoint
        config.sh_token_url = (
            "https://identity.dataspace.copernicus.eu/auth/realms/"
            "CDSE/protocol/openid-connect/token"
        )
    else:
        # Original Sentinel Hub (Planet)
        config.sh_token_url = (
            "https://services.sentinel-hub.com/auth/realms/main/"
            "protocol/openid-connect/token"
        )
    return config


# Max pixels SH Statistical API processes per request (keep well below 1500 m/px limit)
_SH_MAX_RESOLUTION_M = 1000.0
_SH_TARGET_PIXELS_PER_SIDE = 512  # aim for ~512x512 grid


def _bbox_from_geojson(geom: Dict[str, Any]) -> tuple[float, float, float, float]:
    """Extract (min_lon, min_lat, max_lon, max_lat) from GeoJSON coordinates."""
    coords = geom.get("coordinates", [])
    geom_type = geom.get("type", "")
    flat: list[tuple[float, float]] = []
    if geom_type == "Polygon":
        for ring in coords:
            flat.extend(ring)
    elif geom_type == "MultiPolygon":
        for polygon in coords:
            for ring in polygon:
                flat.extend(ring)
    if not flat:
        return (0, 0, 0, 0)
    lons = [p[0] for p in flat]
    lats = [p[1] for p in flat]
    return (min(lons), min(lats), max(lons), max(lats))


def _compute_resolution(geometry: Dict[str, Any]) -> tuple[float, float]:
    """Compute appropriate (x_res, y_res) in **degrees** for a WGS84 GeoJSON geometry.

    The sentinelhub library requires resolution in the same units as the CRS.
    For WGS84 that means degrees, not metres.  We convert from a target
    metre-resolution to degrees using the geometry's centre latitude.

    For small field-scale polygons (<5 km) → ~10 m resolution.
    For district-scale polygons → auto-scaled to stay under SH 1500 m/px limit.
    """
    min_lon, min_lat, max_lon, max_lat = _bbox_from_geojson(geometry)

    if min_lon == max_lon or min_lat == max_lat:
        # Fallback: ~10 m in degrees at equator
        return (0.0001, 0.0001)

    # Approximate metres ↔ degrees at the geometry centre latitude
    centre_lat = (min_lat + max_lat) / 2.0
    m_per_deg_lat = 111_320.0
    m_per_deg_lon = 111_320.0 * math.cos(math.radians(centre_lat))

    width_m = (max_lon - min_lon) * m_per_deg_lon
    height_m = (max_lat - min_lat) * m_per_deg_lat

    # Target resolution in metres
    target_m = 10.0  # native Sentinel-2 resolution

    # If geometry is too large for 10 m, scale up to keep within pixel budget
    if width_m / target_m > _SH_TARGET_PIXELS_PER_SIDE or height_m / target_m > _SH_TARGET_PIXELS_PER_SIDE:
        target_m = max(width_m, height_m) / _SH_TARGET_PIXELS_PER_SIDE
        # Clamp to SH limit
        target_m = min(target_m, _SH_MAX_RESOLUTION_M)

    # Convert metres to degrees
    res_lon = target_m / m_per_deg_lon
    res_lat = target_m / m_per_deg_lat

    return (round(res_lon, 7), round(res_lat, 7))


class SentinelHubService:
    """Real-time field statistics via Sentinel Hub Statistical API on CDSE."""

    def __init__(self):
        if not _SH_AVAILABLE:
            raise ImportError(
                "sentinelhub package not installed. "
                "Install with: pip install sentinelhub==3.11.3"
            )
        self._config = _get_sh_config()

    def is_configured(self) -> bool:
        """Check if Sentinel Hub credentials are set."""
        return bool(self._config.sh_client_id and self._config.sh_client_secret)

    def get_field_stats(
        self,
        geometry: Dict[str, Any],
        date_from: Optional[str] = None,
        date_to: Optional[str] = None,
        index: str = "ndvi",
        collection: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Compute vegetation index statistics for a GeoJSON polygon.

        Args:
            geometry: GeoJSON geometry (Polygon or MultiPolygon)
            date_from: Start date ISO 8601 (default: 30 days ago)
            date_to: End date ISO 8601 (default: today)
            index: "ndvi" for NDVI only, "multi" for NDVI+NDWI+BSI
            collection: Satellite source (e.g. "SENTINEL2_L2A", "sentinel-2-l1c", "s1").
                        Defaults to SENTINEL2_L2A.

        Returns:
            Dict with per-interval statistics (mean, std, min, max, percentiles)
        """
        if not self.is_configured():
            return {"error": "Sentinel Hub credentials not configured (SH_CLIENT_ID, SH_CLIENT_SECRET)"}

        now = datetime.utcnow()
        if date_to is None:
            date_to = now.strftime("%Y-%m-%d")
        if date_from is None:
            date_from = (now - timedelta(days=30)).strftime("%Y-%m-%d")

        evalscript = EVALSCRIPT_NDVI if index == "ndvi" else EVALSCRIPT_MULTI_INDEX

        # Auto-compute resolution based on geometry size (10 m for fields, up to 1 km for districts)
        res = _compute_resolution(geometry)
        logger.info("SH Statistical: resolution=%s m for geometry type=%s", res, geometry.get("type"))

        try:
            data_collection = _resolve_collection(collection)
            logger.info("SH Statistical: using collection=%s", data_collection.name)

            # CDSE requires define_from with service_url; original SH uses collections directly
            is_cdse = "dataspace.copernicus.eu" in self._config.sh_base_url
            if is_cdse:
                input_collection = data_collection.define_from(
                    data_collection.name.lower(),
                    service_url=self._config.sh_base_url,
                )
            else:
                input_collection = data_collection

            request = SentinelHubStatistical(
                aggregation=SentinelHubStatistical.aggregation(
                    evalscript=evalscript,
                    time_interval=(date_from, date_to),
                    aggregation_interval="P1D",
                    resolution=res,
                ),
                input_data=[
                    SentinelHubStatistical.input_data(
                        input_collection,
                        maxcc=0.8,  # Rwanda is tropical/cloudy; allow up to 80% cloud cover
                    ),
                ],
                geometry=Geometry(geometry, crs=CRS.WGS84),
                config=self._config,
            )

            stats = request.get_data()[0]

            # Parse response into clean format
            intervals = []
            for interval_data in stats.get("data", []):
                interval_info = interval_data.get("interval", {})
                outputs = interval_data.get("outputs", {})

                parsed = {
                    "date_from": interval_info.get("from", ""),
                    "date_to": interval_info.get("to", ""),
                }

                for output_name, output_data in outputs.items():
                    bands = output_data.get("bands", {})
                    for band_name, band_stats in bands.items():
                        stats_data = band_stats.get("stats", {})

                        def _safe_round(val: Any, digits: int = 4) -> float:
                            """Round a value safely; SH sometimes returns 'NaN' strings."""
                            try:
                                f = float(val)
                                if math.isnan(f) or math.isinf(f):
                                    return 0.0
                                return round(f, digits)
                            except (TypeError, ValueError):
                                return 0.0

                        parsed[output_name] = {
                            "mean": _safe_round(stats_data.get("mean", 0)),
                            "std": _safe_round(stats_data.get("stDev", 0)),
                            "min": _safe_round(stats_data.get("min", 0)),
                            "max": _safe_round(stats_data.get("max", 0)),
                            "valid_pixels": stats_data.get("sampleCount", 0),
                            "no_data_pixels": stats_data.get("noDataCount", 0),
                        }
                        # Add percentiles if available
                        percentiles = band_stats.get("percentiles", {})
                        if percentiles:
                            parsed[output_name]["percentiles"] = {
                                k: _safe_round(v) for k, v in percentiles.items()
                            }

                intervals.append(parsed)

            return {
                "service": "sentinel_hub_cdse",
                "collection": data_collection.name,
                "index": index,
                "date_from": date_from,
                "date_to": date_to,
                "intervals": intervals,
                "interval_count": len(intervals),
            }

        except ValueError as e:
            return {"error": str(e)}
        except Exception as e:
            logger.exception("Sentinel Hub statistical request failed")
            return {"error": f"Sentinel Hub request failed: {str(e)}"}

    def get_agri_stats(
        self,
        geometry: Dict[str, Any],
        date_from: Optional[str] = None,
        date_to: Optional[str] = None,
        collection: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Compute ALL agricultural indices for a GeoJSON polygon in one API call.

        Returns NDVI, EVI, NDWI, SAVI, NDRE, NDBI with SCL-based cloud masking.
        Uses a single Sentinel Hub Statistical API request (1 processing unit).

        Args:
            geometry: GeoJSON geometry (Polygon or MultiPolygon)
            date_from: Start date ISO 8601 (default: 7 days ago)
            date_to: End date ISO 8601 (default: today)
            collection: Satellite source (e.g. "SENTINEL2_L2A", "sentinel-2-l1c").
                        Defaults to SENTINEL2_L2A.

        Returns:
            Dict with per-interval per-index statistics
        """
        if not self.is_configured():
            return {"error": "Sentinel Hub credentials not configured"}

        now = datetime.utcnow()
        if date_to is None:
            date_to = now.strftime("%Y-%m-%d")
        if date_from is None:
            date_from = (now - timedelta(days=7)).strftime("%Y-%m-%d")

        res = _compute_resolution(geometry)
        logger.info("SH agri_stats: resolution=%s for geometry type=%s", res, geometry.get("type"))

        try:
            data_collection = _resolve_collection(collection)

            is_cdse = "dataspace.copernicus.eu" in self._config.sh_base_url
            if is_cdse:
                input_collection = data_collection.define_from(
                    data_collection.name.lower(),
                    service_url=self._config.sh_base_url,
                )
            else:
                input_collection = data_collection

            request = SentinelHubStatistical(
                aggregation=SentinelHubStatistical.aggregation(
                    evalscript=EVALSCRIPT_AGRI_INDICES,
                    time_interval=(date_from, date_to),
                    aggregation_interval="P1D",
                    resolution=res,
                ),
                input_data=[
                    SentinelHubStatistical.input_data(
                        input_collection,
                        maxcc=0.8,
                    ),
                ],
                geometry=Geometry(geometry, crs=CRS.WGS84),
                config=self._config,
            )

            stats = request.get_data()[0]

            # Parse response
            intervals = []
            for interval_data in stats.get("data", []):
                interval_info = interval_data.get("interval", {})
                outputs = interval_data.get("outputs", {})

                parsed: Dict[str, Any] = {
                    "date_from": interval_info.get("from", ""),
                    "date_to": interval_info.get("to", ""),
                }

                for output_name, output_data in outputs.items():
                    if output_name == "dataMask":
                        continue
                    bands = output_data.get("bands", {})
                    for _band_name, band_stats in bands.items():
                        stats_data = band_stats.get("stats", {})

                        def _safe(val, digits=4):
                            try:
                                f = float(val)
                                return 0.0 if (math.isnan(f) or math.isinf(f)) else round(f, digits)
                            except (TypeError, ValueError):
                                return 0.0

                        parsed[output_name] = {
                            "mean": _safe(stats_data.get("mean", 0)),
                            "std": _safe(stats_data.get("stDev", 0)),
                            "min": _safe(stats_data.get("min", 0)),
                            "max": _safe(stats_data.get("max", 0)),
                            "valid_pixels": stats_data.get("sampleCount", 0),
                            "no_data_pixels": stats_data.get("noDataCount", 0),
                        }

                intervals.append(parsed)

            return {
                "service": "sentinel_hub_cdse",
                "collection": data_collection.name,
                "indices": AGRI_INDEX_NAMES,
                "date_from": date_from,
                "date_to": date_to,
                "intervals": intervals,
                "interval_count": len(intervals),
            }

        except ValueError as e:
            return {"error": str(e)}
        except Exception as e:
            logger.exception("Sentinel Hub agri_stats request failed")
            return {"error": f"Sentinel Hub request failed: {str(e)}"}

    def get_field_timeseries(
        self,
        geometry: Dict[str, Any],
        months: int = 6,
    ) -> Dict[str, Any]:
        """Get NDVI time series for a field over N months.

        Convenience wrapper around get_field_stats with longer date range.
        """
        now = datetime.utcnow()
        date_from = (now - timedelta(days=months * 30)).strftime("%Y-%m-%d")
        date_to = now.strftime("%Y-%m-%d")
        return self.get_field_stats(
            geometry=geometry,
            date_from=date_from,
            date_to=date_to,
            index="ndvi",
        )


# Singleton
_sh_service: Optional[SentinelHubService] = None


def get_sentinel_hub_service() -> Optional[SentinelHubService]:
    """Get Sentinel Hub service singleton. Returns None if package not installed."""
    global _sh_service
    if not _SH_AVAILABLE:
        return None
    if _sh_service is None:
        _sh_service = SentinelHubService()
    return _sh_service
