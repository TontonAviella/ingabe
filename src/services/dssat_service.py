"""DSSAT crop yield forecast service with Sentinel-2 data assimilation.

Integrates the DSSAT crop simulation model with iSDAsoil pedotransfer,
NASA POWER weather data, and Sentinel-2 LAI observations to produce
adjusted yield forecasts (t/ha) for East African smallholder agriculture.

Pipeline:
1. Build soil profile from iSDAsoil via Saxton & Rawls pedotransfer
2. Build weather file from NASA POWER (fallback: Open-Meteo)
3. Build crop management from Rwanda crop calendar + RAB fertilizer recs
4. Run DSSAT baseline simulation
5. Assimilate Sentinel-2 NDVI→LAI observations (Beer-Lambert + ratio scaling)
6. Return adjusted yield (t/ha)
"""

from __future__ import annotations

import logging
import math
from datetime import datetime
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Rwanda crop calendar (RAB — Rwanda Agriculture Board)
# ---------------------------------------------------------------------------

_CROP_CALENDARS: Dict[str, Dict[str, Dict[str, Any]]] = {
    # --- Cereals ---
    "maize": {
        "A": {"planting": "09-15", "harvest_dap": 120},
        "B": {"planting": "02-15", "harvest_dap": 120},
    },
    "rice": {
        "A": {"planting": "09-01", "harvest_dap": 150},
        "B": {"planting": "03-01", "harvest_dap": 150},
    },
    "beans": {
        "A": {"planting": "09-15", "harvest_dap": 90},
        "B": {"planting": "02-15", "harvest_dap": 90},
    },
    "sorghum": {
        "A": {"planting": "09-15", "harvest_dap": 110},
        "B": {"planting": "02-15", "harvest_dap": 110},
    },
    "wheat": {
        "A": {"planting": "06-01", "harvest_dap": 120},
    },
    "finger_millet": {
        "A": {"planting": "09-15", "harvest_dap": 105},
        "B": {"planting": "02-15", "harvest_dap": 105},
    },
    # --- Tubers & roots ---
    "potato": {
        "A": {"planting": "09-15", "harvest_dap": 110},
        "B": {"planting": "02-15", "harvest_dap": 110},
    },
    "sweet_potato": {
        "A": {"planting": "09-15", "harvest_dap": 150},
        "B": {"planting": "02-15", "harvest_dap": 150},
    },
    "cassava": {
        "A": {"planting": "09-01", "harvest_dap": 365},
        "B": {"planting": "02-15", "harvest_dap": 365},
    },
    "yam": {
        "A": {"planting": "09-01", "harvest_dap": 270},
        "B": {"planting": "02-15", "harvest_dap": 270},
    },
    "taro": {
        "A": {"planting": "09-01", "harvest_dap": 270},
        "B": {"planting": "02-15", "harvest_dap": 270},
    },
    # --- Legumes ---
    "soybean": {
        "A": {"planting": "09-15", "harvest_dap": 110},
        "B": {"planting": "02-15", "harvest_dap": 110},
    },
    "groundnut": {
        "A": {"planting": "09-15", "harvest_dap": 120},
        "B": {"planting": "02-15", "harvest_dap": 120},
    },
    "peas": {
        "A": {"planting": "09-15", "harvest_dap": 85},
        "B": {"planting": "02-15", "harvest_dap": 85},
    },
    "cowpea": {
        "A": {"planting": "09-15", "harvest_dap": 80},
        "B": {"planting": "02-15", "harvest_dap": 80},
    },
    "pigeon_pea": {
        "A": {"planting": "09-15", "harvest_dap": 170},
        "B": {"planting": "02-15", "harvest_dap": 170},
    },
    # --- Vegetables ---
    "tomato": {
        "A": {"planting": "09-15", "harvest_dap": 110},
        "B": {"planting": "02-15", "harvest_dap": 110},
    },
    "onion": {
        "A": {"planting": "09-15", "harvest_dap": 130},
        "B": {"planting": "02-15", "harvest_dap": 130},
    },
    "cabbage": {
        "A": {"planting": "09-15", "harvest_dap": 95},
        "B": {"planting": "02-15", "harvest_dap": 95},
    },
    "carrot": {
        "A": {"planting": "09-15", "harvest_dap": 100},
        "B": {"planting": "02-15", "harvest_dap": 100},
    },
    "chili": {
        "A": {"planting": "09-15", "harvest_dap": 130},
        "B": {"planting": "02-15", "harvest_dap": 130},
    },
    "eggplant": {
        "A": {"planting": "09-15", "harvest_dap": 130},
        "B": {"planting": "02-15", "harvest_dap": 130},
    },
    "green_pepper": {
        "A": {"planting": "09-15", "harvest_dap": 120},
        "B": {"planting": "02-15", "harvest_dap": 120},
    },
    "garlic": {
        "A": {"planting": "09-15", "harvest_dap": 150},
        "B": {"planting": "02-15", "harvest_dap": 150},
    },
    "amaranth": {
        "A": {"planting": "09-15", "harvest_dap": 75},
        "B": {"planting": "02-15", "harvest_dap": 75},
    },
    "leek": {
        "A": {"planting": "09-15", "harvest_dap": 150},
        "B": {"planting": "02-15", "harvest_dap": 150},
    },
    "lettuce": {
        "A": {"planting": "09-15", "harvest_dap": 60},
        "B": {"planting": "02-15", "harvest_dap": 60},
    },
    "spinach": {
        "A": {"planting": "09-15", "harvest_dap": 55},
        "B": {"planting": "02-15", "harvest_dap": 55},
    },
    "cucumber": {
        "A": {"planting": "09-15", "harvest_dap": 70},
        "B": {"planting": "02-15", "harvest_dap": 70},
    },
    "watermelon": {
        "A": {"planting": "09-15", "harvest_dap": 90},
        "B": {"planting": "02-15", "harvest_dap": 90},
    },
    "pumpkin": {
        "A": {"planting": "09-15", "harvest_dap": 110},
        "B": {"planting": "02-15", "harvest_dap": 110},
    },
    # --- Fruits (perennial — use Season A for main growing/fruiting cycle) ---
    "banana": {
        "A": {"planting": "09-01", "harvest_dap": 365},
        "B": {"planting": "02-15", "harvest_dap": 365},
    },
    "avocado": {
        "A": {"planting": "09-01", "harvest_dap": 730},
    },
    "mango": {
        "A": {"planting": "09-01", "harvest_dap": 545},
    },
    "passion_fruit": {
        "A": {"planting": "09-01", "harvest_dap": 270},
        "B": {"planting": "02-15", "harvest_dap": 270},
    },
    "pineapple": {
        "A": {"planting": "09-01", "harvest_dap": 540},
    },
    "papaya": {
        "A": {"planting": "09-01", "harvest_dap": 330},
        "B": {"planting": "02-15", "harvest_dap": 330},
    },
    "citrus": {
        "A": {"planting": "09-01", "harvest_dap": 600},
    },
    "strawberry": {
        "A": {"planting": "09-15", "harvest_dap": 110},
        "B": {"planting": "02-15", "harvest_dap": 110},
    },
    "tree_tomato": {
        "A": {"planting": "09-01", "harvest_dap": 365},
        "B": {"planting": "02-15", "harvest_dap": 365},
    },
    "guava": {
        "A": {"planting": "09-01", "harvest_dap": 480},
    },
    "cape_gooseberry": {
        "A": {"planting": "09-15", "harvest_dap": 150},
        "B": {"planting": "02-15", "harvest_dap": 150},
    },
    # --- Cash & industrial crops ---
    "coffee": {
        "A": {"planting": "09-01", "harvest_dap": 640},
    },
    "tea": {
        "A": {"planting": "09-01", "harvest_dap": 730},
    },
    "sugarcane": {
        "A": {"planting": "09-01", "harvest_dap": 420},
        "B": {"planting": "02-15", "harvest_dap": 420},
    },
    "pyrethrum": {
        "A": {"planting": "09-15", "harvest_dap": 210},
        "B": {"planting": "02-15", "harvest_dap": 210},
    },
    "tobacco": {
        "A": {"planting": "09-15", "harvest_dap": 120},
        "B": {"planting": "02-15", "harvest_dap": 120},
    },
    "sunflower": {
        "A": {"planting": "09-15", "harvest_dap": 105},
        "B": {"planting": "02-15", "harvest_dap": 105},
    },
    "macadamia": {
        "A": {"planting": "09-01", "harvest_dap": 730},
    },
    "sesame": {
        "A": {"planting": "09-15", "harvest_dap": 95},
        "B": {"planting": "02-15", "harvest_dap": 95},
    },
    # --- Oil crops ---
    "oil_palm": {
        "A": {"planting": "09-01", "harvest_dap": 730},
    },
    "soya": {
        "A": {"planting": "09-15", "harvest_dap": 110},
        "B": {"planting": "02-15", "harvest_dap": 110},
    },
}

