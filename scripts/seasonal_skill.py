"""How well do Copernicus seasonal forecasts predict Rwanda's Oct-Dec rainfall?

ECMWF SEAS5 (system 51) total precipitation, issued on 1 October, lead
months 1-3 (Oct, Nov, Dec), over a box around Rwanda; 1993-2016 hindcasts
plus 2017-2025 real-time forecasts, compared year by year with CHIRPS v2.0
Oct-Dec totals averaged over Rwanda's districts. Also reports the 2026
forecast as tercile probabilities against the model's own 1993-2025 climate.

    pip install netCDF4   # analysis only; not an app dependency
    python scripts/seasonal_skill.py --out seasonal.json

Needs CDSAPI_KEY (the seasonal-monthly-single-levels licence accepted on the
CDS site), POSTGRES_* (district polygons) and network access to
data.chc.ucsb.edu.
"""

from __future__ import annotations

import argparse
import asyncio
import calendar
import json
import os
import statistics
import tempfile
import zipfile

import asyncpg
import cdsapi
import numpy as np
import rasterio
import xarray as xr
from rasterio.features import geometry_mask
from rasterio.windows import from_bounds

YEARS = list(range(1993, 2026))
FORECAST_YEAR = 2026
AREA = [-0.5, 28.5, -3.5, 31.5]  # N, W, S, E: the 1-degree grid points around Rwanda
RWANDA_BOUNDS = (28.8, -2.9, 30.95, -1.0)
COG = "https://data.chc.ucsb.edu/products/CHIRPS-2.0/global_monthly/cogs/chirps-v2.0.{y}.{m:02d}.cog"
MONTHS = (10, 11, 12)


def _retrieve(years: list[int], path: str) -> None:
    client = cdsapi.Client(url=os.environ.get("CDSAPI_URL", "https://cds.climate.copernicus.eu/api"),
                           key=os.environ["CDSAPI_KEY"], quiet=True)
    client.retrieve("seasonal-monthly-single-levels", {
        "originating_centre": "ecmwf", "system": "51", "variable": ["total_precipitation"],
        "product_type": ["monthly_mean"], "year": [str(y) for y in years], "month": ["10"],
        "leadtime_month": ["1", "2", "3"], "area": AREA, "data_format": "netcdf",
    }, path)


def _open(path: str) -> xr.Dataset:
    if zipfile.is_zipfile(path):
        d = tempfile.mkdtemp()
        with zipfile.ZipFile(path) as z:
            z.extractall(d)
            path = os.path.join(d, next(n for n in z.namelist() if n.endswith(".nc")))
    return xr.open_dataset(path)


def _ond_totals_mm(ds: xr.Dataset) -> dict[int, np.ndarray]:
    """{init year: OND total per ensemble member (mm), mean over the box's grid points}."""
    tp = ds["tprate"] if "tprate" in ds else ds[list(ds.data_vars)[0]]
    time_dim = "forecast_reference_time" if "forecast_reference_time" in tp.dims else "time"
    lead_dim = "forecastMonth" if "forecastMonth" in tp.dims else "leadtime_month"
    member_dim = "number"
    out = {}
    for t in tp[time_dim].values:
        year = int(str(np.datetime_as_string(t))[:4])
        sel = tp.sel({time_dim: t})
        total = 0
        for i, month in enumerate(MONTHS):
            seconds = calendar.monthrange(year, month)[1] * 86400
            field = sel.isel({lead_dim: i}) * seconds * 1000  # m/s mean rate -> mm/month
            total = total + field.mean(dim=[d for d in field.dims if d not in (member_dim,)])
        values = np.asarray(total.values, dtype=float)
        out[year] = values[~np.isnan(values)]
    return out


async def _districts() -> list[dict]:
    conn = await asyncpg.connect(
        host=os.environ.get("POSTGRES_HOST", "postgresdb"), port=int(os.environ.get("POSTGRES_PORT", "5432")),
        user=os.environ["POSTGRES_USER"], password=os.environ["POSTGRES_PASSWORD"],
        database=os.environ.get("POSTGRES_DB", "mundidb"))
    try:
        rows = await conn.fetch("SELECT ST_AsGeoJSON(geom) AS g FROM rwanda_district_boundaries")
    finally:
        await conn.close()
    return [json.loads(r["g"]) for r in rows]


