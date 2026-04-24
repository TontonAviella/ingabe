"""Insurance Intelligence Engine — one function, all signals, any audience, any admin level.

Connects 12 existing mundi.ai capabilities into a single unified report:
  CHIRPS rainfall, crop calendars, season detection, dry spells, NDVI concordance,
  binary accuracy, insurance confidence, WaPOR ET, WaPOR soil moisture, NDVI anomaly
  z-scores, bias correction, and admin boundary resolution.

Called by Sage via `get_insurance_intelligence` tool.
"""

from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from typing import Any, Optional

import asyncpg

logger = logging.getLogger(__name__)

_VALID_AUDIENCES = {"farmer", "insurance", "agronomist", "scientist"}

# ---------------------------------------------------------------------------
# Growth phases per crop (DAP = days after planting)
# ---------------------------------------------------------------------------

_GROWTH_PHASES: dict[str, dict[str, tuple[int, int]]] = {
    # --- Cereals ---
    "maize": {
        "planting": (0, 20),
        "vegetative": (20, 55),
        "flowering": (55, 75),
        "grain_fill": (75, 105),
        "maturity": (105, 120),
    },
    "beans": {
        "planting": (0, 15),
        "vegetative": (15, 40),
        "flowering": (40, 55),
        "grain_fill": (55, 80),
        "maturity": (80, 90),
    },
    "rice": {
        "planting": (0, 25),
        "vegetative": (25, 70),
        "flowering": (70, 100),
        "grain_fill": (100, 135),
        "maturity": (135, 150),
    },
    "sorghum": {
        "planting": (0, 20),
        "vegetative": (20, 50),
        "flowering": (50, 70),
        "grain_fill": (70, 100),
        "maturity": (100, 110),
    },
    "wheat": {
        "planting": (0, 20),
        "vegetative": (20, 50),
        "flowering": (50, 75),
        "grain_fill": (75, 105),
        "maturity": (105, 120),
    },
    "finger_millet": {
        "planting": (0, 15),
        "vegetative": (15, 45),
        "flowering": (45, 65),
        "grain_fill": (65, 90),
        "maturity": (90, 105),
    },
    # --- Tubers & roots ---
    "potato": {
        "planting": (0, 20),
        "vegetative": (20, 45),
        "flowering": (45, 65),
        "grain_fill": (65, 95),
        "maturity": (95, 110),
    },
    "sweet_potato": {
        "planting": (0, 25),
        "vegetative": (25, 60),
        "flowering": (60, 90),
        "grain_fill": (90, 120),
        "maturity": (120, 150),
    },
    "cassava": {
        "planting": (0, 30),
        "vegetative": (30, 120),
        "flowering": (120, 180),
        "grain_fill": (180, 300),
        "maturity": (300, 365),
    },
    "yam": {
        "planting": (0, 30),
        "vegetative": (30, 90),
        "flowering": (90, 150),
        "grain_fill": (150, 210),
        "maturity": (210, 270),
    },
    "taro": {
        "planting": (0, 25),
        "vegetative": (25, 80),
        "flowering": (80, 140),
        "grain_fill": (140, 200),
        "maturity": (200, 270),
    },
    # --- Legumes ---
    "soybean": {
        "planting": (0, 15),
        "vegetative": (15, 45),
        "flowering": (45, 65),
        "grain_fill": (65, 95),
        "maturity": (95, 110),
    },
    "groundnut": {
        "planting": (0, 15),
        "vegetative": (15, 40),
        "flowering": (40, 65),
        "grain_fill": (65, 100),
        "maturity": (100, 120),
    },
    "peas": {
        "planting": (0, 15),
        "vegetative": (15, 35),
        "flowering": (35, 50),
        "grain_fill": (50, 70),
        "maturity": (70, 85),
    },
    "cowpea": {
        "planting": (0, 12),
        "vegetative": (12, 35),
        "flowering": (35, 50),
        "grain_fill": (50, 70),
        "maturity": (70, 80),
    },
    "pigeon_pea": {
        "planting": (0, 20),
        "vegetative": (20, 60),
        "flowering": (60, 100),
        "grain_fill": (100, 140),
        "maturity": (140, 170),
    },
    # --- Vegetables ---
    "tomato": {
        "planting": (0, 20),
        "vegetative": (20, 45),
        "flowering": (45, 65),
        "grain_fill": (65, 90),
        "maturity": (90, 110),
    },
    "onion": {
        "planting": (0, 20),
        "vegetative": (20, 55),
        "flowering": (55, 80),
        "grain_fill": (80, 110),
        "maturity": (110, 130),
    },
    "cabbage": {
        "planting": (0, 20),
        "vegetative": (20, 50),
        "flowering": (50, 65),
        "grain_fill": (65, 80),
        "maturity": (80, 95),
    },
    "carrot": {
        "planting": (0, 15),
        "vegetative": (15, 45),
        "flowering": (45, 65),
        "grain_fill": (65, 85),
        "maturity": (85, 100),
    },
    "chili": {
        "planting": (0, 25),
        "vegetative": (25, 55),
        "flowering": (55, 80),
        "grain_fill": (80, 110),
        "maturity": (110, 130),
    },
    "eggplant": {
        "planting": (0, 25),
        "vegetative": (25, 55),
        "flowering": (55, 80),
        "grain_fill": (80, 110),
        "maturity": (110, 130),
    },
    "green_pepper": {
        "planting": (0, 25),
        "vegetative": (25, 55),
        "flowering": (55, 75),
        "grain_fill": (75, 100),
        "maturity": (100, 120),
    },
    "garlic": {
        "planting": (0, 20),
        "vegetative": (20, 60),
        "flowering": (60, 90),
        "grain_fill": (90, 120),
        "maturity": (120, 150),
    },
    "amaranth": {
        "planting": (0, 12),
        "vegetative": (12, 35),
        "flowering": (35, 50),
        "grain_fill": (50, 65),
        "maturity": (65, 75),
    },
    "leek": {
        "planting": (0, 20),
        "vegetative": (20, 60),
        "flowering": (60, 90),
        "grain_fill": (90, 120),
        "maturity": (120, 150),
    },
    "lettuce": {
        "planting": (0, 10),
        "vegetative": (10, 30),
        "flowering": (30, 40),
        "grain_fill": (40, 50),
        "maturity": (50, 60),
    },
    "spinach": {
        "planting": (0, 10),
        "vegetative": (10, 25),
        "flowering": (25, 35),
        "grain_fill": (35, 45),
        "maturity": (45, 55),
    },
    "cucumber": {
        "planting": (0, 12),
        "vegetative": (12, 30),
        "flowering": (30, 45),
        "grain_fill": (45, 60),
        "maturity": (60, 70),
    },
    "watermelon": {
        "planting": (0, 15),
        "vegetative": (15, 35),
        "flowering": (35, 55),
        "grain_fill": (55, 75),
        "maturity": (75, 90),
    },
    "pumpkin": {
        "planting": (0, 15),
        "vegetative": (15, 40),
        "flowering": (40, 60),
        "grain_fill": (60, 85),
        "maturity": (85, 110),
    },
    # --- Fruits ---
    "banana": {
        "planting": (0, 60),
        "vegetative": (60, 180),
        "flowering": (180, 240),
        "grain_fill": (240, 330),
        "maturity": (330, 365),
    },
    "avocado": {
        "planting": (0, 90),
        "vegetative": (90, 365),
        "flowering": (365, 420),
        "grain_fill": (420, 600),
        "maturity": (600, 730),
    },
    "mango": {
        "planting": (0, 90),
        "vegetative": (90, 365),
        "flowering": (365, 400),
        "grain_fill": (400, 500),
        "maturity": (500, 545),
    },
    "passion_fruit": {
        "planting": (0, 30),
        "vegetative": (30, 120),
        "flowering": (120, 160),
        "grain_fill": (160, 230),
        "maturity": (230, 270),
    },
    "pineapple": {
        "planting": (0, 30),
        "vegetative": (30, 240),
        "flowering": (240, 300),
        "grain_fill": (300, 450),
        "maturity": (450, 540),
    },
    "papaya": {
        "planting": (0, 30),
        "vegetative": (30, 120),
        "flowering": (120, 180),
        "grain_fill": (180, 270),
        "maturity": (270, 330),
    },
    "citrus": {
        "planting": (0, 90),
        "vegetative": (90, 365),
        "flowering": (365, 400),
        "grain_fill": (400, 540),
        "maturity": (540, 600),
    },
    "strawberry": {
        "planting": (0, 20),
        "vegetative": (20, 50),
        "flowering": (50, 70),
        "grain_fill": (70, 95),
        "maturity": (95, 110),
    },
    "tree_tomato": {
        "planting": (0, 60),
        "vegetative": (60, 180),
        "flowering": (180, 240),
        "grain_fill": (240, 330),
        "maturity": (330, 365),
    },
    "guava": {
        "planting": (0, 60),
        "vegetative": (60, 240),
        "flowering": (240, 300),
        "grain_fill": (300, 420),
        "maturity": (420, 480),
    },
    "cape_gooseberry": {
        "planting": (0, 20),
        "vegetative": (20, 60),
        "flowering": (60, 90),
        "grain_fill": (90, 120),
        "maturity": (120, 150),
    },
    # --- Cash & industrial crops ---
    "coffee": {
        "planting": (0, 90),
        "vegetative": (90, 365),
        "flowering": (365, 400),
        "grain_fill": (400, 580),
        "maturity": (580, 640),
    },
    "tea": {
        "planting": (0, 90),
        "vegetative": (90, 365),
        "flowering": (365, 420),
        "grain_fill": (420, 540),
        "maturity": (540, 730),
    },
    "sugarcane": {
        "planting": (0, 30),
        "vegetative": (30, 120),
        "flowering": (120, 240),
        "grain_fill": (240, 330),
        "maturity": (330, 420),
    },
    "pyrethrum": {
        "planting": (0, 20),
        "vegetative": (20, 90),
        "flowering": (90, 150),
        "grain_fill": (150, 180),
        "maturity": (180, 210),
    },
    "tobacco": {
        "planting": (0, 20),
        "vegetative": (20, 55),
        "flowering": (55, 80),
        "grain_fill": (80, 105),
        "maturity": (105, 120),
    },
    "sunflower": {
        "planting": (0, 15),
        "vegetative": (15, 45),
        "flowering": (45, 65),
        "grain_fill": (65, 90),
        "maturity": (90, 105),
    },
    "macadamia": {
        "planting": (0, 90),
        "vegetative": (90, 365),
        "flowering": (365, 420),
        "grain_fill": (420, 600),
        "maturity": (600, 730),
    },
    "sesame": {
        "planting": (0, 12),
        "vegetative": (12, 35),
        "flowering": (35, 55),
        "grain_fill": (55, 80),
        "maturity": (80, 95),
    },
    # --- Oil crops ---
    "oil_palm": {
        "planting": (0, 90),
        "vegetative": (90, 365),
        "flowering": (365, 420),
        "grain_fill": (420, 600),
        "maturity": (600, 730),
    },
    "soya": {
        "planting": (0, 15),
        "vegetative": (15, 45),
        "flowering": (45, 65),
        "grain_fill": (65, 95),
        "maturity": (95, 110),
    },
}

