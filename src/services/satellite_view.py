"""Plants and soil from the satellite, over one drone photo.

Roger, 2026-11-22/23: when someone asks about their crops on any drone photo, Ingabe gives satellite NDVI
that covers the whole photo, and soil moisture with it as a second layer of its own, and says what the two
show together. This replaces the colour-only "Raster Context" hexagons for farm photos.

What was measured before this was written (the Cyampirita photo, 124 ha, 2026-11-23; memory note
project_satellite_ndvi_soil_check_2026_11_23):

- Sentinel-2 passes every 2 to 5 days, but clouds hide most passes in the rains: 30 of 102 passes in a year
  were clear, and the wait between clear ones was 42 days at worst. Taking, for each 10 m square, its newest
  clear look covered the whole photo on all 34 days checked, with the middle square 10 days old or less on 24.
- The scene's own cloud map misses haze. A pass it called fully clear (23 Oct) read NDVI 0.25 with half the
  usual spread and a blue band 2.5 times the usual over two thirds of the photo. HAZE_BLUE over HAZE_BLOCK
  squares flagged under 0.1% of the squares on the 22 sharpest passes of the year, and 38% and 49% on the two
  passes seen hazy by eye. One place, judged by eye: a first rule, not a tested one.
- A square seen a month before its neighbour is not the same crop moment: NDVI went 0.31 to 0.55 between
  8 Oct and 19 Nov on the same squares. So every square carries the day it was seen, and old ones are drawn
  faded.
- FAO WaPOR soil moisture (L2-RSM-D, 100 m, every 10 days) has a value on every square of Rwanda. It follows
  the wet and dry seasons (12 of 12 places) but not whether rain was above or below usual (rank correlation
  0.14 against CHIRPS, bar 0.40), and FAO replaces each file about 67 days later with a steadier one. So it
  is shown as the season's level with its dates, marked a first estimate while it is one, and never as a
  precise weekly reading.

Nothing here is drawn or worded for a screen: `view` returns measured values, `plants_squares` and
`soil_squares` the squares to draw, and `words` the plain facts cards and Sage both read.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import date, timedelta
from pathlib import Path
from typing import Any, Optional, Sequence

import numpy as np
import rasterio
import rasterio.transform
from cachetools import TTLCache
from pyproj import Transformer
from rasterio.enums import Resampling
from rasterio.errors import WindowError
from rasterio.warp import transform_bounds
from rasterio.windows import Window, from_bounds
from rasterio.windows import bounds as window_bounds
from rasterio.windows import transform as window_transform
from scipy.ndimage import median_filter

from src.services import ndvi_classes, raster_process, wapor_service
from src.services.gdal_http import GDAL_HTTP_TIMEOUTS
from src.services.stac_service import SCL_UNUSABLE, STACService

logger = logging.getLogger(__name__)

SQUARE_M = 10  # Sentinel-2's red and near-infrared pixels
# How far back passes are searched, in days, each step only when squares are still without a sharp look.
LOOKBACKS = (45, 100, 180)
HAZE_BLUE = 900.0  # blue reflectance x 10,000; sharp passes read 330-750 across the photo, hazy ground 1,080+
HAZE_BLOCK = 7  # squares on a side (70 m): haze is wide, a bright roof or bare plot is not
FADED_AFTER_DAYS = 14  # a square seen this long before the photo's main look is drawn faded
EARLIER_GAP_DAYS = 14  # a change is measured against a look at least this much older
EARLIER_SHARE = 0.5  # of the main look's squares that the earlier look must cover before change is stated
EARLIER_PASSES = 6  # older passes read at most, after the photo is covered, to find that earlier look
STEADY = 0.05  # NDVI moved by less than this: "about the same"
MAX_SQUARES = 20_000  # squares drawn; a larger photo gets wider squares
MAX_AREA_KM2 = 100.0  # a wider box is not one farm's photo, and each pass would be a large read
PASSES_AT_ONCE = raster_process.WORKERS
THREADS_IN_WORKER = 6  # files read side by side inside one raster worker
SKIP_BELOW_CLEAR = 0.01  # a pass with less clear ground than this is not read further

SOIL_LAYER = "L2-RSM-D"
SOIL_SQUARE_M = 100
SOIL_TRIES = 8  # ten-day periods looked back for the newest published file
SOIL_YEAR_STEPS = 36  # ten-day periods in the year the newest reading is placed in
# Soil moisture is drawn in number bands, lightest to darkest blue. They say nothing about "dry" or "wet":
# no cut has been tested against the ground.
SOIL_BANDS: tuple[tuple[float, str], ...] = (
    (0.0, "#eff3ff"), (45.0, "#bdd7e7"), (55.0, "#6baed6"), (65.0, "#3182bd"), (75.0, "#08519c"),
)

_READ_ENV = {
    "GDAL_HTTP_MERGE_CONSECUTIVE_RANGES": "YES",
    "GDAL_DISABLE_READDIR_ON_OPEN": "EMPTY_DIR",
    "CPL_VSIL_CURL_ALLOWED_EXTENSIONS": ".tif,.tiff",
    "GDAL_HTTP_MAX_RETRY": "3",
    "GDAL_HTTP_RETRY_DELAY": "1",
    "AWS_NO_SIGN_REQUEST": "YES",
    **GDAL_HTTP_TIMEOUTS,
}


@dataclass(frozen=True)
class Look:
    """One pass over the photo, on the photo's grid of 10 m squares."""

    day: date
    ndvi: np.ndarray  # nan where the pass holds no value
    clear: np.ndarray  # the scene's class map calls it ground, and the bands hold a value
    sharp: np.ndarray  # clear, and not hazy