# Beer-Lambert extinction coefficient for NDVI→LAI conversion
_K_EXT = 0.5


# ---------------------------------------------------------------------------
# Pedotransfer functions (Saxton & Rawls 2006)
# ---------------------------------------------------------------------------

def pedotransfer_saxton_rawls(
    clay_pct: float,
    sand_pct: float,
    organic_carbon_g_kg: float,
) -> Dict[str, float]:
    """Convert soil texture + organic carbon to DSSAT hydraulic parameters.

    Implements Saxton & Rawls (2006) pedotransfer functions.

    Args:
        clay_pct: Clay content (%)
        sand_pct: Sand content (%)
        organic_carbon_g_kg: Organic carbon (g/kg)

    Returns:
        Dict with SLLL, SDUL, SSAT, SBDM, SLOC, SLCL, SLSI
    """
    # Convert to fractions (0-1) for Saxton & Rawls equations
    S = sand_pct / 100.0
    C = clay_pct / 100.0
    # Organic matter ≈ organic carbon × 1.724; OC g/kg → fraction
    OM = (organic_carbon_g_kg / 10.0) * 1.724 / 100.0
    silt_pct = max(0.0, 100.0 - clay_pct - sand_pct)

    # Wilting point (1500 kPa) — Saxton & Rawls Eq. 1
    theta_1500t = (
        -0.024 * S
        + 0.487 * C
        + 0.006 * OM
        + 0.005 * S * OM
        - 0.013 * C * OM
        + 0.068 * S * C
        + 0.031
    )
    slll = theta_1500t + (0.14 * theta_1500t - 0.02)

    # Field capacity (33 kPa) — Saxton & Rawls Eq. 2
    theta_33t = (
        -0.251 * S
        + 0.195 * C
        + 0.011 * OM
        + 0.006 * S * OM
        - 0.027 * C * OM
        + 0.452 * S * C
        + 0.299
    )
    sdul = theta_33t + (1.283 * theta_33t * theta_33t - 0.374 * theta_33t - 0.015)

    # Saturation — from bulk density relationship
    # Saxton & Rawls Eq. 5
    theta_s33t = (
        0.278 * S
        + 0.034 * C
        + 0.022 * OM
        - 0.018 * S * OM
        - 0.027 * C * OM
        - 0.584 * S * C
        + 0.078
    )
    theta_s33 = theta_s33t + (0.636 * theta_s33t - 0.107)
    ssat = sdul + theta_s33 - 0.097 * S + 0.043

    # Bulk density from porosity
    sbdm = (1.0 - ssat) * 2.65

    # Clamp to physically valid ranges
    slll = max(0.01, min(0.50, slll))
    sdul = max(slll + 0.01, min(0.60, sdul))
    ssat = max(sdul + 0.01, min(0.80, ssat))
    sbdm = max(0.80, min(1.80, sbdm))

    return {
        "SLLL": round(slll, 3),   # Wilting point (cm³/cm³)
        "SDUL": round(sdul, 3),   # Field capacity (cm³/cm³)
        "SSAT": round(ssat, 3),   # Saturation (cm³/cm³)
        "SBDM": round(sbdm, 2),   # Bulk density (g/cm³)
        "SLOC": round(organic_carbon_g_kg / 10.0, 2),  # Organic C (%)
        "SLCL": round(clay_pct, 1),
        "SLSI": round(silt_pct, 1),
    }


