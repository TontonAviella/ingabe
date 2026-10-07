"""How good is the NDVI z-score the insurance engine predicted from Sentinel-1 radar?

When anomaly_alerts_cache had no NDVI z-score for a district (always, since
the Dagster NDVI pipeline went stale), insurance_engine fell back on
sar_ndvi.SARNDVIPredictor: a GradientBoosting model trained once per process
on the first area anybody asked about, whose prediction became a z-score
against the constants 0.45 and 0.15. This script measures that number
against real optical data for the same places and dates:

- truth NDVI: Sentinel-2 L2A over the same box, clouds masked with the scene
  classification band (SCL), read from Digital Earth Africa (deafrica_stac);
  a date counts when at least 80% of the box is clear;
- truth z-score: Digital Earth Africa's monthly NDVI standardised anomaly
  (`ndvi_anomaly`: Landsat + Sentinel-2 against the 1984-2020 Landsat NDVI
  climatology `ndvi_climatology_ls`), averaged over the box;
- the training labels the predictor used (STACService.compute_admin_ndvi:
  Earth Search, no cloud mask) against the masked NDVI of the same date.

The predictor runs as the app ran it (train_model, predict_ndvi, and the
engine's z-score formula); only its remote reads are kept on disk
(--cache-dir), so a rerun re-analyses without downloading again. Places:
Roger's two field cells plus one cell per farming zone; each box is the
engine's: the polygon's vertex mean +/- 0.05 degrees.

    PYTHONPATH=. python -u scripts/sar_ndvi_skill.py --cache-dir /tmp/sar_ndvi_cache \
        --out docs/evidence/sar_ndvi_skill.json

The predictor (src/services/sar_ndvi.py) and Sage's predict_ndvi_from_sar were
deleted after this measurement; run this script from the parent of the commit
that deleted them (git log --diff-filter=D -- src/services/sar_ndvi.py).
"""

from __future__ import annotations

import argparse
import asyncio
import calendar
import hashlib
import json
import os
import pickle
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, Callable

import asyncpg
import numpy as np

os.environ.setdefault("AWS_NO_SIGN_REQUEST", "YES")
os.environ.setdefault("AWS_DEFAULT_REGION", "af-south-1")

PLACES = [  # (cell, district)
    ("Kanyangese", "Gatsibo"),  # Cyampirita field
    ("Nyarubuye", "Kamonyi"),  # Kabarama field
    ("Kamate", "Nyagatare"),  # dry east lowlands (Karangazi)
    ("Kayumba", "Bugesera"),  # Bugesera lowlands (Nyamata)
    ("Kabarore", "Gatsibo"),  # eastern savanna
    ("Bukomeye", "Huye"),  # central plateau
    ("Gisesero", "Musanze"),  # volcanic highlands (Busogo)
    ("Pera", "Rusizi"),  # Bugarama rice valley
    ("Butunzi", "Rulindo"),  # Kinihira tea hills
    ("Mujuga", "Nyamagabe"),  # Kitabi highland tea
]
BOX_HALF_DEG = 0.05  # the box insurance_engine._sar_predicted_ndvi_z read around a centre
ENGINE_Z_MEAN, ENGINE_Z_STD = 0.45, 0.15  # the constants it turned predicted NDVI into a z-score with
TRIGGER_Z = -1.5  # the default ndvi_z_score trigger (insurance_engine._default_triggers)
EMPIRICAL = "empirical VH/VV mapping"  # what predict_ndvi returns when its model could not be trained
CLEAR_FRACTION = 0.8  # a Sentinel-2 date counts as truth when this share of the box is cloud-free
MAX_DATES = 8  # clear dates tested per place (each costs one 30-day radar read, ~25 s)
DEA_STAC = "https://explorer.digitalearth.africa/stac/collections/{c}/items"
CACHE_DIR = Path("/tmp/sar_ndvi_cache")


# --- remote reads, kept on disk ------------------------------------------------

def _disk(name: str, key: Any, compute: Callable[[], Any]) -> Any:
    path = CACHE_DIR / f"{name}-{hashlib.sha1(repr(key).encode()).hexdigest()}.pkl"
    if path.exists():
        return pickle.loads(path.read_bytes())
    value = compute()
    tmp = path.with_suffix(f".{threading.get_ident()}.tmp")
    tmp.write_bytes(pickle.dumps(value))
    tmp.replace(path)
    return value


