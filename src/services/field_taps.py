"""Field outlines from taps on a drone photo: tap a field, get up to three outlines, pick one, correct it with up
to two more taps ("this part too" / "not this part"), keep it. Switch FIELD_TAPS (off by default).

How it works. Segment Anything (ViT-H) has a heavy half that reads a picture once and a light half that answers a
tap. The heavy half never runs in the app: `scripts/prepare_field_taps.py` runs it on our own machine when we upload
a photo, over a fixed grid of windows (WINDOW_M wide, read at WINDOW_PX, centres every STEP_M from the photo's
top-left corner), and stores one array per window plus a manifest. The app runs only the light half (one ONNX file,
checked by sha256): about half a second for a first tap on the server, of which the model is 0.18 s and cleaning
and shaping three outlines the rest (measured 2026-11-12/13, one thread, throw-away container).

Rules (as measured; plans/PLAN_RWANDA_CROPS.md, Tests 67-70 and the server timing):
* The first tap must fall on a prepared window; the nearest window centre is used. Later taps must fall inside that
  same window: the model cannot see beyond it.
* One tap gives the model's three outlines. Each is cleaned: only the connected part under the first tap, holes
  filled. An outline with nothing under the tap is dropped.
* A correcting tap needs the picked outline. The model is asked again with every tap so far and the outline before
  it, and gives one outline. At most MAX_CORRECTIONS, the number measured.
* Kept outlines are computed again here from the taps; a shape sent by a browser is never stored.
* Areas are hectares on the ellipsoid (`ground_area`), never map units.

What was measured, and what was not, is in MEASURED: the screens show that text, they do not write their own.
"""

from __future__ import annotations

import asyncio
import hashlib
import io
import json
import logging
import math
import os
import threading
import time
import uuid
from collections import OrderedDict
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Optional, Sequence

import numpy as np
from affine import Affine
from pyproj import Transformer
import rasterio
from rasterio.features import geometry_mask, shapes
from rasterio.windows import from_bounds
from scipy import ndimage
from shapely.errors import ShapelyError
from shapely.geometry import Point, Polygon, mapping, shape
from shapely.ops import transform as reproject

from src.services import ground_area, stored_files
from src.services.gdal_http import GDAL_HTTP_TIMEOUTS

logger = logging.getLogger(__name__)

WINDOW_M = 120.0
WINDOW_PX = 1024
STEP_M = 60.0
MAX_CORRECTIONS = 2
MAX_KEPT_FIELDS = 500  # kept fields on one photo: each is a plot the cards and Sage measure
MAX_LINE_REACH_M = 500  # no corner of a hand-moved line may lie further than this from the first tap
MAX_MOVED_HA = 2.0  # a hand-moved outline larger than this is refused: about three times the largest field measured
MAX_TURNED_DOWN_ROWS = 2000  # turned-down taps kept in full; older ones stay in the count only
MAX_LINE_POINTS = 600  # a hand-moved line with more points than this is refused
SAME_OUTLINE = 0.9  # two choices that overlap this much are one outline shown twice; the later one is dropped
EDGE_CHOICE_M = 120.0  # a first-tap choice with this much of itself on the window's border is left out (Test 76)
SIMPLIFY_M = 0.25  # about two pixels of a window: corners stay, the staircase of pixels goes
PREPARE_MODEL = "sam_vit_h_4b8939"  # the heavy half the stored windows must come from

TAP_MODEL_KEY = "models/sam-vit-h-taps-c2cef977127da4a3.onnx"
TAP_MODEL_SHA256 = "c2cef977127da4a396b603e859b2ea00506de9af50cdf0ebcd01af73aeb62ab1"
_TAP_MODEL_PATH = "/tmp/ingabe_cache/models/sam-vit-h-taps-c2cef977127da4a3.onnx"
_STORE_PREFIX = "field_taps/v1"
_KEPT_WINDOWS = 12  # stored windows held in memory (2 MB each)

MEASURED = (
    "Measured on one photo (Cyampirita, 26 fields): one tap and up to two corrections gave a usable outline on 25 "
    "fields. Against an outline drawn by hand, the area of a corrected outline was off by about 6% at the median "
    "and within 5% on 11 of 26 fields; without a correction it was within 5% on 9 of 20. In that test a script "
    "placed the taps; taps by a person are not measured yet. Corners were not measured. A line you move by hand "
    "is your own: it was not part of that test. Areas are map areas: on a slope the ground itself is larger."
)


class TapError(ValueError):
    """The taps cannot be answered as sent; the message says why in plain words."""


def enabled() -> bool:
    """FIELD_TAPS: 1/true/yes turns field outlines from taps on (default off: its routes answer 404)."""
    return os.environ.get("FIELD_TAPS", "0").strip().lower() in {"1", "true", "yes"}


@dataclass(frozen=True)
class Tap:
    lon: float
    lat: float
    add: bool = True  # False: "not this part"


@dataclass(frozen=True)
class Grid:
    """The prepared windows of one photo. Window (i, j) is centred STEP_M * i east and STEP_M * j south of the
    photo's top-left corner, in the photo's own coordinates (metres)."""

    crs: str
    origin_x: float
    origin_y: float
    windows: frozenset[tuple[int, int]]

    def centre(self, window: tuple[int, int]) -> tuple[float, float]:
        return self.origin_x + STEP_M * window[0], self.origin_y - STEP_M * window[1]

    def transform(self, window: tuple[int, int]) -> Affine:
        """Window pixels to the photo's coordinates."""
        cx, cy = self.centre(window)
        size = WINDOW_M / WINDOW_PX
        return Affine(size, 0, cx - WINDOW_M / 2, 0, -size, cy + WINDOW_M / 2)

    def pixel(self, window: tuple[int, int], x: float, y: float) -> tuple[float, float]:
        col, row = ~self.transform(window) * (x, y)
        return float(col), float(row)