@dataclass(frozen=True)
class Grid:
    crs: str
    transform: tuple[float, float, float, float, float, float]  # affine a, b, c, d, e, f of the top-left square
    shape: tuple[int, int]


@dataclass(frozen=True)
class Change:
    now: date
    before: date
    median: float
    squares: int


@dataclass(frozen=True)
class Plants:
    grid: Grid
    ndvi: np.ndarray  # each square's newest clear look; nan where no pass saw it
    seen: np.ndarray  # the day of that look as a date ordinal; 0 where no pass saw it
    hazy: np.ndarray  # squares whose only clear looks were hazy: they may read low
    change: Optional[Change]
    passes_read: int

    @property
    def covered(self) -> float:
        return float((self.seen > 0).mean())

    @property
    def main_day(self) -> date:
        """The day most squares were seen."""
        days, counts = np.unique(self.seen[self.seen > 0], return_counts=True)
        return date.fromordinal(int(days[int(np.argmax(counts))]))


@dataclass(frozen=True)
class Soil:
    period: str  # the ten-day period, FAO's code, e.g. "2026-11-D1"
    percent: np.ndarray  # relative soil moisture, 0-100; nan where the file holds none
    west: float
    north: float
    step: float  # degrees a square
    first_estimate: bool
    centre: Optional[float]  # the reading at the photo's centre
    year_low: Optional[float]  # that centre's lowest and highest reading over the year before
    year_high: Optional[float]


@dataclass(frozen=True)
class View:
    bbox: tuple[float, float, float, float]
    today: date
    plants: Optional[Plants]
    plants_missing: Optional[str]  # why there is no plants picture, in plain words
    soil: Optional[Soil]
    soil_missing: Optional[str]


# --- Plants: the newest clear look at each square ----------------------------------------------------------


def hazy(blue: np.ndarray) -> np.ndarray:
    """Squares under haze or thin cloud the scene's class map let through (module docstring)."""
    return median_filter(blue.astype(np.float32), size=HAZE_BLOCK, mode="nearest") > HAZE_BLUE


def look_from_bands(day: date, red: np.ndarray, nir: np.ndarray, blue: np.ndarray, usable: np.ndarray) -> Look:
    red, nir = red.astype(np.float32), nir.astype(np.float32)
    total = red + nir
    ndvi = np.where(total > 0, (nir - red) / np.where(total > 0, total, 1.0), np.nan).astype(np.float32)
    clear = usable & np.isfinite(ndvi)
    return Look(day, ndvi, clear, clear & ~hazy(blue))


def newest_clear(looks: Sequence[Look]) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """(ndvi, seen, hazy): for each square its newest sharp look, or its newest hazy one when it has no sharp one."""
    ordered = sorted(looks, key=lambda look: look.day, reverse=True)
    shape = ordered[0].ndvi.shape
    ndvi = np.full(shape, np.nan, dtype=np.float32)
    seen = np.zeros(shape, dtype=np.int32)
    from_haze = np.zeros(shape, dtype=bool)
    for use_hazy in (False, True):
        for look in ordered:
            take = (look.clear if use_hazy else look.sharp) & (seen == 0)
            ndvi[take] = look.ndvi[take]
            seen[take] = look.day.toordinal()
            if use_hazy:
                from_haze |= take
    return ndvi, seen, from_haze


def _all_sharp(looks: Sequence[Look]) -> bool:
    return bool(looks) and bool(np.logical_or.reduce([look.sharp for look in looks]).all())


def _main_ordinal(seen: np.ndarray) -> int:
    days, counts = np.unique(seen[seen > 0], return_counts=True)
    return int(days[int(np.argmax(counts))])


def change_since(looks: Sequence[Look], ndvi: np.ndarray, seen: np.ndarray, from_haze: np.ndarray) -> Optional[Change]:
    """How far NDVI moved on the main look's squares since their newest sharp look EARLIER_GAP_DAYS or more
    before it; None when no such look covers EARLIER_SHARE of them."""
    if not (seen > 0).any():
        return None
    main = _main_ordinal(seen)
    earlier = [look for look in looks if look.day.toordinal() <= main - EARLIER_GAP_DAYS]
    if not earlier:
        return None
    before, before_seen, before_haze = newest_clear(earlier)
    fresh = (seen == main) & ~from_haze
    both = fresh & (before_seen > 0) & ~before_haze
    if not fresh.any() or both.sum() < EARLIER_SHARE * fresh.sum():
        return None
    return Change(now=date.fromordinal(main), before=date.fromordinal(_main_ordinal(before_seen[both])),
                  median=float(np.median(ndvi[both] - before[both])), squares=int(both.sum()))


def _enough(looks: Sequence[Look]) -> bool:
    """Every square has a sharp look, and the main look has an earlier one to be compared with."""
    if not _all_sharp(looks):
        return False
    ndvi, seen, from_haze = newest_clear(looks)
    return change_since(looks, ndvi, seen, from_haze) is not None