def _keep_predictor_reads_on_disk() -> None:
    """Wrap the two remote reads SARNDVIPredictor makes, so the real code runs once per input."""
    from src.services.sentinel1_service import Sentinel1Service
    from src.services.stac_service import STACService

    s1_read = Sentinel1Service.get_time_series
    s2_read = STACService.compute_admin_ndvi

    def get_time_series(self, bbox, date_range, limit=20):
        return _disk("s1ts", (tuple(bbox), date_range, limit), lambda: s1_read(self, bbox, date_range, limit=limit))

    def compute_admin_ndvi(self, bbox, days=90, max_cloud_cover=50.0, max_scenes=12):
        key = (self.catalog_name, tuple(bbox), days, max_cloud_cover, max_scenes, date.today().isoformat())
        return _disk("s2label", key, lambda: s2_read(
            self, bbox, days=days, max_cloud_cover=max_cloud_cover, max_scenes=max_scenes))

    Sentinel1Service.get_time_series = get_time_series  # type: ignore[method-assign]
    STACService.compute_admin_ndvi = compute_admin_ndvi  # type: ignore[method-assign]


def _optical_by_date(box: tuple, date_from: str, date_to: str) -> dict[str, dict]:
    """Cloud-masked Sentinel-2 NDVI of the box per date (tiles of one date merged by pixel count)."""
    from src.services.deafrica_stac import get_deafrica_service

    w, s, e, n = box
    polygon = {"type": "Polygon", "coordinates": [[[w, s], [e, s], [e, n], [w, n], [w, s]]]}
    # 30 days at a time: its item search stops at 50 scenes, half a season on a box two tiles cover.
    intervals = []
    start, last = date.fromisoformat(date_from), date.fromisoformat(date_to)
    while start <= last:
        end = min(last, start + timedelta(days=29))
        res = _disk("dea_ndvi", (box, start.isoformat(), end.isoformat()),
                    lambda: get_deafrica_service().get_field_stats(  # noqa: B023 (called at once)
                        geometry=polygon, date_from=start.isoformat(), date_to=end.isoformat(),
                        index="ndvi", max_cloud=80.0))
        if res.get("scenes_found", 0) >= 45:
            print(f"warning: {res['scenes_found']} scenes for {box} {start}..{end}: the search may be cut", flush=True)
        intervals += res.get("intervals", [])
        start = end + timedelta(days=1)
    box_pixels = 0
    by_date: dict[str, dict] = {}
    for iv in intervals:
        st = iv["ndvi"]
        valid = st.get("valid_pixels", 0)
        box_pixels = max(box_pixels, valid + st.get("no_data_pixels", 0))
        acc = by_date.setdefault(iv["date_from"][:10], {"weighted": 0.0, "valid": 0})
        if valid:  # a scene with no clear pixel reports mean 0.0: that is not a value
            acc["weighted"] += st["mean"] * valid
            acc["valid"] += valid
    out = {}
    for d, acc in by_date.items():
        out[d] = {
            "ndvi": round(acc["weighted"] / acc["valid"], 4) if acc["valid"] else None,
            # Overlapping tiles of one date can count a pixel twice; cap at the whole box.
            "clear_fraction": round(min(1.0, acc["valid"] / box_pixels), 3) if box_pixels else 0.0,
        }
    return out


def _dea_items(collection: str, box: tuple, datetime_range: str | None) -> list[dict]:
    import httpx

    params = {"bbox": ",".join(str(v) for v in box), "limit": "100"}
    if datetime_range:
        params["datetime"] = datetime_range

    def fetch() -> list[dict]:
        r = httpx.get(DEA_STAC.format(c=collection), params=params, timeout=60.0, follow_redirects=True,
                      headers={"Accept": "application/json", "User-Agent": "curl/8"})
        r.raise_for_status()
        return r.json().get("features", [])
    return _disk("dea_items", (collection, box, datetime_range), fetch)


