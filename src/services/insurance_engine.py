"""Insurance Intelligence Engine — one function, all signals, any audience, any admin level.

Connects 12 existing mundi.ai capabilities into a single unified report:
  CHIRPS rainfall, crop calendars, season detection, dry spells, NDVI concordance,
  binary accuracy, insurance confidence, WaPOR ET, WaPOR soil moisture, NDVI anomaly
  z-scores, bias correction, and admin boundary resolution.

Called by Sage via `get_insurance_intelligence` tool.
"""

from __future__ import annotations

import asyncio
import calendar
import json
import logging
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Optional

import asyncpg

from src.services import crop_stages
from src.services import et_normals
from src.services.data_coverage import point_sample_note
from src.services.numbers import round_or_none

logger = logging.getLogger(__name__)

_VALID_AUDIENCES = {"farmer", "insurance", "agronomist", "scientist"}
# Other names the model or a user uses for the same readers.
_AUDIENCE_ALIASES = {
    "underwriter": "insurance", "insurer": "insurance", "insurance_officer": "insurance",
    "claims": "insurance", "extension_officer": "agronomist", "extension": "agronomist",
    "researcher": "scientist",
}
DEFAULT_AUDIENCE = "agronomist"


AUDIENCE_LABELS = {"farmer": "Farmer", "insurance": "Insurer", "agronomist": "Agronomist", "scientist": "Scientist"}


def _known_audience(audience: Optional[str]) -> Optional[str]:
    key = (audience or "").strip().lower().replace(" ", "_")
    key = _AUDIENCE_ALIASES.get(key, key)
    return key if key in _VALID_AUDIENCES else None


def normalize_audience(audience: Optional[str]) -> str:
    """The report audience for a requested name; DEFAULT_AUDIENCE when unknown or unset."""
    return _known_audience(audience) or DEFAULT_AUDIENCE


async def audience_setting(conn: asyncpg.Connection, user_id: Optional[str],
                           partner_id: Optional[str]) -> tuple[str, str]:
    """(audience, source) from saved settings: the user's own role, else the
    partner's default (organizations.metadata.default_audience), else
    DEFAULT_AUDIENCE. Source is "user", "partner" or "default"."""
    if user_id:
        mine = await conn.fetchval("SELECT report_audience FROM users WHERE internal_uuid = $1", user_id)
        if _known_audience(mine):
            return _known_audience(mine), "user"  # type: ignore[return-value]
    if partner_id:
        theirs = await conn.fetchval(
            "SELECT metadata->>'default_audience' FROM organizations WHERE id::text = $1", partner_id)
        if _known_audience(theirs):
            return _known_audience(theirs), "partner"  # type: ignore[return-value]
    return DEFAULT_AUDIENCE, "default"


async def resolve_audience(conn: asyncpg.Connection, requested: Optional[str],
                           user_id: Optional[str], partner_id: Optional[str]) -> str:
    """A view the user asked for in the conversation wins; otherwise their saved setting."""
    explicit = _known_audience(requested)
    if explicit:
        return explicit
    try:
        return (await audience_setting(conn, user_id, partner_id))[0]
    except Exception:
        logger.warning("audience setting lookup failed; using %s", DEFAULT_AUDIENCE, exc_info=True)
        return DEFAULT_AUDIENCE


async def save_user_audience(conn: asyncpg.Connection, user_id: str, audience: Optional[str]) -> bool:
    """Save (or clear, with None) the user's report audience. False if the user has no account row."""
    value = None if audience is None else _known_audience(audience)
    if audience is not None and value is None:
        raise ValueError(f"audience must be one of {', '.join(sorted(_VALID_AUDIENCES))}")
    status = await conn.execute("UPDATE users SET report_audience = $2 WHERE internal_uuid = $1", user_id, value)
    return status.endswith(" 1")

_RWANDA_CENTER = (-1.94, 29.87)


# Per-district and national MONTHLY rainfall normals (mm per month), loaded
# from monthly_rainfall_normals.json beside this module (see "source", "years").
# Structure: district -> month (1-12) -> {"mean": mm, "std": mm}
def _load_monthly_normals() -> tuple[dict[str, dict[int, dict[str, float]]], dict[int, dict[str, float]]]:
    path = Path(__file__).parent / "monthly_rainfall_normals.json"
    data = json.loads(path.read_text())
    districts = {
        name: {int(m): v for m, v in entry["monthly"].items()}
        for name, entry in data["districts"].items()
    }
    national = {int(m): v for m, v in data["national"]["monthly"].items()}
    return districts, national


_MONTHLY_RAINFALL_NORMALS, _NATIONAL_MONTHLY_NORMALS = _load_monthly_normals()

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
    # False when under 30% of the phase's elapsed days have data: then
    # cumulative_mm covers only those days and the phase total is unknown.
    complete: bool = True

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
    # Set when `threshold` is a full-season threshold prorated to date.
    full_season_threshold: Optional[float] = None
    phase: str = "full_season"  # or the crop stage the trigger is measured over

    def to_dict(self) -> dict:
        return {
            "signal": self.signal,
            "phase": self.phase,
            "current_value": round(self.current_value, 2),
            "threshold": self.threshold,
            "full_season_threshold": self.full_season_threshold,
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
    season_rainfall_mm: Optional[float] = None  # None until satellite rainfall covers the season
    season_normal_mm: Optional[float] = None  # normal rainfall for the same days since planting
    season_pct_of_normal: Optional[float] = None
    spi: Optional[float] = None  # season-to-date SPI; None when too early or no data
    spi_1: Optional[float] = None
    spi_3: Optional[float] = None
    drought_diagnostic: str = "insufficient_data"
    drought_diagnostic_label: str = ""

    ndvi_z_score: Optional[float] = None
    ndvi_concordance_score: Optional[float] = None

    et_anomaly_pct: Optional[float] = None
    soil_moisture_pct: Optional[float] = None

    max_dry_spell_days: Optional[int] = None  # None: not enough daily rainfall data to tell
    active_dry_spell_days: Optional[int] = None

    triggers: list[TriggerResult] = field(default_factory=list)
    triggers_activated: int = 0
    triggers_total: int = 0

    confidence_score: int = 0
    overall_status: str = "UNKNOWN"
    recommendation: str = ""

    accuracy_components: Optional[dict] = None

    forecast_outlook: Optional[dict] = None

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
            "season_rainfall_mm": round_or_none(self.season_rainfall_mm, 1),
            "season_normal_mm": round_or_none(self.season_normal_mm, 1),
            "season_pct_of_normal": round_or_none(self.season_pct_of_normal, 0),
            "season_vs_usual": rainfall_vs_usual(self.season_pct_of_normal),
            "spi": round(self.spi, 2) if self.spi is not None else None,
            "spi_1": round(self.spi_1, 2) if self.spi_1 is not None else None,
            "spi_3": round(self.spi_3, 2) if self.spi_3 is not None else None,
            "drought_diagnostic": self.drought_diagnostic,
            "drought_diagnostic_label": self.drought_diagnostic_label,
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
            "accuracy_components": self.accuracy_components,
            "forecast_outlook": self.forecast_outlook,
            "sources": self.sources,
            "period_start": self.period_start,
            "period_end": self.period_end,
            "computed_at": self.computed_at,
        }

# ---------------------------------------------------------------------------
# 1. Growth-phase rainfall accumulation
# ---------------------------------------------------------------------------

# Fixed Rwanda season dates — no crop assumption needed
_SEASON_DATES = {
    "A": {"start_month": 9, "start_day": 15, "duration_days": 135},
    "B": {"start_month": 2, "start_day": 15, "duration_days": 120},
}

def _get_season_start(season: str, year: int) -> date:
    sd = _SEASON_DATES.get(season, _SEASON_DATES["B"])
    return date(year, sd["start_month"], sd["start_day"])

def _get_season_duration(season: str) -> int:
    return _SEASON_DATES.get(season, _SEASON_DATES["B"])["duration_days"]

def _season_progress_label(day_in_season: int, season_duration: int) -> str:
    fraction = day_in_season / season_duration if season_duration > 0 else 0
    if fraction < 0.33:
        return "early_season"
    elif fraction < 0.67:
        return "mid_season"
    else:
        return "late_season"

def _compute_phase_rainfall(
    daily_precip: dict[str, Optional[float]],
    planting_date: date,
    season_duration: int,
    today: date,
) -> list[PhaseRainfall]:
    """Accumulate rainfall per season third from daily CHIRPS data."""
    third = season_duration / 3
    phases = {
        "early_season": (0, int(third)),
        "mid_season": (int(third), int(2 * third)),
        "late_season": (int(2 * third), season_duration),
    }
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

        min_coverage = 0.3
        complete = total_days == 0 or (day_count / total_days) >= min_coverage
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
            complete=complete,
        ))

    return results


