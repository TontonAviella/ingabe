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
"""Each Rwanda district's NDVI anomaly for its latest published month, and which are alerts.

Sage's get_anomaly_alerts answers from here. The anomaly is Digital Earth Africa's monthly
`ndvi_anomaly` averaged over the district's clear pixels (deafrica_stac.area_ndvi_anomaly); the
alert classes are ndvi_classes.NDVI_ANOMALY_ALERTS. A district whose month had too few clear
pixels, that has no published month, or that was not read before the deadline is listed as
missing with the reason, never as 0. This replaced weekly_anomaly_scan (2026-10-07), whose
alerts compared a district with its own last 8 weeks and could not fire on the data it had.
"""

from __future__ import annotations

import asyncio
import calendar
import logging
from datetime import date
from typing import Any, Optional

from src.services import admin_boundaries, deafrica_stac
from src.services.ndvi_classes import NDVI_ANOMALY_ALERTS, anomaly_alert_scale_text, ndvi_anomaly_alert

logger = logging.getLogger(__name__)

# District reads run in this many threads at once: a cold read takes 4-27 s and holds three
# bands of up to ~2 million pixels.
WORKERS = 4
# All reads get this long in total, below Sage's 120 s tool limit. A read still running then is
# reported as missing; its thread keeps going (a deadline abandons a thread, it does not stop it)
# and fills deafrica_stac's month cache, so the next request answers it quickly.
DEADLINE_S = 90.0


def _month_ended_days_ago(month: str, today: date) -> int:
    year, mon = (int(v) for v in month.split("-"))
    return (today - date(year, mon, calendar.monthrange(year, mon)[1])).days


def _missing_reason(anomaly: Optional[dict]) -> Optional[str]:
    """Why a district's read has no z-score, or None when it has one."""
    if anomaly is None:
        return "no month published for it in the last two months"
    if anomaly["z"] is None:
        return (f"only {anomaly['clear_fraction']:.0%} of it had a clear view in {anomaly['month']} "
                f"(needs {deafrica_stac.NDVI_ANOMALY_MIN_CLEAR:.0%})")
    return None


async def district_ndvi_anomalies(
    conn,
    today: date,
    *,
    district: str = "",
    severity: str = "",
    deadline_s: float = DEADLINE_S,
) -> dict[str, Any]:
    """Every district's (or one district's) NDVI anomaly for the latest month published by `today`.

    Returns {"source", "scale", "checked", "districts": rows sorted by z (worst first), "alerts":
    the rows with an alert class (only `severity`'s when given), "missing": [{"district",
    "reason"}]}. A row is {"district", "month", "month_ended_days_ago", "z", "ndvi",
    "clear_fraction", "alert", "alert_label"}. Raises ValueError for an unknown district or
    severity.
    """
    if severity and severity not in {a.key for a in NDVI_ANOMALY_ALERTS}:
        raise ValueError(f"severity must be one of {', '.join(a.key for a in NDVI_ANOMALY_ALERTS)}")
    names = [u["name"] for u in (await admin_boundaries.list_admin_units(conn, "district"))["units"]]
    if district:
        wanted = [n for n in names if n.lower() == district.strip().lower()]
        if not wanted:
            raise ValueError(f"no district named {district!r}; Rwanda's districts: {', '.join(names)}")
        names = wanted

    slots = asyncio.Semaphore(WORKERS)

    async def read(name: str) -> Optional[dict]:
        geometry = await admin_boundaries.lookup_admin_geometry(district=name)
        if geometry is None:
            raise LookupError("its boundary could not be read")
        async with slots:
            return await asyncio.to_thread(deafrica_stac.area_ndvi_anomaly, geometry, today)

    tasks = {name: asyncio.create_task(read(name)) for name in names}
    await asyncio.wait(tasks.values(), timeout=deadline_s)

    rows: list[dict[str, Any]] = []
    missing: list[dict[str, str]] = []
    for name, task in tasks.items():
        if not task.done():
            task.cancel()
            missing.append({"district": name, "reason": f"not read within {deadline_s:.0f} s; ask again shortly"})
            continue
        if task.exception() is not None:
            logger.warning("NDVI anomaly read for %s failed: %s", name, task.exception())
            missing.append({"district": name, "reason": f"the read failed ({task.exception()})"})
            continue
        anomaly = task.result()
        reason = _missing_reason(anomaly)
        if reason:
            missing.append({"district": name, "reason": reason})
            continue
        alert = ndvi_anomaly_alert(anomaly["z"])
        rows.append({
            "district": name,
            "month": anomaly["month"],
            "month_ended_days_ago": _month_ended_days_ago(anomaly["month"], today),
            "z": anomaly["z"],
            "ndvi": anomaly["ndvi"],
            "clear_fraction": anomaly["clear_fraction"],
            "alert": alert.key if alert else None,
            "alert_label": alert.label if alert else None,
        })
    rows.sort(key=lambda r: r["z"])
    return {
        "source": deafrica_stac.NDVI_ANOMALY_SOURCE,
        "scale": anomaly_alert_scale_text(),
        "checked": len(names),
        "districts": rows,
        "alerts": [r for r in rows if r["alert"] and (not severity or r["alert"] == severity)],
        "missing": missing,
    }