# Approximate long-term seasonal rainfall normals (mm) for Rwanda
# Source: CHIRPS 2000-2020 seasonal averages across 30 districts
_RWANDA_CENTER = (-1.94, 29.87)

# WaPOR v3 long-term average ET for Rwanda cropland (mm/dekad).
# Single national value. District-specific means need historical WaPOR analysis.
_ET_LONG_TERM_MEAN = 3.5

_RAINFALL_NORMALS: dict[str, dict[str, float]] = {
    "A": {"mean": 400.0, "std": 85.0},
    "B": {"mean": 350.0, "std": 75.0},
}


# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------

@dataclass
class PhaseRainfall:
    phase: str
    cumulative_mm: float
    day_count: int
    daily_avg_mm: float
    date_from: str
    date_to: str


@dataclass
class TriggerResult:
    signal: str
    current_value: float
    threshold: float
    direction: str
    triggered: bool
    margin_pct: float
    weight: float
    description: str

    def to_dict(self) -> dict:
        return {
            "signal": self.signal,
            "current_value": round(self.current_value, 2),
            "threshold": self.threshold,
            "direction": self.direction,
            "triggered": self.triggered,
            "margin_pct": round(self.margin_pct, 1),
            "weight": self.weight,
            "description": self.description,
        }


@dataclass
class InsuranceReport:
    location_name: str
    admin_level: str
    crop: str
    season: str
    growth_phase: str
    days_after_planting: int

    phase_rainfall: list[PhaseRainfall] = field(default_factory=list)
    season_rainfall_mm: float = 0.0
    spi: float = 0.0

    ndvi_z_score: Optional[float] = None
    ndvi_concordance_score: Optional[float] = None

    et_anomaly_pct: Optional[float] = None
    soil_moisture_pct: Optional[float] = None

    max_dry_spell_days: int = 0
    active_dry_spell_days: int = 0

    triggers: list[TriggerResult] = field(default_factory=list)
    triggers_activated: int = 0
    triggers_total: int = 0

    confidence_score: int = 0
    overall_status: str = "UNKNOWN"
    recommendation: str = ""

    accuracy_components: Optional[dict] = None

    sources: list[str] = field(default_factory=list)
    period_start: str = ""
    period_end: str = ""
    computed_at: str = ""
    geometry: Optional[dict] = None

    def to_dict(self) -> dict:
        return {
            "location": self.location_name,
            "admin_level": self.admin_level,
            "crop": self.crop,
            "season": self.season,
            "growth_phase": self.growth_phase,
            "days_after_planting": self.days_after_planting,
            "season_rainfall_mm": round(self.season_rainfall_mm, 1),
            "spi": round(self.spi, 2),
            "phase_rainfall": [
                {
                    "phase": p.phase,
                    "cumulative_mm": round(p.cumulative_mm, 1),
                    "day_count": p.day_count,
                    "daily_avg_mm": round(p.daily_avg_mm, 1),
                    "date_from": p.date_from,
                    "date_to": p.date_to,
                }
                for p in self.phase_rainfall
            ],
            "ndvi_z_score": round(self.ndvi_z_score, 2) if self.ndvi_z_score is not None else None,
            "ndvi_concordance_score": round(self.ndvi_concordance_score, 2) if self.ndvi_concordance_score is not None else None,
            "et_anomaly_pct": round(self.et_anomaly_pct, 1) if self.et_anomaly_pct is not None else None,
            "soil_moisture_pct": round(self.soil_moisture_pct, 1) if self.soil_moisture_pct is not None else None,
            "max_dry_spell_days": self.max_dry_spell_days,
            "active_dry_spell_days": self.active_dry_spell_days,
            "triggers": [t.to_dict() for t in self.triggers],
            "triggers_activated": self.triggers_activated,
            "triggers_total": self.triggers_total,
            "confidence_score": self.confidence_score,
            "overall_status": self.overall_status,
            "recommendation": self.recommendation,
            "sources": self.sources,
            "period_start": self.period_start,
            "period_end": self.period_end,
            "computed_at": self.computed_at,
        }