def _season_rainfall(phases: list[PhaseRainfall]) -> Optional[float]:
    """Rainfall since planting, or None when any elapsed phase lacks the data.

    Missing satellite days are unknown, not dry: summing only the days that
    were downloaded made a season with no data yet read as 0 mm and fired the
    rainfall trigger.
    """
    if not phases or not all(p.complete for p in phases):
        return None
    return sum(p.cumulative_mm for p in phases)


_CHIRPS_FETCH_BUDGET = 90  # daily files per report (~2-3 MB each)
_CHIRPS_RECENT_DAYS = 30  # always fetched in full: SPI-1 and the current phase


def _chirps_dates_to_fetch(planting_date: date, today: date) -> list[str]:
    """Days to download for a report: the season since planting and the 90-day
    SPI-3 window. The last 30 days in full, the rest sampled evenly: seasons
    up to 200 days keep over a third of every phase's days (a phase counts
    from 30%) and over half of the SPI-3 window's days (SPI needs 40%)."""
    start = min(planting_date, today - timedelta(days=89))
    days = [start + timedelta(days=i) for i in range((today - start).days + 1)]
    recent, earlier = days[-_CHIRPS_RECENT_DAYS:], days[:-_CHIRPS_RECENT_DAYS]
    budget = _CHIRPS_FETCH_BUDGET - len(recent)
    if len(earlier) > budget:
        step = len(earlier) / budget
        earlier = [earlier[int(i * step)] for i in range(budget)]
    return [d.strftime("%Y-%m-%d") for d in earlier + recent]


async def _fetch_season_chirps(
    lat: float, lon: float, planting_date: date, today: date,
) -> tuple[dict[str, Optional[float]], set[str]]:
    """Daily CHIRPS for a report's dates, and the days read from the preliminary product."""
    from src.services.forecast_fusion import fetch_chirps_daily  # lazy: rasterio/GDAL stack

    return await asyncio.to_thread(
        fetch_chirps_daily, lat, lon, _chirps_dates_to_fetch(planting_date, today),
    )


def season_rainfall_sentence(mm: Optional[float]) -> str:
    """One line for rainfall since planting; says plainly when it is not known yet."""
    if mm is None:
        return "Rain this season: satellite rainfall estimates do not cover the season yet."
    return f"Rain this season: {mm:.0f}mm"


def _chirps_source_label(prelim_days: set[str]) -> str:
    if not prelim_days:
        return "CHIRPS v2.0"
    return f"CHIRPS v2.0 (preliminary product for the {len(prelim_days)} most recent days)"

# ---------------------------------------------------------------------------
# 2. SPI-1 and SPI-3 from monthly CHIRPS windows
# ---------------------------------------------------------------------------

def _get_monthly_normals(month: int, district: Optional[str] = None) -> dict[str, float]:
    """Get monthly rainfall normals (mean, std) for a given month and district."""
    if district:
        district_key = district.lower().strip()
        district_months = _MONTHLY_RAINFALL_NORMALS.get(district_key)
        if district_months and month in district_months:
            return district_months[month]
    return _NATIONAL_MONTHLY_NORMALS.get(month, {"mean": 60, "std": 25})

@dataclass(frozen=True)
class WindowRainfall:
    """Rainfall over a window of days and the normal for those same dates."""

    observed_mm: float  # observed total, scaled up for missing days
    normal_mm: float  # mean rainfall for those calendar dates
    normal_std_mm: float


def _window_rainfall(
    daily_precip: dict[str, Optional[float]],
    ref_date: date,
    window_days: int,
    district: Optional[str] = None,
) -> Optional[WindowRainfall]:
    """Observed and normal rainfall for the window ending at ref_date.

    None when fewer than 40% of the days have data.
    """
    window_start = ref_date - timedelta(days=window_days - 1)

    observed = 0.0
    obs_count = 0
    for i in range(window_days):
        d = (window_start + timedelta(days=i)).strftime("%Y-%m-%d")
        val = daily_precip.get(d)
        if val is not None:
            observed += val
            obs_count += 1

    if obs_count < window_days * 0.4:
        return None

    if obs_count < window_days:
        observed = observed * (window_days / obs_count)

    import calendar
    month_day_counts: dict[tuple[int, int], int] = {}
    for i in range(window_days):
        d = window_start + timedelta(days=i)
        key = (d.year, d.month)
        month_day_counts[key] = month_day_counts.get(key, 0) + 1

    expected_mean = 0.0
    expected_var = 0.0
    for (yr, mo), days_in_window in month_day_counts.items():
        days_in_month = calendar.monthrange(yr, mo)[1]
        fraction = days_in_window / days_in_month
        normals = _get_monthly_normals(mo, district)
        expected_mean += normals["mean"] * fraction
        expected_var += (normals["std"] * fraction) ** 2

    return WindowRainfall(observed, expected_mean, expected_var ** 0.5)


def _compute_spi_from_daily(
    daily_precip: dict[str, Optional[float]],
    ref_date: date,
    window_days: int,
    district: Optional[str] = None,
) -> Optional[float]:
    """Compute SPI for a specific window ending at ref_date.

    Sums observed daily rainfall over the window, then compares against the
    expected normal for those calendar months.  For SPI-1 (30 days) we use
    the single month's normals.  For SPI-3 (90 days) we sum the normals
    for the 3 months covered.  This is a simplified z-score SPI — proper
    gamma-distribution fitting needs 30+ years of monthly totals which we
    don't have per-pixel.  The z-score approach is standard for operational
    approximation when gamma fit isn't available.
    """
    window = _window_rainfall(daily_precip, ref_date, window_days, district)
    if window is None:
        return None
    if window.normal_std_mm < 1.0:
        return 0.0
    return (window.observed_mm - window.normal_mm) / window.normal_std_mm

def _compute_spi_pair(
    daily_precip: dict[str, Optional[float]],
    ref_date: date,
    district: Optional[str] = None,
) -> dict[str, Optional[float]]:
    """Compute SPI-1 (30-day) and SPI-3 (90-day) from daily CHIRPS data."""
    return {
        "spi_1": _compute_spi_from_daily(daily_precip, ref_date, 30, district),
        "spi_3": _compute_spi_from_daily(daily_precip, ref_date, 90, district),
    }

def _et_anomaly_pct(et_result: Optional[dict]) -> Optional[float]:
    """ET over the dekads with data, as % above/below their seasonal normal at that pixel.

    Only dekads with both an observation and a normal count (unpublished
    dekads are missing, not zero). None when there are none.
    """
    if not et_result or et_result.get("status") != "success":
        return None
    pairs = [(e["et_mm_per_day"], e["normal_et_mm_per_day"]) for e in et_result.get("time_series", [])
             if e.get("et_mm_per_day") is not None and e.get("normal_et_mm_per_day") is not None]
    normal = sum(n for _, n in pairs)
    if not pairs or normal <= 0:
        return None
    return (sum(o for o, _ in pairs) - normal) / normal * 100


def _classify_drought_state(
    spi_3: Optional[float],
    soil_moisture_pct: Optional[float],
) -> str:
    """Classify SPI-SM divergence into a named drought diagnostic.

    Patterns (from Copernicus EDO Combined Drought Indicator):
      SPI dry  + SM dry    → consistent_drought (meteorological → agricultural)
      SPI ok   + SM dry    → flash_drought (high ET demand, heatwave)
      SPI dry  + SM ok     → carryover_storage (irrigation, shallow water table)
      SPI wet  + SM dry    → runoff_dominated (steep slopes, hardpan, intense storms)
      otherwise            → normal
    """
    if spi_3 is None or soil_moisture_pct is None:
        return "insufficient_data"

    spi_dry = spi_3 < -1.0
    spi_ok = -1.0 <= spi_3 <= 1.0
    spi_wet = spi_3 > 1.0
    sm_dry = soil_moisture_pct < 35.0
    sm_ok = soil_moisture_pct >= 35.0

    if spi_dry and sm_dry:
        return "consistent_drought"
    if spi_ok and sm_dry:
        return "flash_drought"
    if spi_dry and sm_ok:
        return "carryover_storage"
    if spi_wet and sm_dry:
        return "runoff_dominated"
    return "normal"

_DROUGHT_STATE_LABELS: dict[str, str] = {
    "consistent_drought": "Consistent drought — precipitation deficit confirmed by soil moisture drop",
    "flash_drought": "Flash drought — soil drying from high ET demand despite normal rainfall",
    "carryover_storage": "Soil buffered — precipitation deficit not yet reflected in soil moisture (irrigation, shallow water table, or stored moisture)",
    "runoff_dominated": "Runoff-dominated — rainfall not reaching soil (steep terrain, hardpan, or intense convective storms)",
    "normal": "Normal conditions — no significant drought signal",
    "insufficient_data": "Insufficient data for drought classification",
}