def _box_pixels(href: str, box: tuple) -> np.ndarray:
    """All pixels of one DE Africa product tile inside the box (empty when they do not meet)."""
    import rasterio
    from rasterio.warp import transform_bounds
    from rasterio.windows import Window, from_bounds

    def read() -> np.ndarray:
        with rasterio.open(href) as src:
            win = from_bounds(*transform_bounds("EPSG:4326", src.crs, *box), transform=src.transform)
            win = win.intersection(Window(0, 0, src.width, src.height))
            return src.read(1, window=win).astype("float64").ravel()
    try:
        return _disk("dea_px", (href, box), read)
    except Exception:  # the window misses this tile
        return np.array([])


def _monthly_anomaly(box: tuple, months: list[str]) -> dict[str, dict]:
    """Digital Earth Africa NDVI standardised anomaly per month, averaged over the box's clear pixels."""
    items = _dea_items("ndvi_anomaly", box, f"{months[0]}-01T00:00:00Z/{months[-1]}-28T23:59:59Z")
    out = {}
    for month in months:
        z_px, ndvi_px, n_px = [], [], 0
        for it in items:
            if it["properties"]["datetime"][:7] != month:
                continue
            z = _box_pixels(it["assets"]["ndvi_std_anomaly"]["href"], box)
            nd = _box_pixels(it["assets"]["ndvi_mean"]["href"], box)
            cc = _box_pixels(it["assets"]["clear_count"]["href"], box)
            ok = np.isfinite(z) & np.isfinite(nd) & (cc > 0)
            z_px.append(z[ok])
            ndvi_px.append(nd[ok])
            n_px += z.size
        z_all = np.concatenate(z_px) if z_px else np.array([])
        nd_all = np.concatenate(ndvi_px) if ndvi_px else np.array([])
        out[month] = {
            "z": round(float(z_all.mean()), 3) if z_all.size else None,
            "ndvi": round(float(nd_all.mean()), 4) if nd_all.size else None,
            "share_below_trigger": round(float((z_all < TRIGGER_Z).mean()), 3) if z_all.size else None,
            "clear_fraction": round(z_all.size / n_px, 3) if n_px else 0.0,
        }
    return out


def _climatology(box: tuple, months: list[str]) -> dict[str, dict]:
    """Digital Earth Africa 1984-2020 Landsat NDVI mean and per-pixel std for each month, over the box."""
    items = _dea_items("ndvi_climatology_ls", box, None)
    out = {}
    for month in months:
        mon = calendar.month_abbr[int(month[5:7])].lower()
        mean_px, std_px = [], []
        for it in items:
            m = _box_pixels(it["assets"][f"mean_{mon}"]["href"], box)
            sd = _box_pixels(it["assets"][f"stddev_{mon}"]["href"], box)
            cnt = _box_pixels(it["assets"][f"count_{mon}"]["href"], box)
            ok = np.isfinite(m) & np.isfinite(sd) & (cnt > 0)
            mean_px.append(m[ok])
            std_px.append(sd[ok])
        m_all = np.concatenate(mean_px) if mean_px else np.array([])
        sd_all = np.concatenate(std_px) if std_px else np.array([])
        out[month] = {
            "mean": round(float(m_all.mean()), 4) if m_all.size else None,
            "pixel_std": round(float(sd_all.mean()), 4) if sd_all.size else None,
        }
    return out


def _s1_scene_count(box: tuple, target: date) -> int:
    """Sentinel-1 RTC scenes over the box in the 30 days predict_ndvi reads (it asks for 10, oldest first)."""
    from src.services.sentinel1_service import _search_items  # the search predict_ndvi's read uses

    date_range = f"{(target - timedelta(days=30)).isoformat()}/{target.isoformat()}"
    return len(_disk("s1count", (box, date_range), lambda: _search_items(box, date_range, limit=100)))


# --- places ------------------------------------------------------------------

async def _places() -> list[dict]:
    from src.services.insurance_engine import _centroid_from_geojson  # the centre the engine read around

    conn = await asyncpg.connect(
        host=os.environ.get("POSTGRES_HOST", "postgresdb"), port=int(os.environ.get("POSTGRES_PORT", "5432")),
        user=os.environ["POSTGRES_USER"], password=os.environ["POSTGRES_PASSWORD"],
        database=os.environ.get("POSTGRES_DB", "mundidb"),
    )
    try:
        out = []
        for cell, district in PLACES:
            row = await conn.fetchrow(
                "SELECT ST_AsGeoJSON(geom) AS g FROM rwanda_cell_boundaries "
                "WHERE lower(cell_name) = lower($1) AND lower(district_name) = lower($2) LIMIT 1",
                cell, district,
            )
            lat, lon = _centroid_from_geojson(json.loads(row["g"]))
            out.append({
                "name": f"{cell} ({district})", "lat": round(lat, 5), "lon": round(lon, 5),
                "box": (lon - BOX_HALF_DEG, lat - BOX_HALF_DEG, lon + BOX_HALF_DEG, lat + BOX_HALF_DEG),
            })
        return out
    finally:
        await conn.close()


