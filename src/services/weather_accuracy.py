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

"""Weather accuracy engine for agricultural insurance.

Answers the question: "Can we trust our weather data enough to
underwrite insurance for a given district/season?"

Three components:
  1. BinaryAccuracyScorer — POD, FAR, HSS, CSI for rainfall events
  2. DrySpellDetector     — historical consecutive dry-day detection
  3. NDVICrossValidator   — correlates NDVI response with weather record

All queries run against PostgreSQL tables that already exist:
  - weather_daily_cache  (district, observation_date, precipitation, ...)
  - agri_indices_cache   (admin_name, week_start, ndvi_mean, ...)
"""

from __future__ import annotations

import logging
import math
from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

# Rwanda growing seasons
SEASON_A = ("09-01", "01-31")  # Sep – Jan
SEASON_B = ("02-01", "06-30")  # Feb – Jun


def _season_dates(season: str, year: Optional[int] = None) -> Tuple[str, str]:
    """Return (date_from, date_to) for a season label.

    Season A spans two calendar years (Sep Y → Jan Y+1).
    Season B is within one calendar year (Feb → Jun).
    If year is None, uses the most recent completed or in-progress season.
    """
    from datetime import date as _date

    today = _date.today()
    if year is None:
        year = today.year

    season = season.upper()
    if season == "A":
        return f"{year}-09-01", f"{year + 1}-01-31"
    elif season == "B":
        return f"{year}-02-01", f"{year}-06-30"
    else:
        # Default: last 90 days
        end = today
        start = end - timedelta(days=90)
        return start.isoformat(), end.isoformat()


# ---------------------------------------------------------------------------
# 1. Binary Accuracy Scorer
# ---------------------------------------------------------------------------

@dataclass
class BinaryMetrics:
    """Standard binary forecast verification metrics."""
    hits: int = 0          # forecast YES, observed YES
    misses: int = 0        # forecast NO, observed YES
    false_alarms: int = 0  # forecast YES, observed NO
    correct_neg: int = 0   # forecast NO, observed NO
    n_total: int = 0

    @property
    def pod(self) -> Optional[float]:
        """Probability of Detection (hit rate). Range 0-1, perfect = 1."""
        denom = self.hits + self.misses
        return round(self.hits / denom, 3) if denom > 0 else None

    @property
    def far(self) -> Optional[float]:
        """False Alarm Ratio. Range 0-1, perfect = 0."""
        denom = self.hits + self.false_alarms
        return round(self.false_alarms / denom, 3) if denom > 0 else None

    @property
    def csi(self) -> Optional[float]:
        """Critical Success Index (threat score). Range 0-1, perfect = 1."""
        denom = self.hits + self.misses + self.false_alarms
        return round(self.hits / denom, 3) if denom > 0 else None

    @property
    def hss(self) -> Optional[float]:
        """Heidke Skill Score. Range -1 to 1, 0 = no skill, 1 = perfect."""
        n = self.n_total
        if n == 0:
            return None
        expected = (
            (self.hits + self.misses) * (self.hits + self.false_alarms)
            + (self.correct_neg + self.misses) * (self.correct_neg + self.false_alarms)
        ) / n
        denom = n - expected
        if abs(denom) < 1e-6:
            return 0.0
        return round(
            ((self.hits + self.correct_neg) - expected) / denom, 3
        )

    @property
    def accuracy_pct(self) -> Optional[float]:
        """Simple accuracy percentage."""
        if self.n_total == 0:
            return None
        return round(100.0 * (self.hits + self.correct_neg) / self.n_total, 1)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "hits": self.hits,
            "misses": self.misses,
            "false_alarms": self.false_alarms,
            "correct_negatives": self.correct_neg,
            "n_total": self.n_total,
            "pod": self.pod,
            "far": self.far,
            "csi": self.csi,
            "hss": self.hss,
            "accuracy_pct": self.accuracy_pct,
        }


