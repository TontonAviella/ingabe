"""Per-district monthly and seasonal rainfall climatology from CHIRPS v2.0.

Reads only Rwanda's window of each CHIRPS global monthly COG (0.05 deg),
averages it over each district polygon (rwanda_district_boundaries), and
writes mean/std per district and month, plus Season A (Sep-Jan) and Season
B (Feb-May) totals, as JSON.

    python scripts/rainfall_climatology.py --years 2000-2023 --out climatology.json

Needs POSTGRES_* env vars (district polygons) and network access to
data.chc.ucsb.edu.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import statistics
from concurrent.futures import ThreadPoolExecutor

import asyncpg
import numpy as np
import rasterio
from rasterio.features import geometry_mask
from rasterio.windows import from_bounds

COG = "https://data.chc.ucsb.edu/products/CHIRPS-2.0/global_monthly/cogs/chirps-v2.0.{y}.{m:02d}.cog"
RWANDA_BOUNDS = (28.8, -2.9, 30.95, -1.0)  # west, south, east, north
SEASON_MONTHS = {"A": (9, 10, 11, 12, 13), "B": (2, 3, 4, 5)}  # 13 = January of next year


async def _districts() -> dict[str, dict]:
    conn = await asyncpg.connect(
        host=os.environ.get("POSTGRES_HOST", "postgresdb"),
        port=int(os.environ.get("POSTGRES_PORT", "5432")),
        user=os.environ["POSTGRES_USER"],
        password=os.environ["POSTGRES_PASSWORD"],
        database=os.environ.get("POSTGRES_DB", "mundidb"),
    )
    try:
        rows = await conn.fetch(
            "SELECT lower(district) AS d, ST_AsGeoJSON(geom) AS g FROM rwanda_district_boundaries"
        )
    finally:
        await conn.close()
    return {r["d"]: json.loads(r["g"]) for r in rows}


def _month_means(year: int, month: int, districts: dict[str, dict]) -> dict[str, float]:
    with rasterio.Env(GDAL_DISABLE_READDIR_ON_OPEN="EMPTY_DIR"):
        with rasterio.open("/vsicurl/" + COG.format(y=year, m=month)) as src:
            window = from_bounds(*RWANDA_BOUNDS, transform=src.transform)
            data = src.read(1, window=window).astype("float64")
            transform = src.window_transform(window)
    data[data < 0] = np.nan  # CHIRPS nodata is -9999
    out = {}
    for name, geom in districts.items():
        inside = ~geometry_mask([geom], data.shape, transform, all_touched=True)
        out[name] = float(np.nanmean(data[inside]))
    return out


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--years", default="2000-2023")
    parser.add_argument("--out", required=True)
    args = parser.parse_args()
    first, last = (int(y) for y in args.years.split("-"))

    districts = asyncio.run(_districts())
    months = [(y, m) for y in range(first, last + 2) for m in range(1, 13)
              if y <= last or m == 1]  # January of last+1 closes the last Season A
    with ThreadPoolExecutor(max_workers=8) as pool:
        values = dict(zip(months, pool.map(lambda ym: _month_means(*ym, districts), months)))

    result: dict = {"source": "CHIRPS v2.0 global monthly, district polygon means",
                    "years": f"{first}-{last}", "districts": {}}
    for d in sorted(districts):
        monthly = {}
        for m in range(1, 13):
            series = [values[(y, m)][d] for y in range(first, last + 1)]
            monthly[m] = {"mean": round(statistics.fmean(series), 1),
                          "std": round(statistics.stdev(series), 1)}
        seasons = {}
        for season, ms in SEASON_MONTHS.items():
            totals = [sum(values[(y + (m - 1) // 12, (m - 1) % 12 + 1)][d] for m in ms)
                      for y in range(first, last + 1)]
            seasons[season] = {"mean": round(statistics.fmean(totals), 1),
                               "std": round(statistics.stdev(totals), 1)}
        result["districts"][d] = {"monthly": monthly, "seasonal": seasons}
    # National: the district average for each month, then mean/std across years
    national = {}
    for m in range(1, 13):
        series = [statistics.fmean(values[(y, m)].values()) for y in range(first, last + 1)]
        national[m] = {"mean": round(statistics.fmean(series), 1),
                       "std": round(statistics.stdev(series), 1)}
    result["national"] = {"monthly": national}
    with open(args.out, "w") as fh:
        json.dump(result, fh, indent=1)
    print(f"wrote {args.out}: {len(districts)} districts, {len(months)} months")


if __name__ == "__main__":
    main()