def _in_threads(jobs: Sequence[Any], env: dict[str, str]) -> list[Any]:
    """Each job() in its own thread under the GDAL settings (they are per thread). Used inside a raster worker,
    where waiting on the network in threads holds up nobody else."""
    def run(job: Any) -> Any:
        with rasterio.Env(**env):
            return job()

    if not jobs:
        return []
    with ThreadPoolExecutor(max_workers=min(len(jobs), THREADS_IN_WORKER)) as pool:
        return list(pool.map(run, jobs))


def _class_map(scl: str, bbox: Sequence[float]) -> tuple[str, Any]:
    """One pass's class map over bbox on the pass's 10 m grid: ("classes", (grid, usable squares)),
    ("edge", None) when the scene does not hold the whole box, ("error", message). The 10 m grid is worked out
    from the class map's own 20 m grid (same corner, half the step), so no band file is opened yet."""
    try:
        with rasterio.open(scl) as src:
            fine = src.transform * rasterio.Affine.scale(SQUARE_M / src.res[0])
            whole = from_bounds(*transform_bounds("EPSG:4326", src.crs, *bbox), transform=fine)
            whole = whole.round_offsets().round_lengths()
            if whole.width < 1 or whole.height < 1:
                return "edge", None
            ground = window_bounds(whole, fine)
            coarse = from_bounds(*ground, transform=src.transform)
            try:
                inside = coarse.intersection(Window(0, 0, src.width, src.height))
            except WindowError:
                return "edge", None
            if abs(inside.width - coarse.width) > 1e-6 or abs(inside.height - coarse.height) > 1e-6:
                return "edge", None
            shape = (int(whole.height), int(whole.width))
            classes = src.read(1, window=coarse, out_shape=shape, resampling=Resampling.nearest)
            grid = (str(src.crs), tuple(window_transform(whole, fine))[:6], shape)
        return "classes", (grid, ~np.isin(classes, list(SCL_UNUSABLE)))
    except Exception as why:  # noqa: BLE001 - a worker returns what happened; the caller logs it
        return "error", f"{type(why).__name__}: {why}"


def _class_maps(scls: Sequence[str], bbox: Sequence[float], env: dict[str, str]) -> list[tuple[str, Any]]:
    """In a raster worker: _class_map for several passes at once."""
    return _in_threads([lambda scl=scl: _class_map(scl, bbox) for scl in scls], env)


def _band(href: str, grid: tuple[Any, Any, Any]) -> np.ndarray:
    crs, transform, shape = grid
    ground = rasterio.transform.array_bounds(shape[0], shape[1], rasterio.Affine(*transform))
    with rasterio.open(href) as src:
        if str(src.crs) != crs:
            raise ValueError(f"band on another grid: {src.crs}")
        return src.read(1, window=from_bounds(*ground, transform=src.transform), out_shape=shape)


def _bands(red: str, nir: str, blue: str, grid: tuple[Any, Any, Any], env: dict[str, str]) -> tuple[str, Any]:
    """In a raster worker: ("bands", (red, nir, blue)) on the grid, read side by side, or ("error", message)."""
    try:
        return "bands", tuple(_in_threads([lambda href=href: _band(href, grid) for href in (red, nir, blue)], env))
    except Exception as why:  # noqa: BLE001 - a worker returns what happened; the caller logs it
        return "error", f"{type(why).__name__}: {why}"


def _href(assets: dict[str, Any], names: Sequence[str]) -> Optional[str]:
    for name in names:
        if name in assets:
            return assets[name].get("href")
    return None


def _passes(items: Sequence[dict[str, Any]], skip_tiles: frozenset[str]) -> list[tuple[date, str, dict[str, str]]]:
    """(day, tile, band addresses) newest first, one a day, from the tile with the most passes.

    A photo near the seam of two satellite grids gets every pass twice, once on each grid (Cyampirita does);
    one grid is kept so squares line up from pass to pass.
    """
    by_tile: dict[str, dict[date, dict[str, str]]] = {}
    for item in items:
        assets = item.get("assets") or {}
        hrefs = {"red": _href(assets, ("red", "B04")), "nir": _href(assets, ("nir", "B08")),
                 "blue": _href(assets, ("blue", "B02")), "scl": _href(assets, ("scl", "SCL"))}
        stamp = str(item.get("datetime") or "")[:10]
        if not stamp or not all(hrefs.values()):
            continue
        parts = str(item.get("id") or "").split("_")
        tile = parts[1] if len(parts) > 1 else ""
        if tile in skip_tiles:
            continue
        by_tile.setdefault(tile, {}).setdefault(date.fromisoformat(stamp), hrefs)  # type: ignore[arg-type]
    if not by_tile:
        return []
    tile = max(by_tile, key=lambda name: (len(by_tile[name]), name))
    return [(day, tile, by_tile[tile][day]) for day in sorted(by_tile[tile], reverse=True)]


class NoPicture(Exception):
    """Why there is no picture, in plain words a reader can be shown."""


def _spread(items: Sequence[Any], piles: int) -> list[list[Any]]:
    return [list(items[i::piles]) for i in range(min(piles, len(items)))]