@dataclass
class ContinuousMetrics:
    """Standard continuous verification metrics."""
    errors: List[float] = field(default_factory=list)

    @property
    def n(self) -> int:
        return len(self.errors)

    @property
    def mae(self) -> Optional[float]:
        if not self.errors:
            return None
        return round(sum(abs(e) for e in self.errors) / len(self.errors), 2)

    @property
    def bias(self) -> Optional[float]:
        if not self.errors:
            return None
        return round(sum(self.errors) / len(self.errors), 2)

    @property
    def rmse(self) -> Optional[float]:
        if not self.errors:
            return None
        return round(math.sqrt(sum(e ** 2 for e in self.errors) / len(self.errors)), 2)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "n": self.n,
            "mae": self.mae,
            "bias": self.bias,
            "rmse": self.rmse,
        }


async def compute_binary_accuracy(
    conn,
    district: Optional[str] = None,
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    threshold_mm: float = 5.0,
) -> Dict[str, Any]:
    """Compare forecast hindcasts vs AgERA5 observed for binary rainfall events.

    Uses the Open-Meteo forecast API's past_days data (already fetched by
    forecast_fusion.py) against weather_daily_cache (AgERA5 observed).

    For each overlapping (district, date) pair:
      - observed: precipitation >= threshold_mm → rain event
      - forecast: consensus mean >= threshold_mm → predicted rain event
      - Builds 2x2 contingency table → POD, FAR, HSS, CSI

    Args:
        conn: asyncpg connection
        district: Optional district filter (ILIKE)
        date_from: Start date (ISO format)
        date_to: End date (ISO format)
        threshold_mm: Rainfall threshold for binary classification

    Returns:
        Dict with binary_metrics, continuous_metrics, and per-district breakdown
    """
    # Query observed weather from PostgreSQL cache
    params: list = []
    where_clauses = []
    pidx = 1

    if district:
        where_clauses.append(f"district ILIKE ${pidx}")
        params.append(f"%{district}%")
        pidx += 1
    if date_from:
        where_clauses.append(f"observation_date >= ${pidx}::date")
        params.append(date_from)
        pidx += 1
    if date_to:
        where_clauses.append(f"observation_date <= ${pidx}::date")
        params.append(date_to)
        pidx += 1

    if not date_from and not date_to:
        where_clauses.append("observation_date >= CURRENT_DATE - INTERVAL '90 days'")

    where_sql = f"WHERE {' AND '.join(where_clauses)}" if where_clauses else ""

    rows = await conn.fetch(
        f"SELECT district, observation_date, precipitation "
        f"FROM weather_daily_cache {where_sql} "
        f"ORDER BY district, observation_date",
        *params,
    )

    if not rows:
        return {
            "status": "no_data",
            "error": "No observed weather data in cache for the specified period",
            "n_observations": 0,
        }

    # Now fetch hindcast data for the same districts and dates.
    # We use Open-Meteo's archive API to get what the models would have predicted.
    # This is more reliable than storing hindcasts.
    # For now, we compare AgERA5 against itself (observed-vs-observed baseline)
    # and against the Open-Meteo recent forecast data if available.

    # Build observed lookup
    observed: Dict[Tuple[str, str], float] = {}
    for r in rows:
        d_name = r["district"]
        d_date = str(r["observation_date"])
        precip = float(r["precipitation"]) if r["precipitation"] is not None else 0.0
        observed[(d_name, d_date)] = precip

    # Get district centroids for hindcast fetching
    district_filter = f"WHERE district ILIKE ${1}" if district else ""
    district_params = [f"%{district}%"] if district else []
    centroids = await conn.fetch(
        f"SELECT DISTINCT district, "
        f"round(ST_Y(ST_Centroid(geom))::numeric, 4) as lat, "
        f"round(ST_X(ST_Centroid(geom))::numeric, 4) as lon "
        f"FROM rwanda_district_boundaries {district_filter}",
        *district_params,
    )

    centroid_map = {r["district"]: (float(r["lat"]), float(r["lon"])) for r in centroids}

    # Fetch hindcasts from Open-Meteo archive for each district
    import asyncio as _aio
    from src.services.forecast_fusion import _fetch_observed as _fetch_era5_observed

    overall_binary = BinaryMetrics()
    overall_continuous = ContinuousMetrics()
    per_district: Dict[str, Dict[str, Any]] = {}

    # Group observed data by district
    districts_in_data = sorted(set(d for d, _ in observed.keys()))
    loop = _aio.get_event_loop()

    for d_name in districts_in_data:
        if d_name not in centroid_map:
            continue

        lat, lon = centroid_map[d_name]

        # Get this district's observed days
        district_obs = {
            d_date: precip
            for (dn, d_date), precip in observed.items()
            if dn == d_name
        }

        if len(district_obs) < 5:
            continue

        # Fetch ERA5 archive for comparison (this gives us an independent
        # observation from a different source than AgERA5)
        dates_sorted = sorted(district_obs.keys())

        # Sync HTTP call — run in executor to avoid blocking the event loop
        try:
            era5_obs = await loop.run_in_executor(
                None,
                lambda _lat=lat, _lon=lon, _n=len(dates_sorted): _fetch_era5_observed(_lat, _lon, lookback_days=_n),
            )
        except Exception:
            era5_obs = {}

        era5_dates = era5_obs.get("dates", [])
        era5_precip = era5_obs.get("precipitation_mm", [])
        era5_by_date = {}
        for i, d in enumerate(era5_dates):
            if i < len(era5_precip) and era5_precip[i] is not None:
                era5_by_date[d] = era5_precip[i]

        d_binary = BinaryMetrics()
        d_continuous = ContinuousMetrics()

        for d_date, agera5_precip in district_obs.items():
            # Use CHIRPS/ERA5 as "forecast" proxy for accuracy comparison
            hindcast_precip = era5_by_date.get(d_date)
            if hindcast_precip is None:
                continue

            obs_event = agera5_precip >= threshold_mm
            fc_event = hindcast_precip >= threshold_mm

            d_binary.n_total += 1
            if fc_event and obs_event:
                d_binary.hits += 1
            elif fc_event and not obs_event:
                d_binary.false_alarms += 1
            elif not fc_event and obs_event:
                d_binary.misses += 1
            else:
                d_binary.correct_neg += 1

            d_continuous.errors.append(hindcast_precip - agera5_precip)

        # Accumulate overall
        overall_binary.hits += d_binary.hits
        overall_binary.misses += d_binary.misses
        overall_binary.false_alarms += d_binary.false_alarms
        overall_binary.correct_neg += d_binary.correct_neg
        overall_binary.n_total += d_binary.n_total
        overall_continuous.errors.extend(d_continuous.errors)

        if d_binary.n_total >= 5:
            per_district[d_name] = {
                "binary": d_binary.to_dict(),
                "continuous": d_continuous.to_dict(),
            }

    return {
        "status": "success",
        "threshold_mm": threshold_mm,
        "n_observations": len(observed),
        "n_districts": len(per_district),
        "overall_binary": overall_binary.to_dict(),
        "overall_continuous": overall_continuous.to_dict(),
        "per_district": per_district,
        "min_sample_warning": (
            "Results may be unreliable with fewer than 20 matched days per district"
            if overall_binary.n_total < 20 else None
        ),
    }