# ---------------------------------------------------------------------------
# NDVI → LAI conversion
# ---------------------------------------------------------------------------

def ndvi_to_lai(ndvi: float, k_ext: float = _K_EXT) -> float:
    """Convert NDVI to LAI using Beer-Lambert law.

    LAI = -ln(1 - NDVI) / k_ext

    Args:
        ndvi: Normalized Difference Vegetation Index (0-1 for vegetation)
        k_ext: Light extinction coefficient (default 0.5)

    Returns:
        Leaf Area Index estimate
    """
    # Clamp NDVI to valid range for log
    ndvi_clamped = max(0.01, min(0.99, ndvi))
    return -math.log(1.0 - ndvi_clamped) / k_ext


# ---------------------------------------------------------------------------
# Season auto-detection
# ---------------------------------------------------------------------------

def detect_current_season(crop_type: str = "maize", ref_date: Optional[datetime] = None) -> str:
    """Auto-detect the current growing season from the date.

    Season A: Sep-Feb (planting Sep)
    Season B: Feb-Jul (planting Feb)

    Returns "A" or "B".
    """
    if ref_date is None:
        ref_date = datetime.utcnow()

    month = ref_date.month

    calendar = _CROP_CALENDARS.get(crop_type, _CROP_CALENDARS["maize"])

    # Wheat only has Season A
    if "B" not in calendar:
        return "A"

    # Season A: Sep(9) through Feb(2)
    if month >= 9 or month <= 2:
        return "A"
    # Season B: Mar(3) through Aug(8)
    return "B"