# --- the measurement -----------------------------------------------------------

def _last_full_months(today: date, n: int) -> list[str]:
    """The n calendar months before today's, oldest first ("YYYY-MM")."""
    y, m, out = today.year, today.month, []
    for _ in range(n):
        y, m = (y, m - 1) if m > 1 else (y - 1, 12)
        out.append(f"{y}-{m:02d}")
    return out[::-1]


def _engine_z(predicted: float | None) -> float | None:
    """The z-score insurance_engine._sar_predicted_ndvi_z returned for a predicted NDVI."""
    return None if predicted is None else (predicted - ENGINE_Z_MEAN) / ENGINE_Z_STD


def _train(place: dict) -> dict:
    from src.services.sar_ndvi import SARNDVIPredictor, _generate_training_data

    predictor = SARNDVIPredictor()
    result = predictor.train_model(place["box"])
    X, y = _generate_training_data(place["box"])  # the same pairs train_model fitted, for the label check
    return {"predictor": predictor, "train": result, "labels": [] if y is None else [float(v) for v in y]}


def _label_rows(place: dict, today: date) -> list[dict]:
    """Each training label (Earth Search, unmasked) next to the masked NDVI of the box on that date."""
    from src.services.stac_service import get_stac_service

    labels = get_stac_service("earth_search").compute_admin_ndvi(
        bbox=list(place["box"]), days=180, max_cloud_cover=30.0, max_scenes=20)  # as _generate_training_data
    truth = _optical_by_date(place["box"], (today - timedelta(days=180)).isoformat(), today.isoformat())
    rows = []
    for obs in labels.get("observations", []):
        d = (obs.get("datetime") or "")[:10]
        t = truth.get(d, {})
        rows.append({"date": d, "item": obs.get("source_item_id"), "label": obs.get("mean_ndvi"),
                     "scene_cloud": obs.get("cloud_cover"), "masked_ndvi": t.get("ndvi"),
                     "clear_fraction": t.get("clear_fraction")})
    return rows


def _predict_all(trained: dict, place: dict, target: date) -> tuple[dict, dict]:
    """NDVI predicted for the box on `target` by each first-asked place's model, plus the VH/VV
    mapping predict_ndvi falls back on when training fails. Returns (predictions, radar series)."""
    from src.services.sar_ndvi import SARNDVIPredictor
    from src.services.sentinel1_service import get_sentinel1_service

    def short(r: dict) -> dict:
        return {"status": r.get("status"), "method": r.get("method"), "ndvi": r.get("predicted_ndvi"),
                "sar_dates_used": r.get("sar_dates_used")}

    preds = {first: short(t["predictor"].predict_ndvi(place["box"], target_date=target.isoformat()))
             for first, t in trained.items()}
    # The same read predict_ndvi made (kept on disk, so this costs nothing).
    ts = get_sentinel1_service().get_time_series(
        place["box"], f"{(target - timedelta(days=30)).isoformat()}/{target.isoformat()}", limit=10)
    if ts.get("status") == "success" and len(ts["dates"]) >= 2:
        preds[EMPIRICAL] = short(SARNDVIPredictor()._empirical_prediction(ts, place["box"]))
    return preds, ts


