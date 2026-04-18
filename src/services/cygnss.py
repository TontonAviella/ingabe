# Copyright (C) 2025 Ingabe Ltd.
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU Affero General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.

"""CYGNSS GNSS-R service — free soil moisture + water detection from NASA.

CYGNSS (Cyclone Global Navigation Satellite System) is a constellation of 8
NASA satellites that measure GPS signals reflected off Earth's surface. The
reflected signal encodes soil moisture, water presence, and vegetation state.

Key products:
    Soil Moisture (L3): 9km/36km grid, 6-hour temporal, 0-5cm depth
    Watermask (L3): 0.01° (~1km) grid, daily, binary water/land
    Coverage: ±38° latitude (Rwanda at 2°S is dead center)
    Revisit: median 3 hours
    Data: 2018-present, ~6 day latency

Killer feature for aquaculture/insurance: CYGNSS detects water UNDER vegetation.
L-band signals penetrate canopy where C-band (Sentinel-1) and optical fail.
Ponds hidden under banana groves show up. Sentinel-1 can't see them.

Collections (NASA CMR / PO.DAAC):
    CYGNSS_L3_SOIL_MOISTURE_V3.2     — C2927902887-POCLOUD (9km + 36km)
    CYGNSS_L3_UC_BERKELEY_WATERMASK_DAILY_V3.2 — C3168830666-POCLOUD (1km)
    CYGNSS_L3_UC_BERKELEY_WATERMASK_V3.1       — C2928282019-POCLOUD (monthly)

Environment:
    EARTHDATA_USERNAME / EARTHDATA_PASSWORD — or ~/.netrc with urs.earthdata.nasa.gov
    Free registration: https://urs.earthdata.nasa.gov/

Usage:
    from src.services.cygnss import get_cygnss_service
    svc = get_cygnss_service()

    # Search available granules (no auth needed)
    granules = svc.search_granules(product="soil_moisture_9km", days_back=30)

    # Get soil moisture timeseries for a point (auth needed for download)
    ts = svc.get_soil_moisture(lat=-1.95, lon=29.87, days_back=90)

    # Get watermask for Rwanda (auth needed for download)
    wm = svc.get_watermask(bbox=[28.86, -2.84, 30.90, -1.05], date="2026-04-01")
"""

from __future__ import annotations

import logging
import math
import os
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import httpx
import numpy as np

logger = logging.getLogger(__name__)

# NASA CMR (Catalog) — no auth needed for search
_CMR_ROOT = "https://cmr.earthdata.nasa.gov/search"

# PO.DAAC archive — auth needed for data download
_ARCHIVE_ROOT = "https://archive.podaac.earthdata.nasa.gov/podaac-ops-cumulus-protected"

# Collection concept IDs
_COLLECTIONS = {
    "soil_moisture_9km": {
        "concept_id": "C2927902887-POCLOUD",
        "short_name": "CYGNSS_L3_SOIL_MOISTURE_V3.2",
        "bucket": "CYGNSS_L3_SOIL_MOISTURE_V3.2",
        "resolution_km": 9,
        "description": "Soil moisture 0-5cm depth, 9km grid, daily",
    },
    "soil_moisture_36km": {
        "concept_id": "C2927902887-POCLOUD",
        "short_name": "CYGNSS_L3_SOIL_MOISTURE_V3.2",
        "bucket": "CYGNSS_L3_SOIL_MOISTURE_V3.2",
        "resolution_km": 36,
        "description": "Soil moisture 0-5cm depth, 36km grid, daily",
    },
    "watermask_daily": {
        "concept_id": "C3168830666-POCLOUD",
        "short_name": "CYGNSS_L3_UC_BERKELEY_WATERMASK_DAILY_V3.2",
        "bucket": "CYGNSS_L3_UC_BERKELEY_WATERMASK_DAILY_V3.2",
        "resolution_km": 1,
        "description": "Binary water/land classification, ~1km grid, daily",
    },
    "watermask_monthly": {
        "concept_id": "C2928282019-POCLOUD",
        "short_name": "CYGNSS_L3_UC_BERKELEY_WATERMASK_V3.1",
        "bucket": "CYGNSS_L3_UC_BERKELEY_WATERMASK_V3.1",
        "resolution_km": 1,
        "description": "Binary water/land classification, ~1km grid, monthly",
    },
}