# ---------------------------------------------------------------------------
# 1. Growth-phase rainfall accumulation
# ---------------------------------------------------------------------------

def _get_planting_date(crop: str, season: str, year: int) -> date:
    """Get planting date for a crop/season/year from crop calendars."""
    from src.services.dssat_service import _CROP_CALENDARS

    cal = _CROP_CALENDARS.get(crop, {}).get(season)
    if not cal:
        cal = _CROP_CALENDARS.get("maize", {}).get("A", {"planting": "09-15"})
    month, day = cal["planting"].split("-")
    return date(year, int(month), int(day))


def _get_harvest_dap(crop: str, season: str) -> int:
    from src.services.dssat_service import _CROP_CALENDARS
    cal = _CROP_CALENDARS.get(crop, {}).get(season)
    if not cal:
        return 120
    return cal.get("harvest_dap", 120)


def _current_growth_phase(crop: str, dap: int) -> str:
    phases = _GROWTH_PHASES.get(crop, _GROWTH_PHASES["maize"])
    for phase_name, (start, end) in phases.items():
        if start <= dap < end:
            return phase_name
    return "maturity"


def _compute_phase_rainfall(
    daily_precip: dict[str, Optional[float]],
    planting_date: date,
    crop: str,
    today: date,
) -> list[PhaseRainfall]:
    """Accumulate rainfall per growth phase from daily CHIRPS data."""
    phases = _GROWTH_PHASES.get(crop, _GROWTH_PHASES["maize"])
    results = []

    for phase_name, (dap_start, dap_end) in phases.items():
        phase_start = planting_date + timedelta(days=dap_start)
        phase_end = min(planting_date + timedelta(days=dap_end), today)
        if phase_start > today:
            break

        total_mm = 0.0
        day_count = 0
        total_days = 0
        d = phase_start
        while d < phase_end:
            total_days += 1
            key = d.strftime("%Y-%m-%d")
            val = daily_precip.get(key)
            if val is not None:
                total_mm += val
                day_count += 1
            d += timedelta(days=1)

        # Extrapolate only when we have ≥30% sample coverage; otherwise
        # report the raw sum to avoid amplifying sparse observations.
        min_coverage = 0.3
        if day_count > 0 and total_days > 0 and (day_count / total_days) >= min_coverage:
            daily_avg = total_mm / day_count
            estimated_cumulative = daily_avg * total_days
        else:
            daily_avg = total_mm / max(day_count, 1)
            estimated_cumulative = total_mm

        results.append(PhaseRainfall(
            phase=phase_name,
            cumulative_mm=estimated_cumulative,
            day_count=day_count,
            daily_avg_mm=daily_avg,
            date_from=phase_start.strftime("%Y-%m-%d"),
            date_to=phase_end.strftime("%Y-%m-%d"),
        ))

    return results


