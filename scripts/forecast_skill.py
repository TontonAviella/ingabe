"""Verify daily rainfall forecasts against CHIRPS, per lead time (1-7 days).

Forecasts as issued come from Open-Meteo's Previous Runs API
(`precipitation_previous_dayN` = the run issued N days before), for the
models forecast_openmeteo.py fuses, at each district centroid. Truth is
CHIRPS v2.0 daily (0.05 deg) averaged over the district polygon. Days are
UTC, as CHIRPS days are. The baseline is climatology: each day's normal
from src/services/monthly_rainfall_normals.json.

    python scripts/forecast_skill.py --start 2025-03-01 --end 2026-06-30 \
        --every 2 --cache-dir .cache/openmeteo --out skill.json

Open-Meteo's free quota is shared with the live app's forecasts on this
machine: responses are cached (--cache-dir), requests go one at a time with
a pause, and a rate-limit response stops the run instead of retrying.
"""

from __future__ import annotations

import argparse
import asyncio
import calendar
import json
import math
import os
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from datetime import date, timedelta
from pathlib import Path

import asyncpg
import numpy as np
import rasterio
from rasterio.features import geometry_mask
from rasterio.windows import from_bounds

MODELS = ["ecmwf_ifs025", "gfs_global", "icon_global", "gfs_graphcast025"]
LEADS = range(1, 8)
RAIN_DAY_MM = 1.0
CHIRPS_DAILY = ("https://data.chc.ucsb.edu/products/CHIRPS-2.0/global_daily/cogs/p05/"
                "{y}/chirps-v2.0.{y}.{m:02d}.{d:02d}.cog")
PREVIOUS_RUNS = "https://previous-runs-api.open-meteo.com/v1/forecast"
RWANDA_BOUNDS = (28.8, -2.9, 30.95, -1.0)
NORMALS = Path(__file__).resolve().parent.parent / "src/services/monthly_rainfall_normals.json"
CACHE_DIR: Path | None = None  # --cache-dir: keep API responses so reruns cost no quota
REQUEST_PAUSE_S = 6.0  # stay under the per-minute limit; each long request counts as many calls


async def _districts() -> dict[str, dict]:
    conn = await asyncpg.connect(
        host=os.environ.get("POSTGRES_HOST", "postgresdb"),
        port=int(os.environ.get("POSTGRES_PORT", "5432")),
        user=os.environ["POSTGRES_USER"], password=os.environ["POSTGRES_PASSWORD"],
        database=os.environ.get("POSTGRES_DB", "mundidb"),
    )
    try:
        rows = await conn.fetch(
            "SELECT lower(district) AS d, ST_AsGeoJSON(geom) AS g, "
            "ST_Y(ST_PointOnSurface(geom)) AS lat, ST_X(ST_PointOnSurface(geom)) AS lon "
            "FROM rwanda_district_boundaries"
        )
    finally:
        await conn.close()
    return {r["d"]: {"geom": json.loads(r["g"]), "lat": r["lat"], "lon": r["lon"]} for r in rows}


def _chirps_day(day: date, districts: dict[str, dict]) -> dict[str, float] | None:
    url = CHIRPS_DAILY.format(y=day.year, m=day.month, d=day.day)
    try:
        with rasterio.Env(GDAL_DISABLE_READDIR_ON_OPEN="EMPTY_DIR"):
            with rasterio.open("/vsicurl/" + url) as src:
                window = from_bounds(*RWANDA_BOUNDS, transform=src.transform)
                data = src.read(1, window=window).astype("float64")
                transform = src.window_transform(window)
    except rasterio.errors.RasterioIOError:
        return None  # not published yet
    data[data < 0] = np.nan
    return {name: float(np.nanmean(data[~geometry_mask([d["geom"]], data.shape, transform,
                                                         all_touched=True)]))
            for name, d in districts.items()}