def _clear_passes(passes: list[tuple[date, str, dict[str, str]]], bbox: Sequence[float]
                  ) -> tuple[Optional[str], list[tuple[date, dict[str, str], Grid, np.ndarray]], int]:
    """Every pass's class map, read together: (a tile to leave out because its scenes do not hold the whole
    box, the passes with clear ground as (day, band addresses, grid, usable squares), how many failed)."""
    piles = _spread(passes, raster_process.WORKERS)
    with ThreadPoolExecutor(max_workers=len(piles)) as pool:
        answers = list(pool.map(lambda pile: raster_process.run(_class_maps, [p[2]["scl"] for p in pile], list(bbox),
                                                                _READ_ENV), piles))
    clear, failed = [], 0
    for pile, results in zip(piles, answers):
        for (day, tile, hrefs), (kind, got) in zip(pile, results):
            if kind == "edge":
                return tile, [], 0
            if kind == "error":
                failed += 1
                logger.warning("satellite view: class map of %s could not be read: %s", day, got)
                continue
            grid = Grid(got[0][0], tuple(got[0][1]), tuple(got[0][2]))  # type: ignore[arg-type]
            if got[1].mean() >= SKIP_BELOW_CLEAR:
                clear.append((day, hrefs, grid, got[1]))
    return None, sorted(clear, key=lambda item: item[0], reverse=True), failed


def plants_view(bbox: Sequence[float], today: date) -> Plants:
    """The newest clear look at each 10 m square of bbox (west, south, east, north), reading back in time until
    every square has a sharp one or LOOKBACKS runs out. Raises NoPicture.

    Each step back reads every pass's class map at once (one small file a pass), then the three bands of the
    passes with clear ground, newest first and PASSES_AT_ONCE at a time, stopping as soon as the picture is
    whole and has an earlier look to be compared with.
    """
    stac = STACService("earth_search")
    looks: list[Look] = []
    grid: Optional[Grid] = None
    skip_tiles: frozenset[str] = frozenset()
    read = failed = extra = 0
    start = today + timedelta(days=1)
    for days in LOOKBACKS:
        end, start = start - timedelta(days=1), today - timedelta(days=days)
        found = stac.search_imagery(bbox=list(bbox), datetime_range=f"{start.isoformat()}/{end.isoformat()}",
                                    max_cloud_cover=101.0, limit=250)
        if "error" in found:
            logger.warning("satellite view: scene search failed: %s", found["error"])
            raise NoPicture("The satellite's pictures could not be searched just now.")
        clear: list[tuple[date, dict[str, str], Grid, np.ndarray]] = []
        for _ in range(3):  # a tile whose scenes stop short of the box is left out and the step read again
            passes = _passes(found.get("items") or [], skip_tiles)
            if not passes:
                break
            read += len(passes)
            left_out, clear, class_failures = _clear_passes(passes, bbox)
            if left_out is None:
                failed += class_failures
                break
            skip_tiles |= {left_out}
        if grid is None and clear:
            grid = clear[0][2]
        waiting = [item for item in clear if item[2] == grid]
        while waiting:
            batch, waiting = waiting[:PASSES_AT_ONCE], waiting[PASSES_AT_ONCE:]
            with ThreadPoolExecutor(max_workers=len(batch)) as pool:
                results = list(pool.map(
                    lambda item: raster_process.run(_bands, item[1]["red"], item[1]["nir"], item[1]["blue"],
                                                    (item[2].crs, item[2].transform, item[2].shape), _READ_ENV), batch))
            for (day, _, _, usable), (kind, got) in zip(batch, results):
                if kind == "error":
                    failed += 1
                    logger.warning("satellite view: bands of %s could not be read: %s", day, got)
                    continue
                red, nir, blue = got
                looks.append(look_from_bands(day, red, nir, blue, usable & ((red > 0) | (nir > 0))))
            if _all_sharp(looks):
                extra += len(batch)
                if _enough(looks) or extra > EARLIER_PASSES:
                    waiting = []
        if _all_sharp(looks):
            break
    if not looks or grid is None:
        if failed and failed >= read:
            raise NoPicture("The satellite's pictures could not be read just now.")
        raise NoPicture(f"No satellite pass saw this ground clearly in the last {LOOKBACKS[-1]} days.")
    ndvi, seen, from_haze = newest_clear(looks)
    return Plants(grid=grid, ndvi=ndvi, seen=seen, hazy=from_haze, change=change_since(looks, ndvi, seen, from_haze),
                  passes_read=read)


# --- Soil moisture ---------------------------------------------------------------------------------------------


def _read_soil(url: str, bbox: Sequence[float], env: dict[str, str]) -> tuple[str, Any]:
    """In a raster worker: every 100 m square of the file that touches bbox.
    ("grid", (stored values, west, north, step)), ("unpublished", None) or ("error", message)."""
    try:
        with rasterio.Env(**env), rasterio.open(url) as ds:
            box = from_bounds(*bbox, transform=ds.transform)
            left, top = int(np.floor(box.col_off)), int(np.floor(box.row_off))
            right, bottom = int(np.ceil(box.col_off + box.width)), int(np.ceil(box.row_off + box.height))
            window = Window(left, top, right - left, bottom - top).intersection(Window(0, 0, ds.width, ds.height))
            values = ds.read(1, window=window)
            west, _, _, north = window_bounds(window, ds.transform)
            return "grid", (values, float(west), float(north), float(ds.res[0]))
    except Exception as why:  # noqa: BLE001 - a worker returns what happened; the caller logs it
        return ("unpublished", None) if "404" in str(why) else ("error", f"{type(why).__name__}: {why}")