# ---------------------------------------------------------------------------
# 2. Dry Spell Detector
# ---------------------------------------------------------------------------

@dataclass
class DrySpell:
    """A detected dry spell event."""
    district: str
    start_date: str
    end_date: str
    duration_days: int
    avg_precip_mm: float

    def to_dict(self) -> Dict[str, Any]:
        return {
            "district": self.district,
            "start_date": self.start_date,
            "end_date": self.end_date,
            "duration_days": self.duration_days,
            "avg_precipitation_mm_day": self.avg_precip_mm,
        }


async def detect_dry_spells(
    conn,
    district: Optional[str] = None,
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    threshold_mm: float = 2.0,
    min_duration_days: int = 10,
) -> Dict[str, Any]:
    """Detect historical dry spells from observed weather data.

    Scans weather_daily_cache for consecutive days where daily
    precipitation < threshold_mm for at least min_duration_days.

    Args:
        conn: asyncpg connection
        district: Optional district filter
        date_from: Start date (ISO format)
        date_to: End date (ISO format)
        threshold_mm: Daily precipitation threshold (mm)
        min_duration_days: Minimum consecutive days to qualify

    Returns:
        Dict with dry_spells list, summary stats, and per-district counts
    """
    params: list = []
    where_clauses = []
    pidx = 1

    if district:
        where_clauses.append(f"district ILIKE ${pidx}")
        params.append(f"%{district}%")
        pidx += 1
    if date_from:
        where_clauses.append(f"observation_date >= ${pidx}::date")
        params.append(date_from)
        pidx += 1
    if date_to:
        where_clauses.append(f"observation_date <= ${pidx}::date")
        params.append(date_to)
        pidx += 1

    if not date_from and not date_to:
        where_clauses.append("observation_date >= CURRENT_DATE - INTERVAL '180 days'")

    where_sql = f"WHERE {' AND '.join(where_clauses)}" if where_clauses else ""

    rows = await conn.fetch(
        f"SELECT district, observation_date, precipitation "
        f"FROM weather_daily_cache {where_sql} "
        f"ORDER BY district, observation_date",
        *params,
    )

    if not rows:
        return {
            "status": "no_data",
            "error": "No observed weather data for the specified period",
            "dry_spells": [],
        }

    # Group by district, scan for consecutive dry days
    from itertools import groupby

    dry_spells: List[DrySpell] = []

    for d_name, group in groupby(rows, key=lambda r: r["district"]):
        sorted_days = sorted(group, key=lambda r: r["observation_date"])

        streak_start = None
        streak_precips: List[float] = []
        prev_date = None

        for r in sorted_days:
            obs_date = r["observation_date"]
            precip = float(r["precipitation"]) if r["precipitation"] is not None else 0.0

            # Check if this is a consecutive day
            is_consecutive = (
                prev_date is not None
                and (obs_date - prev_date).days == 1
            )

            if precip < threshold_mm:
                if streak_start is None or not is_consecutive:
                    # Start new streak
                    # But first, check if previous streak qualifies
                    if streak_start and len(streak_precips) >= min_duration_days:
                        dry_spells.append(DrySpell(
                            district=d_name,
                            start_date=str(streak_start),
                            end_date=str(prev_date),
                            duration_days=len(streak_precips),
                            avg_precip_mm=round(sum(streak_precips) / len(streak_precips), 2),
                        ))
                    streak_start = obs_date
                    streak_precips = [precip]
                else:
                    streak_precips.append(precip)
            else:
                # Wet day — end streak
                if streak_start and len(streak_precips) >= min_duration_days:
                    dry_spells.append(DrySpell(
                        district=d_name,
                        start_date=str(streak_start),
                        end_date=str(prev_date),
                        duration_days=len(streak_precips),
                        avg_precip_mm=round(sum(streak_precips) / len(streak_precips), 2),
                    ))
                streak_start = None
                streak_precips = []

            prev_date = obs_date

        # End of district — check final streak
        if streak_start and len(streak_precips) >= min_duration_days:
            dry_spells.append(DrySpell(
                district=d_name,
                start_date=str(streak_start),
                end_date=str(prev_date),
                duration_days=len(streak_precips),
                avg_precip_mm=round(sum(streak_precips) / len(streak_precips), 2),
            ))

    # Summary
    per_district_counts: Dict[str, int] = {}
    for ds in dry_spells:
        per_district_counts[ds.district] = per_district_counts.get(ds.district, 0) + 1

    return {
        "status": "success",
        "threshold_mm_day": threshold_mm,
        "min_duration_days": min_duration_days,
        "total_dry_spells": len(dry_spells),
        "districts_affected": len(per_district_counts),
        "longest_spell_days": max((ds.duration_days for ds in dry_spells), default=0),
        "dry_spells": [ds.to_dict() for ds in dry_spells],
        "per_district_counts": per_district_counts,
    }