def _season_to_date_spi(
    daily_precip: dict[str, Optional[float]],
    planting_date: date,
    today: date,
    district: Optional[str] = None,
) -> Optional[float]:
    """SPI of rainfall since planting against the normal for those same dates.

    Replaces the legacy season SPI, which divided rainfall *so far* by the
    *full-season* normal: normal rain scored about -4 twenty days into Season
    A, so every crop's `spi < -1` trigger fired early in every season.
    """
    window = _season_to_date_window(daily_precip, planting_date, today)
    if window is None:
        return None
    ref, window_days = window
    return _compute_spi_from_daily(daily_precip, ref, window_days, district)


def _season_to_date_window(
    daily_precip: dict[str, Optional[float]],
    planting_date: date,
    today: date,
) -> Optional[tuple[date, int]]:
    """(last day with data, days since planting) — None when under 10 days."""
    dates_with_data = sorted(k for k, v in daily_precip.items() if v is not None)
    if not dates_with_data:
        return None
    ref = min(date.fromisoformat(dates_with_data[-1]), today)
    window_days = (ref - planting_date).days + 1
    if window_days < 10:  # too early in the season for a meaningful anomaly
        return None
    return ref, window_days


def _season_vs_normal(
    season_rainfall_mm: Optional[float],
    planting_date: date,
    today: date,
    season_days: int,
    district: Optional[str] = None,
) -> tuple[Optional[float], Optional[float]]:
    """(normal mm for the days since planting, rain since planting as % of it).

    The same days and normals as the prorated rainfall trigger, so the
    percentage, the millimetres and the trigger agree. None while the rain
    total is unknown or the season is under 10 days old.
    """
    elapsed = min((today - planting_date).days, season_days)
    if season_rainfall_mm is None or elapsed < 10:
        return None, None
    normal_mm, _ = _climatology_rainfall(planting_date, elapsed, district)
    if normal_mm < 1.0:
        return None, None
    return normal_mm, 100.0 * season_rainfall_mm / normal_mm