def soil_view(bbox: Sequence[float], today: date) -> Soil:
    """The newest published ten days of FAO's soil moisture over bbox, and where the photo's centre sits in its
    own last year. Raises NoPicture."""
    scale = wapor_service.LAYERS[SOIL_LAYER][0] * 100.0  # stored value to per cent
    period = wapor_service.dekad_of(today)
    for _ in range(SOIL_TRIES):
        kind, got = raster_process.run(_read_soil, wapor_service.raster_url(SOIL_LAYER, period), list(bbox),
                                       wapor_service.GDAL_COG_ENV)
        if kind == "grid":
            break
        if kind == "error":
            logger.warning("satellite view: soil moisture for %s could not be read: %s", period, got)
            raise NoPicture("FAO's soil moisture file could not be read just now.")
        period = wapor_service.dekad_before(period)
    else:
        raise NoPicture("FAO has not published soil moisture for the last weeks.")
    values, west, north, step = got
    percent = np.where(values == wapor_service.NODATA, np.nan, values.astype(np.float32) * scale)
    if not np.isfinite(percent).any():
        raise NoPicture("FAO's soil moisture holds no value for this ground.")
    _, ends = wapor_service.dekad_days(period)
    centre, low, high = _centre_in_its_year(bbox, period)
    return Soil(period=period, percent=percent, west=west, north=north, step=step,
                first_estimate=(today - ends).days < wapor_service.SETTLED_AFTER_DAYS, centre=centre, year_low=low, year_high=high)


def _centre_in_its_year(bbox: Sequence[float], period: str) -> tuple[Optional[float], Optional[float], Optional[float]]:
    """The centre's reading for `period`, and its lowest and highest over the SOIL_YEAR_STEPS periods before."""
    west, south, east, north = bbox
    periods = [period]
    for _ in range(SOIL_YEAR_STEPS):
        periods.append(wapor_service.dekad_before(periods[-1]))
    readings = wapor_service.point_series(SOIL_LAYER, periods, (south + north) / 2, (west + east) / 2)
    now = readings.get(period)
    before = [value * 100.0 for code, value in readings.items() if code != period and value is not None]
    if now is None or len(before) < SOIL_YEAR_STEPS * 2 // 3:
        return (None if now is None else now * 100.0), None, None
    return now * 100.0, min(before), max(before)


# --- One view of a photo, kept for the day -------------------------------------------------------------------

# A view takes many remote reads to make (40 s to 2 minutes on a laptop, 2026-11-23), and neither source
# changes more than once in a few days, so a view is used for FRESH_HOURS whatever the calendar says: a view
# kept by its day was made again for everyone at midnight UTC. A restart (every deploy) empties memory, so it
# is also kept on the cache volume, like the rain files.
FRESH_HOURS = 12
_views: TTLCache = TTLCache(maxsize=32, ttl=FRESH_HOURS * 3600)
_views_lock = threading.Lock()
_KEEP_DIR = Path(os.environ.get("SATELLITE_VIEW_CACHE_DIR", "/tmp/ingabe_cache/satellite_view"))
KEEP_DAYS = 3  # files older than this are cleared when a new one is written


def _keep_path(key: tuple[float, ...]) -> Path:
    return _KEEP_DIR / (hashlib.sha256(repr(key).encode()).hexdigest()[:24] + ".npz")


def _keep(key: tuple[float, ...], made: View) -> None:
    """Write the view to the cache volume; a disk that cannot be written is logged and the view is made again."""
    plants, soil = made.plants, made.soil
    assert plants is not None and soil is not None
    change = plants.change
    facts = {
        "bbox": list(made.bbox), "today": made.today.isoformat(), "crs": plants.grid.crs,
        "transform": list(plants.grid.transform), "passes_read": plants.passes_read,
        "change": None if change is None else [change.now.isoformat(), change.before.isoformat(), change.median, change.squares],
        "soil": [soil.period, soil.west, soil.north, soil.step, soil.first_estimate, soil.centre, soil.year_low, soil.year_high],
    }
    try:
        _KEEP_DIR.mkdir(parents=True, exist_ok=True)
        for old in _KEEP_DIR.glob("*.npz"):
            if time.time() - old.stat().st_mtime > KEEP_DAYS * 86400:
                old.unlink(missing_ok=True)
        path = _keep_path(key)
        part = path.with_suffix(f".{threading.get_ident()}.part")
        with open(part, "wb") as out:
            np.savez_compressed(out, ndvi=plants.ndvi, seen=plants.seen, hazy=plants.hazy, percent=soil.percent,
                                facts=np.array(json.dumps(facts)))
        part.replace(path)
    except OSError:
        logger.warning("satellite view: could not keep the view on disk; it will be made again", exc_info=True)