# ---------------------------------------------------------------------------
# 2. Simplified SPI
# ---------------------------------------------------------------------------

def _compute_spi(season_rainfall_mm: float, season: str) -> float:
    """Approximate SPI from season cumulative vs long-term normals."""
    normals = _RAINFALL_NORMALS.get(season, _RAINFALL_NORMALS["A"])
    if normals["std"] == 0:
        return 0.0
    return (season_rainfall_mm - normals["mean"]) / normals["std"]


# ---------------------------------------------------------------------------
# 3. NDVI anomaly from database cache
# ---------------------------------------------------------------------------

async def _fetch_ndvi_anomaly(
    conn: asyncpg.Connection,
    district: Optional[str] = None,
) -> Optional[float]:
    """Get latest mean NDVI z-score from anomaly_alerts_cache."""
    try:
        if district:
            row = await conn.fetchrow(
                "SELECT AVG(z_score) as mean_z FROM anomaly_alerts_cache "
                "WHERE LOWER(district) = LOWER($1) "
                "AND computed_at > NOW() - INTERVAL '30 days'",
                district,
            )
        else:
            row = await conn.fetchrow(
                "SELECT AVG(z_score) as mean_z FROM anomaly_alerts_cache "
                "WHERE computed_at > NOW() - INTERVAL '30 days'",
            )
        if row and row["mean_z"] is not None:
            return float(row["mean_z"])
    except Exception:
        logger.debug("anomaly_alerts_cache query failed", exc_info=True)
    return None


async def _fetch_sar_backscatter(
    lat: float,
    lon: float,
    date_from: str,
    date_to: str,
) -> Optional[float]:
    """Get mean VH/VV ratio from Sentinel-1 SAR. Cloud-penetrating."""
    try:
        from src.services.sentinel1_service import get_sentinel1_service
        svc = get_sentinel1_service()
        buf = 0.05
        bbox = (lon - buf, lat - buf, lon + buf, lat + buf)
        result = await asyncio.to_thread(
            svc.get_backscatter,
            bbox=bbox,
            date_range=f"{date_from}/{date_to}",
        )
        if result and result.get("status") == "ok":
            stats = result.get("statistics", {})
            vh_mean = stats.get("vh", {}).get("mean")
            vv_mean = stats.get("vv", {}).get("mean")
            if vh_mean is not None and vv_mean is not None and vv_mean != 0:
                # Reject NoData sentinels and implausible values.
                # Plausible SAR backscatter: -50 to +10 dB, or 0 to ~10 in linear.
                if vv_mean < -50 or vv_mean > 10 or vh_mean < -50 or vh_mean > 10:
                    return None
                if vv_mean < 0:
                    return 10 ** ((vh_mean - vv_mean) / 10)
                return vh_mean / vv_mean
    except Exception:
        logger.debug("SAR backscatter fetch failed", exc_info=True)
    return None


async def _fetch_ndvi_with_sar_fallback(
    conn: asyncpg.Connection,
    lat: float,
    lon: float,
    date_from: str,
    date_to: str,
    district: Optional[str] = None,
) -> Optional[float]:
    """Get NDVI z-score from optical first, fall back to SAR-predicted NDVI."""
    ndvi_z = await _fetch_ndvi_anomaly(conn, district)
    if ndvi_z is not None:
        return ndvi_z
    try:
        from src.services.sar_ndvi import get_sar_ndvi_predictor
        pred = get_sar_ndvi_predictor()
        buf = 0.05
        bbox = (lon - buf, lat - buf, lon + buf, lat + buf)
        result = await asyncio.to_thread(pred.predict_ndvi, bbox=bbox)
        if result and result.get("status") == "ok":
            predicted = result.get("predicted_ndvi")
            if predicted is not None:
                mean_ndvi = 0.45
                std_ndvi = 0.15
                return (predicted - mean_ndvi) / std_ndvi if std_ndvi > 0 else 0.0
    except Exception:
        logger.debug("SAR-predicted NDVI fallback failed", exc_info=True)
    return None


# ---------------------------------------------------------------------------
# 4. Centroid from GeoJSON geometry
# ---------------------------------------------------------------------------

def _centroid_from_geojson(geom: dict) -> tuple[float, float]:
    """Extract approximate centroid (lat, lon) from a GeoJSON geometry."""
    coords = _flatten_coords(geom.get("coordinates", []))
    if not coords:
        return _RWANDA_CENTER
    lons = [c[0] for c in coords]
    lats = [c[1] for c in coords]
    return (sum(lats) / len(lats), sum(lons) / len(lons))


def _flatten_coords(coords: Any) -> list[tuple[float, float]]:
    """Recursively flatten nested coordinate arrays to (lon, lat) pairs."""
    if not coords:
        return []
    if isinstance(coords[0], (int, float)):
        return [(coords[0], coords[1])]
    result = []
    for item in coords:
        result.extend(_flatten_coords(item))
    return result


# ---------------------------------------------------------------------------
# 5. Trigger evaluation
# ---------------------------------------------------------------------------