def _forecasts(lat: float, lon: float, model: str, start: date, end: date) -> dict[int, dict[str, float]]:
    """{lead: {date: mm}} from hourly previous-run precipitation, summed per UTC day."""
    hourly = ",".join(f"precipitation_previous_day{k}" for k in LEADS)
    url = (f"{PREVIOUS_RUNS}?latitude={lat:.4f}&longitude={lon:.4f}&hourly={hourly}"
           f"&models={model}&start_date={start}&end_date={end}&timezone=GMT")
    cache = CACHE_DIR / f"{model}_{lat:.4f}_{lon:.4f}_{start}_{end}.json" if CACHE_DIR else None
    if cache and cache.exists():
        h = json.loads(cache.read_text())
    else:
        for attempt in range(3):
            try:
                time.sleep(REQUEST_PAUSE_S)
                with urllib.request.urlopen(url, timeout=120) as resp:
                    h = json.load(resp)["hourly"]
                break
            except urllib.error.HTTPError as exc:
                if exc.code == 429:
                    # Open-Meteo's free quota is shared with the live app's
                    # forecasts on this machine: stop, never retry into it.
                    raise SystemExit(f"Open-Meteo rate limit reached: {exc.read()[:200]!r}") from exc
                time.sleep(10 * (attempt + 1))
            except Exception:
                time.sleep(10 * (attempt + 1))
        else:
            return {}
        if cache:
            cache.write_text(json.dumps(h))
    out: dict[int, dict[str, float]] = {}
    for k in LEADS:
        sums: dict[str, list[float]] = {}
        for t, v in zip(h["time"], h[f"precipitation_previous_day{k}"]):
            if v is not None:
                sums.setdefault(t[:10], []).append(v)
        out[k] = {d: sum(vs) for d, vs in sums.items() if len(vs) == 24}
    return out


def _scores(fc: list[float], ob: list[float], clim: list[float]) -> dict:
    f, o, c = np.array(fc), np.array(ob), np.array(clim)
    fr, orain = f >= RAIN_DAY_MM, o >= RAIN_DAY_MM
    a = int(np.sum(fr & orain)); b = int(np.sum(fr & ~orain))
    cc = int(np.sum(~fr & orain)); d = int(np.sum(~fr & ~orain))
    n = a + b + cc + d
    expected = ((a + b) * (a + cc) + (cc + d) * (b + d)) / n
    hss = (a + d - expected) / (n - expected) if n != expected else None
    mae, mae_clim = float(np.mean(np.abs(f - o))), float(np.mean(np.abs(c - o)))
    return {
        "n": n,
        "rain_day_hit_rate": round(a / (a + cc), 3) if a + cc else None,
        "false_alarm_ratio": round(b / (a + b), 3) if a + b else None,
        "percent_correct": round((a + d) / n, 3),
        "heidke_skill": None if hss is None else round(hss, 3),
        "mae_mm": round(mae, 2),
        "bias_mm": round(float(np.mean(f - o)), 2),
        "correlation": round(float(np.corrcoef(f, o)[0, 1]), 3),
        "climatology_mae_mm": round(mae_clim, 2),
        "mae_skill_vs_climatology": round(1 - mae / mae_clim, 3),
        "climatology_correlation": round(float(np.corrcoef(c, o)[0, 1]), 3),
    }