def _kept(key: tuple[float, ...]) -> Optional[View]:
    """The view kept on the cache volume for this box while it is fresh, or None (also when it cannot be read)."""
    path = _keep_path(key)
    try:
        if time.time() - path.stat().st_mtime > FRESH_HOURS * 3600:
            return None
    except OSError:
        return None
    try:
        with np.load(path) as kept:
            facts = json.loads(str(kept["facts"]))
            ndvi, seen, from_haze, percent = kept["ndvi"], kept["seen"], kept["hazy"], kept["percent"]
    except (OSError, ValueError, KeyError):
        logger.warning("satellite view: the kept view could not be read; making it again", exc_info=True)
        return None
    change = facts["change"]
    plants = Plants(
        grid=Grid(facts["crs"], tuple(facts["transform"]), tuple(ndvi.shape)),  # type: ignore[arg-type]
        ndvi=ndvi, seen=seen, hazy=from_haze, passes_read=int(facts["passes_read"]),
        change=None if change is None else Change(date.fromisoformat(change[0]), date.fromisoformat(change[1]),
                                                  float(change[2]), int(change[3])))
    period, west, north, step, first, centre, low, high = facts["soil"]
    soil = Soil(period=period, percent=percent, west=west, north=north, step=step, first_estimate=bool(first),
                centre=centre, year_low=low, year_high=high)
    return View(bbox=key, today=date.fromisoformat(facts["today"]), plants=plants, plants_missing=None, soil=soil,  # type: ignore[arg-type]
                soil_missing=None)


_making: dict[tuple[float, ...], threading.Lock] = {}  # one making of a box's view at a time; others wait for it


def _key(bbox: Sequence[float]) -> tuple[float, ...]:
    return tuple(round(float(v), 5) for v in bbox)


def fresh(bbox: Sequence[float]) -> bool:
    """Whether a whole view of bbox is already kept and fresh, so that asking for it costs nothing."""
    key = _key(bbox)
    with _views_lock:
        if key in _views:
            return True
    try:
        return time.time() - _keep_path(key).stat().st_mtime <= FRESH_HOURS * 3600
    except OSError:
        return False


def view(bbox: Sequence[float], today: date) -> View:
    """Plants and soil over bbox. Each part that cannot be had says why; the other still comes.
    A whole view is used for FRESH_HOURS (memory and the cache volume); its `today` is the day it was made.
    Asked for twice at once (the photo's cards warm it while its reader asks Sage), it is made once."""
    key = _key(bbox)
    with _views_lock:
        kept = _views.get(key)
        making = _making.setdefault(key, threading.Lock())
    if kept is not None:
        return kept
    with making:
        return _view_once(key, bbox, today)


def _view_once(key: tuple[float, ...], bbox: Sequence[float], today: date) -> View:
    with _views_lock:
        kept = _views.get(key)
    if kept is not None:  # made by the caller we waited for
        return kept
    west, south, east, north = key
    if not (-180 <= west < east <= 180 and -90 <= south < north <= 90):
        raise ValueError(f"not a box in latitude and longitude: {bbox}")
    km2 = (east - west) * 111.32 * float(np.cos(np.radians((south + north) / 2))) * (north - south) * 110.57
    if km2 > MAX_AREA_KM2:
        wide = f"This picture covers about {km2:,.0f} km2, too wide for a square-by-square satellite view."
        return View(bbox=key, today=today, plants=None, plants_missing=wide, soil=None, soil_missing=None)  # type: ignore[arg-type]
    kept = _kept(key)
    if kept is not None:
        with _views_lock:
            _views[key] = kept
        return kept
    plants = soil = None
    plants_missing = soil_missing = None
    with ThreadPoolExecutor(max_workers=2) as pool:
        plants_job = pool.submit(plants_view, bbox, today)
        soil_job = pool.submit(soil_view, bbox, today)
        try:
            plants = plants_job.result()
        except NoPicture as why:
            plants_missing = str(why)
        try:
            soil = soil_job.result()
        except NoPicture as why:
            soil_missing = str(why)
    made = View(bbox=key, today=today, plants=plants, plants_missing=plants_missing, soil=soil, soil_missing=soil_missing)  # type: ignore[arg-type]
    if plants is not None and soil is not None:  # a part that failed is asked for again next time
        with _views_lock:
            _views[key] = made
        _keep(key, made)
    return made


# --- Squares to draw ---------------------------------------------------------------------------------------------