async def _load_triggers(
    conn: asyncpg.Connection,
    crop: str,
    season: str,
    phase: str,
    district: Optional[str] = None,
) -> list[dict]:
    """Load trigger thresholds from insurance_triggers table.

    District-specific rows override national defaults (district IS NULL)
    for the same (phase, signal) combination.
    """
    try:
        rows = await conn.fetch(
            "SELECT DISTINCT ON (phase, signal) "
            "signal, direction, threshold, weight, description "
            "FROM insurance_triggers "
            "WHERE crop = $1 AND season = $2 AND (phase = $3 OR phase = 'full_season') "
            "AND enabled = true "
            "AND (district IS NULL OR LOWER(district) = LOWER($4)) "
            "ORDER BY phase, signal, "
            "CASE WHEN district IS NOT NULL THEN 0 ELSE 1 END, "
            "weight DESC",
            crop, season, phase, district,
        )
        return [dict(r) for r in rows]
    except Exception:
        logger.debug("insurance_triggers table not available, using defaults", exc_info=True)
        return _default_triggers(phase)


def _default_triggers(phase: str) -> list[dict]:
    """Hardcoded fallback triggers when the table doesn't exist yet."""
    triggers = [
        {"signal": "rainfall_cumulative", "direction": "below", "threshold": 100.0, "weight": 1.0,
         "description": "Season cumulative rainfall below 100mm"},
        {"signal": "spi", "direction": "below", "threshold": -1.0, "weight": 0.8,
         "description": "SPI indicates moderate drought"},
        {"signal": "dry_spell_days", "direction": "above", "threshold": 15.0, "weight": 0.6,
         "description": "Maximum dry spell exceeds 15 consecutive days"},
        {"signal": "ndvi_z_score", "direction": "below", "threshold": -1.5, "weight": 0.8,
         "description": "NDVI anomaly indicates severe vegetation stress"},
        {"signal": "et_anomaly", "direction": "below", "threshold": -20.0, "weight": 0.4,
         "description": "ET anomaly exceeds -20% deficit"},
        {"signal": "sar_backscatter", "direction": "below", "threshold": 0.15, "weight": 0.7,
         "description": "SAR VH/VV ratio below 0.15 indicates low vegetation density"},
    ]
    return triggers


def _evaluate_triggers(
    trigger_defs: list[dict],
    current_values: dict[str, Optional[float]],
) -> list[TriggerResult]:
    """Evaluate each trigger against current signal values."""
    results = []
    for trig in trigger_defs:
        signal = trig["signal"]
        value = current_values.get(signal)
        if value is None:
            continue

        threshold = trig["threshold"]
        direction = trig["direction"]
        weight = trig.get("weight", 1.0)

        if direction == "below":
            triggered = value < threshold
            margin = ((value - threshold) / abs(threshold)) * 100 if threshold != 0 else 0
        else:
            triggered = value > threshold
            margin = ((value - threshold) / abs(threshold)) * 100 if threshold != 0 else 0
        margin = max(-999, min(999, margin))

        results.append(TriggerResult(
            signal=signal,
            current_value=value,
            threshold=threshold,
            direction=direction,
            triggered=triggered,
            margin_pct=margin,
            weight=weight,
            description=trig.get("description", signal),
        ))

    return results


# ---------------------------------------------------------------------------
# 6. Composite confidence score
# ---------------------------------------------------------------------------

def _compute_confidence(
    triggers: list[TriggerResult],
    expected_signals: int = 0,
) -> tuple[int, str]:
    """Weighted composite confidence score (0-100) and status label.

    When expected_signals > len(triggers), confidence is penalized
    proportionally — missing data means lower certainty.
    """
    if not triggers:
        return 50, "UNKNOWN"

    total_weight = sum(t.weight for t in triggers)
    if total_weight == 0:
        return 50, "UNKNOWN"

    passing_weight = sum(t.weight for t in triggers if not t.triggered)
    score = int((passing_weight / total_weight) * 100)

    if expected_signals > 0 and len(triggers) < expected_signals:
        coverage = len(triggers) / expected_signals
        score = int(score * coverage)

    activated = sum(1 for t in triggers if t.triggered)
    high_weight_activated = any(t.triggered and t.weight >= 0.8 for t in triggers)

    if activated == 0:
        status = "SAFE"
    elif activated == 1 and not high_weight_activated:
        status = "WATCH"
    elif activated <= 2:
        status = "WARNING"
    else:
        status = "PAYOUT_LIKELY"

    return score, status


def _generate_recommendation(
    status: str, crop: str, phase: str, triggers: list[TriggerResult],
) -> str:
    """Generate actionable recommendation based on trigger results."""
    activated = [t for t in triggers if t.triggered]

    if status == "SAFE":
        return f"{crop.title()} crop in {phase} phase is progressing normally. No intervention needed."

    signals = ", ".join(t.signal.replace("_", " ") for t in activated)

    if status == "WATCH":
        return (
            f"Monitor closely: {signals} approaching threshold. "
            f"Recommend field verification within 7 days."
        )
    if status == "WARNING":
        return (
            f"Warning: {signals} exceeded threshold. "
            f"Recommend immediate field assessment and consider early payout preparation."
        )
    return (
        f"Multiple triggers activated ({signals}). "
        f"Payout conditions likely met. Initiate claims verification process."
    )


# ---------------------------------------------------------------------------
# 7. Audience presentation layer
# ---------------------------------------------------------------------------

def format_for_audience(report: InsuranceReport, audience: str) -> str:
    """Format the same report for different audiences."""
    if audience == "farmer":
        return _format_farmer(report)
    if audience == "insurance":
        return _format_insurance(report)
    if audience == "agronomist":
        return _format_agronomist(report)
    if audience == "scientist":
        return _format_scientist(report)
    return _format_insurance(report)


