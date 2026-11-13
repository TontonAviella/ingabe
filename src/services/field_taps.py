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
import uuid
from collections import OrderedDict
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Optional, Sequence

import numpy as np
from affine import Affine
from pyproj import Transformer
from rasterio.features import shapes
from scipy import ndimage
from shapely.geometry import Point, Polygon, mapping, shape
from shapely.ops import transform as reproject

from src.services import ground_area, stored_files

logger = logging.getLogger(__name__)

WINDOW_M = 120.0
WINDOW_PX = 1024
STEP_M = 60.0
MAX_CORRECTIONS = 2
MAX_KEPT_FIELDS = 500  # kept fields on one photo: each is a plot the cards and Sage measure
MAX_LINE_REACH_M = 500  # no corner of a hand-moved line may lie further than this from the first tap
MAX_TURNED_DOWN_ROWS = 2000  # turned-down taps kept in full; older ones stay in the count only
MAX_LINE_POINTS = 600  # a hand-moved line with more points than this is refused
SAME_OUTLINE = 0.9  # two choices that overlap this much are one outline shown twice; the later one is dropped
SIMPLIFY_M = 0.25  # about two pixels of a window: corners stay, the staircase of pixels goes
PREPARE_MODEL = "sam_vit_h_4b8939"  # the heavy half the stored windows must come from

TAP_MODEL_KEY = "models/sam-vit-h-taps-c2cef977127da4a3.onnx"
TAP_MODEL_SHA256 = "c2cef977127da4a396b603e859b2ea00506de9af50cdf0ebcd01af73aeb62ab1"
_TAP_MODEL_PATH = "/tmp/ingabe_cache/models/sam-vit-h-taps-c2cef977127da4a3.onnx"
_STORE_PREFIX = "field_taps/v1"
_KEPT_WINDOWS = 12  # stored windows held in memory (2 MB each)

