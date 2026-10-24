"""Ground (land surface) temperature per district: how hot this month is against the same month in earlier years.

Source: MODIS 8-day land surface temperature at 1 km (MOD11A2 Terra + MYD11A2 Aqua, collection 6.1), read from
Microsoft Planetary Computer (free). The weekly Dagster asset `weekly_land_surface_temperature` stores each 8-day
composite's district means in `lst_district_8day`; history since 2000 is stored too, so the normal for a month is
always every earlier year of that month (it grows by itself each year, nothing to recompute).

A month's value is the mean of its composites (both satellites) in which at least MIN_CLEAR of the district had a
clear view; a month with none is unknown, never a guess. The heat condition (TCI) is
(hottest - this) / (hottest - coolest) over the same calendar month since BASELINE_FROM: 0 = hottest on record.

What it may be used for (measured 2026-10-24 against NISR district maize harvests 2021A-2026B, 330
district-seasons): hotter-than-normal ground went with lower maize harvests within a season (rank correlation
+0.14, 95% range +0.05 to +0.21), strongest in October-December of Season A (+0.13 to +0.25, exploratory).
No link was found in Season B or for beans. So it is context next to rain and greenness, never an insurance trigger
on its own. Re-check the link when NISR publishes a new year (around December).

MODIS science ends in 2027 (Terra February, Aqua September); the table's age alert (DataPipelineStale) fires
when composites stop arriving. The successors are Sentinel-3 SLSTR (Copernicus) and VIIRS VNP21.
"""

from __future__ import annotations

import json
import logging
import time
import urllib.request
from collections import defaultdict
from datetime import date
from statistics import median
from typing import Any, Iterable, Optional

logger = logging.getLogger(__name__)

SOURCE = "MODIS land surface temperature (MOD11A2/MYD11A2 v6.1, 1 km, 8-day), Microsoft Planetary Computer"
MIN_CLEAR = 0.3  # share of a district seen clearly for a composite to count (as for NDVI)
BASELINE_FROM = 2003  # Aqua has full years from 2003; both satellites are averaged
MIN_RANGE_K = 0.5  # a calendar month whose years differ by less than this has no meaningful condition
_STAC = "https://planetarycomputer.microsoft.com/api/stac/v1/search"
# The token is for the storage the asset links point to; a token for the collection name is refused (403).
_TOKEN_URL = "https://planetarycomputer.microsoft.com/api/sas/v1/token/modiseuwest/modis-061-cogs"
_RWANDA_BBOX = [28.85, -2.85, 30.9, -1.04]
_GDAL = {"GDAL_HTTP_CONNECTTIMEOUT": "20", "GDAL_HTTP_TIMEOUT": "90", "GDAL_DISABLE_READDIR_ON_OPEN": "EMPTY_DIR"}

LINKED_NOTE = ("In 2021-2025, hotter-than-normal ground in October-December went with lower Season A maize "
               "harvests (a weak link, measured against NISR district harvests). Context, not a trigger.")
UNLINKED_NOTE = "No measured link to harvests for this month and crop; shown as context only."


def harvest_note(month: int, crop: Optional[str]) -> str:
    """What the heat reading is known to say about harvests, for this calendar month and crop."""
    if (crop or "maize").lower() == "maize" and month in (10, 11, 12):
        return LINKED_NOTE
    return UNLINKED_NOTE


def monthly_means(rows: Iterable[tuple[date, Optional[float], Optional[float]]]) -> dict[tuple[int, int], float]:
    """(year, month) -> mean day-time LST (K) of the month's composites seen clearly enough; others left out."""
    acc: dict[tuple[int, int], list[float]] = defaultdict(list)
    for start, lst_k, clear in rows:
        if lst_k is not None and clear is not None and clear >= MIN_CLEAR:
            acc[(start.year, start.month)].append(lst_k)
    return {k: sum(v) / len(v) for k, v in acc.items()}


def condition(monthly: dict[tuple[int, int], float], year: int, month: int) -> Optional[dict[str, Any]]:
    """This month against the same calendar month in every year since BASELINE_FROM, or None if unknown."""
    if (year, month) not in monthly:
        return None
    same = [v for (y, m), v in monthly.items() if m == month and y >= BASELINE_FROM]
    lst = monthly[(year, month)]
    hi, lo = max(same), min(same)
    normal = median(same)
    return {
        "month": f"{year}-{month:02d}",
        "lst_c": round(lst - 273.15, 1),
        "normal_c": round(normal - 273.15, 1),
        "anomaly_c": round(lst - normal, 1),
        "heat_condition": round((hi - lst) / (hi - lo), 2) if hi - lo >= MIN_RANGE_K else None,
        "years_in_normal": len({y for (y, m) in monthly if m == month and y >= BASELINE_FROM}),
    }