def manifest(crs: str, origin_x: float, origin_y: float, windows: Sequence[tuple[int, int]]) -> dict[str, Any]:
    """The record the prepare script stores beside a photo's windows."""
    return {"version": 1, "model": PREPARE_MODEL, "window_m": WINDOW_M, "window_px": WINDOW_PX, "step_m": STEP_M,
            "crs": crs, "origin": [origin_x, origin_y], "windows": [list(w) for w in windows],
            "prepared_at": datetime.now(timezone.utc).isoformat(timespec="seconds")}


def grid_of(record: dict[str, Any]) -> Grid:
    """The grid a stored manifest describes. ValueError when it was prepared with other settings than this module
    answers taps for: outlines from a different grid or model were never measured."""
    expected = {"version": 1, "model": PREPARE_MODEL, "window_m": WINDOW_M, "window_px": WINDOW_PX, "step_m": STEP_M}
    for name, value in expected.items():
        if record.get(name) != value:
            raise ValueError(f"this photo's windows were prepared with {name}={record.get(name)!r}, not {value!r}")
    return Grid(crs=str(record["crs"]), origin_x=float(record["origin"][0]), origin_y=float(record["origin"][1]),
                windows=frozenset((int(i), int(j)) for i, j in record["windows"]))


def candidate_windows(left: float, bottom: float, right: float, top: float) -> list[tuple[int, int]]:
    """Every grid window that touches a photo with these bounds (its own coordinates, metres), row by row."""
    columns = range(0, int(np.floor((right - left) / STEP_M)) + 1)
    rows = range(0, int(np.floor((top - bottom) / STEP_M)) + 1)
    return [(i, j) for j in rows for i in columns]


def place(grid: Grid, taps: Sequence[Tap]) -> tuple[tuple[int, int], list[tuple[float, float, int]]]:
    """The window that answers these taps, and each tap as (column, row, 1 add / 0 remove) in that window."""
    if not taps:
        raise TapError("Tap a field first.")
    if not taps[0].add:
        raise TapError("The first tap says where the field is; it cannot be a 'not this part' tap.")
    if len(taps) > 1 + MAX_CORRECTIONS:
        raise TapError(f"At most {MAX_CORRECTIONS} corrections were measured. Start again with a new tap.")
    to_photo = Transformer.from_crs("EPSG:4326", grid.crs, always_xy=True).transform
    points = [to_photo(t.lon, t.lat) for t in taps]
    x, y = points[0]
    window = (round((x - grid.origin_x) / STEP_M), round((grid.origin_y - y) / STEP_M))
    if window not in grid.windows:
        raise TapError("This part of the photo is not prepared for outlines.")
    placed = []
    for (px, py), tap in zip((grid.pixel(window, *p) for p in points), taps):
        if not (0 <= px < WINDOW_PX and 0 <= py < WINDOW_PX):
            raise TapError("That tap is too far from the first one. Tap closer to the field, or start again.")
        placed.append((px, py, 1 if tap.add else 0))
    return window, placed


class TapModel:
    """The light half of Segment Anything as exported by segment_anything's SamOnnxModel (all four outlines)."""

    def __init__(self, path: str) -> None:
        import onnxruntime  # lazy: loaded only when the first tap arrives

        options = onnxruntime.SessionOptions()
        options.intra_op_num_threads = 1  # measured on the server: four threads were no faster and less steady
        self._session = onnxruntime.InferenceSession(path, options, providers=["CPUExecutionProvider"])

    def ask(self, window_array: np.ndarray, taps: Sequence[tuple[float, float, int]],
            before: Optional[np.ndarray]) -> tuple[np.ndarray, np.ndarray]:
        """Four outlines (bool, WINDOW_PX square; the first is the single answer, the other three the choices) and
        their four low-resolution forms, one of which is handed back as `before` with the next tap."""
        # the export expects one closing point labelled -1 when no box is given
        points = np.array([[x, y] for x, y, _ in taps] + [[0.0, 0.0]], np.float32)[None]
        labels = np.array([label for _, _, label in taps] + [-1], np.float32)[None]
        masks, _, low = self._session.run(None, {
            "image_embeddings": window_array.astype(np.float32),
            "point_coords": points,
            "point_labels": labels,
            "mask_input": (before[None, None].astype(np.float32) if before is not None
                           else np.zeros((1, 1, 256, 256), np.float32)),
            "has_mask_input": np.array([0.0 if before is None else 1.0], np.float32),
            "orig_im_size": np.array([WINDOW_PX, WINDOW_PX], np.float32),
        })
        return masks[0] > 0, low[0]


def _part_under(mask: np.ndarray, col: float, row: float) -> Optional[np.ndarray]:
    """The connected part of an outline under the first tap, holes filled; None when the tap is not on it."""
    labelled, _ = ndimage.label(mask)
    # the nearest pixel, as measured: a small field can be lost by one; a tap on the very last half pixel stays inside
    r, c = min(int(round(row)), mask.shape[0] - 1), min(int(round(col)), mask.shape[1] - 1)
    part = labelled[r, c]
    if part == 0:
        return None
    return ndimage.binary_fill_holes(labelled == part)