MEASURED = (
    "Measured on one photo (Cyampirita, 26 fields): one tap and up to two corrections gave a usable outline on 25 "
    "fields, with the area typically within 5% of an outline drawn by hand. In that test a script placed the taps; "
    "taps by a person are not measured yet. Corners were not measured. Without a correction the area was within 5% "
    "on 9 of 20 fields. A line you move by hand is your own: "
    "it was not part of that test. Areas are map areas: on a slope the ground itself is larger."
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


def _prefix(photo_key: str) -> str:
    return f"{_STORE_PREFIX}/{hashlib.sha256(photo_key.encode()).hexdigest()[:32]}"


def manifest_key(photo_key: str) -> str:
    return f"{_prefix(photo_key)}/manifest.json"


def window_key(photo_key: str, window: tuple[int, int]) -> str:
    return f"{_prefix(photo_key)}/w_{window[0]}_{window[1]}.npy"


def _kept_key(photo_key: str) -> str:
    return f"{_prefix(photo_key)}/kept.json"


def _turned_down_key(photo_key: str) -> str:
    return f"{_prefix(photo_key)}/turned_down.json"


_model: Optional[TapModel] = None
_model_lock = threading.Lock()
# One process serves the app (UVICORN_WORKERS=1), so these in-process guards are enough; with more workers the
# kept and turned-down records would need a guard in storage itself.
_model_fetch = asyncio.Lock()  # one download of the tap model at a time
_model_work = asyncio.Semaphore(2)  # outlines being computed at once: the server has no GPU and other work to do
_record_locks: dict[str, asyncio.Lock] = {}  # per photo: read, change and write a record as one step
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


async def outlines(s3: Any, bucket: str, photo_key: str, taps: Sequence[Tap],
                   pick: Optional[int]) -> list[dict[str, Any]]:
    """The outlines for these taps: {"choice", "geometry", "area_ha"} each. TapError when the taps cannot be
    answered (not prepared here, too many corrections, a correction with no pick)."""
    prepared = await grid(s3, bucket, photo_key)
    if prepared is None:
        raise TapError("This photo is not prepared for outlines yet.")
    window, placed = place(prepared, taps)
    model = await _tap_model(s3, bucket)
    array = await _window_array(s3, bucket, photo_key, window)
    found = []
    async with _model_work:
        masks = await asyncio.to_thread(outline_masks, model, array, placed, pick)
        for choice, mask in masks:
            outline = await asyncio.to_thread(outline_shape, prepared, window, mask)
            if outline is not None:
                found.append({"choice": choice, **outline})
    return found


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
    return {"geometry": mapping(polygon), "area_ha": round(ground_area.area_ha(polygon), 3)}


def _overlap(a: dict[str, Any], b: dict[str, Any]) -> float:
    """How much two outlines cover the same ground: shared area over total area, 1.0 when they are the same."""
    first, second = shape(a), shape(b)
    union = first.union(second).area
    return round(first.intersection(second).area / union, 3) if union else 0.0


def _centre(geometry: dict[str, Any]) -> list[float]:
    """A point inside the outline, as [longitude, latitude]: where its number is written and where it is."""
    point = shape(geometry).representative_point()
    return [round(point.x, 6), round(point.y, 6)]


async def _kept_record(s3: Any, bucket: str, photo_key: str) -> tuple[list[dict[str, Any]], int]:
    """(kept outlines oldest first, the number the next kept field gets). A field keeps its number for good: a
    number is never handed out twice on a photo, even after its field is removed. Outlines kept before fields had
    numbers get them here, in the order they were kept."""
    record = await stored_files.load_json(s3, bucket, _kept_key(photo_key))
    if record is None:
        return [], 1
    rows = list(record["outlines"])
    following = max([record.get("next_number", 1)] + [row["number"] + 1 for row in rows if "number" in row])
    for row in rows:
        if "number" not in row:
            row["number"], following = following, following + 1
        row.setdefault("centre", _centre(row["geometry"]))
    return rows, following


async def kept(s3: Any, bucket: str, photo_key: str) -> list[dict[str, Any]]:
    """The outlines kept on this photo, oldest first, each with its field number."""
    return (await _kept_record(s3, bucket, photo_key))[0]


async def _store_kept(s3: Any, bucket: str, photo_key: str, rows: list[dict[str, Any]], following: int) -> None:
    await s3.put_object(Bucket=bucket, Key=_kept_key(photo_key),
                        Body=json.dumps({"outlines": rows, "next_number": following}).encode(),
                        ContentType="application/json")


async def keep(s3: Any, bucket: str, photo_key: str, taps: Sequence[Tap], pick: int,
               user_id: str, moved: Optional[Sequence[Sequence[float]]] = None) -> dict[str, Any]:
    """Keep the outline these taps and this pick give (computed again here). TapError when they give none.
    With `moved`, the reader's hand-moved line is kept instead, marked "by_hand", next to the model's own outline
    and how much the two overlap, so hand-moved outlines are never counted as the model's."""
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
    async with _record_locks.setdefault(photo_key, asyncio.Lock()):  # two keeps at once: neither is lost
        rows, number = await _kept_record(s3, bucket, photo_key)
        if len(rows) >= MAX_KEPT_FIELDS:
            raise TapError(f"This photo already has {MAX_KEPT_FIELDS} outlined fields. Remove one to keep another.")
        row.update(number=number, centre=_centre(row["geometry"]))
        await _store_kept(s3, bucket, photo_key, rows + [row], number + 1)
    return row


async def remove(s3: Any, bucket: str, photo_key: str, outline_id: str) -> bool:
    """Take a kept outline off the photo; False when there is none with this id."""
    async with _record_locks.setdefault(photo_key, asyncio.Lock()):
        rows, following = await _kept_record(s3, bucket, photo_key)
        left = [row for row in rows if row["id"] != outline_id]
        if len(left) == len(rows):
            return False
        await _store_kept(s3, bucket, photo_key, left, following)
    return True


# Why a tap ended without a kept outline. Kept and turned-down taps together are every tap that was answered, so
# the share that was kept can be counted instead of guessed.
TURN_DOWN_REASONS = ("none_of_these", "started_again", "closed", "no_outline")


async def _turned_down_record(s3: Any, bucket: str, photo_key: str) -> tuple[list[dict[str, Any]], int]:
    """(the turned-down taps still kept in full, oldest first; how many there have been in all)."""
    record = await stored_files.load_json(s3, bucket, _turned_down_key(photo_key))
    if record is None:
        return [], 0
    rows = list(record["taps"])
    return rows, max(int(record.get("total", 0)), len(rows))


async def turned_down(s3: Any, bucket: str, photo_key: str) -> list[dict[str, Any]]:
    """The taps on this photo that ended without a kept outline, oldest first (the latest MAX_TURNED_DOWN_ROWS)."""
    return (await _turned_down_record(s3, bucket, photo_key))[0]


async def turn_down(s3: Any, bucket: str, photo_key: str, taps: Sequence[Tap], pick: Optional[int], reason: str,
                    user_id: str) -> dict[str, Any]:
    """Record that these taps ended without a kept outline. The taps and the pick are stored, not the outlines:
    the same taps give the same outlines again. TapError for a reason that is not one of TURN_DOWN_REASONS."""
    if reason not in TURN_DOWN_REASONS:
        raise TapError(f"The reason must be one of: {', '.join(TURN_DOWN_REASONS)}.")
    row = {"id": uuid.uuid4().hex[:12], "taps": [{"lon": t.lon, "lat": t.lat, "add": t.add} for t in taps],
           "pick": pick, "corrections": len(taps) - 1, "reason": reason, "turned_down_by": user_id,
           "turned_down_at": datetime.now(timezone.utc).isoformat(timespec="seconds")}
    async with _record_locks.setdefault(photo_key, asyncio.Lock()):
        rows, total = await _turned_down_record(s3, bucket, photo_key)
        body = {"taps": (rows + [row])[-MAX_TURNED_DOWN_ROWS:], "total": total + 1}
        await s3.put_object(Bucket=bucket, Key=_turned_down_key(photo_key), Body=json.dumps(body).encode(),
                            ContentType="application/json")
    return row


async def state(s3: Any, bucket: str, photo_key: str) -> dict[str, Any]:
    """What the screen needs before the first tap."""
    prepared = await grid(s3, bucket, photo_key)
    return {"prepared": prepared is not None, "windows": len(prepared.windows) if prepared else 0,
            "max_corrections": MAX_CORRECTIONS, "measured": MEASURED,
            # who kept a field stays in the record; the screen does not need it and a viewer is not shown it
            "kept": [{k: v for k, v in row.items() if k != "kept_by"}
                     for row in await kept(s3, bucket, photo_key)] if prepared else [],
            "turned_down": (await _turned_down_record(s3, bucket, photo_key))[1] if prepared else 0}
