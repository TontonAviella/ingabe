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

"""iSDAsoil service — query African soil properties from Cloud-Optimized GeoTIFFs.

Data source: https://isdasoil.s3.amazonaws.com/
Resolution: 30m, EPSG:3857
Coverage: All of Africa
Depths: 0–20 cm and 20–50 cm
Bands per file: 4 (mean_0_20, mean_20_50, stdev_0_20, stdev_20_50)
"""

import logging
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Dict, List, Optional, Tuple

import numpy as np
import rasterio
from pyproj import Transformer
from rasterio.env import Env as RasterioEnv
from rasterio.windows import from_bounds

logger = logging.getLogger(__name__)

S3_BASE = "https://isdasoil.s3.amazonaws.com/soil_data"

# All 21 available soil properties with their back-transformation and units.
# Back-transform functions convert raw uint8/uint16 COG values to real-world units.
# fmt: off
SOIL_PROPERTIES: Dict[str, Dict[str, Any]] = {
    "ph": {
        "label": "Soil pH",
        "unit": "",
        "transform": lambda x: x / 10.0,
        "description": "Soil acidity/alkalinity (lower = more acidic)",
    },
    "nitrogen_total": {
        "label": "Total Nitrogen",
        "unit": "g/kg",
        "transform": lambda x: np.expm1(x / 100.0),
        "description": "Total nitrogen content — key nutrient for crop growth",
    },
    "phosphorous_extractable": {
        "label": "Extractable Phosphorus",
        "unit": "ppm",
        "transform": lambda x: np.expm1(x / 10.0),
        "description": "Plant-available phosphorus — essential for root development",
    },
    "potassium_extractable": {
        "label": "Extractable Potassium",
        "unit": "ppm",
        "transform": lambda x: np.expm1(x / 10.0),
        "description": "Plant-available potassium — important for disease resistance",
    },
    "carbon_organic": {
        "label": "Organic Carbon",
        "unit": "g/kg",
        "transform": lambda x: np.expm1(x / 10.0),
        "description": "Soil organic carbon — indicator of soil health and fertility",
    },
    "clay_content": {
        "label": "Clay Content",
        "unit": "%",
        "transform": lambda x: x / 10.0,
        "description": "Clay fraction — affects water retention and nutrient holding",
    },
    "sand_content": {
        "label": "Sand Content",
        "unit": "%",
        "transform": lambda x: x / 10.0,
        "description": "Sand fraction — affects drainage and aeration",
    },
    "silt_content": {
        "label": "Silt Content",
        "unit": "%",
        "transform": lambda x: x / 10.0,
        "description": "Silt fraction — affects soil structure",
    },
    "bulk_density": {
        "label": "Bulk Density",
        "unit": "g/cm³",
        "transform": lambda x: x / 100.0,
        "description": "Soil compaction indicator — affects root penetration",
    },
    "cation_exchange_capacity": {
        "label": "Cation Exchange Capacity",
        "unit": "cmol(+)/kg",
        "transform": lambda x: np.expm1(x / 10.0),
        "description": "Nutrient retention capacity — higher is better for fertility",
    },
    "calcium_extractable": {
        "label": "Extractable Calcium",
        "unit": "ppm",
        "transform": lambda x: np.expm1(x / 10.0),
        "description": "Plant-available calcium",
    },
    "magnesium_extractable": {
        "label": "Extractable Magnesium",
        "unit": "ppm",
        "transform": lambda x: np.expm1(x / 10.0),
        "description": "Plant-available magnesium",
    },
    "iron_extractable": {
        "label": "Extractable Iron",
        "unit": "ppm",
        "transform": lambda x: np.expm1(x / 10.0),
        "description": "Plant-available iron",
    },
    "sulphur_extractable": {
        "label": "Extractable Sulphur",
        "unit": "ppm",
        "transform": lambda x: np.expm1(x / 10.0),
        "description": "Plant-available sulphur",
    },
    "zinc_extractable": {
        "label": "Extractable Zinc",
        "unit": "ppm",
        "transform": lambda x: np.expm1(x / 10.0),
        "description": "Plant-available zinc",
    },
    "aluminium_extractable": {
        "label": "Extractable Aluminium",
        "unit": "ppm",
        "transform": lambda x: np.expm1(x / 10.0),
        "description": "Extractable aluminium — high values indicate toxicity risk",
    },
    "carbon_total": {
        "label": "Total Carbon",
        "unit": "g/kg",
        "transform": lambda x: np.expm1(x / 10.0),
        "description": "Total carbon content",
    },
    "stone_content": {
        "label": "Stone Content",
        "unit": "%",
        "transform": lambda x: x / 10.0,
        "description": "Coarse fragment content",
    },
    "bedrock_depth": {
        "label": "Bedrock Depth",
        "unit": "cm",
        "transform": lambda x: x * 1.0,
        "description": "Depth to bedrock — affects root zone depth",
    },
    "texture_class": {
        "label": "USDA Texture Class",
        "unit": "",
        "transform": lambda x: x * 1.0,  # categorical
        "description": "Soil texture class (USDA system)",
    },
    "fcc": {
        "label": "Fertility Capability Classification",
        "unit": "",
        "transform": lambda x: x % 3000,  # modulo 3000
        "description": "Soil fertility capability classification",
    },
}
# fmt: on

