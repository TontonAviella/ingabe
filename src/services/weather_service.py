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

"""Copernicus AgERA5 weather data service for Rwanda agriculture.

Downloads daily agrometeorological indicators from the Copernicus Climate
Data Store (CDS) and aggregates them to district level using zonal
statistics against PostGIS district boundaries.

Data source:
  - AgERA5 "Agrometeorological indicators from 1979 to present"
  - Dataset ID: sis-agrometeorological-indicators
  - Resolution: 0.1 degree (~11 km) daily
  - Variables: 2m temperature, precipitation, solar radiation

Requires:
  - cdsapi package (pip install cdsapi)
  - CDSAPI_URL and CDSAPI_KEY environment variables
  - OR ~/.cdsapirc configuration file
"""

import logging
import os
import tempfile
import zipfile
from datetime import date
from pathlib import Path
from typing import Any, Dict, List, Optional

import numpy as np

logger = logging.getLogger(__name__)

# Rwanda bounding box (used for spatial subsetting)
RWANDA_BBOX = {
    "north": 0.0,
    "south": -3.0,
    "east": 31.0,
    "west": 28.5,
}

# AgERA5 variable mapping
# key = our internal name, value = CDS API variable name + statistic
AGERA5_VARIABLES = {
    "temperature_mean": {
        "variable": "2m_temperature",
        "statistic": "24_hour_mean",
        "unit": "K",
        "display_unit": "C",
        "convert": lambda k: round(k - 273.15, 1),  # Kelvin to Celsius
    },
    "temperature_max": {
        "variable": "2m_temperature",
        "statistic": "24_hour_maximum",
        "unit": "K",
        "display_unit": "C",
        "convert": lambda k: round(k - 273.15, 1),
    },
    "temperature_min": {
        "variable": "2m_temperature",
        "statistic": "24_hour_minimum",
        "unit": "K",
        "display_unit": "C",
        "convert": lambda k: round(k - 273.15, 1),
    },
    "precipitation": {
        "variable": "precipitation_flux",
        "statistic": "24_hour_mean",
        "unit": "mm d-1",
        "display_unit": "mm/day",
        "convert": lambda mm: round(float(mm), 1),  # Already in mm/day
    },
    "solar_radiation": {
        "variable": "solar_radiation_flux",
        "statistic": "24_hour_mean",
        "unit": "J m-2 day-1",
        "display_unit": "MJ/m2/day",
        "convert": lambda j: round(j / 1e6, 2),  # J -> MJ
    },
}