def _distinct(choices: list[tuple[int, np.ndarray]]) -> list[tuple[int, np.ndarray]]:
    """The choices without repeats: a choice that covers nearly the same ground as an earlier one is dropped.
    No choice is dropped for being small: the smallest fields drawn by hand on Cyampirita are under 0.01 ha."""
    kept: list[tuple[int, np.ndarray]] = []
    for choice, mask in choices:
        repeat = any((mask & other).sum() >= SAME_OUTLINE * (mask | other).sum() for _, other in kept)
        if not repeat:
            kept.append((choice, mask))
    return kept


def on_the_border_m(mask: np.ndarray) -> float:
    """How many metres of an outline lie on the outermost pixels of its window: there the window cut it, not the
    ground."""
    on_border = int(mask[0].sum()) + int(mask[-1].sum()) + int(mask[:, 0].sum()) + int(mask[:, -1].sum())
    return on_border * WINDOW_M / mask.shape[0]


def without_edge_choices(choices: list[tuple[int, np.ndarray]]) -> tuple[list[tuple[int, np.ndarray]], int]:
    """The first-tap choices without those that run along the window's border, and how many were left out. Measured
    on Cyampirita (Test 76): such a choice was a usable outline 0 times in 12 on the exam fields and once in 15 on
    the tuning fields. A rule for plots smaller than a window, so nothing is left out when no other choice remains."""
    inside = [(choice, mask) for choice, mask in choices if on_the_border_m(mask) < EDGE_CHOICE_M]
    return (inside, len(choices) - len(inside)) if inside else (choices, 0)


def outline_masks(model: TapModel, window_array: np.ndarray, taps: Sequence[tuple[float, float, int]],
                  pick: Optional[int]) -> list[tuple[int, np.ndarray]]:
    """The outlines for these taps as (choice number, mask). One tap: the model's three choices (0, 1, 2), fewer
    when a choice has nothing under the tap. More taps: the picked choice corrected tap by tap, one outline."""
    col, row = taps[0][0], taps[0][1]
    masks, low = model.ask(window_array, taps[:1], None)
    if len(taps) == 1:
        cleaned = [(choice, _part_under(masks[choice + 1], col, row)) for choice in range(3)]
        return _distinct([(choice, mask) for choice, mask in cleaned if mask is not None])
    if pick not in (0, 1, 2):
        raise TapError("Pick one of the outlines before correcting it.")
    first = _distinct([(c, m) for c, m in ((c, _part_under(masks[c + 1], col, row)) for c in range(3)) if m is not None])
    if pick not in [c for c, _ in without_edge_choices(first)[0]]:
        raise TapError("That outline was not offered for this tap. Tap the field again.")
    before = low[pick + 1]
    mask = masks[pick + 1]
    for count in range(2, len(taps) + 1):
        masks, low = model.ask(window_array, taps[:count], before)
        mask, before = masks[0], low[0]
    cleaned_mask = _part_under(mask, col, row)
    return [] if cleaned_mask is None else [(pick, cleaned_mask)]


def outline_shape(grid: Grid, window: tuple[int, int], mask: np.ndarray) -> Optional[dict[str, Any]]:
    """An outline as a GeoJSON polygon in longitude and latitude with its area; None for an empty mask."""
    found = [shape(geometry) for geometry, value in
             shapes(mask.astype(np.uint8), mask=mask, transform=grid.transform(window)) if value == 1]
    if not found:
        return None
    polygon = max(found, key=lambda g: g.area).simplify(SIMPLIFY_M)
    lonlat = reproject(Transformer.from_crs(grid.crs, "EPSG:4326", always_xy=True).transform, polygon)
    return {"geometry": mapping(lonlat), "area_ha": round(ground_area.area_ha(lonlat), 3)}


# Green cover: the share of a kept field's ground covered by living green plants of any kind, crop and weeds alike
# (colour cannot tell them apart). The rule is the one Tests 77 and 78 measured, unchanged: excess green 2g - r - b
# (each 0..1) of the 3 x 3 pixel mean above GREEN_THRESHOLD, counted 0.5 m in from the field's line. It is only
# given where it was measured: on photos the rule was checked on (cover_checked), for fields of COVER_MIN_HA to
# COVER_MAX_HA no wider than COVER_MAX_SPAN_M, on photos with pixels of COVER_PIXEL_M.
GREEN_THRESHOLD = 0.15
COVER_INSET_M = 0.5
COVER_MIN_HA, COVER_MAX_HA = 0.007, 0.7  # the drawn fields it was measured on: 0.0077 to 0.68 ha
COVER_MAX_SPAN_M = 150.0  # the box around a field that is read: a thin outline under 0.7 ha can still span a kilometre
COVER_PIXEL_M = (0.025, 0.045)  # the test photo's pixels are 3.25 cm
COVER_BATCH = 20  # fields measured between two writes of the record, in the background
COVER_RETRY_S = 300  # after a photo could not be read, wait this long before trying again
COVER_KEEP_WAIT_S = 2.0  # a keep waits this long for its turn to read the photo; past it, the background job measures
COVER_NOT_CHECKED = (
    "Not measured on this photo yet: the colour rule was checked on one photo, and how green leaves look depends "
    "on the camera and the day."
)
COVER_MEASURED = (
    "Green cover is the share of the field's ground under living green plants of any kind, crop and weeds alike: "
    "the colour of a photo cannot tell them apart. Measured on one photo (Cyampirita, 26 fields) against careful "
    "readers of 40 points per field: within about 3 points for a typical field, and within 10 points on 20 of 24 "
    "fields. Least sure on sparse, pale or blurred ground, where even careful readers could not tell plant from "
    "soil. It is a reading of the day the photo was taken, not a yield. It is given only on photos the rule was "
    "checked on: on another photo with paler colour the same rule found almost no green where plants stood."
)


