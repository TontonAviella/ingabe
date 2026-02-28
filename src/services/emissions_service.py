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

"""EDGAR emissions data service for Rwanda agriculture.

Downloads annual gridded greenhouse gas and air pollutant emissions
from the JRC EDGAR database and aggregates them to district level
using bounding-box zonal statistics.

Data source:
  - EDGAR v8.0 (Emissions Database for Global Atmospheric Research)
  - Published by the European Commission Joint Research Centre (JRC)
  - Resolution: 0.1 degree (~11 km) annual
  - Variables: CH4, N2O, CO2, NH3 for agriculture sectors

Agriculture sectors:
  - AGS: Agricultural soils (fertilizer application, crop residues)
  - ENF: Enteric fermentation (livestock digestion)
  - MNM: Manure management (livestock waste)
  - AWB: Agricultural waste burning (crop residue burning)

No API credentials required — data is publicly accessible via HTTP.
"""

import logging
import os
import tempfile
import zipfile
from typing import Any, Dict, List, Optional

import numpy as np

logger = logging.getLogger(__name__)

# Rwanda bounding box (same as weather_service.py)
RWANDA_BBOX = {
    "north": 0.0,
    "south": -3.0,
    "east": 31.0,
    "west": 28.5,
}

# EDGAR base URLs
_EDGAR_FTP = "https://jeodpp.jrc.ec.europa.eu/ftp/jrc-opendata/EDGAR/datasets"
EDGAR_GHG_URL = f"{_EDGAR_FTP}/v80_FT2022_GHG"   # GHG: CH4, N2O, CO2
EDGAR_AP_URL = f"{_EDGAR_FTP}/v81_FT2022_AP_new"  # Air pollutants: NH3

# Agriculture sector labels
SECTOR_LABELS = {
    "AGS": "Agricultural soils",
    "ENF": "Enteric fermentation",
    "MNM": "Manure management",
    "AWB": "Agricultural waste burning",
}

# Valid emission_type × sector combos that actually exist on JRC servers.
# Not every gas is emitted by every sector:
#   CH4: all 4 sectors (rice paddies, livestock, manure, burning)
#   N2O: AGS (fertilizers), MNM (manure), AWB (burning) — not ENF
#   CO2: AGS only (liming, urea application)
#   NH3: AGS (fertilizers), MNM (manure), AWB (burning) — from AP dataset
VALID_COMBOS: dict[str, list[str]] = {
    "CH4": ["AGS", "ENF", "MNM", "AWB"],
    "N2O": ["AGS", "MNM", "AWB"],
    "CO2": ["AGS"],
    "NH3": ["AGS", "MNM", "AWB"],
}

EMISSION_TYPES = list(VALID_COMBOS.keys())
AGRICULTURE_SECTORS = ["AGS", "ENF", "MNM", "AWB"]