async def heat_conditions(conn: Any, districts: list[str], on: date, crop: Optional[str] = None) -> dict[str, dict[str, Any]]:
    """Per district (keyed as given): its latest month with a clear reading on or before `on` (this month or the
    one before), with its normal, heat condition, harvest note and source. Districts without one are left out."""
    if not districts:
        return {}
    rows = await conn.fetch(
        "SELECT lower(district) AS d, composite_start, lst_day_k, day_clear_share FROM lst_district_8day "
        "WHERE lower(district) = ANY($1::text[]) AND composite_start <= $2",
        [d.lower() for d in districts], on,
    )
    by: dict[str, list[tuple[date, Optional[float], Optional[float]]]] = defaultdict(list)
    for r in rows:
        by[r["d"]].append((r["composite_start"], r["lst_day_k"], r["day_clear_share"]))
    out: dict[str, dict[str, Any]] = {}
    for district in districts:
        monthly = monthly_means(by.get(district.lower(), []))
        for back in (0, 1):
            y, m = (on.year, on.month - back) if on.month - back >= 1 else (on.year - 1, 12)
            found = condition(monthly, y, m)
            if found:
                out[district] = {**found, "district": district, "note": harvest_note(m, crop), "source": SOURCE}
                break
    return out


async def heat_condition(conn: Any, district: Optional[str], on: date, crop: Optional[str] = None) -> Optional[dict[str, Any]]:
    """One district's heat condition (see heat_conditions), or None when it has no recent clear reading."""
    if not district:
        return None
    return (await heat_conditions(conn, [district], on, crop)).get(district)


def heat_lines(heat: dict[str, Any]) -> list[str]:
    """The heat condition as report lines: the reading, its normal, and what it may (not) be used for."""
    usual = f"{heat['normal_c']:.1f}°C for this month ({heat['years_in_normal']} years since {BASELINE_FROM})"
    diff = heat["anomaly_c"]
    versus = f"{abs(diff):.1f}°C {'hotter' if diff > 0 else 'cooler'} than the usual {usual}" if diff else f"the usual {usual}"
    lines = [
        "HEAT (ground temperature; context, not a trigger):",
        f"  {heat['month']}: day-time ground {heat['lst_c']:.1f}°C, {versus}",
    ]
    if heat.get("heat_condition") is not None:
        lines.append(f"  Heat condition: {heat['heat_condition']:.2f} (0 = hottest on record for this month, 1 = coolest)")
    lines.append(f"  {heat['note']}")
    return lines


# --- Reading new composites (used by the weekly Dagster asset) -------------------------------------------------

def _get_json(url: str, body: Optional[dict] = None) -> Any:
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, headers={"User-Agent": "ingabe", "Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=120) as resp:
        return json.load(resp)


def composites(date_from: date, date_to: date) -> dict[tuple[str, str], list[dict[str, Any]]]:
    """MODIS 8-day items over Rwanda starting in [date_from, date_to], by (start date, MOD|MYD); every page."""
    body: Optional[dict] = {"collections": ["modis-11A2-061"], "bbox": _RWANDA_BBOX, "limit": 500,
                            "datetime": f"{date_from.isoformat()}T00:00:00Z/{date_to.isoformat()}T23:59:59Z"}
    url = _STAC
    by: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
    while True:
        page = _get_json(url, body)
        for item in page.get("features", []):
            by[(item["properties"]["start_datetime"][:10], item["id"][:3])].append(item)
        nxt = next((link for link in page.get("links", []) if link.get("rel") == "next"), None)
        if not nxt:
            break
        url = nxt["href"]
        body = {**(body or {}), **nxt.get("body", {})} if nxt.get("method") == "POST" else None
    return by


def district_means(items: list[dict[str, Any]], districts: list[tuple[str, dict]]) -> list[dict[str, Any]]:
    """Per district: mean day and night LST (K) over its clear 1 km pixels in this composite's tiles, and the
    share of its pixels that were clear. `districts` = [(name, GeoJSON geometry in WGS84)]."""
    import rasterio  # lazy: heavy, and only the weekly asset reads rasters
    from rasterio.features import geometry_mask
    from rasterio.warp import transform_geom

    token = _get_json(_TOKEN_URL)["token"]
    sums: dict[str, list[float]] = defaultdict(lambda: [0.0, 0, 0, 0.0, 0])
    for item in items:
        arrays = {}
        for band in ("LST_Day_1km", "LST_Night_1km"):
            for attempt in range(3):
                try:
                    with rasterio.Env(**_GDAL), rasterio.open(item["assets"][band]["href"] + "?" + token) as src:
                        arrays[band] = src.read(1).astype("float64")
                        transform, crs, shape = src.transform, src.crs, (src.height, src.width)
                    break
                except Exception as e:  # noqa: BLE001 - retried, then the composite is reported as failed
                    logger.warning("MODIS read %s %s failed (try %d): %s", item["id"], band, attempt + 1, e)
                    time.sleep(10)
            else:
                raise RuntimeError(f"could not read {item['id']} {band}")
        for name, geometry in districts:
            mask = geometry_mask([transform_geom("EPSG:4326", crs, geometry)], out_shape=shape, transform=transform, invert=True)
            if not mask.any():
                continue
            day, night = arrays["LST_Day_1km"][mask], arrays["LST_Night_1km"][mask]
            s = sums[name]
            s[0] += float(day[day > 7500].sum()) * 0.02  # stored as K / 0.02; 0 = no clear view
            s[1] += int((day > 7500).sum())
            s[2] += int(mask.sum())
            s[3] += float(night[night > 7500].sum()) * 0.02
            s[4] += int((night > 7500).sum())
    return [{"district": name, "lst_day_k": round(s[0] / s[1], 2) if s[1] else None, "day_clear_share": round(s[1] / s[2], 3),
             "lst_night_k": round(s[3] / s[4], 2) if s[4] else None, "night_clear_share": round(s[4] / s[2], 3)}
            for name, s in sums.items() if s[2]]