def rainfall_vs_usual(pct_of_normal: Optional[float]) -> Optional[str]:
    """Plain words for rainfall as a % of normal, rounded to 5% for reading aloud.

    Within 10% of normal reads as "about the usual amount": year-to-year
    variation is larger than that, and a farmer cannot act on it.
    """
    if pct_of_normal is None:
        return None
    if abs(pct_of_normal - 100) < 10:
        return "about the usual amount for this time of year"
    diff = int(5 * round(abs(pct_of_normal - 100) / 5))
    if pct_of_normal < 100:
        return f"about {diff}% less than usual for this time of year"
    return f"about {diff}% more than usual for this time of year"

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
        if result and result.get("status") == "success":
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
        if result and result.get("status") == "success":
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
    phase: str | list[str],
    district: Optional[str] = None,
) -> list[dict]:
    """Load trigger thresholds from insurance_triggers table.

    ``phase`` is a crop stage or a list of them (the stages that have started);
    full-season triggers always come too.

    District-specific rows override national defaults (district IS NULL)
    for the same (phase, signal) combination.
    """
    try:
        rows = await conn.fetch(
            "SELECT DISTINCT ON (phase, signal) "
            "phase, signal, direction, threshold, weight, description "
            "FROM insurance_triggers "
            "WHERE crop = $1 AND season = $2 AND (phase = ANY($3::text[]) OR phase = 'full_season') "
            "AND enabled = true "
            "AND (district IS NULL OR LOWER(district) = LOWER($4)) "
            "ORDER BY phase, signal, "
            "CASE WHEN district IS NOT NULL THEN 0 ELSE 1 END, "
            "weight DESC",
            crop, season, [phase] if isinstance(phase, str) else list(phase), district,
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


def _season_rainfall_threshold(trigger_defs: list[dict]) -> tuple[float, str]:
    """The full-season cumulative-rainfall trigger that evaluation uses.

    Returns (threshold_mm, source). Phase-scoped rainfall triggers (e.g. the
    flowering minimum) are excluded: the forecast outlook projects the season
    total at harvest. Falls back to the engine's declared default trigger when
    no row exists for this crop/season (e.g. crop "general").
    """
    for source, defs in (
        ("insurance_triggers", trigger_defs),
        ("default_trigger", _default_triggers("full_season")),
    ):
        for trig in defs:
            if (
                trig["signal"] == "rainfall_cumulative"
                and trig.get("phase", "full_season") == "full_season"
            ):
                return float(trig["threshold"]), source
    raise ValueError("no full-season rainfall_cumulative trigger defined")


def _prorate_season_rainfall_triggers(
    trigger_defs: list[dict],
    planting_date: date,
    today: date,
    season_days: int,
    district: Optional[str],
) -> list[dict]:
    """Full-season rainfall triggers scaled to the rain normally due by today.

    A full-season minimum (e.g. 100 mm for maize) compared with rain so far
    fired early in every season, even when rain was above normal. Until the
    season ends the threshold is scaled by the share of the season's normal
    rainfall that falls between planting and yesterday (the days the observed
    total covers). Decided by Roger, 2026-10-04.
    """
    elapsed = max(0, min((today - planting_date).days, season_days))
    if elapsed >= season_days:
        return trigger_defs
    season_normal, _ = _climatology_rainfall(planting_date, season_days, district)
    to_date_normal, _ = _climatology_rainfall(planting_date, elapsed, district)
    if season_normal <= 0:
        return trigger_defs
    share = to_date_normal / season_normal
    out = []
    for trig in trigger_defs:
        if trig["signal"] == "rainfall_cumulative" and trig.get("phase", "full_season") == "full_season":
            full = float(trig["threshold"])
            trig = {
                **trig,
                "threshold": round(full * share, 1),
                "full_season_threshold": full,
                "description": (f"Rain so far below {full * share:.0f}mm: the {full:.0f}mm season minimum "
                                f"prorated to the {share:.0%} of normal season rain due by now"),
            }
        out.append(trig)
    return out


_DRY_DAY_MM = 2.0  # a day under 2 mm counts as dry (as in the dry-spell signal)


def _window_rain(daily: dict[str, Optional[float]], start: date, end: date) -> Optional[float]:
    """Rain over start..end, scaled up for missing days; None under 30% coverage."""
    days = [(start + timedelta(days=i)).isoformat() for i in range((end - start).days + 1)]
    values = [daily[d] for d in days if daily.get(d) is not None]
    if not days or len(values) < 0.3 * len(days):
        return None
    return sum(values) * len(days) / len(values)


def _dry_spells(daily: dict[str, Optional[float]], start: date, end: date) -> tuple[Optional[int], Optional[int]]:
    """(longest, still running at `end`) runs of dry days in start..end.

    (None, None) under 80% coverage: a missing day is unknown, not dry, and it
    ends a run, so a gap can only shorten a spell, never invent one.
    """
    days = [(start + timedelta(days=i)).isoformat() for i in range((end - start).days + 1)]
    if not days or sum(daily.get(d) is not None for d in days) < 0.8 * len(days):
        return None, None
    longest = run = 0
    for d in days:
        v = daily.get(d)
        run = run + 1 if v is not None and v < _DRY_DAY_MM else 0
        longest = max(longest, run)
    return longest, run


def _window_dry_spell(daily: dict[str, Optional[float]], start: date, end: date) -> Optional[float]:
    """Longest run of dry days in start..end; None under 80% coverage."""
    longest, _ = _dry_spells(daily, start, end)
    return None if longest is None else float(longest)


def _last_observed_day(daily: dict[str, Optional[float]], today: date) -> date:
    days = [k for k, v in daily.items() if v is not None]
    return min(date.fromisoformat(max(days)), today - timedelta(days=1)) if days else today - timedelta(days=1)


def _stage_triggers(
    trigger_defs: list[dict],
    crop: str,
    daily: dict[str, Optional[float]],
    planting_date: date,
    today: date,
    season_days: int,
    district: Optional[str],
) -> list[dict]:
    """Give each crop-stage trigger (e.g. maize flowering) its value over that stage.

    Before: stage triggers were looked up by the season third ("mid_season"),
    never matched a stage ("flowering") and were never evaluated. Now every
    stage that has started is evaluated over its own days; while it is still
    running, a rainfall minimum is prorated to the normal rain due so far.
    Stages not yet started, unknown stage names and unsupported signals drop out.
    """
    out = []
    last_observed = today - timedelta(days=1)
    for trig in trigger_defs:
        phase = trig.get("phase", "full_season") or "full_season"
        if phase == "full_season":
            out.append(trig)
            continue
        window = crop_stages.stage_window(phase, crop, planting_date, season_days)
        if window is None or window[0] > last_observed:
            continue
        start, end = window
        upto = min(end, last_observed)
        span = f"{start:%d %b}–{end:%d %b}"
        if trig["signal"] == "rainfall_cumulative":
            trig = {**trig, "value": _window_rain(daily, start, upto)}
            if upto < end:
                full_normal, _ = _climatology_rainfall(start, (end - start).days + 1, district)
                so_far, _ = _climatology_rainfall(start, (upto - start).days + 1, district)
                if full_normal > 0:
                    full = float(trig["threshold"])
                    trig["threshold"] = round(full * so_far / full_normal, 1)
                    trig["full_season_threshold"] = full
            trig["description"] = f"{trig.get('description') or 'Rainfall'} ({phase} {span})"
        elif trig["signal"] == "dry_spell_days":
            trig = {**trig, "value": _window_dry_spell(daily, start, upto),
                    "description": f"{trig.get('description') or 'Dry spell'} ({phase} {span})"}
        else:
            continue
        out.append(trig)
    return out


def _climatology_rainfall(
    start: date, n_days: int, district: Optional[str],
) -> tuple[float, float]:
    """Normal rainfall (mean, std) over n_days from start, from monthly normals.

    Each day gets its month's normal divided by the month's length. The
    variance is prorated the same way and summed across months, assuming
    days are independent within a month and months independent of each other.
    """
    mean = 0.0
    var = 0.0
    for i in range(n_days):
        d = start + timedelta(days=i)
        days_in_month = calendar.monthrange(d.year, d.month)[1]
        normal = _get_monthly_normals(d.month, district)
        mean += normal["mean"] / days_in_month
        var += normal["std"] ** 2 / days_in_month
    return mean, var ** 0.5


# z-score of the 10th/90th percentile of a normal distribution
_Z_P90 = 1.2816


def _project_to_harvest(
    forecast_precip_days: list[dict],
    days_remaining: int,
    district: Optional[str] = None,
) -> tuple[float, float, float, str]:
    """Rainfall (mean, p10, p90, method) from today to harvest.

    Days the forecast covers use the forecast. Later days use the district's
    monthly rainfall normals: a forecast says nothing about weeks 3+, so
    repeating its average (the old method) turned one dry or wet fortnight
    into a whole dry or wet season.
    """
    covered = forecast_precip_days[:days_remaining]
    mean = sum(d["mean"] for d in covered)
    p10 = sum(d["p10"] for d in covered)
    p90 = sum(d["p90"] for d in covered)
    clim_days = days_remaining - len(covered)
    if clim_days <= 0:
        return mean, p10, p90, f"{days_remaining}-day forecast (full coverage)"

    clim_start = date.fromisoformat(covered[-1]["date"]) + timedelta(days=1)
    clim_mean, clim_std = _climatology_rainfall(clim_start, clim_days, district)
    where = district.strip().title() if district else "Rwanda"
    return (
        mean + clim_mean,
        p10 + max(0.0, clim_mean - _Z_P90 * clim_std),
        p90 + clim_mean + _Z_P90 * clim_std,
        f"{len(covered)}-day forecast + {clim_days}-day {where} monthly normals",
    )


def _trigger_probability(
    projected_season_p10: float,
    projected_season_mean: float,
    projected_season_p90: float,
    rainfall_threshold: float,
) -> tuple[float, str]:
    """Probability and risk label that the season total ends below the threshold.

    Reads the projected distribution as p10 -> 10%, mean -> 50%, p90 -> 90%,
    linear in between. (Until 2026-10-04 this used 1 - fraction_below, the
    chance of ending ABOVE the threshold, so a season just clearing its
    threshold was reported as a 50% payout and a wet outlook as riskier than
    a dry one.)
    """
    p10, mean, p90 = projected_season_p10, projected_season_mean, projected_season_p90
    t = rainfall_threshold
    if t >= p90:
        probability = 0.90
    elif t <= p10:
        probability = 0.05
    elif t <= mean:
        probability = 0.10 + 0.40 * ((t - p10) / (mean - p10) if mean > p10 else 1.0)
    else:
        probability = 0.50 + 0.40 * ((t - mean) / (p90 - mean) if p90 > mean else 0.0)

    if probability >= 0.90:
        risk = "VERY HIGH"
    elif probability > 0.50:
        risk = "HIGH"
    elif probability > 0.25:
        risk = "MODERATE"
    else:
        risk = "LOW"
    return probability, risk


def _compute_forecast_outlook(
    forecast_data: Optional[dict],
    season_rainfall_so_far: float,
    planting_date: "date",
    harvest_dap: int,
    today: "date",
    season: str,
    district: Optional[str] = None,
    *,
    rainfall_threshold: float,
    rainfall_threshold_source: str,
) -> Optional[dict]:
    """Project rainfall triggers forward using bias-corrected multi-model forecasts.

    Takes the bias-corrected consensus forecast (ECMWF IFS + GFS + ICON + GraphCast)
    and projects cumulative rainfall to harvest. Returns probability assessment of
    whether rainfall triggers will fire.
    """
    if not forecast_data or not forecast_data.get("daily"):
        return None

    forecast_daily = forecast_data["daily"]
    days_remaining = max(0, harvest_dap - (today - planting_date).days)
    if days_remaining == 0:
        return None

    # Sum forecast precipitation (bias-corrected consensus mean)
    forecast_precip_days = []
    for day in forecast_daily:
        precip = day.get("precipitation_mm", {})
        if isinstance(precip, dict) and "mean" in precip:
            forecast_precip_days.append({
                "date": day["date"],
                "mean": precip["mean"],
                "p10": precip.get("p10", precip["mean"]),
                "p90": precip.get("p90", precip["mean"]),
                "models": precip.get("models", {}),
                "n_models": precip.get("n_models", 1),
            })

    if not forecast_precip_days:
        return None

    forecast_days_available = len(forecast_precip_days)
    projected_mean, projected_p10, projected_p90, projection_method = _project_to_harvest(
        forecast_precip_days, days_remaining, district,
    )

    # Projected season totals at harvest
    projected_season_mean = season_rainfall_so_far + projected_mean
    projected_season_p10 = season_rainfall_so_far + projected_p10
    projected_season_p90 = season_rainfall_so_far + projected_p90

    # rainfall_threshold is the same full-season trigger that evaluation uses
    # (see _season_rainfall_threshold); a separate constant here told users a
    # payout threshold the engine never applies.

    trigger_probability, trigger_risk = _trigger_probability(
        projected_season_p10, projected_season_mean, projected_season_p90, rainfall_threshold,
    )

    # Model agreement — confidence in forecast
    model_agreement = "HIGH"
    spreads = [d.get("p90", 0) - d.get("p10", 0) for d in forecast_precip_days]
    avg_spread = sum(spreads) / len(spreads) if spreads else 0
    means = [d["mean"] for d in forecast_precip_days]
    avg_mean = sum(means) / len(means) if means else 1
    if avg_mean > 0.5 and avg_spread / avg_mean > 0.8:
        model_agreement = "LOW"
    elif avg_mean > 0.5 and avg_spread / avg_mean > 0.4:
        model_agreement = "MODERATE"

    bias_corrected = forecast_data.get("bias_correction", {}).get("applied", False)
    terrain_corrected = forecast_data.get("terrain_correction", {}).get("applied", False)

    models_used = forecast_data.get("models_used", [])

    return {
        "days_remaining": days_remaining,
        "forecast_days_available": forecast_days_available,
        "projection_method": projection_method,
        "forecast_precip_mm": round(projected_mean, 1),
        "forecast_precip_p10_mm": round(projected_p10, 1),
        "forecast_precip_p90_mm": round(projected_p90, 1),
        "projected_season_total_mm": round(projected_season_mean, 1),
        "projected_season_p10_mm": round(projected_season_p10, 1),
        "projected_season_p90_mm": round(projected_season_p90, 1),
        "rainfall_trigger_threshold_mm": rainfall_threshold,
        "rainfall_trigger_threshold_source": rainfall_threshold_source,
        "rainfall_trigger_probability": round(trigger_probability, 2),
        "rainfall_trigger_risk": trigger_risk,
        "model_agreement": model_agreement,
        "models_used": models_used,
        "bias_corrected": bias_corrected,
        "terrain_corrected": terrain_corrected,
    }

def _evaluate_triggers(
    trigger_defs: list[dict],
    current_values: dict[str, Optional[float]],
) -> list[TriggerResult]:
    """Evaluate each trigger against current signal values."""
    results = []
    for trig in trigger_defs:
        signal = trig["signal"]
        # Stage-scoped triggers carry their own value (measured over the stage).
        value = trig["value"] if "value" in trig else current_values.get(signal)
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
            full_season_threshold=trig.get("full_season_threshold"),
            phase=trig.get("phase") or "full_season",
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
    status: str, phase: str, triggers: list[TriggerResult],
) -> str:
    """Generate actionable recommendation based on trigger results."""
    activated = [t for t in triggers if t.triggered]

    if status == "SAFE":
        return f"Conditions in {phase} are progressing normally. No intervention needed."

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
        f"{status_emoji} {r.location_name}: {status_word}.",
        _farmer_rain_line(r),
        f"({point_sample_note('chirps', r.location_name, r.admin_level)})",
    ]
    if r.max_dry_spell_days:
        lines.append(f"Longest dry spell: {r.max_dry_spell_days} days")
    if r.ndvi_z_score is not None:
        health = "healthy" if r.ndvi_z_score > -0.5 else "stressed" if r.ndvi_z_score > -1.5 else "very stressed"
        lines.append(f"Vegetation: {health}")
    if r.drought_diagnostic and r.drought_diagnostic not in ("normal", "insufficient_data"):
        labels = {"consistent_drought": "Drought confirmed", "flash_drought": "Flash drought risk",
                  "carryover_storage": "Soil still has moisture", "runoff_dominated": "Rain running off"}
        lines.append(labels.get(r.drought_diagnostic, r.drought_diagnostic))

    activated = [t for t in r.triggers if t.triggered]
    if not activated:
        lines.append("No drought trigger activated.")
    else:
        lines.append(f"{len(activated)} trigger(s) activated — contact your insurance agent.")

    if r.forecast_outlook:
        fo = r.forecast_outlook
        risk = fo["rainfall_trigger_risk"]
        prob = int(fo["rainfall_trigger_probability"] * 100)
        projected = fo["projected_season_total_mm"]
        if risk in ("HIGH", "VERY HIGH"):
            lines.append(f"Forecast: {prob}% chance of drought trigger by harvest ({projected:.0f}mm projected)")
        elif risk == "MODERATE":
            lines.append(f"Forecast: rain outlook moderate — {projected:.0f}mm projected by harvest")
        else:
            lines.append(f"Forecast: rain on track — {projected:.0f}mm projected by harvest")

    lines.append(f"Season progress: {r.growth_phase} (day {r.days_after_planting})")
    return "\n".join(lines)

