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

"""Precision agriculture services — management zones, prescription maps, soil sampling.

Three LLM-callable tools for SatAgro-comparable field analysis:
1. create_management_zones: NDVI-based field zoning via K-means clustering
2. create_prescription_map: Variable-rate fertilizer recommendations per zone
3. create_soil_sampling_plan: Optimized soil sample placement per zone

All functions are synchronous (blocking) — handlers wrap them in run_in_executor.
"""

import logging
import math
import os
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional, Tuple

import numpy as np
from shapely.geometry import MultiPolygon, Point, mapping, shape
from shapely.ops import unary_union
from sklearn.cluster import KMeans

logger = logging.getLogger(__name__)

# -- Optional dependency flags --

_SH_AVAILABLE = False
try:
    from sentinelhub import (
        CRS,
        BBox,
        DataCollection,
        MimeType,
        SentinelHubRequest,
        SHConfig,
    )

    _SH_AVAILABLE = True
except ImportError:
    logger.info("sentinelhub not installed — precision ag features disabled")

_RASTERIO_AVAILABLE = False
try:
    from rasterio.features import geometry_mask, shapes as rasterio_shapes
    from rasterio.transform import from_bounds as transform_from_bounds

    _RASTERIO_AVAILABLE = True
except ImportError:
    logger.info("rasterio not installed — zone vectorization disabled")


# -- Constants --

NODATA = -9999.0
_MAX_PIXELS = 512

# NDVI temporal-mean evalscript — averages all cloud-free scenes in the date range.
EVALSCRIPT_NDVI_RASTER = """
//VERSION=3
function setup() {
  return {
    input: [{ bands: ["B04", "B08", "SCL", "dataMask"] }],
    output: { bands: 1, sampleType: "FLOAT32" },
    mosaicking: "ORBIT"
  };
}
function evaluatePixel(samples) {
  var sum = 0;
  var count = 0;
  for (var i = 0; i < samples.length; i++) {
    var s = samples[i];
    if (s.dataMask == 0) continue;
    // Skip clouds, shadows, snow/ice, cirrus (SCL classes)
    if (s.SCL == 0 || s.SCL == 1 || s.SCL == 3 ||
        s.SCL == 8 || s.SCL == 9 || s.SCL == 10 || s.SCL == 11) continue;
    var ndvi = (s.B08 - s.B04) / (s.B08 + s.B04);
    if (isFinite(ndvi)) {
      sum += ndvi;
      count++;
    }
  }
  return [count > 0 ? sum / count : -9999];
}
"""

# Zone labels by cluster count
_ZONE_LABELS: Dict[int, Dict[int, str]] = {
    2: {1: "Low", 2: "High"},
    3: {1: "Low", 2: "Medium", 3: "High"},
    4: {1: "Low", 2: "Medium-Low", 3: "Medium-High", 4: "High"},
    5: {1: "Very Low", 2: "Low", 3: "Medium", 4: "High", 5: "Very High"},
}

# RAB (Rwanda Agriculture Board) baseline fertilizer rates (kg/ha)
_CROP_BASELINES: Dict[str, Dict[str, float]] = {
    "maize": {"N": 90, "P2O5": 45, "K2O": 30},
    "beans": {"N": 20, "P2O5": 50, "K2O": 20},
    "rice": {"N": 80, "P2O5": 40, "K2O": 30},
    "wheat": {"N": 60, "P2O5": 40, "K2O": 25},
    "sorghum": {"N": 50, "P2O5": 30, "K2O": 20},
    "potato": {"N": 80, "P2O5": 80, "K2O": 60},
    "cassava": {"N": 40, "P2O5": 30, "K2O": 60},
}


# -- Private helpers --


def _bbox_from_geojson(geom: Dict[str, Any]) -> Tuple[float, float, float, float]:
    """Extract (min_lon, min_lat, max_lon, max_lat) from GeoJSON coordinates."""
    coords = geom.get("coordinates", [])
    geom_type = geom.get("type", "")
    flat: List[Tuple[float, float]] = []
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
        config.sh_token_url = (
            "https://identity.dataspace.copernicus.eu/auth/realms/"
            "CDSE/protocol/openid-connect/token"
        )
    else:
        config.sh_token_url = (
            "https://services.sentinel-hub.com/auth/realms/main/"
            "protocol/openid-connect/token"
        )
    return config