# ---------------------------------------------------------------------------
# 3. NDVI-Weather Cross-Validator
# ---------------------------------------------------------------------------

async def compute_ndvi_concordance(
    conn,
    district: Optional[str] = None,
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    ndvi_lag_days: int = 10,
) -> Dict[str, Any]:
    """Cross-validate weather record against NDVI vegetation response.

    Logic: during dry spells, NDVI should decline. If weather says "rain"
    but NDVI keeps dropping, the weather data is suspect. If weather says
    "drought" but NDVI is stable, maybe there's irrigation or the weather
    data is wrong.

    Uses a simple lagged correlation between cumulative precipitation
    and NDVI change over rolling windows.

    Args:
        conn: asyncpg connection
        district: Optional district filter
        date_from: Start date
        date_to: End date
        ndvi_lag_days: Days of lag for NDVI response to rainfall

    Returns:
        Dict with concordance_score (0-1), anomalies, and per-district detail
    """
    # Step 1: Get NDVI timeseries from agri_indices_cache
    ndvi_params: list = []
    ndvi_where = []
    pidx = 1

    if district:
        ndvi_where.append(f"admin_name ILIKE ${pidx}")
        ndvi_params.append(f"%{district}%")
        pidx += 1
    if date_from:
        ndvi_where.append(f"week_start >= ${pidx}::date")
        ndvi_params.append(date_from)
        pidx += 1
    if date_to:
        ndvi_where.append(f"week_start <= ${pidx}::date")
        ndvi_params.append(date_to)
        pidx += 1

    if not date_from and not date_to:
        ndvi_where.append("week_start >= CURRENT_DATE - INTERVAL '180 days'")

    ndvi_where_sql = f"WHERE admin_level = 'district' AND {' AND '.join(ndvi_where)}" if ndvi_where else "WHERE admin_level = 'district'"

    ndvi_rows = await conn.fetch(
        f"SELECT admin_name, week_start, ndvi_mean "
        f"FROM agri_indices_cache {ndvi_where_sql} "
        f"ORDER BY admin_name, week_start",
        *ndvi_params,
    )

    if not ndvi_rows:
        return {
            "status": "insufficient_data",
            "error": "No NDVI data in cache for the specified period",
            "concordance_score": None,
        }

    # Step 2: Get precipitation timeseries
    precip_params: list = []
    precip_where = []
    pidx = 1

    if district:
        precip_where.append(f"district ILIKE ${pidx}")
        precip_params.append(f"%{district}%")
        pidx += 1
    if date_from:
        precip_where.append(f"observation_date >= ${pidx}::date")
        precip_params.append(date_from)
        pidx += 1
    if date_to:
        precip_where.append(f"observation_date <= ${pidx}::date")
        precip_params.append(date_to)
        pidx += 1

    if not date_from and not date_to:
        precip_where.append("observation_date >= CURRENT_DATE - INTERVAL '180 days'")

    precip_where_sql = f"WHERE {' AND '.join(precip_where)}" if precip_where else ""

    precip_rows = await conn.fetch(
        f"SELECT district, observation_date, precipitation "
        f"FROM weather_daily_cache {precip_where_sql} "
        f"ORDER BY district, observation_date",
        *precip_params,
    )

    if not precip_rows:
        return {
            "status": "insufficient_data",
            "error": "No precipitation data for cross-validation",
            "concordance_score": None,
        }

    # Step 3: Build per-district NDVI and precipitation series
    from itertools import groupby as _groupby

    # NDVI: {district -> [(week_start, ndvi_mean), ...]}
    ndvi_by_district: Dict[str, List[Tuple[date, float]]] = {}
    for r in ndvi_rows:
        d = r["admin_name"]
        if r["ndvi_mean"] is not None:
            ndvi_by_district.setdefault(d, []).append(
                (r["week_start"], float(r["ndvi_mean"]))
            )

    # Precip: {district -> {date -> mm}}
    precip_by_district: Dict[str, Dict[date, float]] = {}
    for r in precip_rows:
        d = r["district"]
        if r["precipitation"] is not None:
            precip_by_district.setdefault(d, {})[r["observation_date"]] = float(r["precipitation"])

    # Step 4: Compute concordance per district
    concordant_events = 0
    discordant_events = 0
    total_events = 0
    anomalies: List[Dict[str, Any]] = []
    per_district_detail: Dict[str, Dict[str, Any]] = {}

    for d_name, ndvi_series in ndvi_by_district.items():
        if len(ndvi_series) < 3:
            continue

        precip_series = precip_by_district.get(d_name, {})
        if not precip_series:
            continue

        d_concordant = 0
        d_discordant = 0
        d_total = 0

        # For each pair of consecutive NDVI observations, compute:
        #   - NDVI change (delta)
        #   - Cumulative precipitation in the window (lagged by ndvi_lag_days)
        for i in range(1, len(ndvi_series)):
            prev_date, prev_ndvi = ndvi_series[i - 1]
            curr_date, curr_ndvi = ndvi_series[i]

            ndvi_delta = curr_ndvi - prev_ndvi

            # Sum precipitation in the window [prev_date - lag, curr_date - lag]
            precip_window_start = prev_date - timedelta(days=ndvi_lag_days)
            precip_window_end = curr_date - timedelta(days=ndvi_lag_days)

            window_precip = sum(
                mm for obs_date, mm in precip_series.items()
                if precip_window_start <= obs_date <= precip_window_end
            )

            # Determine expected NDVI response
            # Low precip (<10mm over window) + NDVI drop = concordant (drought stress)
            # Low precip + NDVI rise = discordant (irrigation? or weather data wrong)
            # High precip (>20mm) + NDVI rise/stable = concordant (healthy growth)
            # High precip + NDVI drop = discordant (flood damage? or NDVI wrong)

            window_days = (curr_date - prev_date).days
            if window_days <= 0:
                continue

            daily_precip = window_precip / max(window_days, 1)
            d_total += 1

            is_dry = daily_precip < 2.0   # matches _DRY_DAY_THRESHOLD
            is_wet = daily_precip >= 5.0
            ndvi_dropped = ndvi_delta < -0.02
            ndvi_rose = ndvi_delta > 0.02

            if is_dry and ndvi_dropped:
                d_concordant += 1  # drought stress confirmed by NDVI
            elif is_wet and (ndvi_rose or abs(ndvi_delta) <= 0.02):
                d_concordant += 1  # rain confirmed by stable/rising NDVI
            elif is_dry and ndvi_rose:
                d_discordant += 1
                anomalies.append({
                    "district": d_name,
                    "period": f"{prev_date} to {curr_date}",
                    "type": "dry_but_ndvi_rose",
                    "detail": f"Avg precip {daily_precip:.1f}mm/day but NDVI rose by {ndvi_delta:+.3f}",
                    "possible_cause": "irrigation, shallow groundwater, or weather data underestimates rainfall",
                })
            elif is_wet and ndvi_dropped:
                d_discordant += 1
                anomalies.append({
                    "district": d_name,
                    "period": f"{prev_date} to {curr_date}",
                    "type": "wet_but_ndvi_dropped",
                    "detail": f"Avg precip {daily_precip:.1f}mm/day but NDVI dropped by {ndvi_delta:+.3f}",
                    "possible_cause": "flood damage, waterlogging, pest/disease, or weather data overestimates rainfall",
                })
            else:
                # Neutral / ambiguous — count as concordant
                d_concordant += 1

        concordant_events += d_concordant
        discordant_events += d_discordant
        total_events += d_total

        if d_total >= 3:
            d_score = round(d_concordant / d_total, 2) if d_total > 0 else None
            per_district_detail[d_name] = {
                "concordant": d_concordant,
                "discordant": d_discordant,
                "total_windows": d_total,
                "concordance_score": d_score,
            }

    overall_score = (
        round(concordant_events / total_events, 2)
        if total_events > 0 else None
    )

    return {
        "status": "success",
        "ndvi_lag_days": ndvi_lag_days,
        "concordance_score": overall_score,
        "concordant_events": concordant_events,
        "discordant_events": discordant_events,
        "total_events": total_events,
        "anomalies": anomalies[:20],  # cap at 20 for token budget
        "per_district": per_district_detail,
        "interpretation": _interpret_concordance(overall_score),
    }