def _calibrated(f: np.ndarray, o: np.ndarray, c: np.ndarray) -> dict:
    """Two-fold cross-validated linear calibration o ~ a + b*f (fit on one half
    of the issue dates, scored on the other), against climatology on the same days."""
    half = len(f) // 2
    preds = np.empty_like(f)
    for fit, test in ((slice(0, half), slice(half, None)), (slice(half, None), slice(0, half))):
        b, a = np.polyfit(f[fit], o[fit], 1)
        preds[test] = np.maximum(0.0, a + b * f[test])
    mae, mae_c = float(np.mean(np.abs(preds - o))), float(np.mean(np.abs(c - o)))
    return {"calibrated_mae_mm": round(mae, 1),
            "calibrated_skill_vs_climatology": round(1 - mae / mae_c, 3),
            "calibrated_within_25pct_or_5mm": round(float(np.mean(np.abs(preds - o) <= np.maximum(5, 0.25 * o))), 3),
            "climatology_within_25pct_or_5mm": round(float(np.mean(np.abs(c - o) <= np.maximum(5, 0.25 * o))), 3)}


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--start", required=True)
    parser.add_argument("--end", required=True)
    parser.add_argument("--out", required=True)
    parser.add_argument("--cache-dir")
    parser.add_argument("--every", type=int, default=1,
                        help="use every Nth district (alphabetical) to spend less API quota")
    args = parser.parse_args()
    global CACHE_DIR
    if args.cache_dir:
        CACHE_DIR = Path(args.cache_dir)
        CACHE_DIR.mkdir(parents=True, exist_ok=True)
    start, end = date.fromisoformat(args.start), date.fromisoformat(args.end)
    days = [start + timedelta(days=i) for i in range((end - start).days + 1)]

    districts = asyncio.run(_districts())
    districts = {k: districts[k] for k in sorted(districts)[::args.every]}
    normals = json.loads(NORMALS.read_text())["districts"]

    with ThreadPoolExecutor(max_workers=8) as pool:
        truth_list = list(pool.map(lambda d: _chirps_day(d, districts), days))
    truth = {d.isoformat(): t for d, t in zip(days, truth_list) if t is not None}
    print(f"CHIRPS days with data: {len(truth)} of {len(days)} "
          f"(last {max(truth) if truth else None})")

    jobs = [(name, model) for name in districts for model in MODELS]
    fc = {j: _forecasts(districts[j[0]]["lat"], districts[j[0]]["lon"], j[1], start, end) for j in jobs}

    def clim(name: str, day: str) -> float:
        d = date.fromisoformat(day)
        return normals[name]["monthly"][str(d.month)]["mean"] / calendar.monthrange(d.year, d.month)[1]

    results: dict = {"period": f"{start}..{max(truth)}", "truth": "CHIRPS v2.0 daily, district polygon mean",
                     "districts": sorted(districts), "rain_day_mm": RAIN_DAY_MM,
                     "daily": {}, "weekly_total": {}, "coverage": {}}
    for model in MODELS + ["multi_model_mean"]:
        results["daily"][model] = {}
        for k in LEADS:
            f, o, c = [], [], []
            for name in districts:
                if model == "multi_model_mean":
                    per = [fc[(name, m)].get(k, {}) for m in MODELS]
                    dates = set.intersection(*(set(p) for p in per)) if all(per) else set()
                    series = {d: sum(p[d] for p in per) / len(per) for d in dates}
                else:
                    series = fc[(name, model)].get(k, {})
                for day, value in series.items():
                    if day in truth and not math.isnan(truth[day][name]):
                        f.append(value); o.append(truth[day][name]); c.append(clim(name, day))
            results["daily"][model][k] = _scores(f, o, c) if f else None
        # 7-day totals from a single run: issue date I, target days I+1..I+7 (lead k = day I+k)
        f, o, c = [], [], []
        for name in districts:
            per_lead = ([{d: sum(fc[(name, m)].get(k, {}).get(d, math.nan) for m in MODELS) / len(MODELS)
                          for d in truth} for k in LEADS] if model == "multi_model_mean"
                        else [fc[(name, model)].get(k, {}) for k in LEADS])
            for i in range(len(days) - 7):
                targets = [(days[i] + timedelta(days=k)).isoformat() for k in LEADS]
                vals = [per_lead[k - 1].get(t, math.nan) for k, t in zip(LEADS, targets)]
                obs = [truth[t][name] if t in truth else math.nan for t in targets]
                if any(math.isnan(v) for v in vals + obs):
                    continue
                f.append(sum(vals)); o.append(sum(obs)); c.append(sum(clim(name, t) for t in targets))
        if f:
            fa, oa, ca = np.array(f), np.array(o), np.array(c)
            mae, mae_c = float(np.mean(np.abs(fa - oa))), float(np.mean(np.abs(ca - oa)))
            results["weekly_total"][model] = {
                "n": len(f), "mae_mm": round(mae, 1), "bias_mm": round(float(np.mean(fa - oa)), 1),
                "correlation": round(float(np.corrcoef(fa, oa)[0, 1]), 3),
                "climatology_mae_mm": round(mae_c, 1),
                "mae_skill_vs_climatology": round(1 - mae / mae_c, 3),
                "within_25pct_or_5mm": round(float(np.mean(np.abs(fa - oa) <= np.maximum(5, 0.25 * oa))), 3),
                "climatology_correlation": round(float(np.corrcoef(ca, oa)[0, 1]), 3),
                **_calibrated(fa, oa, ca),
            }
        results["coverage"][model] = {k: (results["daily"][model][k] or {}).get("n") for k in LEADS}
    Path(args.out).write_text(json.dumps(results, indent=1))
    print(f"wrote {args.out}")


if __name__ == "__main__":
    main()