def _compute_pixel_size(
    bbox: Tuple[float, float, float, float],
    target_res_m: float = 10.0,
) -> Tuple[int, int]:
    """Compute (width_px, height_px) for a bbox at target ground resolution."""
    min_lon, min_lat, max_lon, max_lat = bbox
    centre_lat = (min_lat + max_lat) / 2
    m_per_deg_lon = 111_320.0 * math.cos(math.radians(centre_lat))
    m_per_deg_lat = 111_320.0

    width_m = (max_lon - min_lon) * m_per_deg_lon
    height_m = (max_lat - min_lat) * m_per_deg_lat

    width_px = max(4, int(width_m / target_res_m))
    height_px = max(4, int(height_m / target_res_m))

    # Cap at _MAX_PIXELS per side
    if max(width_px, height_px) > _MAX_PIXELS:
        scale = max(width_px, height_px) / _MAX_PIXELS
        width_px = max(4, int(width_px / scale))
        height_px = max(4, int(height_px / scale))

    return (width_px, height_px)


def _download_ndvi_raster(
    geometry: Dict[str, Any],
    date_from: str,
    date_to: str,
) -> Tuple[Optional[np.ndarray], Any, Optional[Tuple[float, float, float, float]]]:
    """Download temporal-mean NDVI float32 raster via Sentinel Hub Process API.

    Returns (ndvi_array, affine_transform, bbox_coords) or (None, None, None).
    """
    config = _get_sh_config()
    if not config.sh_client_id or not config.sh_client_secret:
        return None, None, None

    bbox = _bbox_from_geojson(geometry)
    if bbox == (0, 0, 0, 0):
        return None, None, None

    width_px, height_px = _compute_pixel_size(bbox)
    logger.info(
        "SH Process API: downloading NDVI raster %dx%d for bbox %s",
        width_px, height_px, bbox,
    )

    # CDSE requires define_from with service_url; original SH uses collections directly
    is_cdse = "dataspace.copernicus.eu" in config.sh_base_url
    if is_cdse:
        input_collection = DataCollection.SENTINEL2_L2A.define_from(
            "sentinel2_l2a",
            service_url=config.sh_base_url,
        )
    else:
        input_collection = DataCollection.SENTINEL2_L2A

    request = SentinelHubRequest(
        evalscript=EVALSCRIPT_NDVI_RASTER,
        input_data=[
            SentinelHubRequest.input_data(
                data_collection=input_collection,
                time_interval=(date_from, date_to),
                maxcc=0.8,
            )
        ],
        responses=[
            SentinelHubRequest.output_response("default", MimeType.TIFF),
        ],
        bbox=BBox(bbox=bbox, crs=CRS.WGS84),
        size=(width_px, height_px),
        config=config,
    )

    data = request.get_data()
    if not data:
        return None, None, None

    ndvi = data[0]
    if ndvi.ndim == 3:
        ndvi = ndvi[:, :, 0]

    transform = transform_from_bounds(*bbox, width_px, height_px)
    return ndvi, transform, bbox


def _zone_labels(num_zones: int) -> Dict[int, str]:
    """Get human-readable zone labels for a given number of zones."""
    if num_zones in _ZONE_LABELS:
        return _ZONE_LABELS[num_zones]
    return {i: f"Zone {i}" for i in range(1, num_zones + 1)}


def _pixel_area_ha(
    bbox: Tuple[float, float, float, float],
    shape_hw: Tuple[int, int],
) -> float:
    """Compute area of a single pixel in hectares."""
    min_lon, min_lat, max_lon, max_lat = bbox
    centre_lat = (min_lat + max_lat) / 2
    m_per_deg_lon = 111_320.0 * math.cos(math.radians(centre_lat))
    m_per_deg_lat = 111_320.0
    pixel_w_m = (max_lon - min_lon) * m_per_deg_lon / shape_hw[1]
    pixel_h_m = (max_lat - min_lat) * m_per_deg_lat / shape_hw[0]
    return (pixel_w_m * pixel_h_m) / 10_000.0