def _trigger_label(t: TriggerResult) -> str:
    return t.signal if t.phase == "full_season" else f"{t.signal} ({t.phase})"


def _farmer_rain_line(r: InsuranceReport) -> str:
    usual = rainfall_vs_usual(r.season_pct_of_normal)
    if usual is None or r.season_normal_mm is None or r.season_rainfall_mm is None:
        return season_rainfall_sentence(r.season_rainfall_mm)
    return (f"Rain since planting: {r.season_rainfall_mm:.0f}mm, {usual} "
            f"(usual by now: about {r.season_normal_mm:.0f}mm).")


def _season_vs_normal_line(r: InsuranceReport) -> Optional[str]:
    if r.season_pct_of_normal is None or r.season_normal_mm is None or r.season_rainfall_mm is None:
        return None
    return (f"Rain since planting: {r.season_rainfall_mm:.0f}mm = {r.season_pct_of_normal:.0f}% of normal "
            f"for these dates (normal {r.season_normal_mm:.0f}mm, CHIRPS 2000-2023)")


def _format_insurance(r: InsuranceReport) -> str:
    """Trigger assessment table for insurance workers."""
    header = (
        f"TRIGGER ASSESSMENT: {r.location_name} — Season {r.season} "
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
            f"  {_trigger_label(t):<34s} {t.current_value:>8.1f}  {op}{t.threshold:<8.1f}  "
            f"{status:<10s} {t.weight:.1f}"
        )

    table = "\n".join([
        f"  {'Signal':<34s} {'Current':>8s}  {'Threshold':<9s}  {'Status':<10s} {'Weight'}",
        "  " + "-" * 77,
        *rows,
    ])

    sources = ", ".join(r.sources) if r.sources else "CHIRPS, Sentinel-1/2, WaPOR"
    phase_info = f"Season progress: {r.growth_phase} (day {r.days_after_planting} of {_get_season_duration(r.season)})"

    prorated = [
        f"  Note: {_trigger_label(t)} uses the {t.full_season_threshold:.0f}mm "
        f"{'season' if t.phase == 'full_season' else t.phase} minimum prorated to {t.threshold:.0f}mm, "
        f"the share of normal rain due so far."
        for t in r.triggers if t.full_season_threshold is not None
    ]
    vs_normal = _season_vs_normal_line(r)
    sections = [header, status_line, *([vs_normal] if vs_normal else []), "", table, *prorated, ""]

    if r.forecast_outlook:
        fo = r.forecast_outlook
        sections.append("FORECAST OUTLOOK (bias-corrected multi-model):")
        sections.append(f"  Projected season total: {fo['projected_season_total_mm']:.0f}mm "
                        f"(range {fo['projected_season_p10_mm']:.0f}–{fo['projected_season_p90_mm']:.0f}mm)")
        sections.append(f"  Rainfall trigger threshold: {fo['rainfall_trigger_threshold_mm']:.0f}mm")
        sections.append(f"  Trigger probability: {int(fo['rainfall_trigger_probability'] * 100)}% — {fo['rainfall_trigger_risk']}")
        sections.append(f"  Model agreement: {fo['model_agreement']} | "
                        f"Bias-corrected: {'yes' if fo['bias_corrected'] else 'no'} | "
                        f"Terrain-corrected: {'yes' if fo['terrain_corrected'] else 'no'}")
        sections.append(f"  Method: {fo['projection_method']}")
        sections.append("")

    sections.append(phase_info)
    sections.append(f"Sources: {sources}")
    sections.append(f"Rainfall coverage: {point_sample_note('chirps', r.location_name, r.admin_level)}")

    return "\n".join(sections)

def _format_agronomist(r: InsuranceReport) -> str:
    """Technical detail + recommendations."""
    lines = [
        f"AGRONOMIC ASSESSMENT: {r.location_name} — Season {r.season}",
        f"Season progress: {r.growth_phase} (day {r.days_after_planting} of {_get_season_duration(r.season)})",
        "",
        "RAINFALL:",
        f"  {point_sample_note('chirps', r.location_name, r.admin_level)}",
        f"  {season_rainfall_sentence(r.season_rainfall_mm)}",
        *([f"  Normal for these dates: {r.season_normal_mm:.0f}mm ({r.season_pct_of_normal:.0f}% of normal)"]
          if r.season_pct_of_normal is not None and r.season_normal_mm is not None else []),
        f"  SPI-1 (30-day): {r.spi_1:.2f}" if r.spi_1 is not None else "  SPI-1: n/a",
        f"  SPI-3 (90-day): {r.spi_3:.2f}" if r.spi_3 is not None else "  SPI-3: n/a",
    ]

    for p in r.phase_rainfall:
        lines.append(f"  {p.phase:<12s}: {p.cumulative_mm:.0f}mm over {p.day_count} days ({p.daily_avg_mm:.1f}mm/day)")

    if r.max_dry_spell_days:
        lines.append(f"  Max dry spell: {r.max_dry_spell_days} days")
    if r.active_dry_spell_days:
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
    if r.drought_diagnostic and r.drought_diagnostic != "insufficient_data":
        lines.append(f"  Drought diagnostic: {r.drought_diagnostic_label}")

    if r.forecast_outlook:
        fo = r.forecast_outlook
        lines.append("")
        lines.append("FORECAST:")
        lines.append(f"  {fo['days_remaining']} days to harvest, {fo['forecast_days_available']}-day model forecast available")
        lines.append(f"  Projected season total: {fo['projected_season_total_mm']:.0f}mm "
                     f"(p10={fo['projected_season_p10_mm']:.0f}, p90={fo['projected_season_p90_mm']:.0f})")
        lines.append(f"  Rainfall trigger risk: {fo['rainfall_trigger_risk']} "
                     f"({int(fo['rainfall_trigger_probability'] * 100)}% probability)")
        if fo.get("bias_corrected"):
            lines.append("  Forecast is bias-corrected against CHIRPS/ERA5 observations")

    lines.append("")
    lines.append(f"STATUS: {r.overall_status} (confidence {r.confidence_score}/100)")
    lines.append(f"RECOMMENDATION: {r.recommendation}")

    return "\n".join(lines)