class WeatherService:
    """Service for downloading and processing AgERA5 weather data."""

    def __init__(self):
        self._client = None

    def is_configured(self) -> bool:
        """Check if CDS API credentials are available."""
        # Check env vars first
        if os.environ.get("CDSAPI_KEY"):
            return True
        # Check for config file
        cdsapirc = Path.home() / ".cdsapirc"
        return cdsapirc.exists()

    def _get_client(self):
        """Get or create CDS API client."""
        if self._client is None:
            try:
                import cdsapi

                # cdsapi reads from env vars CDSAPI_URL + CDSAPI_KEY
                # or from ~/.cdsapirc
                url = os.environ.get(
                    "CDSAPI_URL", "https://cds.climate.copernicus.eu/api"
                )
                key = os.environ.get("CDSAPI_KEY")
                if key:
                    self._client = cdsapi.Client(url=url, key=key, quiet=True)
                else:
                    self._client = cdsapi.Client(quiet=True)
            except ImportError:
                logger.error("cdsapi package not installed — pip install cdsapi")
                return None
            except Exception as e:
                logger.error("Failed to create CDS API client: %s", e)
                return None
        return self._client

    def download_agera5_day(
        self,
        target_date: date,
        variables: Optional[List[str]] = None,
    ) -> Dict[str, Any]:
        """Download AgERA5 data for a single day over Rwanda.

        Args:
            target_date: Date to download (AgERA5 has ~5 day latency)
            variables: List of variable keys from AGERA5_VARIABLES.
                       Defaults to all variables.

        Returns:
            Dict with variable name -> numpy array (or path to NetCDF/GRIB)
        """
        client = self._get_client()
        if client is None:
            return {"error": "CDS API client not available"}

        if variables is None:
            variables = list(AGERA5_VARIABLES.keys())

        year = str(target_date.year)
        month = f"{target_date.month:02d}"
        day = f"{target_date.day:02d}"

        results = {}
        errors = []

        # Group requests by CDS variable to minimize API calls
        # (temperature_mean/max/min are same variable, different statistics)
        variable_groups: Dict[str, List[str]] = {}
        for var_key in variables:
            if var_key not in AGERA5_VARIABLES:
                errors.append(f"Unknown variable: {var_key}")
                continue
            cds_var = AGERA5_VARIABLES[var_key]["variable"]
            if cds_var not in variable_groups:
                variable_groups[cds_var] = []
            variable_groups[cds_var].append(var_key)

        for cds_var, var_keys in variable_groups.items():
            for var_key in var_keys:
                var_info = AGERA5_VARIABLES[var_key]
                try:
                    with tempfile.NamedTemporaryFile(
                        suffix=".zip", delete=False
                    ) as tmp:
                        tmp_path = tmp.name

                    request = {
                        "variable": [var_info["variable"]],
                        "year": [year],
                        "month": [month],
                        "day": [day],
                        "statistic": [var_info["statistic"]],
                        "version": ["1_1"],
                        "area": [
                            RWANDA_BBOX["north"],
                            RWANDA_BBOX["west"],
                            RWANDA_BBOX["south"],
                            RWANDA_BBOX["east"],
                        ],
                    }

                    logger.info(
                        "Downloading AgERA5 %s (%s) for %s",
                        var_key,
                        var_info["statistic"],
                        target_date,
                    )

                    client.retrieve(
                        "sis-agrometeorological-indicators",
                        request,
                        tmp_path,
                    )

                    # Extract NetCDF from zip
                    nc_data = self._extract_and_read_netcdf(tmp_path, var_key)
                    if nc_data is not None:
                        results[var_key] = nc_data
                    else:
                        errors.append(f"Failed to read {var_key} data")

                except Exception as e:
                    logger.error("Download failed for %s: %s", var_key, e)
                    errors.append(f"{var_key}: {str(e)}")
                finally:
                    # Clean up temp file
                    try:
                        os.unlink(tmp_path)
                    except Exception:
                        pass

        return {
            "date": str(target_date),
            "variables": results,
            "errors": errors if errors else None,
        }

    def _extract_and_read_netcdf(
        self, zip_path: str, var_key: str
    ) -> Optional[Dict[str, Any]]:
        """Extract NetCDF from downloaded zip and read as numpy array."""
        try:
            import xarray as xr

            # AgERA5 downloads come as zip containing NetCDF
            extract_dir = tempfile.mkdtemp()
            nc_path = None

            if zipfile.is_zipfile(zip_path):
                with zipfile.ZipFile(zip_path, "r") as zf:
                    for name in zf.namelist():
                        if name.endswith(".nc"):
                            zf.extract(name, extract_dir)
                            nc_path = os.path.join(extract_dir, name)
                            break
            else:
                # Sometimes CDS returns the NetCDF directly (not zipped)
                nc_path = zip_path

            if nc_path is None or not os.path.exists(nc_path):
                logger.warning("No NetCDF file found in download for %s", var_key)
                return None

            ds = xr.open_dataset(nc_path)

            # Get the data variable (first non-coordinate variable)
            data_vars = list(ds.data_vars)
            if not data_vars:
                logger.warning("No data variables in NetCDF for %s", var_key)
                ds.close()
                return None

            data_var = ds[data_vars[0]]

            # Extract lat/lon and values
            lats = ds.coords["lat"].values if "lat" in ds.coords else ds.coords["latitude"].values
            lons = ds.coords["lon"].values if "lon" in ds.coords else ds.coords["longitude"].values

            # Squeeze time dimension if present
            values = data_var.values
            if values.ndim == 3:
                values = values[0]  # Take first time step

            result = {
                "values": values,
                "lats": lats,
                "lons": lons,
                "unit": AGERA5_VARIABLES[var_key]["unit"],
                "display_unit": AGERA5_VARIABLES[var_key]["display_unit"],
            }

            ds.close()

            # Clean up extracted files
            try:
                import shutil

                shutil.rmtree(extract_dir, ignore_errors=True)
            except Exception:
                pass

            return result

        except ImportError:
            logger.error("xarray not available — cannot read NetCDF")
            return None
        except Exception as e:
            logger.error("Failed to read NetCDF for %s: %s", var_key, e)
            return None

    def aggregate_to_districts(
        self,
        weather_data: Dict[str, Any],
        district_geometries: List[Dict[str, Any]],
    ) -> List[Dict[str, Any]]:
        """Aggregate gridded weather data to district-level zonal stats.

        Args:
            weather_data: Output from download_agera5_day()
            district_geometries: List of dicts with 'district' and 'geom_wkt' keys

        Returns:
            List of dicts with district-level weather stats
        """
        variables = weather_data.get("variables", {})
        if not variables:
            return []

        district_stats = []

        for dist_info in district_geometries:
            district_name = dist_info["district"]
            bbox = dist_info.get("bbox")  # (west, south, east, north)

            stats = {"district": district_name, "date": weather_data.get("date")}

            for var_key, var_data in variables.items():
                if var_data is None:
                    continue

                values = var_data["values"]
                lats = var_data["lats"]
                lons = var_data["lons"]
                convert_fn = AGERA5_VARIABLES[var_key]["convert"]

                if bbox:
                    # Simple bbox-based zonal stats (fast, good enough for 0.1 deg)
                    west, south, east, north = bbox
                    lat_mask = (lats >= south) & (lats <= north)
                    lon_mask = (lons >= west) & (lons <= east)

                    # Create 2D mask
                    lat_idx = np.where(lat_mask)[0]
                    lon_idx = np.where(lon_mask)[0]

                    if len(lat_idx) == 0 or len(lon_idx) == 0:
                        continue

                    subset = values[
                        lat_idx[0] : lat_idx[-1] + 1,
                        lon_idx[0] : lon_idx[-1] + 1,
                    ]

                    # Filter out NaN/nodata
                    valid = subset[np.isfinite(subset)]
                    if len(valid) == 0:
                        continue

                    raw_mean = float(np.mean(valid))
                    stats[var_key] = convert_fn(raw_mean)
                else:
                    # Fallback: use all Rwanda data
                    valid = values[np.isfinite(values)]
                    if len(valid) == 0:
                        continue
                    raw_mean = float(np.mean(valid))
                    stats[var_key] = convert_fn(raw_mean)

            district_stats.append(stats)

        return district_stats


    def fetch_openmeteo_districts(
        self,
        district_centroids: List[Dict[str, Any]],
        past_days: int = 10,
    ) -> List[Dict[str, Any]]:
        """Fetch recent weather from Open-Meteo for district centroids.

        Open-Meteo is free, requires no API key, and provides observed
        weather up to yesterday plus today's partial data.  This fills
        the gap between AgERA5's ~5-day latency and the current date.

        Args:
            district_centroids: List of dicts with 'district', 'lat', 'lon'
            past_days: How many past days to request (default 10)

        Returns:
            List of per-district-per-day dicts matching DuckDB schema
        """
        import urllib.request
        import json as _json

        results: List[Dict[str, Any]] = []

        # Build bulk request — Open-Meteo supports multi-location in one call
        lats = ",".join(str(d["lat"]) for d in district_centroids)
        lons = ",".join(str(d["lon"]) for d in district_centroids)

        url = (
            f"https://api.open-meteo.com/v1/forecast?"
            f"latitude={lats}&longitude={lons}"
            f"&daily=temperature_2m_max,temperature_2m_min,temperature_2m_mean,"
            f"precipitation_sum,shortwave_radiation_sum"
            f"&past_days={past_days}"
            f"&timezone=Africa/Kigali"
            f"&forecast_days=1"
        )

        try:
            req = urllib.request.Request(url, headers={"User-Agent": "mundi.ai/1.0"})
            with urllib.request.urlopen(req, timeout=30) as resp:
                data = _json.loads(resp.read().decode())
        except Exception as e:
            logger.error("Open-Meteo request failed: %s", e)
            return []

        # Open-Meteo returns a list when multiple locations are requested
        if isinstance(data, dict) and "daily" in data:
            # Single location response — wrap in list
            data = [data]

        for idx, district_info in enumerate(district_centroids):
            district_name = district_info["district"]
            if idx >= len(data):
                break

            loc_data = data[idx]
            daily = loc_data.get("daily", {})
            dates = daily.get("time", [])
            t_mean = daily.get("temperature_2m_mean", [])
            t_max = daily.get("temperature_2m_max", [])
            t_min = daily.get("temperature_2m_min", [])
            precip = daily.get("precipitation_sum", [])
            solar = daily.get("shortwave_radiation_sum", [])

            for i, dt_str in enumerate(dates):
                results.append({
                    "district": district_name,
                    "date": dt_str,
                    "temperature_mean": round(t_mean[i], 1) if i < len(t_mean) and t_mean[i] is not None else None,
                    "temperature_max": round(t_max[i], 1) if i < len(t_max) and t_max[i] is not None else None,
                    "temperature_min": round(t_min[i], 1) if i < len(t_min) and t_min[i] is not None else None,
                    "precipitation": round(precip[i], 1) if i < len(precip) and precip[i] is not None else None,
                    "solar_radiation": round(solar[i], 2) if i < len(solar) and solar[i] is not None else None,
                    "source": "nwp-reanalysis",
                })

        logger.info(
            "Open-Meteo: fetched %d records for %d districts (%d past days)",
            len(results), len(district_centroids), past_days,
        )
        return results


# Module-level singleton
_weather_service: Optional[WeatherService] = None


def get_weather_service() -> Optional[WeatherService]:
    """Get the weather service singleton."""
    global _weather_service
    if _weather_service is None:
        _weather_service = WeatherService()
    return _weather_service