def _cover_from_pixels(rgb: np.ndarray, valid: np.ndarray) -> Optional[float]:
    """The share of `valid` pixels whose 3 x 3 mean colour is green by the measured rule; None when none is valid.
    `rgb` is (3, rows, cols) of 0..255."""
    if not valid.any():
        return None
    # excess green of each pixel first, in whole numbers, then its 3 x 3 mean: the mean of a sum is the sum of the
    # means, so this is the measured rule with a third of the memory (one band is filtered, not three)
    excess = 2 * rgb[1].astype(np.int16) - rgb[0] - rgb[2]
    mean = ndimage.uniform_filter(excess.astype(np.float32), size=3, mode="nearest")
    return float((mean[valid] > GREEN_THRESHOLD * 255.0).mean())


def _not_measured(why: str) -> dict[str, Any]:
    return {"share": None, "why_not": why}


def _cover_by_size(area_ha: float) -> Optional[dict[str, Any]]:
    """Why a field of this area gets no green cover, before the photo is opened; None when its size was measured."""
    if area_ha > COVER_MAX_HA:
        return _not_measured(f"Not measured for fields over {COVER_MAX_HA} ha: it was tested on smaller fields.")
    if area_ha < COVER_MIN_HA:
        return _not_measured("Not measured for a field this small.")
    return None


def _cover_on(ds: Any, geometry: dict[str, Any], area_ha: float) -> dict[str, Any]:
    """Green cover of one field on an open photo; see green_cover. Nothing is read before every limit is checked:
    the box around the field decides how many pixels are read, not its area."""
    by_size = _cover_by_size(area_ha)
    if by_size is not None:
        return by_size
    if ds.crs is None or not ds.crs.is_projected:
        return _not_measured("Not measured: this photo's coordinates are not in metres.")
    across, down = abs(ds.transform.a), abs(ds.transform.e)
    square = ds.transform.b == 0 and ds.transform.d == 0 and all(COVER_PIXEL_M[0] <= p <= COVER_PIXEL_M[1] for p in (across, down))
    if ds.count < 3 or not square:
        return _not_measured("Not measured at this photo's detail: it was tested on 3 cm pixels.")
    outline = reproject(Transformer.from_crs("EPSG:4326", ds.crs, always_xy=True).transform, shape(geometry))
    inner = outline.buffer(-COVER_INSET_M)
    if inner.is_empty:
        return _not_measured("Not measured for a field this narrow.")
    west, south, east, north = inner.bounds
    if max(east - west, north - south) > COVER_MAX_SPAN_M:
        return _not_measured(f"Not measured for a field more than {COVER_MAX_SPAN_M:g} m across: it was tested on "
                             "smaller fields.")
    window = from_bounds(west, south, east, north, ds.transform).round_offsets().round_lengths()
    rgb = ds.read([1, 2, 3], window=window, boundless=True, fill_value=0)
    photo = ds.read_masks(1, window=window, boundless=True) > 0
    inside = ~geometry_mask([mapping(inner)], out_shape=rgb.shape[1:], transform=ds.window_transform(window))
    share = _cover_from_pixels(rgb, inside & photo)
    if share is None:
        return _not_measured("The photo has no picture inside this field.")
    return {"share": round(share, 2), "why_not": None}


def green_cover(cog_url: str, geometry: dict[str, Any], area_ha: float) -> dict[str, Any]:
    """{"share": 0..1, "why_not": None} for a kept field, or {"share": None, "why_not": plain words} where green
    cover was not measured. `geometry` is the field's outline in longitude and latitude. Reads the photo, with the
    network timeouts every photo read here uses."""
    by_size = _cover_by_size(area_ha)
    if by_size is not None:
        return by_size
    with rasterio.Env(**GDAL_HTTP_TIMEOUTS), rasterio.open(cog_url) as ds:
        return _cover_on(ds, geometry, area_ha)


def green_covers(cog_url: str, fields: Sequence[tuple[str, dict[str, Any], float]]) -> dict[str, dict[str, Any]]:
    """green_cover for several fields, each (id, geometry, area_ha), with the photo opened once. A field whose own
    outline cannot be measured gets that as its reason, so it does not hold up the fields after it; a photo that
    cannot be read raises, to be tried again later."""
    covers = {}
    with rasterio.Env(**GDAL_HTTP_TIMEOUTS), rasterio.open(cog_url) as ds:
        for field_id, geometry, area_ha in fields:
            try:
                covers[field_id] = _cover_on(ds, geometry, area_ha)
            except (ValueError, TypeError, KeyError, ShapelyError):  # this outline, not the photo
                logger.exception("field taps: the outline of field %s cannot be measured for green cover", field_id)
                covers[field_id] = _not_measured("This field's outline could not be measured.")
    return covers