def _clean_label_models(samples: list[dict]) -> dict:
    """Could the predictor work with clean labels? Same features, labels from the masked optical NDVI."""
    from sklearn.ensemble import GradientBoostingRegressor

    from src.services.sar_ndvi import _GBR_PARAMS

    def fit_predict(train: list[dict], test: list[dict]) -> list[float]:
        model = GradientBoostingRegressor(**_GBR_PARAMS)
        model.fit(np.array([s["x"] for s in train]), np.array([s["truth"] for s in train]))
        return [float(v) for v in model.predict(np.array([s["x"] for s in test]))]

    pooled, per_place = [], []
    names = sorted({s["place"] for s in samples})
    for name in names:
        test = [s for s in samples if s["place"] == name]
        train = [s for s in samples if s["place"] != name]
        mean_other = float(np.mean([s["truth"] for s in train]))
        for s, p in zip(test, fit_predict(train, test)):
            pooled.append({"place": name, "date": s["date"], "truth": s["truth"], "pred": p, "baseline": mean_other})
        if len(test) >= 6:  # leave one date out inside the place
            for i, s in enumerate(test):
                rest = test[:i] + test[i + 1:]
                p = fit_predict(rest, [s])[0]
                per_place.append({"place": name, "date": s["date"], "truth": s["truth"], "pred": p,
                                  "baseline": float(np.mean([r["truth"] for r in rest]))})
    return {"leave_one_place_out": pooled, "leave_one_date_out": per_place}


def run(today: date, workers: int, places_file: str | None) -> dict:
    from src.services.sar_ndvi import _extract_features

    _keep_predictor_reads_on_disk()
    if places_file:  # centres found earlier (--write-places), for a run without the database
        places = [{**p, "box": tuple(p["box"])} for p in json.loads(Path(places_file).read_text())]
    else:
        places = asyncio.run(_places())
    months = _last_full_months(today, 6)
    print("places:", [p["name"] for p in places], "months:", months, flush=True)

    month_ends = [min(today, date(int(m[:4]), int(m[5:]), calendar.monthrange(int(m[:4]), int(m[5:]))[1]))
                  for m in months]

    # Digital Earth Africa reads (its own host) run `workers` places at a time, alongside the
    # predictor's reads. Those go one place at a time, as the app trains one model at a time: four
    # places at once (32 Planetary Computer connections) timed out on connect.
    with ThreadPoolExecutor(max_workers=workers) as pool:
        truth_f = {p["name"]: pool.submit(
            _optical_by_date, p["box"], (today - timedelta(days=180)).isoformat(), today.isoformat())
            for p in places}
        anomaly_f = {p["name"]: pool.submit(_monthly_anomaly, p["box"], months) for p in places}
        clim_f = {p["name"]: pool.submit(_climatology, p["box"], months) for p in places}
        all_trained = {}
        for p in places:
            all_trained[p["name"]] = _train(p)
            print("trained", p["name"], all_trained[p["name"]]["train"], flush=True)
        # A predictor whose training failed trains again on the next box it is asked about: it has no
        # fixed model to measure. Its first answer is the VH/VV mapping, measured as EMPIRICAL below.
        trained = {k: v for k, v in all_trained.items() if v["train"].get("status") == "success"}

        # Month ends and today need no optical truth: predicted while it is read.
        month_preds = {p["name"]: [_predict_all(trained, p, end)[0] for end in month_ends] for p in places}
        print("month-end predictions done", flush=True)
        today_rows = []
        for place in places:
            # What insurance_engine._sar_predicted_ndvi_z returned (removed 2026-10-07): the shared
            # predictor's NDVI for the box around the centre, for today, through the constants.
            zs = {}
            for first, t in trained.items():
                r = t["predictor"].predict_ndvi(place["box"])
                zs[first] = _engine_z(r.get("predicted_ndvi")) if r.get("status") == "success" else None
            today_rows.append({"place": place["name"], "engine_z_by_first_place": zs,
                               "s1_scenes_in_window": _s1_scene_count(place["box"], today)})
        print("today done", flush=True)

        truth = {k: f.result() for k, f in truth_f.items()}
        anomaly = {k: f.result() for k, f in anomaly_f.items()}
        clim = {k: f.result() for k, f in clim_f.items()}
    labels = {p["name"]: _label_rows(p, today) for p in places}  # both of its reads are on disk by now
    for row in today_rows:
        row["latest_dea_anomaly"] = anomaly[row["place"]][months[-1]]
    print("optical, anomaly and climatology read", flush=True)

    # Clear dates with a full 30-day radar window inside the period, at most MAX_DATES spread evenly.
    targets = {}
    for p in places:
        clear = sorted(d for d, t in truth[p["name"]].items()
                       if t["ndvi"] is not None and t["clear_fraction"] >= CLEAR_FRACTION
                       and date.fromisoformat(d) >= today - timedelta(days=150))
        if len(clear) > MAX_DATES:
            clear = [clear[round(i * (len(clear) - 1) / (MAX_DATES - 1))] for i in range(MAX_DATES)]
        targets[p["name"]] = clear

    def place_predictions(place: dict) -> dict:
        rows, samples = [], []
        for d in targets[place["name"]]:
            target = date.fromisoformat(d)
            preds, ts = _predict_all(trained, place, target)
            x = None if ts.get("status") != "success" or len(ts["dates"]) < 2 else _extract_features(
                ts["dates"], ts["vv_means"], ts["vh_means"], ts["vv_stds"], ts["vh_stds"])
            rows.append({"date": d, "truth": truth[place["name"]][d]["ndvi"],
                         "s1_scenes_in_window": _s1_scene_count(place["box"], target), "predictions": preds})
            if x is not None:
                samples.append({"place": place["name"], "date": d, "x": [float(v) for v in x],
                                "truth": truth[place["name"]][d]["ndvi"]})
        monthly = [{"month": m, "target": end.isoformat(), "predictions": preds,
                    "dea_anomaly": anomaly[place["name"]][m], "climatology": clim[place["name"]][m]}
                   for m, end, preds in zip(months, month_ends, month_preds[place["name"]])]
        return {"rows": rows, "monthly": monthly, "samples": samples}

    per_place = {}
    for p in places:
        per_place[p["name"]] = place_predictions(p)
        print("predicted", p["name"], flush=True)

    samples = [s for v in per_place.values() for s in v["samples"]]
    return {
        "today": today.isoformat(), "months": months, "places": places,
        "training": {k: {"result": v["train"], "labels": v["labels"]} for k, v in all_trained.items()},
        "labels": labels, "per_place": {k: {"rows": v["rows"], "monthly": v["monthly"]} for k, v in per_place.items()},
        "today_rows": today_rows, "clean_label_models": _clean_label_models(samples),
        "n_clean_samples": len(samples),
    }