def _blocks(values: np.ndarray, side: int, how: str) -> np.ndarray:
    """values in blocks of side x side squares: their mean, their oldest day, or whether any is set."""
    if side == 1:
        return values
    rows, cols = -(-values.shape[0] // side) * side, -(-values.shape[1] // side) * side
    if how == "any":
        padded = np.zeros((rows, cols), dtype=bool)
    else:
        padded = np.full((rows, cols), np.nan, dtype=np.float64)
    padded[: values.shape[0], : values.shape[1]] = values
    cut = padded.reshape(rows // side, side, cols // side, side)
    if how == "any":
        return cut.any(axis=(1, 3))
    with np.errstate(all="ignore"):
        return np.nanmean(cut, axis=(1, 3)) if how == "mean" else np.nanmin(cut, axis=(1, 3))


def _day_words(day: date, today: date) -> str:
    return f"{day.day} {day:%b}" if day.year == today.year else f"{day.day} {day:%b %Y}"


def plants_squares(plants: Plants) -> dict[str, Any]:
    """The plants picture as squares in latitude and longitude: {"geojson", "square_m", "main_day"}.
    Each square says its NDVI, its class in ndvi_classes' words, the day it was seen (with its year: the squares
    are saved on a map and read on later days) and whether it is drawn faded."""
    count = int((plants.seen > 0).sum())
    side = max(1, int(np.ceil(np.sqrt(count / MAX_SQUARES))))
    ndvi = _blocks(plants.ndvi, side, "mean")
    seen = _blocks(np.where(plants.seen > 0, plants.seen, np.nan), side, "oldest")
    haze = _blocks(plants.hazy, side, "any")
    a, b, c, d, e, f = plants.grid.transform
    to_wgs84 = Transformer.from_crs(plants.grid.crs, "EPSG:4326", always_xy=True)
    rows, cols = np.nonzero(np.isfinite(ndvi) & np.isfinite(seen))
    corners_col = np.stack([cols, cols + 1, cols + 1, cols], axis=1) * side
    corners_row = np.stack([rows, rows, rows + 1, rows + 1], axis=1) * side
    lon, lat = to_wgs84.transform(a * corners_col + b * corners_row + c, d * corners_col + e * corners_row + f)
    main = plants.main_day
    features = []
    for i, (row, col) in enumerate(zip(rows, cols)):
        day = date.fromordinal(int(seen[row, col]))
        ring = [[round(float(lon[i, k]), 7), round(float(lat[i, k]), 7)] for k in (0, 1, 2, 3, 0)]
        value = round(float(ndvi[row, col]), 2)
        cls = ndvi_classes.ndvi_class(value)
        faded = (main - day).days >= FADED_AFTER_DAYS
        properties: dict[str, Any] = {
            "plants": ndvi_classes.ground_label(cls) if cls else "",
            "ndvi": value,
            "seen_on": f"{day.day} {day:%b %Y}",
            "faded": bool(faded),
        }
        notes = (["An older look than most of the photo"] if faded else []) + (
            ["Seen through haze: may read low"] if haze[row, col] else [])
        if notes:
            properties["note"] = "; ".join(notes)
        features.append({"type": "Feature", "properties": properties, "geometry": {"type": "Polygon", "coordinates": [ring]}})
    return {"geojson": {"type": "FeatureCollection", "features": features}, "square_m": SQUARE_M * side, "main_day": main}


def period_words(period: str, today: Optional[date] = None) -> str:
    """The ten days in words, "1-10 Nov"; with the year when it is not today's, or when no today is given."""
    starts, ends = wapor_service.dekad_days(period)
    year = "" if today is not None and ends.year == today.year else f" {ends.year}"
    return f"{starts.day}-{ends.day} {ends:%b}{year}"


def soil_band(percent: float) -> tuple[str, str]:
    """(the band's words, its colour) for a soil moisture reading."""
    index = max(i for i, (low, _) in enumerate(SOIL_BANDS) if percent >= low)
    low, colour = SOIL_BANDS[index]
    if index == 0:
        return f"Below {SOIL_BANDS[1][0]:g}%", colour
    if index == len(SOIL_BANDS) - 1:
        return f"{low:g}% and above", colour
    return f"{low:g}-{SOIL_BANDS[index + 1][0]:g}%", colour


def soil_squares(soil: Soil) -> dict[str, Any]:
    """The soil moisture squares in latitude and longitude, each with its reading and the ten days it is for."""
    words = period_words(soil.period)
    features = []
    for row, col in zip(*np.nonzero(np.isfinite(soil.percent))):
        left, top = soil.west + col * soil.step, soil.north - row * soil.step
        ring = [[round(x, 7), round(y, 7)] for x, y in ((left, top), (left + soil.step, top), (left + soil.step, top - soil.step),
                                                         (left, top - soil.step), (left, top))]
        value = round(float(soil.percent[row, col]), 1)
        properties: dict[str, Any] = {"moisture": value, "band": soil_band(value)[0], "period": words}
        if soil.first_estimate:
            properties["note"] = "FAO's first estimate"
        features.append({"type": "Feature", "properties": properties, "geometry": {"type": "Polygon", "coordinates": [ring]}})
    return {"geojson": {"type": "FeatureCollection", "features": features}}


# --- What the two show, in plain facts ----------------------------------------------------------------------


def _percent(share: float) -> str:
    return "under 1%" if 0 < share < 0.005 else f"{share:.0%}"


def _list(parts: Sequence[str]) -> str:
    return parts[0] if len(parts) == 1 else ", ".join(parts[:-1]) + " and " + parts[-1]


def plants_facts(plants: Plants, today: date) -> dict[str, Any]:
    """The plants picture in numbers and sentences: {"sentences", "direction", "median", "classes", "days"}."""
    known = plants.seen > 0
    total = int(known.sum())
    days, counts = np.unique(plants.seen[known], return_counts=True)
    by_day = sorted(((date.fromordinal(int(d)), int(n) / plants.seen.size) for d, n in zip(days, counts)),
                    key=lambda pair: pair[1], reverse=True)
    sentences = []
    if plants.covered >= 0.999:
        sentences.append(f"Every {SQUARE_M} m square of this photo has a clear satellite look (Sentinel-2).")
    else:
        sentences.append(f"{_percent(plants.covered)} of this photo has a clear satellite look (Sentinel-2); no pass saw "
                         f"the rest in the last {LOOKBACKS[-1]} days.")
    shown = [f"{_percent(share)} on {_day_words(day, today)}" for day, share in by_day[:4]]
    sentences.append(f"Seen: {_list(shown)}" + ("; the rest earlier." if len(by_day) > 4 else "."))
    main = plants.main_day
    old = float(((plants.seen > 0) & (plants.seen <= main.toordinal() - FADED_AFTER_DAYS)).mean())
    if old >= 0.05:
        sentences.append(f"{_percent(old)} of the photo is drawn faded: clouds hid it on {_day_words(main, today)}, so its "
                         "look is older and must not be read against the fresh part.")
    haze_share = float(plants.hazy.mean())
    if haze_share >= 0.01:
        sentences.append(f"{_percent(haze_share)} was only seen through haze and may read low.")
    values = plants.ndvi[known]
    classes = []
    for i, cls in enumerate(ndvi_classes.NDVI_CLASSES):
        upper = ndvi_classes.NDVI_CLASSES[i + 1].min_ndvi if i + 1 < len(ndvi_classes.NDVI_CLASSES) else np.inf
        share = float(((values >= cls.min_ndvi) & (values < upper)).sum() / total)
        classes.append({"key": cls.key, "label": ndvi_classes.ground_label(cls), "share": share})
    said = [f"{_percent(c['share'])} {c['label'].lower()}" for c in reversed(classes) if c["share"] > 0]
    median = float(np.median(values))
    sentences.append(f"By greenness (NDVI): {_list(said)}. The middle square reads {median:.2f}.")
    direction = None
    if plants.change is not None:
        moved = plants.change.median
        direction = "steady" if abs(moved) < STEADY else ("rose" if moved > 0 else "fell")
        how = {"steady": f"stayed about the same ({moved:+.2f})", "rose": f"rose by {moved:.2f}",
               "fell": f"fell by {abs(moved):.2f}"}[direction]
        sentences.append(f"On the squares seen {_day_words(plants.change.now, today)}, NDVI {how} since "
                         f"{_day_words(plants.change.before, today)}.")
    return {"sentences": sentences, "direction": direction, "median": round(median, 2), "classes": classes,
            "days": [{"day": day.isoformat(), "share": round(share, 3)} for day, share in by_day]}


def soil_facts(soil: Soil, today: date) -> dict[str, Any]:
    """The soil moisture reading in numbers and sentences: {"sentences", "position", "median", "low", "high"}."""
    values = soil.percent[np.isfinite(soil.percent)]
    median, low, high = float(np.median(values)), float(values.min()), float(values.max())
    words = period_words(soil.period, today)
    sentences = [f"Soil moisture for {words} (FAO WaPOR estimate, {SOIL_SQUARE_M} m squares): the middle square reads "
                 f"{median:.0f}%, from {low:.0f}% to {high:.0f}% across the photo."]
    position = None
    if soil.centre is not None and soil.year_low is not None and soil.year_high is not None and soil.year_high > soil.year_low:
        place = (soil.centre - soil.year_low) / (soil.year_high - soil.year_low)
        position = "low" if place < 1 / 3 else ("high" if place > 2 / 3 else "middle")
        where = {"low": "near the bottom", "middle": "in the middle", "high": "near the top"}[position]
        sentences.append(f"At the photo's centre it reads {soil.centre:.0f}%; over the year before it ran from "
                         f"{soil.year_low:.0f}% to {soil.year_high:.0f}%, so it is {where} of its own year.")
    if soil.first_estimate:
        sentences.append("This is FAO's first estimate; FAO replaces it with a steadier one about two months later.")
    sentences.append("It follows Rwanda's wet and dry seasons but was not confirmed against rain week by week "
                     "(tested 2026-11-23): read it as the season's level, not a precise reading.")
    return {"sentences": sentences, "position": position, "median": round(median, 1), "low": round(low, 1), "high": round(high, 1)}


_TOGETHER = {
    ("rose", "high"): "Plants are filling in and the soil reads near the top of its year: the rains are carrying the crop.",
    ("rose", "middle"): "Plants are filling in and the soil reads in the middle of its year.",
    ("rose", "low"): "Plants are still filling in while the soil reads near the bottom of its year: worth another look "
                     "at the next clear pass.",
    ("fell", "low"): "Plants are thinning and the soil reads near the bottom of its year, as in a dry spell or the dry "
                     "season. A harvest lowers greenness too.",
    ("fell", "middle"): "Plants are thinning though the soil does not read dry. A harvest, pests, disease or too much "
                        "water are worth checking on the ground.",
    ("fell", "high"): "Plants are thinning though the soil reads near the top of its year. A harvest, pests, disease or "
                      "too much water are worth checking on the ground.",
    ("steady", "high"): "Greenness has held steady and the soil reads near the top of its year.",
    ("steady", "middle"): "Greenness has held steady and the soil reads in the middle of its year.",
    ("steady", "low"): "Greenness has held steady while the soil reads near the bottom of its year: worth another look "
                       "at the next clear pass.",
}


def words(seen: View) -> dict[str, Any]:
    """What the view shows, for cards and Sage alike: {"plants", "soil", "together", "missing"}.
    "together" is a sentence only when greenness has a direction and the soil has a place in its year."""
    plants = plants_facts(seen.plants, seen.today) if seen.plants is not None else None
    soil = soil_facts(seen.soil, seen.today) if seen.soil is not None else None
    together = None
    if plants and soil and plants["direction"] and soil["position"]:
        together = _TOGETHER[(plants["direction"], soil["position"])]
    missing = [why for why in (seen.plants_missing, seen.soil_missing) if why]
    return {"plants": plants, "soil": soil, "together": together, "missing": missing}