# -- Public API --


def create_management_zones(
    geometry: Dict[str, Any],
    num_zones: int = 3,
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
) -> Dict[str, Any]:
    """Create NDVI-based management zones for a field polygon.

    Downloads NDVI raster via Sentinel Hub Process API, clusters pixels with
    K-means, and vectorizes the result into zone polygons.

    Returns GeoJSON FeatureCollection with zone polygons, or {"error": "..."}.
    """
    if not _SH_AVAILABLE:
        return {"error": "Sentinel Hub not available (sentinelhub package not installed)"}
    if not _RASTERIO_AVAILABLE:
        return {"error": "rasterio not available for zone vectorization"}

    num_zones = max(2, min(5, num_zones))

    now = datetime.utcnow()
    if date_to is None:
        date_to = now.strftime("%Y-%m-%d")
    if date_from is None:
        date_from = (now - timedelta(days=30)).strftime("%Y-%m-%d")

    # 1. Download NDVI raster
    logger.info("Downloading NDVI raster for management zones (%s to %s)", date_from, date_to)
    ndvi, transform, bbox = _download_ndvi_raster(geometry, date_from, date_to)
    if ndvi is None:
        return {"error": "Failed to download NDVI data from Sentinel Hub. Check credentials."}

    # 2. Mask pixels outside field polygon
    field_shape = shape(geometry)
    outside_mask = geometry_mask(
        [geometry],
        out_shape=ndvi.shape,
        transform=transform,
        invert=False,  # True = outside polygon (masked)
    )
    nodata_mask = ndvi <= (NODATA + 1)
    invalid = outside_mask | nodata_mask
    valid_values = ndvi[~invalid]

    if len(valid_values) < num_zones * 3:
        return {
            "error": (
                f"Insufficient satellite data: only {len(valid_values)} valid pixels. "
                "Try a wider date range or check for persistent cloud cover."
            )
        }

    # 3. K-means clustering
    logger.info("K-means clustering %d valid pixels into %d zones", len(valid_values), num_zones)
    km = KMeans(n_clusters=num_zones, random_state=42, n_init=10)
    labels = km.fit_predict(valid_values.reshape(-1, 1))

    # Sort clusters by NDVI mean (low -> high) so zone 1 = lowest productivity
    cluster_means = km.cluster_centers_.flatten()
    sorted_indices = np.argsort(cluster_means)
    label_remap = np.zeros(num_zones, dtype=np.int32)
    for new_idx, old_idx in enumerate(sorted_indices):
        label_remap[old_idx] = new_idx + 1  # 1-based zone IDs

    # Build labeled raster
    labeled = np.zeros(ndvi.shape, dtype=np.int32)
    labeled[~invalid] = label_remap[labels]

    # 4. Vectorize clusters -> polygons
    logger.info("Vectorizing zone polygons")
    zone_polys: Dict[int, List] = {}
    for geom_dict, value in rasterio_shapes(
        labeled, mask=(labeled > 0), transform=transform
    ):
        zone_id = int(value)
        poly = shape(geom_dict)
        clipped = poly.intersection(field_shape)
        if clipped.is_empty:
            continue
        zone_polys.setdefault(zone_id, []).append(clipped)

    if not zone_polys:
        return {"error": "Failed to create zone polygons from clustered data"}

    # 5. Build GeoJSON features
    labels_map = _zone_labels(num_zones)
    px_area = _pixel_area_ha(bbox, ndvi.shape)
    features = []

    for zone_id in sorted(zone_polys.keys()):
        dissolved = unary_union(zone_polys[zone_id])

        zone_mask = labeled == zone_id
        zone_ndvi = ndvi[zone_mask & ~invalid]
        area_ha = len(zone_ndvi) * px_area

        feature = {
            "type": "Feature",
            "geometry": mapping(dissolved),
            "properties": {
                "zone_id": zone_id,
                "zone_label": labels_map.get(zone_id, f"Zone {zone_id}"),
                "ndvi_mean": round(float(np.mean(zone_ndvi)), 4) if len(zone_ndvi) else 0.0,
                "ndvi_std": round(float(np.std(zone_ndvi)), 4) if len(zone_ndvi) else 0.0,
                "area_ha": round(area_ha, 2),
                "pixel_count": int(len(zone_ndvi)),
            },
        }
        features.append(feature)

    return {
        "type": "FeatureCollection",
        "features": features,
        "metadata": {
            "num_zones": num_zones,
            "date_from": date_from,
            "date_to": date_to,
            "total_valid_pixels": int(len(valid_values)),
            "source": "sentinel2_l2a",
        },
    }