class EmissionsService:
    """Service for downloading and processing EDGAR emissions data."""

    def __init__(self):
        pass

    def download_edgar_gridmap(
        self,
        emission_type: str,
        sector: str,
        year: int,
    ) -> Dict[str, Any]:
        """Download an EDGAR 0.1° gridmap for a substance/sector/year.

        GHG gases (CH4, N2O, CO2) come from EDGAR v8.0 GHG dataset.
        Air pollutants (NH3) come from EDGAR v8.1 AP dataset.

        Args:
            emission_type: Gas species (CH4, N2O, CO2, NH3)
            sector: Agriculture sector code (AGS, ENF, MNM, AWB)
            year: Year of emissions data

        Returns:
            Dict with grid data (values, lats, lons, unit) or error info
        """
        import urllib.request

        # Validate combo exists
        valid_sectors = VALID_COMBOS.get(emission_type, [])
        if sector not in valid_sectors:
            return {"error": f"{emission_type}/{sector} is not a valid EDGAR combo"}

        # Build URL — different dataset for GHG vs AP
        if emission_type == "NH3":
            filename = f"v8.1_FT2022_AP_{emission_type}_{year}_{sector}_emi_nc.zip"
            url = f"{EDGAR_AP_URL}/{emission_type}/{sector}/emi_nc/{filename}"
        else:
            filename = f"v8.0_FT2022_GHG_{emission_type}_{year}_{sector}_emi_nc.zip"
            url = f"{EDGAR_GHG_URL}/{emission_type}/{sector}/emi_nc/{filename}"

        logger.info(
            "Downloading EDGAR gridmap: %s %s %d from %s",
            emission_type, sector, year, url,
        )

        tmp_path = None
        try:
            with tempfile.NamedTemporaryFile(
                suffix=".zip", delete=False
            ) as tmp:
                tmp_path = tmp.name

            req = urllib.request.Request(
                url, headers={"User-Agent": "mundi.ai/1.0"}
            )
            with urllib.request.urlopen(req, timeout=120) as resp:
                with open(tmp_path, "wb") as f:
                    f.write(resp.read())

            # Extract and read the NetCDF
            grid_data = self._extract_and_read_netcdf(tmp_path)
            if grid_data is None:
                return {"error": f"Failed to read NetCDF for {emission_type}/{sector}/{year}"}

            grid_data["emission_type"] = emission_type
            grid_data["sector"] = sector
            grid_data["year"] = year
            return grid_data

        except Exception as e:
            logger.error(
                "EDGAR download failed for %s/%s/%d: %s",
                emission_type, sector, year, e,
            )
            return {"error": str(e)}
        finally:
            if tmp_path:
                try:
                    os.unlink(tmp_path)
                except Exception:
                    pass

    def _extract_and_read_netcdf(
        self, zip_path: str
    ) -> Optional[Dict[str, Any]]:
        """Extract NetCDF from downloaded zip and read as numpy array.

        Crops data to Rwanda bounding box.
        """
        try:
            import xarray as xr

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
                # Sometimes the file is a bare NetCDF
                nc_path = zip_path

            if nc_path is None or not os.path.exists(nc_path):
                logger.warning("No NetCDF file found in EDGAR download")
                return None

            ds = xr.open_dataset(nc_path)

            # Get the data variable (first non-coordinate variable)
            data_vars = list(ds.data_vars)
            if not data_vars:
                logger.warning("No data variables in EDGAR NetCDF")
                ds.close()
                return None

            data_var = ds[data_vars[0]]

            # Extract lat/lon
            lats = ds.coords["lat"].values if "lat" in ds.coords else ds.coords["latitude"].values
            lons = ds.coords["lon"].values if "lon" in ds.coords else ds.coords["longitude"].values

            # Squeeze time dimension if present
            values = data_var.values
            if values.ndim == 3:
                values = values[0]

            # Crop to Rwanda bbox
            lat_mask = (lats >= RWANDA_BBOX["south"]) & (lats <= RWANDA_BBOX["north"])
            lon_mask = (lons >= RWANDA_BBOX["west"]) & (lons <= RWANDA_BBOX["east"])

            lat_idx = np.where(lat_mask)[0]
            lon_idx = np.where(lon_mask)[0]

            if len(lat_idx) == 0 or len(lon_idx) == 0:
                logger.warning("No EDGAR grid cells within Rwanda bbox")
                ds.close()
                return None

            cropped_values = values[
                lat_idx[0] : lat_idx[-1] + 1,
                lon_idx[0] : lon_idx[-1] + 1,
            ]
            cropped_lats = lats[lat_idx[0] : lat_idx[-1] + 1]
            cropped_lons = lons[lon_idx[0] : lon_idx[-1] + 1]

            # Get units from the variable attributes
            unit = data_var.attrs.get("units", "kg m-2 s-1")

            result = {
                "values": cropped_values,
                "lats": cropped_lats,
                "lons": cropped_lons,
                "unit": unit,
            }

            ds.close()

            # Clean up
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
            logger.error("Failed to read EDGAR NetCDF: %s", e)
            return None

    def aggregate_to_districts(
        self,
        grid_data: Dict[str, Any],
        district_geometries: List[Dict[str, Any]],
    ) -> List[Dict[str, Any]]:
        """Aggregate gridded emissions to district-level using SUM.

        EDGAR emi_nc files provide values in tonnes/cell/year, so we
        simply SUM the grid cells within each district's bounding box.

        Args:
            grid_data: Output from download_edgar_gridmap()
            district_geometries: List of dicts with 'district' and 'bbox' keys

        Returns:
            List of dicts with district-level emissions stats
        """
        values = grid_data.get("values")
        if values is None:
            return []

        lats = grid_data["lats"]
        lons = grid_data["lons"]
        emission_type = grid_data.get("emission_type", "")
        sector = grid_data.get("sector", "")
        year = grid_data.get("year", 0)
        unit = grid_data.get("unit", "")

        district_stats = []

        for dist_info in district_geometries:
            district_name = dist_info["district"]
            bbox = dist_info.get("bbox")  # (west, south, east, north)

            if not bbox:
                continue

            west, south, east, north = bbox
            lat_mask = (lats >= south) & (lats <= north)
            lon_mask = (lons >= west) & (lons <= east)

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

            # emi_nc values are already in tonnes/cell/year — just SUM
            total_tonnes = float(np.sum(valid))
            mean_per_cell = float(np.mean(valid))

            district_stats.append({
                "district": district_name,
                "year": year,
                "emission_type": emission_type,
                "sector": sector,
                "sector_label": SECTOR_LABELS.get(sector, sector),
                "total_tonnes": round(total_tonnes, 2),
                "mean_flux_kg_m2_s": mean_per_cell,
                "grid_cells": int(len(valid)),
                "source_version": "EDGAR_v8.1_FT2022_AP" if emission_type == "NH3" else "EDGAR_v8.0_FT2022_GHG",
            })

        return district_stats


# Module-level singleton
_emissions_service: Optional[EmissionsService] = None


def get_emissions_service() -> Optional[EmissionsService]:
    """Get the emissions service singleton."""
    global _emissions_service
    if _emissions_service is None:
        _emissions_service = EmissionsService()
    return _emissions_service