def _prefix(photo_key: str) -> str:
    return f"{_STORE_PREFIX}/{hashlib.sha256(photo_key.encode()).hexdigest()[:32]}"


def manifest_key(photo_key: str) -> str:
    return f"{_prefix(photo_key)}/manifest.json"


def window_key(photo_key: str, window: tuple[int, int]) -> str:
    return f"{_prefix(photo_key)}/w_{window[0]}_{window[1]}.npy"


def _fields_prefix(photo_key: str, layer_id: str) -> str:
    """Where one photo layer's own records live. The prepared windows and the cover mark belong to the stored image,
    which copies of a layer share; kept fields and turned-down taps belong to the layer, whose access the routes
    check: a field kept on one copy must not appear on another project's copy."""
    return f"{_prefix(photo_key)}/layer_{hashlib.sha256(layer_id.encode()).hexdigest()[:16]}"


def _kept_key(photo_key: str, layer_id: str) -> str:
    return f"{_fields_prefix(photo_key, layer_id)}/kept.json"


def _turned_down_key(photo_key: str, layer_id: str) -> str:
    return f"{_fields_prefix(photo_key, layer_id)}/turned_down.json"


_model: Optional[TapModel] = None
_model_lock = threading.Lock()
# One process serves the app (UVICORN_WORKERS=1), so these in-process guards are enough; with more workers the
# kept and turned-down records would need a guard in storage itself.
_model_fetch = asyncio.Lock()  # one download of the tap model at a time
_model_work = asyncio.Semaphore(2)  # outlines being computed at once: the server has no GPU and other work to do
_record_locks: dict[str, asyncio.Lock] = {}  # per photo layer: read, change and write a record as one step
_cover_work = asyncio.Semaphore(1)  # one green cover read at a time: each holds a field's pixels in memory
_fills: dict[str, "asyncio.Task[None]"] = {}  # per photo layer: the background job measuring missing green cover
_fill_retry_at: dict[str, float] = {}  # per photo layer: not before this (time.monotonic) after a failed read
_windows: OrderedDict[str, np.ndarray] = OrderedDict()


async def _tap_model(s3: Any, bucket: str) -> TapModel:
    """The tap model, fetched from object storage once and refused if its sha256 differs."""
    global _model
    if _model is not None:
        return _model

    def on_disk() -> bool:
        return os.path.exists(_TAP_MODEL_PATH) and stored_files.file_sha256(_TAP_MODEL_PATH) == TAP_MODEL_SHA256

    def write(raw: bytes) -> None:
        if hashlib.sha256(raw).hexdigest() != TAP_MODEL_SHA256:
            raise RuntimeError(f"the tap model in storage ({TAP_MODEL_KEY}) does not match its sha256")
        os.makedirs(os.path.dirname(_TAP_MODEL_PATH), exist_ok=True)
        part = f"{_TAP_MODEL_PATH}.{uuid.uuid4().hex[:8]}.part"
        with open(part, "wb") as f:
            f.write(raw)
        os.replace(part, _TAP_MODEL_PATH)

    def load() -> TapModel:
        global _model
        with _model_lock:
            if _model is None:
                _model = TapModel(_TAP_MODEL_PATH)
            return _model

    async with _model_fetch:  # hashing, writing and loading all happen off the event loop, one caller at a time
        if _model is None and not await asyncio.to_thread(on_disk):
            response = await s3.get_object(Bucket=bucket, Key=TAP_MODEL_KEY)
            async with response["Body"] as body:
                raw = await body.read()
            await asyncio.to_thread(write, raw)
        return await asyncio.to_thread(load)


async def _window_array(s3: Any, bucket: str, photo_key: str, window: tuple[int, int]) -> np.ndarray:
    key = window_key(photo_key, window)
    if key in _windows:
        _windows.move_to_end(key)
        return _windows[key]
    try:
        response = await s3.get_object(Bucket=bucket, Key=key)
    except s3.exceptions.NoSuchKey:
        logger.error("field taps: window %s is in the manifest but not in storage", key)
        raise TapError("This part of the photo is not prepared for outlines.") from None
    async with response["Body"] as body:
        raw = await body.read()
    array = await asyncio.to_thread(lambda: np.load(io.BytesIO(raw), allow_pickle=False))
    if array.shape != (1, 256, 64, 64):
        raise RuntimeError(f"stored window {key} has shape {array.shape}, not (1, 256, 64, 64)")
    _windows[key] = array
    while len(_windows) > _KEPT_WINDOWS:
        _windows.popitem(last=False)
    return array


async def grid(s3: Any, bucket: str, photo_key: str) -> Optional[Grid]:
    """The photo's prepared grid, or None when the photo was not prepared."""
    record = await stored_files.load_json(s3, bucket, manifest_key(photo_key))
    if record is None:
        return None
    try:
        return grid_of(record)
    except ValueError as e:  # prepared with other settings: never measured, so the photo counts as not prepared
        logger.warning("field taps: the manifest of %s is refused: %s", photo_key, e)
        return None