# ---------------------------------------------------------------------------
# Soil profile builder (iSDAsoil → DSSAT)
# ---------------------------------------------------------------------------

def _build_soil_profile(lat: float, lon: float) -> Optional[Any]:
    """Query iSDAsoil and convert to DSSAT SoilProfile via pedotransfer.

    Returns DSSATTools.SoilProfile or None if unavailable.
    Uses DSSATTools v3 API (SoilProfile + SoilLayer).
    """
    try:
        from DSSATTools import SoilLayer, SoilProfile
    except ImportError:
        logger.warning("DSSATTools not available — cannot build soil profile")
        return None

    from src.services.isdasoil_service import query_soil_point

    soil_props = [
        "clay_content", "sand_content", "carbon_organic",
        "ph", "bulk_density", "nitrogen_total", "cation_exchange_capacity",
    ]

    resp = query_soil_point(lon=lon, lat=lat, properties=soil_props, depth="0-20")
    if resp.get("status") != "success":
        logger.warning("iSDAsoil query failed for %.4f, %.4f: %s", lat, lon, resp.get("error"))
        return None

    props = resp.get("properties", {})

    clay = props.get("clay_content", {}).get("value")
    sand = props.get("sand_content", {}).get("value")
    oc = props.get("carbon_organic", {}).get("value")
    ph = props.get("ph", {}).get("value")
    ntot = props.get("nitrogen_total", {}).get("value")
    cec = props.get("cation_exchange_capacity", {}).get("value")

    # Minimum required: clay, sand, organic carbon
    if clay is None or sand is None or oc is None:
        logger.warning("Missing essential soil properties for %.4f, %.4f", lat, lon)
        return None

    pt = pedotransfer_saxton_rawls(
        clay_pct=float(clay),
        sand_pct=float(sand),
        organic_carbon_g_kg=float(oc),
    )

    # Build single 0-20cm SoilLayer (DSSATTools v3)
    layer = SoilLayer(
        slb=20,
        slll=pt["SLLL"],
        sdul=pt["SDUL"],
        ssat=pt["SSAT"],
        srgf=1.0,
        sbdm=pt["SBDM"],
        sloc=pt["SLOC"],
        slcl=pt["SLCL"],
        slsi=pt["SLSI"],
        slni=round(float(ntot) / 10.0, 3) if ntot else 0.1,
        slhw=round(float(ph), 1) if ph else 6.0,
        scec=round(float(cec), 1) if cec else 15.0,
    )

    # DSSATTools v3: SoilProfile requires table + surface parameters
    soil_profile = SoilProfile(
        table=[layer],
        name="ISDA000001",
        salb=0.13,   # Albedo (typical tropical)
        slu1=6.0,    # Stage 1 evaporation limit (mm)
        sldr=0.4,    # Drainage rate (fraction/day)
        slro=76.0,   # Runoff curve number
        slnf=1.0,    # N mineralisation factor
        slpf=1.0,    # Photosynthesis factor
    )

    return soil_profile


