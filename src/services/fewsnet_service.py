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

"""FEWS NET IPC food security classification service.

Fetches IPC phase classifications from the FEWS NET Data Warehouse API
(https://fdw.fews.net/api/) and returns structured results for Rwanda.
"""

import logging
import time
from typing import Any

import httpx

logger = logging.getLogger(__name__)

FEWS_NET_API = "https://fdw.fews.net/api/ipcphase/"
COUNTRY_CODE = "RW"

# In-memory cache with TTL (API data updates monthly at most)
_cache: dict[str, Any] = {}
_cache_ts: float = 0.0
_CACHE_TTL_S = 86_400  # 24 hours

IPC_LABELS = {
    1: "Minimal",
    2: "Stressed",
    3: "Crisis",
    4: "Emergency",
    5: "Famine",
}


def _fetch_all() -> list[dict]:
    """Fetch all IPC records for Rwanda from FEWS NET API."""
    global _cache, _cache_ts

    now = time.time()
    if _cache.get("records") and (now - _cache_ts) < _CACHE_TTL_S:
        return _cache["records"]

    resp = httpx.get(
        FEWS_NET_API,
        params={"country_code": COUNTRY_CODE, "format": "json"},
        timeout=30,
    )
    resp.raise_for_status()
    records = resp.json()

    _cache["records"] = records
    _cache_ts = now
    logger.info("FEWS NET: cached %d IPC records for %s", len(records), COUNTRY_CODE)
    return records


def get_food_security(
    district: str | None = None,
    period: str = "current",
) -> dict:
    """Query IPC food security classification for Rwanda.

    Args:
        district: Optional district name filter (case-insensitive).
        period: "current" for latest situation, "projected" for near-term projection.

    Returns:
        Dict with status, classifications list, and metadata.
    """
    records = _fetch_all()

    # Map period argument to FEWS NET scenario names
    scenario_map = {
        "current": "Current Situation",
        "projected": "Most Likely",
        "near_term": "Near Term Projection",
        "medium_term": "Medium Term Projection",
    }
    target_scenario = scenario_map.get(period, "Current Situation")

    # Get the most recent reporting date
    dates = sorted(set(r.get("reporting_date", "") for r in records if r.get("reporting_date")))
    if not dates:
        return {"status": "error", "error": "No IPC data available for Rwanda"}

    latest_date = dates[-1]

    # Filter to latest date + target scenario
    filtered = [
        r for r in records
        if r.get("reporting_date") == latest_date
        and target_scenario.lower() in (r.get("scenario_name", "").strip().lower())
    ]

    # If no exact match, try all scenarios at latest date
    if not filtered:
        filtered = [r for r in records if r.get("reporting_date") == latest_date]

    # If district filter, also search in geographic_unit_name and in admin records
    if district:
        district_lower = district.lower()
        # Also search across all dates for district-specific admin data
        admin_records = [
            r for r in records
            if r.get("unit_type") in ("fsc_admin", "fsc_rm_admin")
            and district_lower in (r.get("geographic_unit_name", "").lower())
        ]
        if admin_records:
            # Use the most recent admin record for this district
            admin_records.sort(key=lambda r: r.get("reporting_date", ""), reverse=True)
            filtered = admin_records[:4]  # current + projections

    classifications = []
    for r in filtered:
        phase = r.get("value")
        if phase is not None:
            phase = int(phase)
        classifications.append({
            "area": r.get("geographic_unit_name", "Rwanda"),
            "ipc_phase": phase,
            "ipc_label": IPC_LABELS.get(phase, "Unknown") if phase else "No data",
            "scenario": r.get("scenario_name", "").strip(),
            "period": f"{r.get('projection_start', '?')} to {r.get('projection_end', '?')}",
            "unit_type": r.get("unit_type", ""),
        })

    # Also include any records with IPC >= 2 (Stressed or worse) from recent history
    stressed_areas = []
    recent_cutoff = dates[-1][:4]  # same year as latest
    for r in records:
        if (r.get("value") or 0) >= 2 and (r.get("reporting_date", "") >= f"{recent_cutoff}-01-01"):
            stressed_areas.append({
                "area": r.get("geographic_unit_name", ""),
                "ipc_phase": int(r["value"]),
                "ipc_label": IPC_LABELS.get(int(r["value"]), ""),
                "date": r.get("reporting_date", ""),
                "scenario": r.get("scenario_name", "").strip(),
            })

    return {
        "status": "success",
        "country": "Rwanda",
        "reporting_date": latest_date,
        "source": "FEWS NET IPC (USAID)",
        "ipc_scale": "1=Minimal, 2=Stressed, 3=Crisis, 4=Emergency, 5=Famine",
        "classifications": classifications,
        "stressed_areas": stressed_areas if stressed_areas else [],
        "note": (
            "Rwanda is generally food-secure (IPC Phase 1). "
            "District-level IPC data may have limited temporal coverage. "
            "For localized food security concerns, combine with crop health and weather data."
        ),
    }