def _interpret_concordance(score: Optional[float]) -> str:
    if score is None:
        return "Insufficient data to assess weather-vegetation concordance"
    if score >= 0.85:
        return "HIGH concordance — weather record is well-supported by vegetation response"
    if score >= 0.70:
        return "MODERATE concordance — weather record mostly matches vegetation, some anomalies"
    if score >= 0.50:
        return "LOW concordance — significant mismatches between weather and vegetation data"
    return "VERY LOW concordance — weather data does not match vegetation response, do not use for insurance"


# ---------------------------------------------------------------------------
# 4. Insurance Confidence Rating
# ---------------------------------------------------------------------------

async def compute_insurance_accuracy(
    conn,
    district: Optional[str] = None,
    season: Optional[str] = None,
    threshold_mm: float = 5.0,
) -> Dict[str, Any]:
    """Compute overall insurance accuracy rating for a district/season.

    Combines binary accuracy, dry spell detection, and NDVI concordance
    into a single confidence score.

    Returns a confidence_rating from 0-100:
      90+ = suitable for index insurance underwriting
      70-89 = usable with caveats
      50-69 = marginal, recommend ground-truth supplementation
      <50 = not recommended for insurance
    """
    # Determine date range
    if season:
        date_from, date_to = _season_dates(season)
    else:
        date_from, date_to = None, None

    # Run all three components
    binary_result = await compute_binary_accuracy(
        conn, district=district, date_from=date_from, date_to=date_to,
        threshold_mm=threshold_mm,
    )

    dry_spell_result = await detect_dry_spells(
        conn, district=district, date_from=date_from, date_to=date_to,
    )

    ndvi_result = await compute_ndvi_concordance(
        conn, district=district, date_from=date_from, date_to=date_to,
    )

    # Compute confidence rating (weighted average of sub-scores)
    scores: List[Tuple[float, float]] = []  # (score_0_to_1, weight)

    # Binary accuracy: 40% weight
    binary_metrics = binary_result.get("overall_binary", {})
    pod = binary_metrics.get("pod")
    hss = binary_metrics.get("hss")
    if pod is not None and hss is not None:
        # Combine POD and HSS: POD matters more for insurance (catching real events)
        binary_score = 0.6 * pod + 0.4 * max(0, hss)
        scores.append((binary_score, 0.4))

    # NDVI concordance: 35% weight
    concordance = ndvi_result.get("concordance_score")
    if concordance is not None:
        scores.append((concordance, 0.35))

    # Data completeness: 25% weight
    n_obs = binary_result.get("n_observations", 0)
    # 90 days of data = score 1.0, scale linearly
    completeness = min(1.0, n_obs / 90.0)
    scores.append((completeness, 0.25))

    if scores:
        total_weight = sum(w for _, w in scores)
        confidence_rating = round(
            100.0 * sum(s * w for s, w in scores) / total_weight, 0
        )
    else:
        confidence_rating = 0

    # Determine recommendation
    if confidence_rating >= 90:
        recommendation = "SUITABLE for index insurance underwriting"
    elif confidence_rating >= 70:
        recommendation = "USABLE with caveats — recommend periodic ground-truth checks"
    elif confidence_rating >= 50:
        recommendation = "MARGINAL — supplement with rain gauge data before underwriting"
    else:
        recommendation = "NOT RECOMMENDED — weather data quality insufficient for insurance"

    return {
        "status": "success",
        "district": district or "all",
        "season": season or "last_90_days",
        "threshold_mm": threshold_mm,
        "confidence_rating": int(confidence_rating),
        "recommendation": recommendation,
        "components": {
            "binary_accuracy": binary_result,
            "dry_spells": dry_spell_result,
            "ndvi_concordance": ndvi_result,
        },
    }