def _chirps_ond(year: int, geoms: list[dict]) -> float:
    total = 0.0
    for m in MONTHS:
        with rasterio.open("/vsicurl/" + COG.format(y=year, m=m)) as src:
            w = from_bounds(*RWANDA_BOUNDS, transform=src.transform)
            a = src.read(1, window=w).astype("float64")
            tr = src.window_transform(w)
        a[a < 0] = np.nan
        total += statistics.fmean(float(np.nanmean(a[~geometry_mask([g], a.shape, tr, all_touched=True)]))
                                  for g in geoms)
    return total


def _tercile(value: float, sample: list[float]) -> int:
    lo, hi = np.percentile(sample, [100 / 3, 200 / 3])
    return 0 if value < lo else (2 if value > hi else 1)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--out", required=True)
    args = parser.parse_args()
    tmp = tempfile.mkdtemp()
    _retrieve(YEARS, f"{tmp}/hind.nc")
    hind = _open(f"{tmp}/hind.nc")
    lats, lons = hind["latitude"].values.tolist(), hind["longitude"].values.tolist()
    model = _ond_totals_mm(hind)
    unavailable = ""
    try:  # ECMWF publishes each month's forecast on the 5th
        _retrieve([FORECAST_YEAR], f"{tmp}/fc.nc")
        current = _ond_totals_mm(_open(f"{tmp}/fc.nc"))[FORECAST_YEAR]
    except Exception as exc:  # noqa: BLE001 - reported in the output, not fatal
        current = None
        unavailable = f"{type(exc).__name__}: {str(exc)[:200]}"

    geoms = asyncio.run(_districts())
    obs = {y: _chirps_ond(y, geoms) for y in YEARS}

    years = [y for y in YEARS if y in model]
    ens_mean = [float(np.mean(model[y])) for y in years]
    observed = [obs[y] for y in years]
    corr = float(np.corrcoef(ens_mean, observed)[0, 1])
    hits, dry_calls, dry_right, wet_calls, wet_right = 0, 0, 0, 0, 0
    for i, _year in enumerate(years):  # leave-one-out terciles
        f_cat = _tercile(ens_mean[i], ens_mean[:i] + ens_mean[i + 1:])
        o_cat = _tercile(observed[i], observed[:i] + observed[i + 1:])
        hits += f_cat == o_cat
        dry_calls += f_cat == 0
        dry_right += f_cat == 0 and o_cat == 0
        wet_calls += f_cat == 2
        wet_right += f_cat == 2 and o_cat == 2
    all_members = np.concatenate([model[y] for y in years])
    lo, hi = np.percentile(all_members, [100 / 3, 200 / 3])
    result = {
        "system": "ECMWF SEAS5 (51), issued 1 October, Oct-Dec total", "years": f"{years[0]}-{years[-1]}",
        "grid": {"resolution_deg": abs(lats[1] - lats[0]) if len(lats) > 1 else None,
                 "latitudes": lats, "longitudes": lons, "points_averaged": len(lats) * len(lons)},
        "skill": {
            "n_years": len(years), "correlation_with_chirps": round(corr, 3),
            "tercile_hit_rate": round(hits / len(years), 3), "tercile_hit_rate_by_chance": 0.333,
            "dry_tercile_calls": dry_calls, "dry_calls_correct": dry_right,
            "wet_tercile_calls": wet_calls, "wet_calls_correct": wet_right,
        },
        "observed_chirps_ond_mm": {str(y): round(obs[y], 1) for y in years},
        "model_ensemble_mean_ond_mm": {str(y): round(m, 1) for y, m in zip(years, ens_mean)},
        "forecast_2026": {"unavailable": unavailable} if current is None else {
            "members": int(len(current)),
            "p_below_normal": round(float(np.mean(current < lo)), 2),
            "p_near_normal": round(float(np.mean((current >= lo) & (current <= hi))), 2),
            "p_above_normal": round(float(np.mean(current > hi)), 2),
            "ensemble_mean_mm": round(float(np.mean(current)), 1),
            "model_climate_mean_mm": round(float(np.mean(all_members)), 1),
        },
    }
    with open(args.out, "w") as fh:
        json.dump(result, fh, indent=1)
    print(json.dumps(result["grid"]), json.dumps(result["skill"]), json.dumps(result["forecast_2026"]), sep="\n")


if __name__ == "__main__":
    main()