async def outlines_and_left_out(s3: Any, bucket: str, photo_key: str, taps: Sequence[Tap],
                                pick: Optional[int]) -> tuple[list[dict[str, Any]], int]:
    """The outlines for these taps, {"choice", "geometry", "area_ha"} each, and how many first-tap choices were left
    out for running along the window's border. TapError when the taps cannot be answered (not prepared here, too
    many corrections, a correction with no pick)."""
    prepared = await grid(s3, bucket, photo_key)
    if prepared is None:
        raise TapError("This photo is not prepared for outlines yet.")
    window, placed = place(prepared, taps)
    model = await _tap_model(s3, bucket)
    array = await _window_array(s3, bucket, photo_key, window)
    found = []
    left_out = 0
    async with _model_work:
        masks = await asyncio.to_thread(outline_masks, model, array, placed, pick)
        if len(placed) == 1:
            masks, left_out = without_edge_choices(masks)
        for choice, mask in masks:
            outline = await asyncio.to_thread(outline_shape, prepared, window, mask)
            if outline is not None:
                found.append({"choice": choice, **outline})
    return found, left_out


async def outlines(s3: Any, bucket: str, photo_key: str, taps: Sequence[Tap],
                   pick: Optional[int]) -> list[dict[str, Any]]:
    """The outlines for these taps alone; see outlines_and_left_out."""
    return (await outlines_and_left_out(s3, bucket, photo_key, taps, pick))[0]


def moved_line(ring: Sequence[Sequence[float]], first_tap: Tap) -> dict[str, Any]:
    """An outline whose line the reader moved by hand, as {"geometry", "area_ha"}: `ring` is its corners as
    (longitude, latitude), closed or not. TapError when it is not one simple shape around the first tap."""
    points = [(float(p[0]), float(p[1])) for p in ring]
    # A corner must be a real place, near the tapped field: a hand-moved line adjusts an outline, it does not
    # draw a new one somewhere else.
    reach_lat = MAX_LINE_REACH_M / 111_000
    reach_lon = reach_lat / max(math.cos(math.radians(first_tap.lat)), 0.01)
    for lon, lat in points:
        if not (math.isfinite(lon) and math.isfinite(lat) and -180 <= lon <= 180 and -90 <= lat <= 90):
            raise TapError("A corner of the outline is not a place on the map.")
        if abs(lon - first_tap.lon) > reach_lon or abs(lat - first_tap.lat) > reach_lat:
            raise TapError(f"A corner is more than {MAX_LINE_REACH_M} m from the spot you tapped. Start again with a new tap.")
    if len(points) > 1 and points[0] == points[-1]:
        points = points[:-1]
    if not 3 <= len(points) <= MAX_LINE_POINTS:
        raise TapError(f"An outline needs between 3 and {MAX_LINE_POINTS} corners.")
    polygon = Polygon(points)
    if not polygon.is_valid or polygon.area == 0:
        raise TapError("The line crosses itself. Move that corner back.")
    if not polygon.contains(Point(first_tap.lon, first_tap.lat)):
        raise TapError("The outline no longer covers the spot you tapped. Move that corner back.")
    area_ha = round(ground_area.area_ha(polygon), 3)
    if area_ha > MAX_MOVED_HA:
        raise TapError(f"A hand-moved outline can be at most {MAX_MOVED_HA:g} ha; this one is {area_ha:g} ha.")
    return {"geometry": mapping(polygon), "area_ha": area_ha}


def _overlap(a: dict[str, Any], b: dict[str, Any]) -> float:
    """How much two outlines cover the same ground: shared area over total area, 1.0 when they are the same."""
    first, second = shape(a), shape(b)
    union = first.union(second).area
    return round(first.intersection(second).area / union, 3) if union else 0.0


def _centre(geometry: dict[str, Any]) -> list[float]:
    """A point inside the outline, as [longitude, latitude]: where its number is written and where it is."""
    point = shape(geometry).representative_point()
    return [round(point.x, 6), round(point.y, 6)]


def _lock(photo_key: str, layer_id: str) -> asyncio.Lock:
    return _record_locks.setdefault(f"{photo_key}|{layer_id}", asyncio.Lock())


async def _kept_record(s3: Any, bucket: str, photo_key: str, layer_id: str) -> tuple[list[dict[str, Any]], int]:
    """(kept outlines oldest first, the number the next kept field gets). A field keeps its number for good: a
    number is never handed out twice on a photo layer, even after its field is removed. Outlines kept before fields
    had numbers get them here, in the order they were kept."""
    record = await stored_files.load_json(s3, bucket, _kept_key(photo_key, layer_id))
    if record is None:
        return [], 1
    rows = list(record["outlines"])
    following = max([record.get("next_number", 1)] + [row["number"] + 1 for row in rows if "number" in row])
    for row in rows:
        if "number" not in row:
            row["number"], following = following, following + 1
        row.setdefault("centre", _centre(row["geometry"]))
    return rows, following


async def kept(s3: Any, bucket: str, photo_key: str, layer_id: str) -> list[dict[str, Any]]:
    """The outlines kept on this photo layer, oldest first, each with its field number."""
    return (await _kept_record(s3, bucket, photo_key, layer_id))[0]


async def _store_kept(s3: Any, bucket: str, photo_key: str, layer_id: str, rows: list[dict[str, Any]],
                      following: int) -> None:
    await s3.put_object(Bucket=bucket, Key=_kept_key(photo_key, layer_id),
                        Body=json.dumps({"outlines": rows, "next_number": following}).encode(),
                        ContentType="application/json")


def cover_rule_key(photo_key: str) -> str:
    """Where the record lives that says the green rule was checked on this photo."""
    return f"{_prefix(photo_key)}/cover_rule.json"