def _format_scientist(r: InsuranceReport) -> str:
    """Full JSON with methodology and provenance — returned as formatted string."""
    data = r.to_dict()
    data["methodology"] = {
        "rainfall": "CHIRPS v2.0 daily precipitation, 0.05° resolution",
        "season_vs_normal": "Observed CHIRPS rainfall since planting (scaled up for missing days, as for the season SPI) over the sum of per-district monthly CHIRPS normals (2000-2023) prorated to the same calendar dates.",
        "spi": "SPI-1 (30-day) and SPI-3 (90-day) from daily CHIRPS against per-district monthly normals (CHIRPS 2000-2023). Z-score approximation; gamma fit deferred.",
        "drought_diagnostic": "SPI-SM divergence classification: consistent_drought (SPI<-1, SM<35%), flash_drought (SPI normal, SM<35%), carryover_storage (SPI<-1, SM>=35%), runoff_dominated (SPI>1, SM<35%)",
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
# 8a. Multi-area comparison mode
# ---------------------------------------------------------------------------

_COMPARE_HIERARCHY = {
    "district": {
        "child_table": "rwanda_sector_boundaries",
        "child_col": "sector_name",
        "parent_col": "district_name",
    },
    "sector": {
        "child_table": "rwanda_sector_boundaries",
        "child_col": "sector_name",
        "parent_col": "district_name",
    },
    "cell": {
        "child_table": "rwanda_cell_boundaries",
        "child_col": "cell_name",
        "parent_col": "sector_name",
        "grandparent_col": "district_name",
    },
}

_COMPARE_SEMAPHORE = asyncio.Semaphore(6)

async def _fetch_area_signals(
    lat: float, lon: float,
    planting_date: date,
    today: date,
    season: str,
    district: Optional[str],
    et_normals_by_cell: Optional[dict[str, dict[int, float]]] = None,
) -> dict[str, Any]:
    """Fetch all signals for a single centroid. Lightweight — no DB, no triggers.

    ET normals are looked up by the caller for every area in one query.
    """
    signals: dict[str, Any] = {}

    async def _chirps():
        try:
            return await _fetch_season_chirps(lat, lon, planting_date, today)
        except Exception:
            return {}, set()

    async def _wapor_et():
        try:
            from src.services.wapor_service import query_et
            return await asyncio.to_thread(query_et, lat, lon, planting_date, today)
        except Exception:
            return None

    async def _wapor_soil():
        try:
            from src.services.wapor_service import query_soil_moisture
            return await asyncio.to_thread(query_soil_moisture, lat, lon, planting_date, today)
        except Exception:
            return None

    async def _sar():
        try:
            return await _fetch_sar_backscatter(
                lat, lon,
                planting_date.strftime("%Y-%m-%d"), today.strftime("%Y-%m-%d"),
            )
        except Exception:
            return None

    async def _weather():
        try:
            from src.services.forecast_fusion import _fetch_observed
            return await asyncio.to_thread(_fetch_observed, lat, lon, 10)
        except Exception:
            return None

    async with _COMPARE_SEMAPHORE:
        results = await asyncio.gather(
            _chirps(), _wapor_et(), _wapor_soil(), _sar(), _weather(),
            return_exceptions=True,
        )

    chirps_daily, _ = results[0] if not isinstance(results[0], BaseException) else ({}, set())
    et_result = results[1] if not isinstance(results[1], BaseException) else None
    soil_result = results[2] if not isinstance(results[2], BaseException) else None
    sar_result = results[3] if not isinstance(results[3], BaseException) else None
    weather_result = results[4] if not isinstance(results[4], BaseException) else None

    # Rainfall + SPI-1 + SPI-3
    if chirps_daily:
        season_rain = _season_rainfall(_compute_phase_rainfall(
            chirps_daily, planting_date, _get_season_duration(season), today,
        ))
        if season_rain is not None:
            signals["rainfall_mm"] = round(season_rain, 1)
        dates_with_data = sorted(k for k, v in chirps_daily.items() if v is not None)
        spi_ref = date.fromisoformat(dates_with_data[-1]) if dates_with_data else today
        spi_pair = _compute_spi_pair(chirps_daily, spi_ref, district)
        if spi_pair["spi_1"] is not None:
            signals["spi_1"] = round(spi_pair["spi_1"], 2)
        if spi_pair["spi_3"] is not None:
            signals["spi_3"] = round(spi_pair["spi_3"], 2)
        longest, _ = _dry_spells(chirps_daily, planting_date, _last_observed_day(chirps_daily, today))
        if longest is not None:
            signals["max_dry_spell_days"] = longest

    # ET anomaly
    et_normals.attach(et_result, (et_normals_by_cell or {}).get(et_normals.cell_for(lat, lon), {}))
    et_anomaly = _et_anomaly_pct(et_result)
    if et_anomaly is not None:
        signals["et_anomaly_pct"] = round(et_anomaly, 1)

    # Soil moisture
    if soil_result and isinstance(soil_result, dict) and soil_result.get("status") == "success":
        series = soil_result.get("time_series", [])
        values = [s.get("relative_soil_moisture_pct") for s in series if s.get("relative_soil_moisture_pct") is not None]
        if values:
            signals["soil_moisture_pct"] = round(values[-1], 1)

    # SPI-SM drought diagnostic
    sm_val = signals.get("soil_moisture_pct")
    spi3_val = signals.get("spi_3")
    drought_state = _classify_drought_state(spi3_val, sm_val)
    signals["drought_diagnostic"] = drought_state
    signals["drought_diagnostic_label"] = _DROUGHT_STATE_LABELS.get(drought_state, "")

    # SAR backscatter
    if isinstance(sar_result, (int, float)):
        signals["sar_vh_vv_ratio"] = round(float(sar_result), 3)

    # Weather (temperature, precipitation from recent observations)
    if weather_result and isinstance(weather_result, dict):
        t_max = weather_result.get("temperature_max", [])
        t_min = weather_result.get("temperature_min", [])
        precip = weather_result.get("precipitation_mm", [])
        if t_max:
            valid = [v for v in t_max if v is not None]
            if valid:
                signals["temperature_max_c"] = round(max(valid), 1)
        if t_min:
            valid = [v for v in t_min if v is not None]
            if valid:
                signals["temperature_min_c"] = round(min(valid), 1)
        if t_max and t_min:
            valid_max = [v for v in t_max if v is not None]
            valid_min = [v for v in t_min if v is not None]
            if valid_max and valid_min:
                signals["temperature_mean_c"] = round(
                    (sum(valid_max) / len(valid_max) + sum(valid_min) / len(valid_min)) / 2, 1
                )
        if precip:
            valid = [v for v in precip if v is not None]
            if valid:
                signals["recent_precip_mm_day"] = round(sum(valid) / len(valid), 1)

    return signals

async def _compare_areas(
    conn: asyncpg.Connection,
    crop: str = "",
    season: Optional[str] = None,
    district: Optional[str] = None,
    sector: Optional[str] = None,
    cell: Optional[str] = None,
    compare_level: str = "sector",
    ref_date: Optional[date] = None,
) -> dict[str, Any]:
    """Compare all child areas at compare_level within the parent area.

    Example: district=Nyamasheke, compare_level=sector → compares all sectors.
    """
    today = ref_date or date.today()

    if season is None:
        m = today.month
        season = "B" if 2 <= m <= 7 else "A"

    compare_level = compare_level.lower().strip()

    # Discover child areas with centroids
    if compare_level == "sector" and district:
        rows = await conn.fetch(
            "SELECT sector_name AS name, district_name, "
            "round(ST_Y(ST_Centroid(geom))::numeric, 5) AS lat, "
            "round(ST_X(ST_Centroid(geom))::numeric, 5) AS lon "
            "FROM rwanda_sector_boundaries WHERE LOWER(district_name) = LOWER($1) "
            "ORDER BY sector_name",
            district,
        )
        parent_name = district
        parent_level = "district"
    elif compare_level == "cell" and (sector or district):
        if sector:
            rows = await conn.fetch(
                "SELECT cell_name AS name, district_name, "
                "round(ST_Y(ST_Centroid(geom))::numeric, 5) AS lat, "
                "round(ST_X(ST_Centroid(geom))::numeric, 5) AS lon "
                "FROM rwanda_cell_boundaries WHERE LOWER(sector_name) = LOWER($1) "
                "ORDER BY cell_name",
                sector,
            )
            parent_name = sector
            parent_level = "sector"
        else:
            rows = await conn.fetch(
                "SELECT cell_name AS name, district_name, "
                "round(ST_Y(ST_Centroid(geom))::numeric, 5) AS lat, "
                "round(ST_X(ST_Centroid(geom))::numeric, 5) AS lon "
                "FROM rwanda_cell_boundaries WHERE LOWER(district_name) = LOWER($1) "
                "ORDER BY cell_name",
                district,
            )
            parent_name = district
            parent_level = "district"
    elif compare_level == "district":
        rows = await conn.fetch(
            "SELECT district AS name, district AS district_name, "
            "round(ST_Y(ST_Centroid(geom))::numeric, 5) AS lat, "
            "round(ST_X(ST_Centroid(geom))::numeric, 5) AS lon "
            "FROM rwanda_district_boundaries ORDER BY district",
        )
        parent_name = "Rwanda"
        parent_level = "country"
    else:
        return {
            "status": "error",
            "error": (
                f"compare_level='{compare_level}' requires a parent area. "
                "Use district= for sector comparison, sector= for cell comparison, "
                "or compare_level='district' to compare all districts."
            ),
        }

    if not rows:
        return {
            "status": "error",
            "error": f"No {compare_level}s found in {parent_name}.",
        }

    # Resolve season dates
    planting_year = today.year if season == "B" or today.month >= 9 else today.year - 1
    if season == "A" and today.month <= 2:
        planting_year = today.year - 1
    planting_date = _get_season_start(season, planting_year)
    dap = (today - planting_date).days
    if dap < 0:
        planting_year -= 1
        planting_date = _get_season_start(season, planting_year)
        dap = (today - planting_date).days
    harvest_dap = _get_season_duration(season)
    dap = max(0, min(dap, harvest_dap + 30))
    growth_phase = _season_progress_label(dap, harvest_dap)

    # Also get NDVI from cache (fast, already aggregated)
    ndvi_by_area: dict[str, float] = {}
    try:
        if compare_level == "sector" and district:
            ndvi_rows = await conn.fetch(
                "SELECT cb.sector_name AS area_name, AVG(nc.mean_ndvi) AS avg_ndvi "
                "FROM ndvi_cell_cache nc "
                "JOIN rwanda_cell_boundaries cb ON nc.cell_name = cb.cell_name AND nc.district_name = cb.district_name "
                "WHERE LOWER(nc.district_name) = LOWER($1) "
                "AND nc.computed_at > NOW() - INTERVAL '30 days' "
                "GROUP BY cb.sector_name",
                district,
            )
        elif compare_level == "cell":
            filter_col = "sector_name" if sector else "district_name"
            filter_val = sector or district
            ndvi_rows = await conn.fetch(
                f"SELECT nc.cell_name AS area_name, AVG(nc.mean_ndvi) AS avg_ndvi "
                f"FROM ndvi_cell_cache nc "
                f"JOIN rwanda_cell_boundaries cb ON nc.cell_name = cb.cell_name AND nc.district_name = cb.district_name "
                f"WHERE LOWER(cb.{filter_col}) = LOWER($1) "
                f"AND nc.computed_at > NOW() - INTERVAL '30 days' "
                f"GROUP BY nc.cell_name",
                filter_val,
            )
        elif compare_level == "district":
            ndvi_rows = await conn.fetch(
                "SELECT nc.district_name AS area_name, AVG(nc.mean_ndvi) AS avg_ndvi "
                "FROM ndvi_cell_cache nc "
                "WHERE nc.computed_at > NOW() - INTERVAL '30 days' "
                "GROUP BY nc.district_name",
            )
        else:
            ndvi_rows = []
        for nr in ndvi_rows:
            ndvi_by_area[nr["area_name"].lower()] = round(float(nr["avg_ndvi"]), 4)
    except Exception:
        logger.debug("NDVI cache lookup for comparison failed", exc_info=True)

    # ET normals for every area in one query (the areas then fetch in parallel)
    try:
        normals_by_cell = await et_normals.normals_by_cell(
            conn, [et_normals.cell_for(float(r["lat"]), float(r["lon"])) for r in rows],
            et_normals.dekads_between(planting_date, today),
        )
    except Exception:
        logger.warning("ET normals lookup failed; ET anomaly unavailable for this comparison", exc_info=True)
        normals_by_cell = {}

    # Fetch all signals in parallel for each area
    async def _fetch_one(row: asyncpg.Record) -> dict[str, Any]:
        lat = float(row["lat"])
        lon = float(row["lon"])
        name = row["name"]
        d_name = row["district_name"] if "district_name" in row.keys() else district
        signals = await _fetch_area_signals(
            lat, lon, planting_date, today, season, district=d_name,
            et_normals_by_cell=normals_by_cell,
        )
        # Merge cached NDVI
        ndvi = ndvi_by_area.get(name.lower())
        if ndvi is not None:
            signals["ndvi"] = ndvi
        signals["name"] = name
        signals["lat"] = lat
        signals["lon"] = lon
        return signals

    area_results = await asyncio.gather(
        *[_fetch_one(r) for r in rows],
        return_exceptions=True,
    )

    comparison = []
    for r in area_results:
        if isinstance(r, BaseException):
            logger.debug("Compare area fetch failed: %s", r)
            continue
        comparison.append(r)

    comparison = _rank_by_rainfall(comparison)
    no_rain_data = [c["name"] for c in comparison if c.get("rainfall_mm") is None]

    # Collect which signals are present across all areas
    all_signals = set()
    for c in comparison:
        all_signals.update(k for k in c if k not in ("name", "lat", "lon"))

    return {
        "status": "ok",
        "mode": "comparison",
        "parent": parent_name,
        "parent_level": parent_level,
        "compare_level": compare_level,
        "season": season,
        "season_progress": growth_phase,
        "days_into_season": dap,
        "period": f"{planting_date.strftime('%Y-%m-%d')} to {today.strftime('%Y-%m-%d')}",
        "area_count": len(comparison),
        "signals_available": sorted(all_signals),
        "areas": comparison,
        "areas_without_rain_data": no_rain_data,
        "instruction": COMPARISON_INSTRUCTION,
        "sources": "CHIRPS v2.0, WaPOR v3, Sentinel-1 SAR, Sentinel-2 NDVI, Open-Meteo/ERA5",
    }


COMPARISON_INSTRUCTION = (
    "Present the comparison naturally. Highlight which areas stand out "
    "(wettest, driest, best NDVI, worst soil moisture, etc). "
    "Areas in areas_without_rain_data have no rainfall data: say their rain is unknown, never call them driest. "
    "Use a short table if >3 areas, otherwise describe in sentences. "
    "Mention the most interesting contrasts — don't list every number for every area. "
    "End with sources in parentheses."
)


def _rank_by_rainfall(areas: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Most rain first; areas with no rain data last (by name), never ranked as if they had 0 mm."""
    measured = sorted((a for a in areas if a.get("rainfall_mm") is not None),
                      key=lambda a: a["rainfall_mm"], reverse=True)
    unknown = sorted((a for a in areas if a.get("rainfall_mm") is None), key=lambda a: str(a.get("name", "")))
    return measured + unknown

# ---------------------------------------------------------------------------
# 8b. Composite orchestrator — THE MAIN ENTRY POINT
# ---------------------------------------------------------------------------

async def compute_insurance_intelligence(
    conn: asyncpg.Connection,
    crop: str = "maize",
    season: Optional[str] = None,
    district: Optional[str] = None,
    sector: Optional[str] = None,
    cell: Optional[str] = None,
    village: Optional[str] = None,
    audience: Optional[str] = None,
    ref_date: Optional[date] = None,
    compare_level: Optional[str] = None,
    owner_uuid: Optional[str] = None,
) -> dict[str, Any]:
    """One call, all signals, any audience, any admin level.

    Returns dict with 'status', 'report' (formatted string), 'data' (raw dict),
    'geometry' (GeoJSON for Brain persistence) and 'slug' (the Brain page the
    report is saved under: pass owner_uuid, the user it is saved for).

    When compare_level is set (e.g. "sector", "cell", "district"), discovers all
    child admin units at that level within the parent area and returns a
    comparison table with all signals for each.
    """
    if compare_level:
        return await _compare_areas(
            conn, crop=crop, season=season,
            district=district, sector=sector, cell=cell,
            compare_level=compare_level, ref_date=ref_date,
        )

    if not any([district, sector, cell, village]):
        return {
            "status": "error",
            "error": "At least one location parameter (district, sector, cell, or village) is required.",
        }

    today = ref_date or date.today()
    crop = crop.lower().strip() if crop else ""
    audience = normalize_audience(audience)

    if season is None:
        # Simple season detection from date — no crop needed
        m = today.month
        if 2 <= m <= 7:
            season = "B"
        else:
            season = "A"

    # Resolve admin level name for display
    location_name, admin_level = _resolve_location_name(district, sector, cell, village)
    if not location_name:
        return {"status": "error", "error": "Specify at least one of: district, sector, cell, or village"}

    # Season dates — fixed for Rwanda, no crop assumption
    season_year = today.year if season == "B" or today.month >= 9 else today.year - 1
    if season == "A" and today.month <= 2:
        season_year = today.year - 1

    planting_date = _get_season_start(season, season_year)
    dap = (today - planting_date).days
    if dap < 0:
        season_year -= 1
        planting_date = _get_season_start(season, season_year)
        dap = (today - planting_date).days
    harvest_dap = _get_season_duration(season)
    dap = max(0, min(dap, harvest_dap + 30))

    growth_phase = _season_progress_label(dap, harvest_dap)

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
            return await _fetch_season_chirps(lat, lon, planting_date, today)
        except Exception:
            logger.debug("chirps fetch failed", exc_info=True)
            return {}, set()

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

    async def fetch_forecast():
        try:
            from src.services.forecast_openmeteo import fetch_openmeteo_multimodel
            days_left = max(0, harvest_dap - dap)
            forecast_days = min(days_left, 16)
            if forecast_days < 1:
                return None
            return await asyncio.to_thread(
                fetch_openmeteo_multimodel, lat, lon, forecast_days,
            )
        except Exception:
            logger.debug("forecast fetch failed", exc_info=True)
            return None

    # Network-only fetches: safe to parallelize (return_exceptions prevents
    # one failure from cancelling the others)
    network_results = await asyncio.gather(
        fetch_sar_backscatter(),
        fetch_chirps(),
        fetch_wapor_et(),
        fetch_wapor_soil(),
        fetch_forecast(),
        return_exceptions=True,
    )
    sar_result = network_results[0] if not isinstance(network_results[0], BaseException) else None
    chirps_daily, chirps_prelim_days = (
        network_results[1] if not isinstance(network_results[1], BaseException) else ({}, set())
    )
    et_result = network_results[2] if not isinstance(network_results[2], BaseException) else None
    soil_result = network_results[3] if not isinstance(network_results[3], BaseException) else None
    forecast_result = network_results[4] if not isinstance(network_results[4], BaseException) else None
    if isinstance(network_results[4], BaseException):
        logger.warning("forecast fetch raised: %s", network_results[4])
    elif forecast_result is None:
        logger.info("forecast fetch returned None")
    else:
        logger.info("forecast fetch OK: %d daily entries, models=%s",
                     len(forecast_result.get("daily", [])),
                     forecast_result.get("models_used", []))

    # DB-dependent fetches: sequential on the shared connection
    if et_result and et_result.get("status") == "success":
        try:
            cell = et_normals.cell_for(lat, lon)
            normals = await et_normals.normals_by_cell(conn, [cell], et_normals.dekads_between(planting_date, today))
            et_normals.attach(et_result, normals.get(cell, {}))
        except Exception:
            logger.warning("ET normals lookup failed; ET anomaly unavailable", exc_info=True)
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

    # Rainfall + SPI
    phase_rainfall = _compute_phase_rainfall(chirps_daily, planting_date, harvest_dap, today)
    season_rainfall = _season_rainfall(phase_rainfall)
    spi = _season_to_date_spi(chirps_daily or {}, planting_date, today, district)
    season_normal_mm, season_pct_of_normal = _season_vs_normal(
        season_rainfall, planting_date, today, harvest_dap, district,
    )
    if chirps_daily:
        _dates_with_data = sorted(k for k, v in chirps_daily.items() if v is not None)
        _spi_ref = date.fromisoformat(_dates_with_data[-1]) if _dates_with_data else today
        spi_pair = _compute_spi_pair(chirps_daily, _spi_ref, district)
    else:
        spi_pair = {"spi_1": None, "spi_3": None}
    spi_1 = spi_pair["spi_1"]
    spi_3 = spi_pair["spi_3"]
    if chirps_daily:
        sources.append(_chirps_source_label(chirps_prelim_days))

    # Dry spells
    # From the same CHIRPS days as the rainfall (the methodology says so); the
    # weather cache only when CHIRPS cannot tell. Unknown stays None: a 0 here
    # used to pass the dry-spell trigger whenever the cache was empty.
    max_dry_spell, active_dry_spell = _dry_spells(
        chirps_daily or {}, planting_date, _last_observed_day(chirps_daily or {}, today))
    if max_dry_spell is None and dry_spells_result and dry_spells_result.get("status") == "success":
        max_dry_spell = dry_spells_result.get("longest_spell_days", 0)
        spells = dry_spells_result.get("dry_spells", [])
        if spells:
            last_spell = spells[-1] if isinstance(spells[-1], dict) else {}
            if last_spell.get("ongoing"):
                active_dry_spell = last_spell.get("duration_days", 0)

    # NDVI
    ndvi_concordance_score = None
    if ndvi_conc_result and ndvi_conc_result.get("status") == "success":
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
    et_anomaly = _et_anomaly_pct(et_result)
    if et_anomaly is not None:
        sources.append("WaPOR v3 ET")

    soil_moisture = None
    if soil_result and soil_result.get("status") == "success":
        series = soil_result.get("time_series", [])
        if series:
            values = [s.get("relative_soil_moisture_pct") for s in series if s.get("relative_soil_moisture_pct") is not None]
            if values:
                soil_moisture = values[-1]  # most recent
                if "WaPOR v3 ET" not in sources:
                    sources.append("WaPOR v3")

    # SPI-SM divergence diagnostic
    drought_diagnostic = _classify_drought_state(spi_3, soil_moisture)
    drought_diagnostic_label = _DROUGHT_STATE_LABELS.get(drought_diagnostic, "")

    # --- TRIGGER EVALUATION ---
    stage = crop_stages.stage_from_dap(dap, harvest_dap, crop or "general")
    labels = crop_stages.stage_labels(crop or "general")
    started = labels if stage == "_any" and dap > 0 else labels[: labels.index(stage) + 1] if stage in labels else []
    trigger_defs = _stage_triggers(
        await _load_triggers(conn, crop or "general", season, started, district),
        crop or "general", chirps_daily or {}, planting_date, today, harvest_dap, district,
    )

    current_values: dict[str, Optional[float]] = {
        "rainfall_cumulative": season_rainfall,
        "spi": spi,
        "dry_spell_days": None if max_dry_spell is None else float(max_dry_spell),
        "ndvi_z_score": ndvi_z,
        "sar_backscatter": sar_vh_vv_ratio,
        "et_anomaly": et_anomaly,
        "soil_moisture": soil_moisture,
    }

    trigger_results = _evaluate_triggers(
        _prorate_season_rainfall_triggers(trigger_defs, planting_date, today, harvest_dap, district),
        current_values,
    )
    triggers_activated = sum(1 for t in trigger_results if t.triggered)
    confidence_score, overall_status = _compute_confidence(
        trigger_results, expected_signals=len(trigger_defs),
    )
    recommendation = _generate_recommendation(overall_status, growth_phase, trigger_results)

    # Merge with existing accuracy components if available
    accuracy_components = None
    if accuracy_result and accuracy_result.get("status") == "success":
        binary = (accuracy_result.get("components") or {}).get(
            "binary_accuracy", {}
        ).get("overall_binary", {})
        accuracy_components = {
            "confidence_rating": accuracy_result.get("confidence_rating"),
            "recommendation": accuracy_result.get("recommendation"),
            "pod": binary.get("pod"),
            "far": binary.get("far"),
            "hss": binary.get("hss"),
            "csi": binary.get("csi"),
        }

    # --- FORECAST OUTLOOK ---
    season_threshold_mm, season_threshold_source = _season_rainfall_threshold(trigger_defs)
    forecast_outlook = None if season_rainfall is None else _compute_forecast_outlook(
        forecast_result, season_rainfall, planting_date, harvest_dap,
        today, season, district,
        rainfall_threshold=season_threshold_mm,
        rainfall_threshold_source=season_threshold_source,
    )
    if forecast_outlook:
        logger.info("forecast outlook: risk=%s prob=%.2f projected=%.0fmm threshold=%.0fmm",
                     forecast_outlook.get("rainfall_trigger_risk"),
                     forecast_outlook.get("rainfall_trigger_probability", 0),
                     forecast_outlook.get("projected_season_total_mm", 0),
                     forecast_outlook.get("rainfall_trigger_threshold_mm", 0))
        sources.append(f"Multi-model forecast ({', '.join(forecast_outlook.get('models_used', []))})")
    else:
        logger.info("forecast outlook is None (forecast_result=%s)", type(forecast_result).__name__)

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
        season_normal_mm=season_normal_mm,
        season_pct_of_normal=season_pct_of_normal,
        spi=spi,
        spi_1=spi_1,
        spi_3=spi_3,
        drought_diagnostic=drought_diagnostic,
        drought_diagnostic_label=drought_diagnostic_label,
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
        forecast_outlook=forecast_outlook,
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
        "slug": insurance_page_slug(location_name, season, today, owner_uuid),
    }

def insurance_page_slug(
    location_name: str, season: str, day: date, owner_uuid: Optional[str] = None
) -> str:
    """Slug of the Brain page that keeps one user's report on a location for a day.

    Brain slugs are unique across all users and their pages are private, so a
    slug without its owner made the second user to ask about the same place
    on the same day fail to save their report (2026-10-07). Lower case, as the
    Brain stores it, so Sage can look the page up by the slug it is shown.
    """
    slug = f"insurance-{location_name.replace(' ', '-')}-{season}-{day.strftime('%Y%m%d')}"
    if owner_uuid:
        slug += "-" + owner_uuid.replace("-", "")[:8]
    return slug.lower()


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