def _format_farmer(r: InsuranceReport) -> str:
    """WhatsApp-ready, <200 chars per section, clear and simple."""
    status_emoji = {"SAFE": "✅", "WATCH": "👀", "WARNING": "⚠️", "PAYOUT_LIKELY": "🚨"}.get(
        r.overall_status, "❓"
    )
    status_word = {
        "SAFE": "SAFE", "WATCH": "NEEDS WATCHING",
        "WARNING": "AT RISK", "PAYOUT_LIKELY": "INSURANCE MAY PAY",
    }.get(r.overall_status, "UNKNOWN")

    lines = [
        f"{status_emoji} Your {r.crop} in {r.location_name} is {status_word}.",
        f"Rain this season: {r.season_rainfall_mm:.0f}mm",
    ]
    if r.max_dry_spell_days > 0:
        lines.append(f"Longest dry spell: {r.max_dry_spell_days} days")
    if r.ndvi_z_score is not None:
        health = "healthy" if r.ndvi_z_score > -0.5 else "stressed" if r.ndvi_z_score > -1.5 else "very stressed"
        lines.append(f"Vegetation: {health}")

    activated = [t for t in r.triggers if t.triggered]
    if not activated:
        lines.append("No drought trigger activated.")
    else:
        lines.append(f"{len(activated)} trigger(s) activated — contact your insurance agent.")

    lines.append(f"Growth stage: {r.growth_phase} (day {r.days_after_planting})")
    return "\n".join(lines)


def _format_insurance(r: InsuranceReport) -> str:
    """Trigger assessment table for insurance workers."""
    header = (
        f"TRIGGER ASSESSMENT: {r.location_name} — {r.crop.title()} Season {r.season} "
        f"({r.period_start} to {r.period_end})"
    )

    activated = sum(1 for t in r.triggers if t.triggered)
    status_line = (
        f"Status: {r.overall_status} | "
        f"Triggers: {activated}/{r.triggers_total} activated | "
        f"Confidence: {r.confidence_score}/100"
    )

    rows = []
    for t in r.triggers:
        status = "TRIGGERED" if t.triggered else "PASS"
        # Show the trigger condition: "below" means payout if current < threshold
        if t.direction == "below":
            op = "<"
        else:
            op = ">"
        rows.append(
            f"  {t.signal:<22s} {t.current_value:>8.1f}  {op}{t.threshold:<8.1f}  "
            f"{status:<10s} {t.weight:.1f}"
        )

    table = "\n".join([
        f"  {'Signal':<22s} {'Current':>8s}  {'Threshold':<9s}  {'Status':<10s} {'Weight'}",
        "  " + "-" * 65,
        *rows,
    ])

    sources = ", ".join(r.sources) if r.sources else "CHIRPS, Sentinel-1/2, WaPOR"
    phase_info = f"Phase: {r.growth_phase} (day {r.days_after_planting} of {_get_harvest_dap(r.crop, r.season)})"

    return f"{header}\n{status_line}\n\n{table}\n\n{phase_info}\nSources: {sources}"


def _format_agronomist(r: InsuranceReport) -> str:
    """Technical detail + recommendations."""
    lines = [
        f"AGRONOMIC ASSESSMENT: {r.location_name} — {r.crop.title()} Season {r.season}",
        f"Growth phase: {r.growth_phase} (day {r.days_after_planting} of {_get_harvest_dap(r.crop, r.season)})",
        "",
        "RAINFALL:",
        f"  Season cumulative: {r.season_rainfall_mm:.0f}mm | SPI: {r.spi:.2f}",
    ]

    for p in r.phase_rainfall:
        lines.append(f"  {p.phase:<12s}: {p.cumulative_mm:.0f}mm over {p.day_count} days ({p.daily_avg_mm:.1f}mm/day)")

    if r.max_dry_spell_days > 0:
        lines.append(f"  Max dry spell: {r.max_dry_spell_days} days")
    if r.active_dry_spell_days > 0:
        lines.append(f"  Active dry spell: {r.active_dry_spell_days} days (ongoing)")

    lines.append("")
    lines.append("VEGETATION:")
    if r.ndvi_z_score is not None:
        lines.append(f"  NDVI z-score: {r.ndvi_z_score:.2f}")
    if r.ndvi_concordance_score is not None:
        lines.append(f"  Rainfall-NDVI concordance: {r.ndvi_concordance_score:.2f}")

    lines.append("")
    lines.append("WATER BALANCE:")
    if r.et_anomaly_pct is not None:
        lines.append(f"  ET anomaly: {r.et_anomaly_pct:+.1f}%")
    if r.soil_moisture_pct is not None:
        lines.append(f"  Soil moisture: {r.soil_moisture_pct:.1f}%")

    lines.append("")
    lines.append(f"STATUS: {r.overall_status} (confidence {r.confidence_score}/100)")
    lines.append(f"RECOMMENDATION: {r.recommendation}")

    return "\n".join(lines)


def _format_scientist(r: InsuranceReport) -> str:
    """Full JSON with methodology and provenance — returned as formatted string."""
    data = r.to_dict()
    data["methodology"] = {
        "rainfall": "CHIRPS v2.0 daily precipitation, 0.05° resolution",
        "spi": f"Simplified SPI: (cumulative - mean) / std, normals: {_RAINFALL_NORMALS}",
        "ndvi": "Sentinel-2 NDVI with SAR fallback (cloud-penetrating) anomaly z-scores",
        "sar_backscatter": "Sentinel-1 C-band SAR VH/VV ratio, cloud-penetrating vegetation density",
        "ndvi_concordance": "Rainfall deficit vs NDVI response lag analysis",
        "et": "WaPOR v3 AETI dekadal, 100m resolution",
        "soil_moisture": "WaPOR v3 relative soil moisture, dekadal",
        "dry_spells": "Consecutive days < 2mm threshold from CHIRPS daily",
        "triggers": "Parametric thresholds from insurance_triggers table",
        "confidence": "Weighted composite: passing_weight / total_weight * 100",
    }
    return json.dumps(data, indent=2, default=str)


# ---------------------------------------------------------------------------
# 8. Composite orchestrator — THE MAIN ENTRY POINT
# ---------------------------------------------------------------------------