def create_prescription_map(
    geometry: Dict[str, Any],
    crop_type: str = "maize",
    num_zones: int = 3,
) -> Dict[str, Any]:
    """Create variable-rate fertilizer prescription map.

    Combines NDVI management zones with iSDAsoil data and RAB baseline rates.
    Returns GeoJSON FeatureCollection with fertilizer rates per zone, or {"error": "..."}.
    """
    crop_type = crop_type.lower().strip()
    if crop_type not in _CROP_BASELINES:
        return {
            "error": (
                f"Unknown crop type '{crop_type}'. "
                f"Supported: {', '.join(sorted(_CROP_BASELINES.keys()))}"
            )
        }
    baseline = _CROP_BASELINES[crop_type]

    # 1. Create management zones
    zones_result = create_management_zones(geometry, num_zones=num_zones)
    if "error" in zones_result:
        return zones_result

    features = zones_result["features"]
    num_actual_zones = len(features)

    # 2. Query soil at zone centroids
    soil_query_fn = None
    try:
        from src.services.isdasoil_service import query_soil_point

        soil_query_fn = query_soil_point
    except ImportError:
        logger.warning("iSDAsoil service not available, using baseline rates only")

    for feature in features:
        props = feature["properties"]
        zone_id = props["zone_id"]
        zone_label = props["zone_label"]

        # NDVI-based adjustment: low zones need more input, high zones need less
        if "Low" in zone_label or zone_id == 1:
            ndvi_factor = 1.3
        elif "High" in zone_label or zone_id == num_actual_zones:
            ndvi_factor = 0.7
        else:
            ndvi_factor = 1.0

        n_rate = baseline["N"] * ndvi_factor
        p_rate = baseline["P2O5"] * ndvi_factor
        k_rate = baseline["K2O"] * ndvi_factor

        # Soil-based corrections
        lime_needed = False
        soil_data = None

        if soil_query_fn is not None:
            try:
                zone_shape = shape(feature["geometry"])
                centroid = zone_shape.representative_point()
                soil_result = soil_query_fn(
                    lon=centroid.x,
                    lat=centroid.y,
                    properties=[
                        "ph",
                        "phosphorous_extractable",
                        "potassium_extractable",
                        "nitrogen_total",
                    ],
                    depth="0-20",
                )
                if soil_result.get("status") == "success":
                    soil_data = soil_result.get("properties", {})

                    # pH correction — recommend lime if acidic
                    ph_val = soil_data.get("ph", {}).get("value")
                    if ph_val is not None and ph_val < 5.5:
                        lime_needed = True

                    # Phosphorus correction — boost if very low
                    p_val = soil_data.get("phosphorous_extractable", {}).get("value")
                    if p_val is not None and p_val < 5:
                        p_rate *= 1.2

                    # Potassium correction — boost if low
                    k_val = soil_data.get("potassium_extractable", {}).get("value")
                    if k_val is not None and k_val < 50:
                        k_rate *= 1.2

            except Exception as e:
                logger.warning("Soil query failed for zone %d: %s", zone_id, e)

        # Add prescription properties
        props["crop_type"] = crop_type
        props["nitrogen_kg_ha"] = round(n_rate, 1)
        props["phosphorus_kg_ha"] = round(p_rate, 1)
        props["potassium_kg_ha"] = round(k_rate, 1)
        props["lime_needed"] = lime_needed
        props["ndvi_adjustment_factor"] = ndvi_factor

        if soil_data:
            props["soil_ph"] = soil_data.get("ph", {}).get("value")
            props["soil_p_ppm"] = soil_data.get("phosphorous_extractable", {}).get("value")
            props["soil_k_ppm"] = soil_data.get("potassium_extractable", {}).get("value")

    zones_result["metadata"]["crop_type"] = crop_type
    zones_result["metadata"]["baseline_rates"] = baseline
    zones_result["metadata"]["source_soil"] = "iSDAsoil (30m)"
    zones_result["metadata"]["source_rates"] = "Rwanda Agriculture Board (RAB)"

    return zones_result