# --- summary -------------------------------------------------------------------

def _stats(pred: list[float], truth: list[float]) -> dict:
    p, t = np.array(pred, dtype=float), np.array(truth, dtype=float)
    if p.size < 2:
        return {"n": int(p.size)}
    return {"n": int(p.size), "mae": round(float(np.abs(p - t).mean()), 3), "bias": round(float((p - t).mean()), 3),
            "r": round(float(np.corrcoef(p, t)[0, 1]), 2) if p.std() > 0 and t.std() > 0 else None}


def summarise(res: dict) -> dict:
    out: dict[str, Any] = {}

    # 1. Training labels against the masked NDVI of the same date.
    lab = [r for rows in res["labels"].values() for r in rows if r["label"] is not None]
    matched = [r for r in lab if r["masked_ndvi"] is not None]
    by_class = {}
    for name, lo, hi in (("clear >=80%", 0.8, 1.01), ("partly 20-80%", 0.2, 0.8), ("cloudy <20%", 0.0, 0.2)):
        rows = [r for r in matched if lo <= r["clear_fraction"] < hi]
        by_class[name] = {**_stats([r["label"] for r in rows], [r["masked_ndvi"] for r in rows]),
                          "share": round(len(rows) / len(lab), 2) if lab else None}
    out["labels"] = {"n_labels": len(lab), "matched": len(matched), "no_clear_pixel_that_date": len(lab) - len(matched),
                     "by_clear_fraction": by_class}

    # 2. Predicted NDVI against clear-sky truth, for each possible first-asked place.
    cross, same, emp, spread, const = [], [], [], [], []
    methods: dict[str, int] = {}
    for q, v in res["per_place"].items():
        for row in v["rows"]:
            vals = []
            for first, p in row["predictions"].items():
                methods[p["method"] or p["status"]] = methods.get(p["method"] or p["status"], 0) + 1
                if p["ndvi"] is None:
                    continue
                if first == EMPIRICAL:
                    emp.append((p["ndvi"], row["truth"]))
                    continue
                vals.append(p["ndvi"])
                (same if first == q else cross).append((p["ndvi"], row["truth"]))
            if len(vals) > 1:
                spread.append(max(vals) - min(vals))
            const.append((ENGINE_Z_MEAN, row["truth"]))
    out["prediction"] = {
        "other_place_model": _stats([a for a, _ in cross], [b for _, b in cross]),
        "own_place_model_in_sample": _stats([a for a, _ in same], [b for _, b in same]),
        "empirical_vh_vv_mapping": _stats([a for a, _ in emp], [b for _, b in emp]),
        "constant_0.45": _stats([a for a, _ in const], [b for _, b in const]),
        "median_spread_by_first_place": round(float(np.median(spread)), 3) if spread else None,
        "methods": methods,
    }

    # 3. z-scores: the engine's against Digital Earth Africa's anomaly, per place and month.
    pairs, fires, clim_rows = [], {"both": 0, "sar_only": 0, "dea_only": 0, "neither": 0}, []
    for q, v in res["per_place"].items():
        for m in v["monthly"]:
            dz = m["dea_anomaly"]["z"]
            clim_rows.append(m["climatology"])
            for first, p in m["predictions"].items():
                if first in (q, EMPIRICAL):  # own place is in-sample; the mapping is reported apart
                    continue
                sz = _engine_z(p["ndvi"])
                if sz is None or dz is None:
                    continue
                pairs.append((sz, dz))
                key = ("both" if dz < TRIGGER_Z else "sar_only") if sz < TRIGGER_Z else (
                    "dea_only" if dz < TRIGGER_Z else "neither")
                fires[key] += 1
    out["z"] = {**_stats([a for a, _ in pairs], [b for _, b in pairs]), "trigger_below_-1.5": fires,
                "climatology_mean_range": [min(c["mean"] for c in clim_rows if c["mean"] is not None),
                                           max(c["mean"] for c in clim_rows if c["mean"] is not None)],
                "climatology_pixel_std_range": [min(c["pixel_std"] for c in clim_rows if c["pixel_std"] is not None),
                                                max(c["pixel_std"] for c in clim_rows if c["pixel_std"] is not None)]}

    # Does a model follow the changes inside one place? Errors and truth with each place's mean removed.
    within: dict[str, list] = {}
    for q, v in res["per_place"].items():
        for first in {f for row in v["rows"] for f in row["predictions"]} - {q, EMPIRICAL}:
            pairs_q = [(row["predictions"][first]["ndvi"], row["truth"]) for row in v["rows"]
                       if row["predictions"].get(first, {}).get("ndvi") is not None]
            if len(pairs_q) >= 3:
                mp, mt = np.mean([a for a, _ in pairs_q]), np.mean([b for _, b in pairs_q])
                within.setdefault("pred", []).extend(a - mp for a, _ in pairs_q)
                within.setdefault("truth", []).extend(b - mt for _, b in pairs_q)
    out["prediction"]["other_place_model_within_place_r"] = _stats(within.get("pred", []), within.get("truth", [])).get("r")

    # Per place: what each model learnt from, what it can say, and what a report said today.
    first_model = next(iter(res["training"]))
    table = []
    for place in res["places"]:
        q = place["name"]
        lab = [r for r in res["labels"][q] if r["label"] is not None]
        own_preds = [row["predictions"][q]["ndvi"] for v in res["per_place"].values() for row in v["rows"]
                     if row["predictions"].get(q, {}).get("ndvi") is not None]
        own_preds += [m["predictions"][q]["ndvi"] for v in res["per_place"].values() for m in v["monthly"]
                      if m["predictions"].get(q, {}).get("ndvi") is not None]
        today = next(r for r in res["today_rows"] if r["place"] == q)
        zs = [z for f, z in today["engine_z_by_first_place"].items() if z is not None and f != q]
        last = res["per_place"][q]["monthly"][-1]
        table.append({
            "place": q,
            "labels": len(lab), "label_dates": f"{min(r['date'] for r in lab)}..{max(r['date'] for r in lab)}" if lab else None,
            "label_range": [min(r["label"] for r in lab), max(r["label"] for r in lab)] if lab else None,
            "partly_cloudy_labels": sum(1 for r in lab if r["clear_fraction"] is not None and r["clear_fraction"] < 0.8),
            "train": res["training"][q]["result"],
            "its_predictions_range": [min(own_preds), max(own_preds)] if own_preds else None,
            "today_z_if_first_was_" + first_model: today["engine_z_by_first_place"].get(first_model),
            "today_z_other_firsts_range": [round(min(zs), 2), round(max(zs), 2)] if zs else None,
            "dea_z_" + last["month"]: last["dea_anomaly"]["z"],
            "climatology_" + last["month"]: last["climatology"],
            "s1_scenes_30d_today": today["s1_scenes_in_window"],
        })
    out["places"] = table
    counts = [row["s1_scenes_in_window"] for v in res["per_place"].values() for row in v["rows"]]
    out["s1_scenes_per_30_days"] = {"median": float(np.median(counts)), "min": min(counts), "max": max(counts),
                                    "predict_ndvi_reads": 10} if counts else None
    today_pairs = [(z, r["latest_dea_anomaly"]["z"]) for r in res["today_rows"]
                   for f, z in r["engine_z_by_first_place"].items() if z is not None and f != r["place"]]
    out["today"] = {"engine_z_below_-1.5": sum(1 for z, _ in today_pairs if z < TRIGGER_Z),
                    "engine_z_at_or_below_-0.5 (farmer: stressed)": sum(1 for z, _ in today_pairs if z <= -0.5),
                    "pairs": len(today_pairs),
                    "dea_latest_z_below_-1.5_places": sum(1 for r in res["today_rows"]
                                                          if (r["latest_dea_anomaly"]["z"] or 0) < TRIGGER_Z)}

    # 4. Clean labels: does the radar carry the NDVI at all?
    clean = res["clean_label_models"]
    for k in ("leave_one_place_out", "leave_one_date_out"):
        rows = clean[k]
        anom_p = [r["pred"] - r["baseline"] for r in rows]
        anom_t = [r["truth"] - r["baseline"] for r in rows]
        # As z-scores against the place's own 1984-2020 climatology for the month: what a fixed fallback
        # would report, next to the same z-score from the optical NDVI of that date.
        clim = {(q, m["month"]): m["climatology"] for q, v in res["per_place"].items() for m in v["monthly"]}
        zp, zt = [], []
        for r in rows:
            c = clim.get((r["place"], r["date"][:7]))
            if c and c["mean"] is not None and c["pixel_std"]:
                zp.append((r["pred"] - c["mean"]) / c["pixel_std"])
                zt.append((r["truth"] - c["mean"]) / c["pixel_std"])
        z_fires = {"both": sum(1 for a, b in zip(zp, zt) if a < TRIGGER_Z and b < TRIGGER_Z),
                   "model_only": sum(1 for a, b in zip(zp, zt) if a < TRIGGER_Z <= b),
                   "optical_only": sum(1 for a, b in zip(zp, zt) if b < TRIGGER_Z <= a)}
        out[f"clean_{k}"] = {"model": _stats([r["pred"] for r in rows], [r["truth"] for r in rows]),
                             "baseline_mean_ndvi": _stats([r["baseline"] for r in rows], [r["truth"] for r in rows]),
                             "anomaly_r": _stats(anom_p, anom_t).get("r"),
                             "z_vs_optical_z": {**_stats(zp, zt), "trigger_below_-1.5": z_fires}}
    return out


