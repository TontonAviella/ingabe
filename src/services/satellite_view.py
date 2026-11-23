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
  8 Oct and 19 Nov on the same squares. So every square carries the day it was seen, and a hexagon whose
  look is older than the photo's main one is drawn in grey: in the legend's colours a month-old look reads as a
  sick crop beside a fresh one (seen on the Cyampirita photo: the third last seen 23 Oct drew orange).
- FAO WaPOR soil moisture (L2-RSM-D, 100 m, every 10 days) has a value on every square of Rwanda. It follows
  the wet and dry seasons (12 of 12 places) but not whether rain was above or below usual (rank correlation
  0.14 against CHIRPS, bar 0.40), and FAO replaces each file about 67 days later with a steadier one. So it
  is shown as the season's level with its dates, marked a first estimate while it is one, and never as a
  precise weekly reading.

- The band files' numbers are reflectance x 10,000 as they stand. Each pass's own record says both that its
  offset is already taken off (`earthsearch:boa_offset_applied`) and, band by band, that one of -0.1 is still
  to be applied; the first is true. Over open water of Lake Kivu near-infrared read 44 to 209 (2026-11-23);
  with the offset still in the file it could not read under 1,000. Applying it again would pull every NDVI.
- At 11 other farming places (2026-11-23, boxes of 108 ha in both of Rwanda's satellite zones) the kept NDVI
  equalled a separate read of the same files on every square, and every square had a clear look. At 5 of them
  the main look was the older one, with a newer look on 6% to 40% of the box: the sentences say so.

Nothing here is drawn or worded for a screen: `view` returns measured values, `plants_hexagons` and
`soil_hexagons` the hexagons to draw, `words` the plain facts cards and Sage both read, and `of_fields` what
is said of one field's outline. `ready` gives the kept view to a surface that must never wait on a satellite.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import threading
import time
import zipfile
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import date, timedelta
from pathlib import Path
from typing import Any, Callable, Optional, Sequence

import h3
import h3.api.basic_int as h3_numbers  # hexagons as numbers: thousands of squares are placed and sorted without strings
import numpy as np
import rasterio
import rasterio.transform
import shapely
from cachetools import TTLCache
from pyproj import Transformer
from rasterio.enums import Resampling
from rasterio.errors import WindowError
from rasterio.warp import transform_bounds
from rasterio.windows import Window, from_bounds
from rasterio.windows import bounds as window_bounds
from rasterio.windows import transform as window_transform
from scipy.ndimage import median_filter
from shapely.geometry import Polygon, box, shape
from shapely.ops import transform as shapely_transform
from shapely.validation import make_valid

from src.services import ndvi_classes, raster_process, wapor_service
from src.services.gdal_http import GDAL_HTTP_TIMEOUTS
from src.services.stac_service import SCL_UNUSABLE, STACService

logger = logging.getLogger(__name__)

SQUARE_M = 10  # Sentinel-2's red and near-infrared pixels
# How far back passes are searched, in days, each step only when squares are still without a sharp look.
LOOKBACKS = (45, 100, 180)
HAZE_BLUE = 900.0  # blue reflectance x 10,000; sharp passes read 330-750 across the photo, hazy ground 1,080+
HAZE_BLOCK = 7  # squares on a side (70 m): haze is wide, a bright roof or bare plot is not
OLDER_AFTER_DAYS = 14  # a square seen this long before the photo's main look is marked as an older look
EARLIER_GAP_DAYS = 14  # a change is measured against a look at least this much older
EARLIER_SHARE = 0.5  # of the main look's squares that the earlier look must cover before change is stated
EARLIER_PASSES = 6  # older passes read at most, after the photo is covered, to find that earlier look
STEADY = 0.05  # NDVI moved by less than this: "about the same"
MAX_SQUARES = 20_000  # squares the drawing starts from; a larger photo's squares are first joined into wider ones
# The picture is drawn as hexagons (H3), like the app's other findings, in sizes that take over from one another
# as the map is zoomed in: a wide hexagon says "somewhere in here", a small one says where.
HEX_OVER_SQUARE = 1.5  # the smallest hexagon holds this many measured squares: none is drawn finer than the data
HEX_FEWEST = 7  # a wider size is added while the photo still holds this many of its hexagons
HEX_SIZES = 3  # sizes drawn at most
READING_NEEDS = 0.15  # of a hexagon left inside the photo's frame before its reading is written on it
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
    newest_pass: Optional[date] = None  # the newest pass the search held when this was made, clear or not

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


def _newest_day(items: Sequence[dict[str, Any]]) -> Optional[date]:
    """The newest day among the search's passes, on any grid and whatever their files: what "a pass has come since"
    is asked against. (_passes keeps one grid, and over a few days the grid it keeps can differ.)"""
    days = [str(item.get("datetime") or "")[:10] for item in items]
    return max((date.fromisoformat(day) for day in days if day), default=None)


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
    stac = STACService("earth_search", use_pystac=False)
    looks: list[Look] = []
    grid: Optional[Grid] = None
    skip_tiles: frozenset[str] = frozenset()
    read = failed = extra = 0
    newest_pass: Optional[date] = None
    start = today + timedelta(days=1)
    for days in LOOKBACKS:
        end, start = start - timedelta(days=1), today - timedelta(days=days)
        found = stac.search_imagery(bbox=list(bbox), datetime_range=f"{start.isoformat()}/{end.isoformat()}",
                                    max_cloud_cover=101.0, limit=250)
        if "error" in found:
            logger.warning("satellite view: scene search failed: %s", found["error"])
            raise NoPicture("The satellite's pictures could not be searched just now.")
        newest_pass = max(filter(None, (newest_pass, _newest_day(found.get("items") or []))), default=None)
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
    if not (seen > 0).any():  # class maps showed clear ground, but the bands held no value there
        raise NoPicture(f"No satellite pass saw this ground clearly in the last {LOOKBACKS[-1]} days.")
    return Plants(grid=grid, ndvi=ndvi, seen=seen, hazy=from_haze, change=change_since(looks, ndvi, seen, from_haze),
                  passes_read=read, newest_pass=newest_pass)


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
        return ("unpublished", None) if "HTTP response code: 404" in str(why) else ("error", f"{type(why).__name__}: {why}")


def _newest_soil(bbox: Sequence[float], today: date) -> tuple[str, Any]:
    """(the newest ten days FAO has published, that file's squares over bbox). Raises NoPicture."""
    period = wapor_service.dekad_of(today)
    for _ in range(SOIL_TRIES):
        kind, got = raster_process.run(_read_soil, wapor_service.raster_url(SOIL_LAYER, period), list(bbox),
                                       wapor_service.GDAL_COG_ENV)
        if kind == "grid":
            return period, got
        if kind == "error":
            logger.warning("satellite view: soil moisture for %s could not be read: %s", period, got)
            raise NoPicture("FAO's soil moisture file could not be read just now.")
        period = wapor_service.dekad_before(period)
    raise NoPicture("FAO has not published soil moisture for the last weeks.")


def soil_view(bbox: Sequence[float], today: date, newest: Optional[tuple[str, Any]] = None) -> Soil:
    """The newest published ten days of FAO's soil moisture over bbox, and where the photo's centre sits in its
    own last year. `newest` is _newest_soil's answer when the caller has already asked. Raises NoPicture."""
    scale, offset = wapor_service.LAYERS[SOIL_LAYER][:2]  # stored value to a share of 1
    period, got = newest or _newest_soil(bbox, today)
    values, west, north, step = got
    percent = np.where(values == wapor_service.NODATA, np.nan, (values.astype(np.float32) * scale + offset) * 100.0)
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
# Each night the views of the photos in farm projects are made ahead (refresh, run by the pipelines'
# nightly_satellite_views), so a view lasts until the next night's run and a little over. A photo the night has not
# reached yet gets its view on the first ask, and that one lasts as long.
FRESH_HOURS = 26
_views: TTLCache = TTLCache(maxsize=32, ttl=FRESH_HOURS * 3600)  # box -> (view, when its kept file was written)
_views_lock = threading.Lock()
_KEEP_DIR = Path(os.environ.get("SATELLITE_VIEW_CACHE_DIR", "/tmp/ingabe_cache/satellite_view"))
KEEP_DAYS = 3  # files older than this are cleared when a new one is written


def _keep_path(key: tuple[float, ...]) -> Path:
    return _KEEP_DIR / (hashlib.sha256(repr(key).encode()).hexdigest()[:24] + ".npz")


def _keep(key: tuple[float, ...], made: View) -> bool:
    """Write the view to the cache volume and say whether it was written; a disk that cannot be written is logged
    and the view is made again. The app and the night's run (another process) both write here."""
    plants, soil = made.plants, made.soil
    assert plants is not None and soil is not None
    change = plants.change
    facts = {
        "bbox": list(made.bbox), "today": made.today.isoformat(), "crs": plants.grid.crs,
        "transform": list(plants.grid.transform), "passes_read": plants.passes_read,
        "newest_pass": plants.newest_pass.isoformat() if plants.newest_pass else None,
        "change": None if change is None else [change.now.isoformat(), change.before.isoformat(), change.median, change.squares],
        "soil": [soil.period, soil.west, soil.north, soil.step, soil.first_estimate, soil.centre, soil.year_low, soil.year_high],
    }
    try:
        _KEEP_DIR.mkdir(parents=True, exist_ok=True)
        for old in (*_KEEP_DIR.glob("*.npz"), *_KEEP_DIR.glob("*.part")):  # a .part is a write that never finished
            try:
                if time.time() - old.stat().st_mtime > KEEP_DAYS * 86400:
                    old.unlink(missing_ok=True)
            except OSError:  # the other process finished or cleared it meanwhile
                continue
        path = _keep_path(key)
        part = path.with_suffix(f".{os.getpid()}-{threading.get_ident()}.part")
        with open(part, "wb") as out:
            np.savez_compressed(out, ndvi=plants.ndvi, seen=plants.seen, hazy=plants.hazy, percent=soil.percent,
                                facts=np.array(json.dumps(facts)))
        part.replace(path)
    except OSError:
        logger.warning("satellite view: could not keep the view on disk; it will be made again", exc_info=True)
        return False
    return True


def _written(key: tuple[float, ...]) -> Optional[float]:
    """When the box's kept view was written, or None when there is none."""
    try:
        return _keep_path(key).stat().st_mtime
    except OSError:
        return None


def _kept(key: tuple[float, ...], however_old: bool = False) -> Optional[View]:
    """The view kept on the cache volume for this box while it is fresh, or None (also when it cannot be read).
    however_old gives it past its freshness too: the night's refresh starts from it."""
    path = _keep_path(key)
    written = _written(key)
    if written is None or (not however_old and time.time() - written > FRESH_HOURS * 3600):
        return None
    try:
        with np.load(path) as kept:
            facts = json.loads(str(kept["facts"]))
            ndvi, seen, from_haze, percent = kept["ndvi"], kept["seen"], kept["hazy"], kept["percent"]
    except (OSError, ValueError, KeyError, EOFError, zipfile.BadZipFile):
        logger.warning("satellite view: the kept view could not be read; making it again", exc_info=True)
        return None
    change = facts["change"]
    plants = Plants(
        grid=Grid(facts["crs"], tuple(facts["transform"]), tuple(ndvi.shape)),  # type: ignore[arg-type]
        ndvi=ndvi, seen=seen, hazy=from_haze, passes_read=int(facts["passes_read"]),
        newest_pass=date.fromisoformat(facts["newest_pass"]) if facts.get("newest_pass") else None,
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


def _held(key: tuple[float, ...]) -> Optional[View]:
    """The box's view in this process's memory, unless a newer one has been written to the cache volume since (the
    night's refresh runs in another process). Call with _views_lock held."""
    held = _views.get(key)
    if held is None:
        return None
    written = _written(key)
    if (written is not None and written > held[1]) or time.time() - held[1] > FRESH_HOURS * 3600:
        del _views[key]
        return None
    return held[0]


def _hold(key: tuple[float, ...], made: View, written: Optional[float]) -> None:
    """Hold the view in memory as of when its kept file was written (now, when it could not be written)."""
    with _views_lock:
        _views[key] = (made, written or time.time())


def fresh(bbox: Sequence[float]) -> bool:
    """Whether a whole view of bbox is already kept and fresh, so that asking for it costs nothing."""
    key = _key(bbox)
    with _views_lock:
        if _held(key) is not None:
            return True
    written = _written(key)
    return written is not None and time.time() - written <= FRESH_HOURS * 3600


def ready(bbox: Sequence[float]) -> Optional[View]:
    """The fresh, whole view of bbox that is already kept (memory, else the cache volume), or None. No satellite is
    read, so a card or a field panel may ask every time it opens; None means "not made yet", never "nothing there"."""
    return _ready(_key(bbox))[0]


def _ready(key: tuple[float, ...]) -> tuple[Optional[View], float]:
    """(the fresh kept view, the stamp it is held under) read in one go, so that what is worked out from the view
    is filed under that view's own stamp and never under a newer one the night wrote meanwhile."""
    with _views_lock:
        held = _held(key)
        if held is not None:
            return held, _views[key][1]
    written = _written(key)  # before the read, as in _view_once
    made = _kept(key)
    if made is None:
        return None, 0.0
    stamp = written or time.time()
    with _views_lock:
        _views[key] = (made, stamp)
    return made, stamp


def view(bbox: Sequence[float], today: date) -> View:
    """Plants and soil over bbox. Each part that cannot be had says why; the other still comes.
    A whole view is used for FRESH_HOURS (memory and the cache volume); its `today` is the day it was made.
    Asked for twice at once (the photo's cards warm it while its reader asks Sage), it is made once."""
    key = _key(bbox)
    with _views_lock:
        kept = _held(key)
        making = _making.setdefault(key, threading.Lock())
    if kept is not None:
        return kept
    with making:
        return _view_once(key, bbox, today)


NOT_A_BOX = "This picture's place on the map could not be read, so the satellite cannot be matched to it."


def too_wide(bbox: Sequence[float]) -> Optional[str]:
    """Why no square-by-square view is made of this box, in plain words, or None when one is. Reads nothing and
    never raises: a box that is not one (a side of no length, a latitude off the globe) is said as such, since a
    photo's cards ask this while they are being listed."""
    try:
        return _too_wide(_key(bbox), bbox)
    except (ValueError, TypeError):
        return NOT_A_BOX


def _too_wide(key: tuple[float, ...], bbox: Sequence[float]) -> Optional[str]:
    """Why no square-by-square view is made of this box, or None. Raises ValueError when it is not a box."""
    west, south, east, north = key
    if not (-180 <= west < east <= 180 and -90 <= south < north <= 90):
        raise ValueError(f"not a box in latitude and longitude: {bbox}")
    km2 = (east - west) * 111.32 * float(np.cos(np.radians((south + north) / 2))) * (north - south) * 110.57
    if km2 > MAX_AREA_KM2:
        return f"This picture covers about {km2:,.0f} km2, too wide for a square-by-square satellite view."
    return None


def _view_once(key: tuple[float, ...], bbox: Sequence[float], today: date) -> View:
    with _views_lock:
        kept = _held(key)
    if kept is not None:  # made by the caller we waited for
        return kept
    wide = _too_wide(key, bbox)
    if wide:
        return View(bbox=key, today=today, plants=None, plants_missing=wide, soil=None, soil_missing=None)  # type: ignore[arg-type]
    written = _written(key)  # before the read: a file the night writes meanwhile is newer than this stamp
    kept = _kept(key)
    if kept is not None:
        _hold(key, kept, written)
        return kept
    return _make(key, bbox, today)[0]


def _make(key: tuple[float, ...], bbox: Sequence[float], today: date) -> tuple[View, bool]:
    """Read the satellites for both parts; a whole view is kept in memory and on the cache volume.
    Returns (the view, whether it is now on the cache volume)."""
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
    on_disk = False
    if plants is not None and soil is not None:  # a part that failed is asked for again next time
        on_disk = _keep(key, made)
        _hold(key, made, _written(key) if on_disk else None)
    return made, on_disk


# --- Made ahead, each night -----------------------------------------------------------------------------------


def _newest_pass_since(bbox: Sequence[float], since: date, today: date) -> Optional[date]:
    """The newest pass the search holds from `since` to today, clear or not; `since` itself when there is none
    newer; None when the search could not be made."""
    found = STACService("earth_search", use_pystac=False).search_imagery(
        bbox=list(bbox), datetime_range=f"{since.isoformat()}/{today.isoformat()}", max_cloud_cover=101.0, limit=250)
    if "error" in found:
        logger.warning("satellite view: scene search failed: %s", found["error"])
        return None
    return max(_newest_day(found.get("items") or []) or since, since)


def _soil_may_be_newer(soil: Soil, today: date) -> bool:
    """Whether FAO can hold something this reading does not: the next ten days have ended (their file comes 2 to 6
    days later), or this first estimate is old enough to have been replaced."""
    ends = wapor_service.dekad_days(soil.period)[1]
    next_ends = wapor_service.dekad_days(wapor_service.dekad_of(ends + timedelta(days=1)))[1]
    return today > next_ends or (soil.first_estimate and (today - ends).days >= wapor_service.SETTLED_AFTER_DAYS)


MADE, NOTHING_NEW, SOIL_AGAIN = "made", "nothing new", "soil moisture read again"
KEPT_OLDER, NOT_WHOLE, NOT_CHECKED, NOT_KEPT, TOO_WIDE = (
    "new pass not read, older view kept", "not whole", "not checked", "not kept", "too wide")
LEFT_FRESH = frozenset({MADE, NOTHING_NEW, SOIL_AGAIN, KEPT_OLDER})  # a fresh view is on the cache volume


def refresh(bbox: Sequence[float], today: date) -> str:
    """Have a fresh view of bbox kept, for the night's run, reading the satellites again only for what is new.

    One search says whether a pass has come since the kept view was made (4 seconds, against the 30 to 60 of reading
    every pass again; measured 2026-11-23). With none, the kept view is stamped fresh as it is, and soil moisture is
    read again only when FAO has published a newer period, or has replaced this one's first estimate.

    Returns which happened. A fresh view is on the cache volume after MADE, NOTHING_NEW, SOIL_AGAIN and KEPT_OLDER
    (a new pass has come but a part could not be read: the kept view stays, and the next night tries again). It is
    not after NOT_WHOLE (no view was kept before and a part could not be had: the first ask tries again),
    NOT_CHECKED (the search failed: the kept view is left as it is), NOT_KEPT (the volume could not be written)
    and TOO_WIDE."""
    key = _key(bbox)
    if _too_wide(key, bbox):
        return TOO_WIDE
    with _views_lock:
        making = _making.setdefault(key, threading.Lock())
    with making:
        old = _kept(key, however_old=True)
        if old is None or old.plants is None or old.soil is None or old.plants.newest_pass is None:
            made, on_disk = _make(key, bbox, today)
            if made.plants is None or made.soil is None:
                return NOT_WHOLE
            return MADE if on_disk else NOT_KEPT
        newest = _newest_pass_since(bbox, old.plants.newest_pass, today)
        if newest is None:
            return NOT_CHECKED
        soil, did = old.soil, NOTHING_NEW
        if newest > old.plants.newest_pass:
            made, on_disk = _make(key, bbox, today)
            if made.plants is not None and made.soil is not None:
                return MADE if on_disk else NOT_KEPT
            did = KEPT_OLDER
        elif _soil_may_be_newer(old.soil, today):
            # FAO's file comes 2 to 6 days after its ten days end. Until it is there, asking which period is the
            # newest is two or three small reads; the centre's year of readings (20 s) is read only for a new period.
            # A period older than the kept one is one file failing to open, not news: the kept reading stays.
            try:
                newest_soil = _newest_soil(bbox, today)
                starts, ends = wapor_service.dekad_days(old.soil.period)
                replaced = old.soil.first_estimate and (today - ends).days >= wapor_service.SETTLED_AFTER_DAYS
                newer = wapor_service.dekad_days(newest_soil[0])[0] > starts
                if newer or (replaced and newest_soil[0] == old.soil.period):
                    soil, did = soil_view(bbox, today, newest_soil), SOIL_AGAIN
            except NoPicture as why:
                logger.warning("satellite view: soil moisture was not read again (%s); the kept reading stays", why)
        again = View(bbox=old.bbox, today=today, plants=old.plants, plants_missing=None, soil=soil, soil_missing=None)
        if not _keep(key, again):
            return NOT_KEPT
        _hold(key, again, _written(key))
        return did


# --- Hexagons to draw -------------------------------------------------------------------------------------------


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


def hexagon_sizes(area_m2: float, square_m: float) -> list[int]:
    """The hexagon sizes (H3 resolutions) a picture of area_m2 measured in squares of square_m is drawn in, widest
    first. The smallest is the finest that still holds HEX_OVER_SQUARE squares; wider ones are added while the
    picture holds HEX_FEWEST of them."""
    def area(size: int) -> float:
        return float(h3.average_hexagon_area(size, unit="m^2"))

    finest = max((size for size in range(16) if area(size) >= HEX_OVER_SQUARE * square_m * square_m), default=0)
    sizes = [finest]
    while len(sizes) < HEX_SIZES and sizes[0] > 0 and area_m2 / area(sizes[0] - 1) >= HEX_FEWEST:
        sizes.insert(0, sizes[0] - 1)
    return sizes


def _inside(cell: int, bbox: Sequence[float]) -> Optional[tuple[dict[str, Any], float, tuple[float, float]]]:
    """A hexagon cut to the photo's frame: (its outline, the share of it left, a point inside what is left), or
    None when nothing is left. A hexagon's value is the mean of the measured ground under it, so it is not drawn
    over ground outside the photo that was not measured."""
    west, south, east, north = bbox
    corners = [(lng, lat) for lat, lng in h3_numbers.cell_to_boundary(cell)]
    middle_lat, middle_lon = h3_numbers.cell_to_latlng(cell)
    share, at = 1.0, (middle_lon, middle_lat)
    if not all(west <= x <= east and south <= y <= north for x, y in corners):
        whole = Polygon(corners)
        left = whole.intersection(box(west, south, east, north))
        if left.is_empty or not isinstance(left, Polygon):  # nothing, or only a point or a line of it
            return None
        share = float(left.area / whole.area)
        corners = list(left.exterior.coords)[:-1]
        if not (west <= middle_lon <= east and south <= middle_lat <= north):
            point = left.representative_point()
            at = (float(point.x), float(point.y))
    ring = [[round(float(x), 7), round(float(y), 7)] for x, y in corners]
    return {"type": "Polygon", "coordinates": [ring + [ring[0]]]}, share, (round(at[0], 7), round(at[1], 7))


def _placed(bbox: Sequence[float], size: int, lon: np.ndarray, lat: np.ndarray, square_at: Callable[[np.ndarray, np.ndarray], np.ndarray]
            ) -> tuple[list[tuple[dict[str, Any], float, tuple[float, float]]], np.ndarray, np.ndarray]:
    """The hexagons of one size that touch the photo's frame, and the squares each holds: (each hexagon's cut to
    the frame, then for every pairing of a square with a hexagon the hexagon's place in that list and the
    square's place among the given middles).

    A square belongs to the hexagon its middle (lon, lat) falls in. The hexagons come from the frame itself, not
    from the squares, so none that touches the photo is missed; one that holds no square's middle (a sliver at the
    frame's edge, or a hexagon between the middles of wide squares) borrows the square under it, which
    square_at(lons, lats) names, -1 for none."""
    west, south, east, north = bbox
    pad = 2 * float(h3.average_hexagon_edge_length(size, unit="m")) / 111_000  # a hexagon's width, in degrees
    around = np.array(sorted(h3_numbers.geo_to_cells(box(west - pad, south - pad, east + pad, north + pad), size)), dtype=np.uint64)
    cuts_around = [_inside(int(cell), bbox) for cell in around]
    touching = np.array([cut is not None for cut in cuts_around], dtype=bool)
    cells, cuts = around[touching], [cut for cut in cuts_around if cut is not None]
    nothing = np.zeros(0, dtype=np.int64)
    if not cuts:
        return [], nothing, nothing
    place_of = h3_numbers.latlng_to_cell
    of_square = np.fromiter((place_of(y, x, size) for x, y in zip(lon.tolist(), lat.tolist())), dtype=np.uint64, count=lon.size)
    place = np.searchsorted(cells, of_square).clip(max=cells.size - 1)
    squares = np.nonzero(cells[place] == of_square)[0]
    place = place[squares]
    empty = np.setdiff1d(np.arange(cells.size), place)
    if empty.size:
        borrowed = square_at(np.array([cuts[i][2][0] for i in empty]), np.array([cuts[i][2][1] for i in empty]))
        place = np.concatenate([place, empty[borrowed >= 0]])
        squares = np.concatenate([squares, borrowed[borrowed >= 0]])
    return cuts, place, squares


def plants_hexagons(plants: Plants, bbox: Sequence[float]) -> dict[str, Any]:
    """The plants picture as hexagons inside the photo's frame: {"geojson", "sizes", "square_m", "main_day",
    "latitude"}.

    Every size covers the whole frame wherever a pass saw the ground. A hexagon says the mean NDVI of the squares
    under it, its class in ndvi_classes' words, the day most of it was seen (with its year: the layer is saved on
    a map and read on later days), whether that is an older look than the photo's main one, and, on the wider
    sizes, its weakest square: the reason to zoom in."""
    count = int((plants.seen > 0).sum())
    side = max(1, int(np.ceil(np.sqrt(count / MAX_SQUARES))))
    ndvi = _blocks(plants.ndvi, side, "mean")
    seen = _blocks(np.where(plants.seen > 0, plants.seen, np.nan), side, "oldest")
    haze = _blocks(plants.hazy, side, "any")
    rows, cols = np.nonzero(np.isfinite(ndvi) & np.isfinite(seen))
    main = plants.main_day
    square_m = SQUARE_M * side
    drawn: dict[str, Any] = {"geojson": {"type": "FeatureCollection", "features": []}, "sizes": [], "square_m": square_m,
                             "main_day": main, "latitude": (bbox[1] + bbox[3]) / 2}
    if rows.size == 0:
        return drawn
    a, _, c, _, e, f = plants.grid.transform  # a north-up grid: the two turning terms are 0
    lon, lat = Transformer.from_crs(plants.grid.crs, "EPSG:4326", always_xy=True).transform(
        a * (cols + 0.5) * side + c, e * (rows + 0.5) * side + f)
    from_wgs84 = Transformer.from_crs("EPSG:4326", plants.grid.crs, always_xy=True)
    index_of = np.full(ndvi.shape, -1, dtype=np.int64)
    index_of[rows, cols] = np.arange(rows.size)

    def square_at(lons: np.ndarray, lats: np.ndarray) -> np.ndarray:
        x, y = from_wgs84.transform(lons, lats)
        col = np.floor((x - c) / (a * side)).astype(np.int64).clip(0, ndvi.shape[1] - 1)
        row = np.floor((y - f) / (e * side)).astype(np.int64).clip(0, ndvi.shape[0] - 1)
        return index_of[row, col]

    values_of, days_of, hazes_of = ndvi[rows, cols].astype(np.float64), seen[rows, cols].astype(np.int64), haze[rows, cols].astype(np.float64)
    distinct = np.unique(days_of)
    drawn["sizes"] = sizes = hexagon_sizes(rows.size * square_m * square_m, square_m)
    features = drawn["geojson"]["features"]
    for size in sizes:
        cuts, place, squares = _placed(bbox, size, lon, lat, square_at)
        values, days, hazes = values_of[squares], days_of[squares], hazes_of[squares]
        held = np.bincount(place, minlength=len(cuts))
        with np.errstate(all="ignore"):
            mean = np.bincount(place, weights=values, minlength=len(cuts)) / held
        weakest = np.full(len(cuts), np.inf)
        np.minimum.at(weakest, place, values)
        votes = np.stack([np.bincount(place, weights=(days == day).astype(np.float64), minlength=len(cuts)) for day in distinct])
        most = distinct[np.argmax(votes, axis=0)]  # a tie goes to the older day
        mixed = votes.max(axis=0) < held
        through_haze = np.bincount(place, weights=hazes, minlength=len(cuts)) > 0
        for i, (outline, _, _) in enumerate(cuts):
            if held[i] == 0:  # no pass saw the ground under it
                continue
            day = date.fromordinal(int(most[i]))
            value = round(float(mean[i]), 2)
            cls = ndvi_classes.ndvi_class(value)
            older = (main - day).days >= OLDER_AFTER_DAYS
            properties: dict[str, Any] = {"plants": ndvi_classes.ground_label(cls) if cls else "", "ndvi": value}
            if size != sizes[-1]:
                properties["weakest"] = round(float(weakest[i]), 2)
            properties.update({"seen_on": f"{day.day} {day:%b %Y}", "older": bool(older), "h3_resolution": size})
            notes = (["An older look than most of the photo"] if older else []) + (
                ["Parts of it were seen on other days"] if mixed[i] else []) + (
                ["Seen through haze: may read low"] if through_haze[i] else [])
            if notes:
                properties["note"] = "; ".join(notes)
            features.append({"type": "Feature", "properties": properties, "geometry": outline})
    return drawn


def period_words(period: str, today: Optional[date] = None) -> str:
    """The ten days in words, "1-10 Nov"; with the year when it is not today's, or when no today is given."""
    starts, ends = wapor_service.dekad_days(period)
    year = "" if today is not None and ends.year == today.year else f" {ends.year}"
    return f"{starts.day}-{ends.day} {ends:%b}{year}"


def soil_band(percent: float) -> tuple[str, str]:
    """(the band's words, its colour) for a soil moisture reading."""
    index = max((i for i, (low, _) in enumerate(SOIL_BANDS) if percent >= low), default=0)
    low, colour = SOIL_BANDS[index]
    if index == 0:
        return f"Below {SOIL_BANDS[1][0]:g}%", colour
    if index == len(SOIL_BANDS) - 1:
        return f"{low:g}% and above", colour
    return f"{low:g}-{SOIL_BANDS[index + 1][0]:g}%", colour


def soil_hexagons(soil: Soil, bbox: Sequence[float]) -> dict[str, Any]:
    """Soil moisture as hexagons inside the photo's frame: {"geojson", "sizes", "latitude"}. Each hexagon says the
    mean reading of the squares under it and the ten days it is for. With each comes a point carrying the same
    facts, which is where the map writes the reading (a hexagon cut by a tile's edge would otherwise be labelled
    twice); a hexagon with less than READING_NEEDS of itself inside the frame gets none."""
    rows, cols = np.nonzero(np.isfinite(soil.percent))
    drawn: dict[str, Any] = {"geojson": {"type": "FeatureCollection", "features": []}, "sizes": [], "latitude": (bbox[1] + bbox[3]) / 2}
    if rows.size == 0:
        return drawn
    lon, lat = soil.west + (cols + 0.5) * soil.step, soil.north - (rows + 0.5) * soil.step
    index_of = np.full(soil.percent.shape, -1, dtype=np.int64)
    index_of[rows, cols] = np.arange(rows.size)

    def square_at(lons: np.ndarray, lats: np.ndarray) -> np.ndarray:
        col = np.floor((lons - soil.west) / soil.step).astype(np.int64).clip(0, soil.percent.shape[1] - 1)
        row = np.floor((soil.north - lats) / soil.step).astype(np.int64).clip(0, soil.percent.shape[0] - 1)
        return index_of[row, col]

    values_of = soil.percent[rows, cols].astype(np.float64)
    words = period_words(soil.period)
    drawn["sizes"] = sizes = hexagon_sizes(rows.size * SOIL_SQUARE_M * SOIL_SQUARE_M, SOIL_SQUARE_M)
    features = drawn["geojson"]["features"]
    for size in sizes:
        cuts, place, squares = _placed(bbox, size, lon, lat, square_at)
        held = np.bincount(place, minlength=len(cuts))
        with np.errstate(all="ignore"):
            mean = np.bincount(place, weights=values_of[squares], minlength=len(cuts)) / held
        for i, (outline, share, at) in enumerate(cuts):
            if held[i] == 0:  # FAO's file holds no value under it
                continue
            value = round(float(mean[i]), 1)
            properties: dict[str, Any] = {"moisture": value, "band": soil_band(value)[0], "period": words, "h3_resolution": size}
            if soil.first_estimate:
                properties["note"] = "FAO's first estimate"
            features.append({"type": "Feature", "properties": properties, "geometry": outline})
            if share >= READING_NEEDS:
                features.append({"type": "Feature", "properties": {**properties, "reading": f"{value:.0f}%"},
                                 "geometry": {"type": "Point", "coordinates": list(at)}})
    return drawn


# --- One field of the photo -----------------------------------------------------------------------------------

# Whole squares a field must hold before its NDVI is said. A square on the field's edge takes in the neighbour's crop,
# the path or the hedge, so only squares lying wholly inside the outline count; and Sentinel-2 places a square within
# a few metres, not to the metre, so one or two squares can sit half on the neighbour without our knowing. With four
# the middle value holds.
FIELD_SQUARES = 4


@dataclass(frozen=True)
class FieldSeen:
    """What the kept view holds for one field's outline."""

    whole: int  # 10 m squares lying wholly inside the field
    squares: int  # of those, the ones seen sharp on the field's main day: what the figure is made of
    ndvi: Optional[float]  # the middle of those squares; None when there are fewer than FIELD_SQUARES
    day: Optional[date]  # the day most of the field's whole squares were seen
    older: bool  # that day is an older look than the photo's main one
    soil: Optional[float]  # soil moisture of the 100 m square under the field's middle
    period: Optional[str]  # the ten days that reading is for


def _whole_squares(outline: Any, grid: Grid) -> np.ndarray:
    """The grid's squares that lie wholly inside an outline given in the grid's own coordinates: exact geometry on the
    squares around the outline, not a rule of thumb (a square's middle well inside the line misses squares that lie
    wholly inside along a straight edge: 35 of 69 Cyampirita plots lost squares that way, measured 2026-11-23)."""
    inside = np.zeros(grid.shape, dtype=bool)
    a, _, c, _, e, f = grid.transform  # a square is a wide and -e tall; (c, f) is the grid's top-left corner
    minx, miny, maxx, maxy = outline.bounds
    left, right = max(0, int((minx - c) // a)), min(grid.shape[1], int((maxx - c) // a) + 1)
    top, bottom = max(0, int((f - maxy) // -e)), min(grid.shape[0], int((f - miny) // -e) + 1)
    if left >= right or top >= bottom:
        return inside
    cols, rows = np.meshgrid(np.arange(left, right), np.arange(top, bottom))
    squares = shapely.box(c + cols * a, f + (rows + 1) * e, c + (cols + 1) * a, f + rows * e)
    shapely.prepare(outline)
    inside[top:bottom, left:right] = shapely.contains(outline, squares)
    return inside


def field_seen(seen: View, geometry: dict[str, Any], to_grid: Optional[Callable[..., Any]] = None) -> Optional[FieldSeen]:
    """The view's values for one field (a GeoJSON outline in latitude and longitude), or None when the outline
    cannot be read or the view holds nothing for it. No satellite is read: the squares come from the kept view.
    to_grid: longitude and latitude to the plants grid's coordinates, when the caller has it (of_fields, for
    hundreds of plots)."""
    try:
        outline = make_valid(shape(geometry))
    except Exception:  # noqa: BLE001 - a broken outline has no satellite figure; the field is still shown
        logger.warning("satellite view: a field's outline could not be read", exc_info=True)
        return None
    if outline.is_empty:
        return None
    whole_squares, squares, ndvi, day, older = 0, 0, None, None, False
    plants = seen.plants
    if plants is not None:
        to_grid = to_grid or Transformer.from_crs("EPSG:4326", plants.grid.crs, always_xy=True).transform
        inside = _whole_squares(shapely_transform(to_grid, outline), plants.grid)
        whole_squares = int(inside.sum())
        sharp = inside & (plants.seen > 0) & ~plants.hazy & np.isfinite(plants.ndvi)
        if sharp.any():
            days, counts = np.unique(plants.seen[sharp], return_counts=True)
            main = int(days[int(np.argmax(counts))])
            on_day = sharp & (plants.seen == main)
            squares, day = int(on_day.sum()), date.fromordinal(main)
            older = main <= plants.main_day.toordinal() - OLDER_AFTER_DAYS
            if squares >= FIELD_SQUARES:
                ndvi = float(np.median(plants.ndvi[on_day]))
    soil, period = None, None
    if seen.soil is not None:
        middle = outline.representative_point()
        col = int((middle.x - seen.soil.west) // seen.soil.step)
        row = int((seen.soil.north - middle.y) // seen.soil.step)
        rows, cols = seen.soil.percent.shape
        if 0 <= row < rows and 0 <= col < cols and np.isfinite(seen.soil.percent[row, col]):
            soil, period = float(seen.soil.percent[row, col]), seen.soil.period
    if plants is None and soil is None:
        return None
    return FieldSeen(whole=whole_squares, squares=squares, ndvi=ndvi, day=day, older=older, soil=soil, period=period)


def field_words(field: FieldSeen, today: date, plants_held: bool = True) -> tuple[Optional[str], Optional[str]]:
    """(the field's short part, its sentences) as every surface says them: "satellite NDVI 0.52 on 19 Nov" for the
    one line a field has, and the sentences for where there is room. The short part is None when the field has no
    figure of its own; the sentences then say why. plants_held: whether the view holds a plants picture at all."""
    short, sentences = None, []
    if field.ndvi is not None and field.day is not None:
        when = _day_words(field.day, today)
        short = f"satellite NDVI {field.ndvi:.2f} on {when}" + (", an older look" if field.older else "")
        made_of = (f"the {field.squares} whole {SQUARE_M} m squares inside this field" if field.squares == field.whole
                   else f"{field.squares} of the {field.whole} whole {SQUARE_M} m squares inside this field (the others "
                        "had no clear look that day)")
        sentences.append(
            f"From the satellite (Sentinel-2): NDVI {field.ndvi:.2f} on {when}, the middle of {made_of}."
            + (" That is weeks older than the look most of the photo has, so do not read it against fields seen later."
               if field.older else ""))
    elif plants_held and field.whole < FIELD_SQUARES:
        holds = "none" if field.whole == 0 else str(field.whole)
        sentences.append(f"From the satellite: this field holds {holds} of the satellite's {SQUARE_M} m squares whole, "
                         f"too few for an NDVI of its own (it takes {FIELD_SQUARES}); a square on its edge takes in "
                         "the neighbours.")
    elif plants_held:
        sentences.append(f"From the satellite: no NDVI for this field yet; too few of its {SQUARE_M} m squares had a "
                         "clear look on one day.")
    if field.soil is not None and field.period is not None:
        sentences.append(f"Soil moisture {field.soil:.0f}% for {period_words(field.period, today)} in the "
                         f"{SOIL_SQUARE_M} m square under its middle (FAO estimate): the season's level, not this "
                         "field's own reading.")
    return short, " ".join(sentences) or None


_fields_said: dict[tuple[float, ...], tuple[float, dict[str, Optional[tuple[Optional[str], Optional[str]]]]]] = {}


def of_fields(bbox: Sequence[float], outlines: dict[str, dict[str, Any]]) -> dict[str, tuple[Optional[str], Optional[str]]]:
    """key -> (short part, sentences) for each field outline of the photo at bbox, from the view already kept.
    {} when no fresh view is kept: nothing is read from a satellite here, and nothing waits. What was said of an
    outline is remembered for as long as the same view is held (445 plots took 0.56 s in the local container,
    2026-11-23, and a photo's cards ask on every open); keys must name the outline (field_health.key_of)."""
    key = _key(bbox)
    seen, stamp = _ready(key)
    if seen is None:
        return {}
    with _views_lock:
        remembered = _fields_said.get(key)
        if remembered is None or remembered[0] != stamp:
            remembered = (stamp, {})
            _fields_said[key] = remembered
            for gone in [k for k in _fields_said if k not in _views and k != key]:
                del _fields_said[gone]
    said = remembered[1]
    to_grid = (Transformer.from_crs("EPSG:4326", seen.plants.grid.crs, always_xy=True).transform
               if seen.plants is not None else None)
    out: dict[str, tuple[Optional[str], Optional[str]]] = {}
    for name, geometry in outlines.items():
        if name not in said:
            try:
                field = field_seen(seen, geometry, to_grid)
                said[name] = (field_words(field, seen.today, plants_held=seen.plants is not None)
                              if field is not None else None)
            except Exception:  # noqa: BLE001 - one outline that cannot be worked out loses its own words, not every field's
                logger.warning("satellite view: a field's figure could not be worked out", exc_info=True)
                said[name] = None
        words_of = said[name]
        if words_of is not None:
            out[name] = words_of
    return out


# --- What the two show, in plain facts ----------------------------------------------------------------------


def _percent(share: float) -> str:
    return "under 1%" if 0 < share < 0.005 else f"{share:.0%}"


def _list(parts: Sequence[str]) -> str:
    return parts[0] if len(parts) == 1 else ", ".join(parts[:-1]) + " and " + parts[-1]


def middle_ndvi(plants: Plants) -> float:
    """The NDVI of the photo's middle square: the median of every square with a clear look. The sentences and the
    map's key say this one number."""
    return float(np.median(plants.ndvi[plants.seen > 0]))


def soil_spread(soil: Soil) -> tuple[float, float, float]:
    """(middle, lowest, highest) soil moisture across the photo's squares, in percent."""
    values = soil.percent[np.isfinite(soil.percent)]
    return float(np.median(values)), float(values.min()), float(values.max())


def soil_year_place(soil: Soil) -> Optional[str]:
    """Where the reading at the photo's centre sits in its own last year: "low", "middle" or "high" (thirds of the
    year's range); None when the year is not known."""
    if soil.centre is None or soil.year_low is None or soil.year_high is None or soil.year_high <= soil.year_low:
        return None
    place = (soil.centre - soil.year_low) / (soil.year_high - soil.year_low)
    return "low" if place < 1 / 3 else ("high" if place > 2 / 3 else "middle")


YEAR_PLACE_WORDS = {"low": "near the bottom", "middle": "in the middle", "high": "near the top"}


def plants_facts(plants: Plants, today: date) -> dict[str, Any]:
    """The plants picture in numbers and sentences: {"sentences", "parts", "care", "direction", "median", "classes",
    "seen_said", "days"}. "seen_said" is each day's share in words, largest first ("66% on 19 Nov"), as the "seen" sentence
    lists them. "parts" names each sentence ("covered", "seen", "older", "newer", "haze", "greenness", "change"; only those
    said), so a surface with little room can say some of them in the same words. "care" is the sentences among them a
    reader must not miss even in a short answer: ground no pass saw, a look weeks older or newer than the main one, haze.
    "short" is "greenness" and "change" as one line, in the same numbers."""
    known = plants.seen > 0
    total = int(known.sum())
    days, counts = np.unique(plants.seen[known], return_counts=True)
    by_day = sorted(((date.fromordinal(int(d)), int(n) / plants.seen.size) for d, n in zip(days, counts)),
                    key=lambda pair: pair[1], reverse=True)
    parts: dict[str, str] = {}
    if plants.covered >= 0.999:
        parts["covered"] = f"Every {SQUARE_M} m square of this photo has a clear satellite look (Sentinel-2)."
    else:
        parts["covered"] = (f"{_percent(plants.covered)} of this photo has a clear satellite look (Sentinel-2); no pass "
                            f"saw the rest in the last {LOOKBACKS[-1]} days.")
    shown = [f"{_percent(share)} on {_day_words(day, today)}" for day, share in by_day[:4]]
    parts["seen"] = f"Seen: {_list(shown)}" + ("; the rest earlier." if len(by_day) > 4 else ".")
    main = plants.main_day
    old = float(((plants.seen > 0) & (plants.seen <= main.toordinal() - OLDER_AFTER_DAYS)).mean())
    if old >= 0.05:
        parts["older"] = (f"{_percent(old)} of the photo holds an older look, drawn in grey: clouds hid it on "
                          f"{_day_words(main, today)}, so it must not be read against the fresh part.")
    # The main look can itself be the older one: at 5 of 11 other farming places (2026-11-23) a newer look, two weeks
    # or more after the main one, covered 6% to 40% of the box. It is drawn in the legend's colours beside the main
    # look, so the words must say that the two parts are weeks apart.
    later = plants.seen >= main.toordinal() + OLDER_AFTER_DAYS
    if float(later.mean()) >= 0.05:
        newest = date.fromordinal(int(plants.seen[later].max()))
        parts["newer"] = (f"{_percent(float(later.mean()))} of the photo holds a newer look (up to {_day_words(newest, today)}) "
                          f"than the main one of {_day_words(main, today)}: a difference between the two parts can be "
                          "the weeks between them, not the crop.")
    haze_share = float(plants.hazy.mean())
    if haze_share >= 0.01:
        parts["haze"] = f"{_percent(haze_share)} was only seen through haze and may read low."
    values = plants.ndvi[known]
    classes = []
    for i, cls in enumerate(ndvi_classes.NDVI_CLASSES):
        upper = ndvi_classes.NDVI_CLASSES[i + 1].min_ndvi if i + 1 < len(ndvi_classes.NDVI_CLASSES) else np.inf
        share = float(((values >= cls.min_ndvi) & (values < upper)).sum() / total)
        classes.append({"key": cls.key, "label": ndvi_classes.ground_label(cls), "share": share})
    said = [f"{_percent(c['share'])} {c['label'].lower()}" for c in reversed(classes) if c["share"] > 0]
    median = middle_ndvi(plants)
    parts["greenness"] = f"By greenness (NDVI): {_list(said)}. The middle square reads {median:.2f}."
    direction = None
    short = f"NDVI {median:.2f} in the middle square"
    if plants.change is not None:
        moved = plants.change.median
        direction = "steady" if abs(moved) < STEADY else ("rose" if moved > 0 else "fell")
        how = {"steady": f"stayed about the same ({moved:+.2f})", "rose": f"rose by {moved:.2f}",
               "fell": f"fell by {abs(moved):.2f}"}[direction]
        parts["change"] = (f"On the squares seen {_day_words(plants.change.now, today)}, NDVI {how} since "
                           f"{_day_words(plants.change.before, today)}.")
        short += f"; it {how} since {_day_words(plants.change.before, today)}"
    # the shares are the whole photo's, so they are said apart from the middle square's figure
    short += f". Across the photo: {_list(said)}"
    care = [parts[name] for name in ("older", "newer", "haze") if name in parts]
    if plants.covered < 0.999:
        care.insert(0, parts["covered"])
    return {"sentences": list(parts.values()), "parts": parts, "short": short + ".", "care": care, "direction": direction,
            "median": round(median, 2),
            "classes": classes, "seen_said": shown,
            "days": [{"day": day.isoformat(), "share": round(share, 3)} for day, share in by_day]}


def soil_facts(soil: Soil, today: date) -> dict[str, Any]:
    """The soil moisture reading in numbers and sentences: {"sentences", "parts", "care", "position", "median", "low",
    "high"}. "parts" names each sentence ("reading", "year", "first", "limits"; only those said). "care" is "first" and
    "limits" in one short sentence, for an answer with little room, and "short" is "reading" and "year" as one line, in the
    same numbers."""
    median, low, high = soil_spread(soil)
    words = period_words(soil.period, today)
    parts = {"reading": f"Soil moisture for {words} (FAO WaPOR estimate, {SOIL_SQUARE_M} m squares): the middle square "
                        f"reads {median:.0f}%, from {low:.0f}% to {high:.0f}% across the photo."}
    position = soil_year_place(soil)
    short = f"{median:.0f}% for {words}, from {low:.0f}% to {high:.0f}% across the photo"
    if position is not None:
        where = YEAR_PLACE_WORDS[position]
        short += f"; {where} of its own year"
        parts["year"] = (f"At the photo's centre it reads {soil.centre:.0f}%; over the year before it ran from "
                         f"{soil.year_low:.0f}% to {soil.year_high:.0f}%, so it is {where} of its own year.")
    if soil.first_estimate:
        parts["first"] = "This is FAO's first estimate; FAO replaces it with a steadier one about two months later."
    parts["limits"] = ("It follows Rwanda's wet and dry seasons but was not confirmed against rain week by week "
                       "(tested 2026-11-23): read it as the season's level, not a precise reading.")
    care = ("Soil moisture is FAO's first estimate: read it as the season's level, not a precise reading."
            if soil.first_estimate else "Read soil moisture as the season's level, not a precise reading.")
    return {"sentences": list(parts.values()), "parts": parts, "short": short + ".", "care": care, "position": position,
            "median": round(median, 1),
            "low": round(low, 1), "high": round(high, 1)}


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