def cover_rule_record(checked_by: str) -> dict[str, Any]:
    """The record `scripts/mark_cover_checked.py` stores for a photo the green rule was checked on."""
    return {"version": 1, "threshold": GREEN_THRESHOLD, "checked_by": checked_by,
            "checked_at": datetime.now(timezone.utc).isoformat(timespec="seconds")}


async def cover_checked(s3: Any, bucket: str, photo_key: str) -> bool:
    """Whether the green rule was checked on this photo. A threshold measures how green one camera on one day
    recorded leaves (Test 79: on Kabarama the same rule called 21 of 24 squares 0 to 1% green), so it is only used
    on a photo it was checked on, and only at the threshold that was checked."""
    record = await stored_files.load_json(s3, bucket, cover_rule_key(photo_key))
    return isinstance(record, dict) and record.get("version") == 1 and record.get("threshold") == GREEN_THRESHOLD


def _cover_missing(row: dict[str, Any]) -> bool:
    """Whether a kept field still needs its green cover measured: it has none, or one from another threshold than
    today's (a figure never outlives the rule that made it)."""
    cover = row.get("green_cover")
    return not isinstance(cover, dict) or cover.get("threshold") != GREEN_THRESHOLD


def _stamped(cover: dict[str, Any]) -> dict[str, Any]:
    return {**cover, "threshold": GREEN_THRESHOLD}


async def _fill_green_cover(s3: Any, bucket: str, photo_key: str, layer_id: str, cog_url: str) -> None:
    """Measure green cover for every kept field still without it, COVER_BATCH at a time, the photo opened once a
    batch. A photo that cannot be read stops the job and is not tried again for COVER_RETRY_S."""
    key = f"{photo_key}|{layer_id}"
    try:
        while True:
            todo = [row for row in await kept(s3, bucket, photo_key, layer_id) if _cover_missing(row)][:COVER_BATCH]
            if not todo:
                return
            async with _cover_work:
                covers = await asyncio.to_thread(
                    green_covers, cog_url, [(row["id"], row["geometry"], row.get("area_ha", 0.0)) for row in todo])
            async with _lock(photo_key, layer_id):  # a keep or remove meanwhile is not lost
                rows, following = await _kept_record(s3, bucket, photo_key, layer_id)
                for row in rows:
                    if row["id"] in covers and _cover_missing(row):
                        row["green_cover"] = _stamped(covers[row["id"]])
                await _store_kept(s3, bucket, photo_key, layer_id, rows, following)
    except Exception:
        _fill_retry_at[key] = time.monotonic() + COVER_RETRY_S
        logger.exception("field taps: green cover could not be measured on %s; trying again in %d s", key,
                         COVER_RETRY_S)


def _start_fill(s3: Any, bucket: str, photo_key: str, layer_id: str, cog_url: str) -> bool:
    """Start measuring the missing green cover in the background, unless a job for this photo layer is running
    (never two at once) or the photo could not be read a moment ago. True while a job is running."""
    key = f"{photo_key}|{layer_id}"
    running = _fills.get(key)
    if running is not None and not running.done():
        return True
    if time.monotonic() < _fill_retry_at.get(key, 0.0):
        return False
    task = asyncio.create_task(_fill_green_cover(s3, bucket, photo_key, layer_id, cog_url))
    _fills[key] = task
    task.add_done_callback(lambda done: _fills.pop(key, None) if _fills.get(key) is done else None)
    return True


async def keep(s3: Any, bucket: str, photo_key: str, layer_id: str, taps: Sequence[Tap], pick: int,
               user_id: str, moved: Optional[Sequence[Sequence[float]]] = None,
               cog_url: Optional[str] = None) -> dict[str, Any]:
    """Keep the outline these taps and this pick give (computed again here). TapError when they give none.
    With `moved`, the reader's hand-moved line is kept instead, marked "by_hand", next to the model's own outline
    and how much the two overlap, so hand-moved outlines are never counted as the model's. With `cog_url`, on a
    photo the green rule was checked on, the field's green cover is measured and kept with it; if the photo cannot
    be read the field is kept all the same and its cover is measured later, in the background."""
    if pick not in (0, 1, 2):
        raise TapError("Pick one of the outlines before keeping it.")
    found = [o for o in await outlines(s3, bucket, photo_key, taps, pick if len(taps) > 1 else None)
             if o["choice"] == pick]
    if not found:
        raise TapError("There is no such outline for these taps.")
    row = {"id": uuid.uuid4().hex[:12], "geometry": found[0]["geometry"], "area_ha": found[0]["area_ha"],
           "taps": [{"lon": t.lon, "lat": t.lat, "add": t.add} for t in taps], "pick": pick,
           "corrections": len(taps) - 1, "by_hand": False, "kept_by": user_id,
           "kept_at": datetime.now(timezone.utc).isoformat(timespec="seconds")}
    if moved is not None:
        own = await asyncio.to_thread(moved_line, moved, taps[0])
        row.update(by_hand=True, model_geometry=row["geometry"], model_area_ha=row["area_ha"],
                   model_overlap=_overlap(row["geometry"], own["geometry"]), **own)
    if cog_url is not None and await cover_checked(s3, bucket, photo_key):
        try:  # not behind a whole background batch: past a short wait the field is kept and measured later
            await asyncio.wait_for(_cover_work.acquire(), COVER_KEEP_WAIT_S)
            try:
                cover = await asyncio.to_thread(green_cover, cog_url, row["geometry"], row["area_ha"])
            finally:
                _cover_work.release()
            row["green_cover"] = _stamped(cover)
        except asyncio.TimeoutError:
            pass
        except Exception:  # the field is kept either way; its cover is measured later, in the background
            logger.exception("field taps: green cover of a newly kept field could not be read")
    async with _lock(photo_key, layer_id):  # two keeps at once: neither is lost
        rows, number = await _kept_record(s3, bucket, photo_key, layer_id)
        if len(rows) >= MAX_KEPT_FIELDS:
            raise TapError(f"This photo already has {MAX_KEPT_FIELDS} outlined fields. Remove one to keep another.")
        row.update(number=number, centre=_centre(row["geometry"]))
        await _store_kept(s3, bucket, photo_key, layer_id, rows + [row], number + 1)
    return row