async def compute_insurance_intelligence(
    conn: asyncpg.Connection,
    crop: str = "maize",
    season: Optional[str] = None,
    district: Optional[str] = None,
    sector: Optional[str] = None,
    cell: Optional[str] = None,
    village: Optional[str] = None,
    audience: str = "farmer",
    ref_date: Optional[date] = None,
) -> dict[str, Any]:
    """One call, all signals, any audience, any admin level.

    Returns dict with 'status', 'report' (formatted string), 'data' (raw dict),
    and 'geometry' (GeoJSON for Brain persistence).
    """
    from src.services.dssat_service import detect_current_season

    if not any([district, sector, cell, village]):
        return {
            "status": "error",
            "error": "At least one location parameter (district, sector, cell, or village) is required.",
        }

    today = ref_date or date.today()
    crop = crop.lower().strip()
    if crop not in _GROWTH_PHASES:
        crop = "maize"
    if audience not in _VALID_AUDIENCES:
        audience = "farmer"

    if season is None:
        season = detect_current_season(crop, datetime(today.year, today.month, today.day))

    # Resolve admin level name for display
    location_name, admin_level = _resolve_location_name(district, sector, cell, village)
    if not location_name:
        return {"status": "error", "error": "Specify at least one of: district, sector, cell, or village"}

    # Determine planting date and current DAP
    planting_year = today.year if season == "B" or today.month >= 9 else today.year - 1
    if season == "A" and today.month <= 2:
        planting_year = today.year - 1

    planting_date = _get_planting_date(crop, season, planting_year)
    dap = (today - planting_date).days
    if dap < 0:
        planting_year -= 1
        planting_date = _get_planting_date(crop, season, planting_year)
        dap = (today - planting_date).days
    dap = max(0, min(dap, _get_harvest_dap(crop, season) + 30))

    growth_phase = _current_growth_phase(crop, dap)

    # Get geometry and centroid for CHIRPS/WaPOR
    from src.services.admin_boundaries import lookup_admin_geometry
    geometry = await lookup_admin_geometry(
        district=district, sector=sector, cell=cell, village=village,
    )
    if geometry:
        lat, lon = _centroid_from_geojson(geometry)
    else:
        lat, lon = _RWANDA_CENTER

    # --- PARALLEL DATA FETCH ---
    # Network-only fetches (no shared conn) run in parallel.
    # DB-dependent fetches run sequentially on `conn` — asyncpg connections
    # are not safe for concurrent use (raises InterfaceError).

    async def fetch_sar_backscatter():
        return await _fetch_sar_backscatter(
            lat, lon,
            planting_date.strftime("%Y-%m-%d"), today.strftime("%Y-%m-%d"),
        )

    async def fetch_chirps():
        try:
            from src.services.forecast_fusion import _fetch_chirps_precip
            all_dates = []
            d = planting_date
            while d <= today:
                all_dates.append(d.strftime("%Y-%m-%d"))
                d += timedelta(days=1)
            if not all_dates:
                return {}
            max_downloads = 45
            if len(all_dates) > max_downloads:
                step = len(all_dates) / max_downloads
                dates = [all_dates[int(i * step)] for i in range(max_downloads)]
                if all_dates[-1] not in dates:
                    dates[-1] = all_dates[-1]
            else:
                dates = all_dates
            return await asyncio.to_thread(_fetch_chirps_precip, lat, lon, dates)
        except Exception:
            logger.debug("chirps fetch failed", exc_info=True)
            return {}

    async def fetch_wapor_et():
        try:
            from src.services.wapor_service import query_et
            return await asyncio.to_thread(
                query_et, lat, lon, planting_date, today,
            )
        except Exception:
            logger.debug("wapor ET fetch failed", exc_info=True)
            return None

    async def fetch_wapor_soil():
        try:
            from src.services.wapor_service import query_soil_moisture
            return await asyncio.to_thread(
                query_soil_moisture, lat, lon, planting_date, today,
            )
        except Exception:
            logger.debug("wapor soil moisture fetch failed", exc_info=True)
            return None

    # Network-only fetches: safe to parallelize (return_exceptions prevents
    # one failure from cancelling the others)
    network_results = await asyncio.gather(
        fetch_sar_backscatter(),
        fetch_chirps(),
        fetch_wapor_et(),
        fetch_wapor_soil(),
        return_exceptions=True,
    )
    sar_result = network_results[0] if not isinstance(network_results[0], BaseException) else None
    chirps_daily = network_results[1] if not isinstance(network_results[1], BaseException) else {}
    et_result = network_results[2] if not isinstance(network_results[2], BaseException) else None
    soil_result = network_results[3] if not isinstance(network_results[3], BaseException) else None

    # DB-dependent fetches: sequential on the shared connection
    try:
        accuracy_result = await compute_insurance_accuracy_safe(conn, district, season)
    except Exception:
        logger.debug("insurance_accuracy fetch failed", exc_info=True)
        accuracy_result = None

    try:
        from src.services.weather_accuracy import detect_dry_spells
        dry_spells_result = await detect_dry_spells(
            conn, district=district,
            date_from=planting_date.strftime("%Y-%m-%d"),
            date_to=today.strftime("%Y-%m-%d"),
        )
    except Exception:
        logger.debug("dry_spells fetch failed", exc_info=True)
        dry_spells_result = None

    try:
        from src.services.weather_accuracy import compute_ndvi_concordance
        ndvi_conc_result = await compute_ndvi_concordance(
            conn, district=district,
            date_from=planting_date.strftime("%Y-%m-%d"),
            date_to=today.strftime("%Y-%m-%d"),
        )
    except Exception:
        logger.debug("ndvi_concordance fetch failed", exc_info=True)
        ndvi_conc_result = None

    ndvi_z = await _fetch_ndvi_with_sar_fallback(
        conn, lat, lon,
        planting_date.strftime("%Y-%m-%d"), today.strftime("%Y-%m-%d"),
        district,
    )

    # --- PROCESS RESULTS ---
    sources = []

    # Rainfall
    phase_rainfall = _compute_phase_rainfall(chirps_daily, planting_date, crop, today)
    season_rainfall = sum(p.cumulative_mm for p in phase_rainfall)
    spi = _compute_spi(season_rainfall, season)
    if chirps_daily:
        sources.append("CHIRPS v2.0")

    # Dry spells
    max_dry_spell = 0
    active_dry_spell = 0
    if dry_spells_result and dry_spells_result.get("status") == "ok":
        max_dry_spell = dry_spells_result.get("longest_spell_days", 0)
        spells = dry_spells_result.get("dry_spells", [])
        if spells:
            last_spell = spells[-1] if isinstance(spells[-1], dict) else {}
            if last_spell.get("ongoing"):
                active_dry_spell = last_spell.get("duration_days", 0)

    # NDVI
    ndvi_concordance_score = None
    if ndvi_conc_result and ndvi_conc_result.get("status") == "ok":
        ndvi_concordance_score = ndvi_conc_result.get("concordance_score")
    if ndvi_z is not None:
        sources.append("Sentinel-2/SAR NDVI")

    # SAR backscatter (VH/VV ratio) — cloud-penetrating vegetation signal
    sar_vh_vv_ratio: Optional[float] = None
    if isinstance(sar_result, (int, float)):
        sar_vh_vv_ratio = float(sar_result)
        if "Sentinel-1 SAR" not in sources:
            sources.append("Sentinel-1 SAR")

    # ET and soil moisture
    et_anomaly = None
    if et_result and et_result.get("status") == "ok":
        series = et_result.get("time_series", [])
        if series:
            values = [s.get("value") for s in series if s.get("value") is not None]
            if values:
                mean_et = sum(values) / len(values)
                et_anomaly = ((mean_et - _ET_LONG_TERM_MEAN) / _ET_LONG_TERM_MEAN) * 100
                sources.append("WaPOR v3 ET")

    soil_moisture = None
    if soil_result and soil_result.get("status") == "ok":
        series = soil_result.get("time_series", [])
        if series:
            values = [s.get("value") for s in series if s.get("value") is not None]
            if values:
                soil_moisture = values[-1]  # most recent
                if "WaPOR v3 ET" not in sources:
                    sources.append("WaPOR v3")

    # --- TRIGGER EVALUATION ---
    trigger_defs = await _load_triggers(conn, crop, season, growth_phase, district)

    current_values: dict[str, Optional[float]] = {
        "rainfall_cumulative": season_rainfall,
        "spi": spi,
        "dry_spell_days": float(max_dry_spell),
        "ndvi_z_score": ndvi_z,
        "sar_backscatter": sar_vh_vv_ratio,
        "et_anomaly": et_anomaly,
        "soil_moisture": soil_moisture,
    }

    trigger_results = _evaluate_triggers(trigger_defs, current_values)
    triggers_activated = sum(1 for t in trigger_results if t.triggered)
    confidence_score, overall_status = _compute_confidence(
        trigger_results, expected_signals=len(trigger_defs),
    )
    recommendation = _generate_recommendation(overall_status, crop, growth_phase, trigger_results)

    # Merge with existing accuracy components if available
    accuracy_components = None
    if accuracy_result and accuracy_result.get("status") == "ok":
        accuracy_components = {
            "confidence_rating": accuracy_result.get("confidence_rating"),
            "recommendation": accuracy_result.get("recommendation"),
        }

    # --- BUILD REPORT ---
    report = InsuranceReport(
        location_name=location_name,
        admin_level=admin_level,
        crop=crop,
        season=season,
        growth_phase=growth_phase,
        days_after_planting=dap,
        phase_rainfall=phase_rainfall,
        season_rainfall_mm=season_rainfall,
        spi=spi,
        ndvi_z_score=ndvi_z,
        ndvi_concordance_score=ndvi_concordance_score,
        et_anomaly_pct=et_anomaly,
        soil_moisture_pct=soil_moisture,
        max_dry_spell_days=max_dry_spell,
        active_dry_spell_days=active_dry_spell,
        triggers=trigger_results,
        triggers_activated=triggers_activated,
        triggers_total=len(trigger_results),
        confidence_score=confidence_score,
        overall_status=overall_status,
        recommendation=recommendation,
        accuracy_components=accuracy_components,
        sources=sources,
        period_start=planting_date.strftime("%Y-%m-%d"),
        period_end=today.strftime("%Y-%m-%d"),
        computed_at=datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        geometry=geometry,
    )

    formatted = format_for_audience(report, audience)

    return {
        "status": "ok",
        "report": formatted,
        "data": report.to_dict(),
        "audience": audience,
        "geometry": geometry,
        "slug": f"insurance-{crop}-{location_name.lower().replace(' ', '-')}-{season}-{today.strftime('%Y%m%d')}",
    }


async def compute_insurance_accuracy_safe(
    conn: asyncpg.Connection,
    district: Optional[str],
    season: Optional[str],
) -> Optional[dict]:
    """Safe wrapper around existing compute_insurance_accuracy."""
    try:
        from src.services.weather_accuracy import compute_insurance_accuracy
        return await compute_insurance_accuracy(conn, district=district, season=season)
    except Exception:
        logger.debug("compute_insurance_accuracy failed", exc_info=True)
        return None


def _resolve_location_name(
    district: Optional[str] = None,
    sector: Optional[str] = None,
    cell: Optional[str] = None,
    village: Optional[str] = None,
) -> tuple[str, str]:
    """Return (display_name, admin_level) from the most specific provided."""
    if village:
        return village.strip(), "village"
    if cell:
        return cell.strip(), "cell"
    if sector:
        return sector.strip(), "sector"
    if district:
        return district.strip(), "district"
    return "", ""