# Rwanda bounding box
RWANDA_BBOX = (28.86, -2.84, 30.90, -1.05)


def _safe_round(v: float, decimals: int = 4) -> float:
    f = float(v)
    return 0.0 if (math.isnan(f) or math.isinf(f)) else round(f, decimals)


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


class CYGNSSService:
    """CYGNSS GNSS-R data access via NASA CMR + PO.DAAC.

    Search operations (CMR) work without authentication.
    Download operations require NASA Earthdata credentials.
    """

    def __init__(self) -> None:
        self._earthdata_session: Optional[Any] = None

    def _get_earthdata_auth(self) -> Optional[Tuple[str, str]]:
        """Get Earthdata credentials from env or .netrc."""
        username = os.environ.get("EARTHDATA_USERNAME")
        password = os.environ.get("EARTHDATA_PASSWORD")
        if username and password:
            return (username, password)

        # Try .netrc
        netrc_path = Path.home() / ".netrc"
        if netrc_path.exists():
            try:
                import netrc as netrc_mod
                auth = netrc_mod.netrc(str(netrc_path))
                entry = auth.authenticators("urs.earthdata.nasa.gov")
                if entry:
                    return (entry[0], entry[2])
            except Exception:
                pass
        return None

    def _get_earthaccess_session(self) -> Any:
        """Get authenticated earthaccess session (lazy init)."""
        if self._earthdata_session is not None:
            return self._earthdata_session

        try:
            import earthaccess
            auth = earthaccess.login(strategy="netrc")
            if not auth.authenticated:
                auth = earthaccess.login(strategy="environment")
            if auth.authenticated:
                self._earthdata_session = earthaccess
                return earthaccess
        except Exception as e:
            logger.warning("earthaccess login failed: %s", e)

        return None

    def search_granules(
        self,
        product: str = "soil_moisture_9km",
        bbox: Tuple[float, float, float, float] = RWANDA_BBOX,
        days_back: int = 30,
        limit: int = 50,
    ) -> Dict[str, Any]:
        """Search CMR for CYGNSS granules. No auth needed.

        Returns granule metadata including download URLs and temporal coverage.
        """
        if product not in _COLLECTIONS:
            return {
                "status": "error",
                "message": f"Unknown product: {product}. Available: {list(_COLLECTIONS.keys())}",
            }

        col = _COLLECTIONS[product]
        end_dt = _utc_now()
        start_dt = end_dt - timedelta(days=days_back)

        url = f"{_CMR_ROOT}/granules.json"
        params = {
            "collection_concept_id": col["concept_id"],
            "temporal": f"{start_dt.strftime('%Y-%m-%dT%H:%M:%SZ')},{end_dt.strftime('%Y-%m-%dT%H:%M:%SZ')}",
            "bounding_box": f"{bbox[0]},{bbox[1]},{bbox[2]},{bbox[3]}",
            "page_size": str(limit),
            "sort_key": "-start_date",
        }

        try:
            r = httpx.get(url, params=params, timeout=30.0)
            r.raise_for_status()
            data = r.json()
        except Exception as e:
            logger.warning("CYGNSS CMR search failed: %s", e)
            return {"status": "error", "message": str(e)}

        entries = data.get("feed", {}).get("entry", [])

        # Filter by resolution for soil moisture (9km vs 36km share same collection)
        if product == "soil_moisture_9km":
            entries = [e for e in entries if "9km" in e.get("title", "")]
        elif product == "soil_moisture_36km":
            entries = [e for e in entries if "36km" in e.get("title", "")]

        granules = []
        for entry in entries:
            links = entry.get("links", [])
            data_url = None
            opendap_url = None
            for link in links:
                href = link.get("href", "")
                if href.startswith("https://archive.podaac") and href.endswith(".nc"):
                    data_url = href
                elif "opendap" in href and "collections" in href:
                    opendap_url = href

            granules.append({
                "title": entry.get("title", ""),
                "time_start": entry.get("time_start", ""),
                "time_end": entry.get("time_end", ""),
                "data_url": data_url,
                "opendap_url": opendap_url,
            })

        return {
            "status": "success",
            "product": product,
            "description": col["description"],
            "resolution_km": col["resolution_km"],
            "bbox": list(bbox),
            "date_range": {
                "start": start_dt.strftime("%Y-%m-%d"),
                "end": end_dt.strftime("%Y-%m-%d"),
            },
            "granules_found": len(granules),
            "granules": granules[:20],  # Cap response size
            "auth_required": True,
            "auth_note": "Download requires NASA Earthdata login. "
                         "Set EARTHDATA_USERNAME/EARTHDATA_PASSWORD env vars "
                         "or create ~/.netrc with urs.earthdata.nasa.gov entry.",
        }

    def get_soil_moisture(
        self,
        lat: float = -1.95,
        lon: float = 29.87,
        days_back: int = 90,
        resolution_km: int = 9,
    ) -> Dict[str, Any]:
        """Get soil moisture timeseries for a point.

        Downloads CYGNSS L3 soil moisture netCDF files and extracts
        the nearest grid cell to the given lat/lon.

        Returns volumetric water content (m³/m³) for 0-5cm soil depth.
        """
        product = f"soil_moisture_{resolution_km}km"
        search = self.search_granules(product=product, days_back=days_back)

        if search.get("status") != "success":
            return search

        if not search["granules"]:
            return {
                "status": "no_data",
                "message": f"No CYGNSS soil moisture granules found for last {days_back} days",
            }

        # Try earthaccess download
        ea = self._get_earthaccess_session()
        if ea is None:
            # Return what we can without download (granule listing)
            return {
                "status": "auth_required",
                "message": "NASA Earthdata credentials needed to download data. "
                           "Set EARTHDATA_USERNAME/EARTHDATA_PASSWORD or configure ~/.netrc",
                "granules_available": len(search["granules"]),
                "date_range": search["date_range"],
                "product": product,
                "resolution_km": resolution_km,
                "lat": lat,
                "lon": lon,
            }

        # Download and extract timeseries
        try:
            import xarray as xr

            results = ea.search_data(
                short_name="CYGNSS_L3_SOIL_MOISTURE_V3.2",
                temporal=(
                    (_utc_now() - timedelta(days=days_back)).strftime("%Y-%m-%d"),
                    _utc_now().strftime("%Y-%m-%d"),
                ),
                bounding_box=(lon - 0.5, lat - 0.5, lon + 0.5, lat + 0.5),
            )

            if not results:
                return {"status": "no_data", "message": "No granules found via earthaccess"}

            # Filter by resolution
            res_key = f"{resolution_km}km"
            results = [r for r in results if res_key in str(r)]

            with tempfile.TemporaryDirectory() as tmpdir:
                files = ea.download(results[:30], tmpdir)  # Cap at 30 days

                timeseries = []
                for f in sorted(files):
                    if not str(f).endswith(".nc") or res_key not in str(f):
                        continue
                    try:
                        ds = xr.open_dataset(f)
                        # Find nearest grid cell
                        sm = ds.sel(
                            latitude=lat, longitude=lon, method="nearest"
                        )
                        # Extract soil moisture variable
                        for var in ["SM_daily", "SM", "soil_moisture"]:
                            if var in sm:
                                val = float(sm[var].values)
                                if not math.isnan(val):
                                    timeseries.append({
                                        "date": str(ds.time.values)[:10] if "time" in ds else Path(f).stem[:10],
                                        "soil_moisture_m3m3": _safe_round(val),
                                    })
                                break
                        ds.close()
                    except Exception as e:
                        logger.debug("Failed to read %s: %s", f, e)

            if not timeseries:
                return {
                    "status": "no_valid_data",
                    "message": f"Downloaded {len(files)} files but no valid soil moisture at ({lat}, {lon})",
                    "files_downloaded": len(files),
                }

            values = [t["soil_moisture_m3m3"] for t in timeseries]
            return {
                "status": "success",
                "product": product,
                "resolution_km": resolution_km,
                "lat": lat,
                "lon": lon,
                "days_back": days_back,
                "observations": len(timeseries),
                "stats": {
                    "mean_m3m3": _safe_round(np.mean(values)),
                    "std_m3m3": _safe_round(np.std(values)),
                    "min_m3m3": _safe_round(min(values)),
                    "max_m3m3": _safe_round(max(values)),
                },
                "timeseries": timeseries,
                "interpretation": {
                    "dry": "< 0.10 m³/m³ (dry soil, no recent rain)",
                    "moist": "0.10-0.25 m³/m³ (recent rain or irrigated)",
                    "wet": "0.25-0.40 m³/m³ (saturated, flood risk or water body nearby)",
                    "standing_water": "> 0.40 m³/m³ (likely surface water present)",
                },
            }

        except ImportError:
            return {
                "status": "error",
                "message": "xarray required for netCDF reading. pip install xarray",
            }
        except Exception as e:
            logger.warning("CYGNSS soil moisture extraction failed: %s", e)
            return {"status": "error", "message": str(e)}

    def get_watermask(
        self,
        bbox: Tuple[float, float, float, float] = RWANDA_BBOX,
        date: Optional[str] = None,
        product: str = "watermask_daily",
    ) -> Dict[str, Any]:
        """Get CYGNSS water mask for a bounding box.

        Returns binary water/land classification at ~1km resolution.
        Water detection works UNDER vegetation canopy (L-band penetration).
        """
        if date is None:
            # Default to 7 days ago (6-day latency)
            date = (_utc_now() - timedelta(days=7)).strftime("%Y-%m-%d")

        search = self.search_granules(
            product=product,
            bbox=bbox,
            days_back=14,
            limit=5,
        )

        if search.get("status") != "success":
            return search

        if not search["granules"]:
            return {
                "status": "no_data",
                "message": f"No CYGNSS watermask granules found near {date}",
            }

        ea = self._get_earthaccess_session()
        if ea is None:
            return {
                "status": "auth_required",
                "message": "NASA Earthdata credentials needed. "
                           "Set EARTHDATA_USERNAME/EARTHDATA_PASSWORD or configure ~/.netrc",
                "granules_available": len(search["granules"]),
                "nearest_date": search["granules"][0]["time_start"][:10] if search["granules"] else None,
                "product": product,
                "bbox": list(bbox),
            }

        try:
            import xarray as xr

            results = ea.search_data(
                short_name=_COLLECTIONS[product]["short_name"],
                temporal=(date, date),
                bounding_box=(bbox[0], bbox[1], bbox[2], bbox[3]),
            )

            if not results:
                return {"status": "no_data", "message": f"No watermask for {date}"}

            with tempfile.TemporaryDirectory() as tmpdir:
                files = ea.download(results[:1], tmpdir)  # Single day

                if not files:
                    return {"status": "download_failed", "message": "No files downloaded"}

                ds = xr.open_dataset(files[0])

                # Extract Rwanda subset
                lat_slice = slice(bbox[3], bbox[1])  # North to south
                lon_slice = slice(bbox[0], bbox[2])

                # Try common variable names
                watermask = None
                for var in ["watermask", "water_mask", "RWAWC", "rwawc"]:
                    if var in ds:
                        watermask = ds[var].sel(
                            latitude=lat_slice, longitude=lon_slice
                        ) if "latitude" in ds.dims else ds[var]
                        break

                if watermask is None:
                    # Use first data variable
                    data_vars = list(ds.data_vars)
                    if data_vars:
                        watermask = ds[data_vars[0]]

                if watermask is None:
                    ds.close()
                    return {"status": "error", "message": f"No watermask variable found. Variables: {list(ds.data_vars)}"}

                arr = watermask.values
                valid = ~np.isnan(arr) & (arr != -99)  # -99 = no data/ocean
                water = (arr == 1) & valid
                land = (arr == 0) & valid

                total_valid = int(valid.sum())
                water_count = int(water.sum())
                land_count = int(land.sum())
                water_fraction = water_count / total_valid if total_valid > 0 else 0.0

                ds.close()

                return {
                    "status": "success",
                    "product": product,
                    "date": date,
                    "bbox": list(bbox),
                    "resolution_km": 1,
                    "total_pixels": int(arr.size),
                    "valid_pixels": total_valid,
                    "water_pixels": water_count,
                    "land_pixels": land_count,
                    "water_fraction": _safe_round(water_fraction),
                    "water_area_km2": _safe_round(water_count * 1.0),  # ~1km² per pixel
                    "note": "CYGNSS detects water under vegetation canopy. "
                            "Individual ponds <100m are below resolution, but "
                            "pond clusters and persistently wet areas are detectable.",
                }

        except ImportError:
            return {"status": "error", "message": "xarray required. pip install xarray"}
        except Exception as e:
            logger.warning("CYGNSS watermask extraction failed: %s", e)
            return {"status": "error", "message": str(e)}

    def check_data_availability(
        self,
        bbox: Tuple[float, float, float, float] = RWANDA_BBOX,
    ) -> Dict[str, Any]:
        """Check what CYGNSS data is available for a region. No auth needed.

        Quick diagnostic: how many granules exist, what's the latest date,
        and what products cover this area.
        """
        results = {}
        for product_key, col in _COLLECTIONS.items():
            search = self.search_granules(
                product=product_key,
                bbox=bbox,
                days_back=30,
                limit=5,
            )
            if search.get("status") == "success":
                latest = search["granules"][0]["time_start"][:10] if search["granules"] else None
                results[product_key] = {
                    "description": col["description"],
                    "resolution_km": col["resolution_km"],
                    "granules_last_30d": search["granules_found"],
                    "latest_date": latest,
                    "available": search["granules_found"] > 0,
                }
            else:
                results[product_key] = {
                    "description": col["description"],
                    "available": False,
                    "error": search.get("message", "search failed"),
                }

        has_auth = self._get_earthdata_auth() is not None
        return {
            "status": "success",
            "sensor": "CYGNSS (8-satellite GNSS-R constellation)",
            "technique": "GPS signal reflection (passive L-band)",
            "coverage": "±38° latitude (Rwanda at 2°S: full coverage)",
            "revisit": "median 3 hours",
            "data_start": "2018-08-01",
            "latency_days": 6,
            "bbox": list(bbox),
            "auth_configured": has_auth,
            "products": results,
            "capabilities": {
                "soil_moisture": "Volumetric water content 0-5cm, 6-hourly, 9km/36km",
                "water_detection": "Binary water/land at ~1km, sees UNDER vegetation",
                "flood_monitoring": "Features as small as 100-200m wide detectable",
                "aquaculture": "Pond clusters and persistently wet areas visible at 1km",
            },
        }


_singleton: Optional[CYGNSSService] = None


def get_cygnss_service() -> CYGNSSService:
    """Return the shared CYGNSSService instance."""
    global _singleton
    if _singleton is None:
        _singleton = CYGNSSService()
    return _singleton