# ---------------------------------------------------------------------------
# Weather file builder
# ---------------------------------------------------------------------------

def _build_weather(
    lat: float,
    lon: float,
    date_from: str,
    date_to: str,
) -> Optional[Any]:
    """Fetch NASA POWER daily data and build DSSAT WeatherStation object.

    Falls back to Open-Meteo if POWER fails.
    Returns DSSATTools.WeatherStation or None (v3 API).
    """
    try:
        from DSSATTools import WeatherRecord, WeatherStation
    except ImportError:
        logger.warning("DSSATTools not available — cannot build weather")
        return None

    from src.services.nasa_power_service import fetch_power_daily_with_fallback

    data = fetch_power_daily_with_fallback(lat, lon, date_from, date_to)
    if not data or not data.get("dates"):
        logger.warning("No weather data available for %.4f, %.4f", lat, lon)
        return None

    records = []
    for i, date_str in enumerate(data["dates"]):
        try:
            dt = datetime.strptime(date_str, "%Y-%m-%d").date()
        except (ValueError, TypeError):
            continue
        srad = data["SRAD"][i] if data.get("SRAD") else 15.0
        tmax = data["TMAX"][i] if data.get("TMAX") else 28.0
        tmin = data["TMIN"][i] if data.get("TMIN") else 16.0
        rain = data["RAIN"][i] if data.get("RAIN") else 0.0
        if any(v is None for v in (srad, tmax, tmin, rain)):
            continue
        records.append(WeatherRecord(
            date=dt,
            srad=float(srad),
            tmax=float(tmax),
            tmin=float(tmin),
            rain=float(rain),
        ))

    if not records:
        logger.warning("No valid weather records for %.4f, %.4f", lat, lon)
        return None

    weather = WeatherStation(
        table=records,
        lat=lat,
        long=lon,
        insi="MWST",
    )

    return weather


# ---------------------------------------------------------------------------
# Crop management builder
# ---------------------------------------------------------------------------

def _build_treatment_components(
    crop_type: str,
    season: str,
    planting_year: int,
    soil: Any,
    weather: Any,
) -> Optional[Dict[str, Any]]:
    """Build DSSAT v3 treatment components with Rwanda-appropriate defaults.

    Fertilizer: 50 kg/ha N (DAP at planting) + 50 kg/ha N (urea at 30 DAP)
    — matches RAB (Rwanda Agriculture Board) recommendations for smallholders.

    Returns dict with field, cultivar, planting, simulation_controls, fertilizer
    or None if unavailable.
    """
    try:
        from DSSATTools.filex import (
            Fertilizer,
            FertilizerEvent,
            Field,
            Planting,
            SCGeneral,
            SimulationControls,
        )
    except ImportError:
        logger.warning("DSSATTools not available — cannot build treatment components")
        return None

    calendar = _CROP_CALENDARS.get(crop_type, _CROP_CALENDARS["maize"])
    season_cal = calendar.get(season)
    if not season_cal:
        logger.warning("No calendar for %s season %s", crop_type, season)
        return None

    planting_str = f"{planting_year}-{season_cal['planting']}"
    planting_dt = datetime.strptime(planting_str, "%Y-%m-%d").date()

    # Simulation start: 1 day before planting
    from datetime import timedelta
    sdate = planting_dt - timedelta(days=1)

    # Crop cultivar (v3 crop objects)
    from DSSATTools.crop import Maize, Rice, Sorghum, Wheat
    _CROP_CLASS_MAP = {
        "maize": (Maize, "IB0012"),      # PIO 3382 — medium maturity tropical
        "rice": (Rice, "IB0001"),
        "beans": (Maize, "IB0012"),       # Fallback: DSSAT DryBean not always available
        "sorghum": (Sorghum, "IB0001"),
        "wheat": (Wheat, "IB0001"),
    }

    crop_cls, cultivar_code = _CROP_CLASS_MAP.get(crop_type, (Maize, "IB0012"))
    try:
        cultivar = crop_cls(cultivar_code)
    except Exception as e:
        logger.warning("Failed to create crop cultivar %s/%s: %s", crop_type, cultivar_code, e)
        return None

    # Field links soil and weather to the treatment
    field = Field(id_field="MUNDI001", wsta=weather, id_soil=soil)

    # Planting details
    planting = Planting(
        pdate=planting_dt,
        ppop=6.0,     # Plants per m² (typical maize density)
        plrs=75.0,    # Row spacing (cm)
        pldp=5,       # Planting depth (cm)
    )

    # Simulation controls
    sc = SimulationControls(general=SCGeneral(sdate=sdate))

    # RAB smallholder fertilizer: 50 kg/ha N at planting + 50 kg/ha N at 30 DAP
    fert_date_2 = planting_dt + timedelta(days=30)
    fertilizer = Fertilizer(table=[
        FertilizerEvent(fdate=planting_dt, fmcd="FE005", facd="AP001", fdep=10, famn=50),
        FertilizerEvent(fdate=fert_date_2, fmcd="FE001", facd="AP002", fdep=5, famn=50),
    ])

    return {
        "field": field,
        "cultivar": cultivar,
        "planting": planting,
        "simulation_controls": sc,
        "fertilizer": fertilizer,
    }