def main() -> None:
    global CACHE_DIR
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--cache-dir", default=str(CACHE_DIR))
    ap.add_argument("--out", required=True)
    ap.add_argument("--today", default=datetime.utcnow().date().isoformat(),
                    help="must be the day the cache was filled: the predictor reads up to datetime.utcnow()")
    ap.add_argument("--workers", type=int, default=4)
    ap.add_argument("--places", help="read the cells' centres and boxes from this file instead of the database")
    ap.add_argument("--write-places", help="only look the cells up in the database and write them to this file")
    ap.add_argument("--summarise-only", action="store_true", help="recompute the summary of an existing --out file")
    args = ap.parse_args()
    if args.summarise_only:
        res = json.loads(Path(args.out).read_text())
        res["summary"] = summarise(res)
        Path(args.out).write_text(json.dumps(res, indent=1, default=str))
        print(json.dumps(res["summary"], indent=1))
        return
    if args.write_places:
        Path(args.write_places).write_text(json.dumps(asyncio.run(_places()), indent=1))
        return
    CACHE_DIR = Path(args.cache_dir)
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    res = run(date.fromisoformat(args.today), args.workers, args.places)
    res["summary"] = summarise(res)
    for p in res["places"]:
        p["box"] = [round(v, 5) for v in p["box"]]
    Path(args.out).write_text(json.dumps(res, indent=1, default=str))
    print(json.dumps(res["summary"], indent=1))


if __name__ == "__main__":
    main()