async def remove(s3: Any, bucket: str, photo_key: str, layer_id: str, outline_id: str) -> bool:
    """Take a kept outline off the photo layer; False when there is none with this id."""
    async with _lock(photo_key, layer_id):
        rows, following = await _kept_record(s3, bucket, photo_key, layer_id)
        left = [row for row in rows if row["id"] != outline_id]
        if len(left) == len(rows):
            return False
        await _store_kept(s3, bucket, photo_key, layer_id, left, following)
    return True


# Why a tap ended without a kept outline. Kept and turned-down taps together are every tap that was answered, so
# the share that was kept can be counted instead of guessed.
TURN_DOWN_REASONS = ("none_of_these", "started_again", "closed", "no_outline")


async def _turned_down_record(s3: Any, bucket: str, photo_key: str, layer_id: str) -> tuple[list[dict[str, Any]], int]:
    """(the turned-down taps still kept in full, oldest first; how many there have been in all)."""
    record = await stored_files.load_json(s3, bucket, _turned_down_key(photo_key, layer_id))
    if record is None:
        return [], 0
    rows = list(record["taps"])
    return rows, max(int(record.get("total", 0)), len(rows))


async def turned_down(s3: Any, bucket: str, photo_key: str, layer_id: str) -> list[dict[str, Any]]:
    """The taps on this photo layer that ended without a kept outline, oldest first (the latest
    MAX_TURNED_DOWN_ROWS)."""
    return (await _turned_down_record(s3, bucket, photo_key, layer_id))[0]


async def turn_down(s3: Any, bucket: str, photo_key: str, layer_id: str, taps: Sequence[Tap], pick: Optional[int],
                    reason: str, user_id: str) -> dict[str, Any]:
    """Record that these taps ended without a kept outline. The taps and the pick are stored, not the outlines:
    the same taps give the same outlines again. TapError for a reason that is not one of TURN_DOWN_REASONS."""
    if reason not in TURN_DOWN_REASONS:
        raise TapError(f"The reason must be one of: {', '.join(TURN_DOWN_REASONS)}.")
    row = {"id": uuid.uuid4().hex[:12], "taps": [{"lon": t.lon, "lat": t.lat, "add": t.add} for t in taps],
           "pick": pick, "corrections": len(taps) - 1, "reason": reason, "turned_down_by": user_id,
           "turned_down_at": datetime.now(timezone.utc).isoformat(timespec="seconds")}
    async with _lock(photo_key, layer_id):
        rows, total = await _turned_down_record(s3, bucket, photo_key, layer_id)
        body = {"taps": (rows + [row])[-MAX_TURNED_DOWN_ROWS:], "total": total + 1}
        await s3.put_object(Bucket=bucket, Key=_turned_down_key(photo_key, layer_id), Body=json.dumps(body).encode(),
                            ContentType="application/json")
    return row


def shown(row: dict[str, Any]) -> dict[str, Any]:
    """A kept field as a screen is given it: who kept it stays in the record and is not shown to a viewer."""
    return {k: v for k, v in row.items() if k != "kept_by"}


async def state(s3: Any, bucket: str, photo_key: str, layer_id: str, cog_url: Optional[str] = None) -> dict[str, Any]:
    """What the screen needs before the first tap. Reads only: on a photo the green rule was not checked on every
    field says so and no stored figure is shown; on one it was, fields still without a figure are measured in the
    background (with `cog_url`) and "cover_filling" says a job is running, so the screen asks again."""
    prepared = await grid(s3, bucket, photo_key)
    rows = await kept(s3, bucket, photo_key, layer_id) if prepared else []
    filling = False
    if rows and not await cover_checked(s3, bucket, photo_key):
        rows = [{**row, "green_cover": _not_measured(COVER_NOT_CHECKED)} for row in rows]
    elif rows:
        missing = [row for row in rows if _cover_missing(row)]
        rows = [{k: v for k, v in row.items() if k != "green_cover"} if _cover_missing(row) else row for row in rows]
        filling = bool(missing) and cog_url is not None and _start_fill(s3, bucket, photo_key, layer_id, cog_url)
    return {"prepared": prepared is not None, "windows": len(prepared.windows) if prepared else 0,
            "max_corrections": MAX_CORRECTIONS, "measured": MEASURED, "cover_measured": COVER_MEASURED,
            "kept": [shown(row) for row in rows], "cover_filling": filling,
            "turned_down": (await _turned_down_record(s3, bucket, photo_key, layer_id))[1] if prepared else 0}