# ---------------------------------------------------------------------------
# Main pipeline: DSSAT run + data assimilation
# ---------------------------------------------------------------------------

def run_dssat_with_assimilation(
    lat: float,
    lon: float,
    crop_type: str = "maize",
    season: Optional[str] = None,
    geom: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Run DSSAT simulation with Sentinel-2 LAI data assimilation.

    Pipeline:
    1. Build soil profile from iSDAsoil via pedotransfer
    2. Build weather file from NASA POWER
    3. Build management from crop calendar + default fertilizer
    4. Run DSSAT baseline → simulated yield + LAI curve
    5. Fetch Sentinel-2 NDVI time series
    6. Convert NDVI → LAI (Beer-Lambert)
    7. Data assimilation: LAI ratio scaling with [0.5, 1.5] clamp
    8. Return adjusted yield (t/ha) + diagnostics

    Args:
        lat: Latitude (WGS84)
        lon: Longitude (WGS84)
        crop_type: One of maize, rice, beans, sorghum, wheat
        season: "A" or "B" (auto-detected if None)
        geom: GeoJSON geometry for Sentinel-2 query (optional)

    Returns:
        Dict with yield_tha, baseline_tha, assimilation_ratio, crop, season
    """
    try:
        from DSSATTools import DSSAT
    except ImportError:
        logger.warning("DSSATTools not installed — returning 0.0 yield")
        return _error_result(crop_type, season or "A", "DSSATTools not available")

    if crop_type not in _CROP_CALENDARS:
        crop_type = "maize"

    if season is None:
        season = detect_current_season(crop_type)

    now = datetime.utcnow()

    # Determine planting year based on season
    if season == "A" and now.month < 9:
        planting_year = now.year - 1  # Season A started previous Sep
    else:
        planting_year = now.year

    calendar = _CROP_CALENDARS[crop_type][season]

    # Date range for weather: start of planting month → planting + harvest_dap + 15
    planting_str = f"{planting_year}-{calendar['planting']}"
    date_from = f"{planting_year}-{calendar['planting'].split('-')[0]}-01"
    harvest_dap = calendar["harvest_dap"]

    from datetime import timedelta
    planting_dt = datetime.strptime(planting_str, "%Y-%m-%d")
    end_dt = planting_dt + timedelta(days=harvest_dap + 15)
    # Don't fetch future weather
    if end_dt > now:
        end_dt = now
    date_to = end_dt.strftime("%Y-%m-%d")

    # 1. Build soil profile
    soil = _build_soil_profile(lat, lon)
    if soil is None:
        return _error_result(crop_type, season, "Soil profile unavailable")

    # 2. Build weather
    weather = _build_weather(lat, lon, date_from, date_to)
    if weather is None:
        return _error_result(crop_type, season, "Weather data unavailable")

    # 3. Build treatment components (v3 API: field, cultivar, planting, sc, fertilizer)
    components = _build_treatment_components(crop_type, season, planting_year, soil, weather)
    if components is None:
        return _error_result(crop_type, season, "Treatment setup failed")

    # 4. Run DSSAT (v3 API: run_treatment)
    try:
        dssat = DSSAT()
        dssat.run_treatment(
            field=components["field"],
            cultivar=components["cultivar"],
            planting=components["planting"],
            simulation_controls=components["simulation_controls"],
            fertilizer=components["fertilizer"],
        )

        tables = dssat.output_tables
        if not tables or "PlantGro" not in tables:
            dssat.close()
            return _error_result(crop_type, season, "DSSAT produced no output")

        plant_gro = tables["PlantGro"]

        # GWAD = grain weight above ground, dry (kg/ha)
        baseline_kg_ha = float(plant_gro["GWAD"].iloc[-1])
        baseline_tha = baseline_kg_ha / 1000.0

        # Get simulated LAI values for assimilation
        sim_lai_values = plant_gro.get("LAID")
        dssat.close()
    except Exception as e:
        logger.error("DSSAT simulation failed: %s", e)
        return _error_result(crop_type, season, f"DSSAT error: {e}")

    # 5-7. Data assimilation with Sentinel-2
    ratio = 1.0
    if geom is not None:
        ratio = _compute_assimilation_ratio(geom, sim_lai_values)

    adjusted_tha = round(baseline_tha * ratio, 2)

    return {
        "yield_tha": adjusted_tha,
        "baseline_tha": round(baseline_tha, 2),
        "assimilation_ratio": round(ratio, 3),
        "crop": crop_type,
        "season": season,
    }


def _compute_assimilation_ratio(
    geom: Dict[str, Any],
    sim_lai_values: Optional[Any] = None,
) -> float:
    """Compute LAI-based assimilation ratio from Sentinel-2 observations.

    ratio = mean(observed_LAI) / mean(simulated_LAI)
    Clamped to [0.5, 1.5] for robustness.

    Returns 1.0 if Sentinel-2 data unavailable.
    """
    try:
        from src.services.deafrica_stac import get_deafrica_service

        dea = get_deafrica_service()
        ts = dea.get_field_timeseries(geometry=geom, months=4)
        intervals = ts.get("intervals", [])

        # Extract NDVI means and convert to LAI
        observed_lais = []
        for iv in intervals:
            ndvi_data = iv.get("ndvi") or iv.get("NDVI", {})
            if isinstance(ndvi_data, dict):
                ndvi_mean = ndvi_data.get("mean")
            else:
                continue
            if ndvi_mean is not None and ndvi_mean > 0.1:
                observed_lais.append(ndvi_to_lai(ndvi_mean))

        if not observed_lais:
            return 1.0

        mean_obs_lai = sum(observed_lais) / len(observed_lais)

        # Get simulated LAI mean
        if sim_lai_values is not None and len(sim_lai_values) > 0:
            import numpy as np
            valid_sim = [float(v) for v in sim_lai_values if v > 0]
            if valid_sim:
                mean_sim_lai = float(np.mean(valid_sim))
                if mean_sim_lai > 0.1:
                    ratio = mean_obs_lai / mean_sim_lai
                    return max(0.5, min(1.5, ratio))

        return 1.0

    except Exception as e:
        logger.warning("Data assimilation failed, using ratio=1.0: %s", e)
        return 1.0


def _error_result(crop_type: str, season: str, reason: str) -> Dict[str, Any]:
    """Return a zero-yield result with error reason."""
    logger.warning("DSSAT yield forecast failed: %s", reason)
    return {
        "yield_tha": 0.0,
        "baseline_tha": 0.0,
        "assimilation_ratio": 1.0,
        "crop": crop_type,
        "season": season,
        "error": reason,
    }