# USDA texture class lookup (texture_class band values → names)
TEXTURE_CLASSES = {
    0: "No data",
    1: "Clay",
    2: "Silty clay",
    3: "Sandy clay",
    4: "Clay loam",
    5: "Silty clay loam",
    6: "Sandy clay loam",
    7: "Loam",
    8: "Silt loam",
    9: "Sandy loam",
    10: "Silt",
    11: "Loamy sand",
    12: "Sand",
}

# Most useful properties for agriculture — queried by default
DEFAULT_PROPERTIES = [
    "ph",
    "nitrogen_total",
    "phosphorous_extractable",
    "potassium_extractable",
    "carbon_organic",
    "clay_content",
    "sand_content",
    "texture_class",
]

# WGS84 → EPSG:3857 transformer (thread-safe after creation)
_transformer = Transformer.from_crs("EPSG:4326", "EPSG:3857", always_xy=True)


def _cog_url(prop: str) -> str:
    return f"{S3_BASE}/{prop}/{prop}.tif"


def _read_point(
    url: str, lon: float, lat: float, buffer_m: float = 150.0
) -> np.ndarray:
    """Read a small window around a point from a COG. Returns array of shape (bands,)."""
    cx, cy = _transformer.transform(lon, lat)
    west = cx - buffer_m
    east = cx + buffer_m
    south = cy - buffer_m
    north = cy + buffer_m

    with RasterioEnv(
        GDAL_DISABLE_READDIR_ON_OPEN="EMPTY_DIR",
        CPL_VSIL_CURL_ALLOWED_EXTENSIONS=".tif",
        GDAL_HTTP_MERGE_CONSECUTIVE_RANGES="YES",
    ):
        with rasterio.open(url) as src:
            window = from_bounds(west, south, east, north, src.transform)
            data = src.read(window=window)  # (bands, h, w)

    # Return mean per band (excluding nodata=0)
    result = np.zeros(data.shape[0], dtype=np.float64)
    for i in range(data.shape[0]):
        band = data[i].astype(np.float64)
        valid = band[band > 0]
        result[i] = float(np.mean(valid)) if len(valid) > 0 else 0.0
    return result


def query_soil_point(
    lon: float,
    lat: float,
    properties: Optional[List[str]] = None,
    depth: str = "0-20",
) -> Dict[str, Any]:
    """Query soil properties at a single point.

    Args:
        lon: Longitude (WGS84)
        lat: Latitude (WGS84)
        properties: List of property names (default: DEFAULT_PROPERTIES)
        depth: "0-20" or "20-50" cm

    Returns:
        Dict with property values, units, and descriptions.
    """
    if properties is None:
        properties = DEFAULT_PROPERTIES

    # Validate
    invalid = [p for p in properties if p not in SOIL_PROPERTIES]
    if invalid:
        return {"error": f"Unknown soil properties: {invalid}"}

    if not (-35 <= lat <= 38 and -32 <= lon <= 58):
        return {"error": "Coordinates outside Africa coverage"}

    def _fetch_one(prop_name: str) -> Tuple[str, Dict[str, Any]]:
        """Fetch a single soil property — designed for ThreadPoolExecutor."""
        prop_info = SOIL_PROPERTIES[prop_name]
        url = _cog_url(prop_name)
        try:
            raw = _read_point(url, lon, lat)
            n_bands = len(raw)

            if n_bands == 4:
                depth_band = 0 if depth == "0-20" else 1
                stdev_band = 2 if depth == "0-20" else 3
            elif n_bands == 2:
                depth_band = 0 if depth == "0-20" else 1
                stdev_band = None
            else:
                depth_band = 0
                stdev_band = None

            if raw[depth_band] == 0:
                return prop_name, {
                    "value": None,
                    "unit": prop_info["unit"],
                    "label": prop_info["label"],
                    "description": prop_info["description"],
                    "note": "No data at this location",
                }

            transform_fn = prop_info["transform"]
            value = float(transform_fn(raw[depth_band]))
            uncertainty = None
            if stdev_band is not None and raw[stdev_band] > 0:
                uncertainty = float(transform_fn(raw[stdev_band]))

            entry: Dict[str, Any] = {
                "value": round(value, 2),
                "unit": prop_info["unit"],
                "label": prop_info["label"],
                "description": prop_info["description"],
                "depth": f"{depth} cm",
            }
            if uncertainty is not None:
                entry["uncertainty"] = round(uncertainty, 2)

            if prop_name == "texture_class":
                class_id = int(round(value))
                entry["texture_name"] = TEXTURE_CLASSES.get(class_id, f"Unknown ({class_id})")

            return prop_name, entry
        except Exception as e:
            logger.warning("Failed to read %s: %s", prop_name, e)
            return prop_name, {
                "value": None,
                "label": prop_info["label"],
                "error": str(e),
            }

    # Fetch all properties in parallel — each HTTP range-request is I/O-bound
    results: Dict[str, Any] = {}
    with ThreadPoolExecutor(max_workers=min(len(properties), 8)) as pool:
        futures = {pool.submit(_fetch_one, p): p for p in properties}
        for future in futures:
            prop_name, entry = future.result()
            results[prop_name] = entry

    return {
        "status": "success",
        "coordinates": {"lon": lon, "lat": lat},
        "depth": f"{depth} cm",
        "source": "iSDAsoil (30m resolution, machine learning predictions)",
        "properties": results,
    }