def create_soil_sampling_plan(
    geometry: Dict[str, Any],
    num_zones: int = 3,
) -> Dict[str, Any]:
    """Create optimized soil sampling plan with stratified points per zone.

    Places 1-2 representative sampling points per management zone based on
    NDVI productivity levels.

    Returns GeoJSON FeatureCollection with sampling points, or {"error": "..."}.
    """
    # 1. Create management zones
    zones_result = create_management_zones(geometry, num_zones=num_zones)
    if "error" in zones_result:
        return zones_result

    features = zones_result["features"]
    num_actual_zones = len(features)
    sample_id = 0
    sampling_points = []

    for feature in features:
        props = feature["properties"]
        zone_id = props["zone_id"]
        zone_label = props["zone_label"]
        area_ha = props.get("area_ha", 0)

        zone_shape = shape(feature["geometry"])

        # Priority: low-NDVI zones are highest priority for sampling
        if "Low" in zone_label or zone_id == 1:
            priority = "high"
        elif "High" in zone_label or zone_id == num_actual_zones:
            priority = "low"
        else:
            priority = "medium"

        # Point 1: representative point (guaranteed inside polygon)
        pt1 = zone_shape.representative_point()
        sample_id += 1
        sampling_points.append({
            "type": "Feature",
            "geometry": mapping(pt1),
            "properties": {
                "sample_id": sample_id,
                "zone_id": zone_id,
                "zone_label": zone_label,
                "latitude": round(pt1.y, 6),
                "longitude": round(pt1.x, 6),
                "priority": priority,
                "instructions": "Collect 0-20cm composite sample (5 subsamples in 2m radius)",
            },
        })

        # Point 2: farthest internal point if zone > 2 ha
        if area_ha > 2.0:
            try:
                if isinstance(zone_shape, MultiPolygon):
                    boundary_coords = []
                    for poly in zone_shape.geoms:
                        boundary_coords.extend(list(poly.exterior.coords))
                else:
                    boundary_coords = list(zone_shape.exterior.coords)

                if boundary_coords:
                    distances = [pt1.distance(Point(c)) for c in boundary_coords]
                    farthest_idx = int(np.argmax(distances))
                    farthest_pt = Point(boundary_coords[farthest_idx])

                    # Midpoint between representative point and farthest boundary point
                    midpt = Point(
                        (pt1.x + farthest_pt.x) / 2,
                        (pt1.y + farthest_pt.y) / 2,
                    )
                    if not zone_shape.contains(midpt):
                        midpt = zone_shape.representative_point()

                    # Only add if sufficiently far from pt1 (~11m at equator)
                    if midpt.distance(pt1) > 0.0001:
                        sample_id += 1
                        sampling_points.append({
                            "type": "Feature",
                            "geometry": mapping(midpt),
                            "properties": {
                                "sample_id": sample_id,
                                "zone_id": zone_id,
                                "zone_label": zone_label,
                                "latitude": round(midpt.y, 6),
                                "longitude": round(midpt.x, 6),
                                "priority": priority,
                                "instructions": "Collect 0-20cm composite sample (5 subsamples in 2m radius)",
                            },
                        })
            except Exception as e:
                logger.debug("Could not compute second sampling point for zone %d: %s", zone_id, e)

    return {
        "type": "FeatureCollection",
        "features": sampling_points,
        "metadata": {
            "total_samples": sample_id,
            "num_zones": num_actual_zones,
            "instructions": (
                "At each sampling point, collect a composite soil sample from 0-20cm depth. "
                "Take 5 subsamples within a 2m radius and combine into one bag. "
                "Label with the sample ID and GPS coordinates. "
                "High-priority points (low-NDVI zones) should be sampled first."
            ),
        },
    }
