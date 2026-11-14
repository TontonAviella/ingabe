"""Only my crop: of the plants the plant finder boxed in one field, which are the reader's crop.

The plant finder (plant_finder.py) boxes crop plants of every kind. A reader who grows avocado wants the avocados
counted, not every plant. So the reader taps a few plants of their crop and a few green things that are not, and a
boxed plant is "their crop" when it looks more like the first taps than the second (Test 81, 2026-11-14).

What a plant looks like: a square of the photo's grid is read at twice its size (SIDE pixels), DINOv2-small gives
one vector for each 14-pixel patch (GRID x GRID patches, one about 0.22 m), and a plant's look is the mean of the
3 x 3 patches around its centre, at unit length. A box is the reader's crop when its look has the larger dot product
with the mean look of the crop taps than with the mean look of the other taps (the means are not brought back to
unit length, and a tie goes to the crop: exactly the rule Test 81 scored, `t82/chain.py`).

Measured on Cyampirita (3.25 cm pixels), on 16 squares two blind markers fully counted, with three taps of each
sort drawn from the markers' own dots: over all 44 cases 80% of highlighted plants were the tapped crop and 88% of
that crop's plants were highlighted (56% with no taps); where one crop grew in the square 89% and 91%; where two or
more grew 74% and 86%, which fails the bar for clients. So a count here is an estimate and is always said as
"about". Not measured: taps made by a person, taps on one square sorting the plants of another (here the taps sort
the whole field), tree crops (the photo has none), any other photo.

The look model is slow (seconds to half a minute a square), so each square's parts (picture check, plant boxes,
looks, green peaks) are made once in the background and kept by photo and square; sorting the boxes by a new set
of taps then reads only what is kept.
"""

from __future__ import annotations

import asyncio
import hashlib
import io
import logging
from datetime import datetime, timezone
from typing import Any, Callable, Optional, Sequence

import numpy as np
import rasterio
from botocore.exceptions import ClientError
from PIL import Image
from pyproj import Transformer
from shapely.geometry import Point
from shapely.prepared import prep

from src.services import plant_counts, plant_finder, stored_files

logger = logging.getLogger(__name__)

SIDE = 784  # a square is read at this size for its looks (Test 81)
GRID = SIDE // 14  # patches along one side
MIN_TAPS, MAX_TAPS = 3, 10  # of each sort; three were tested, more changed little
METHOD = "only my crop v1: plant finder s1 + looks dinov2-small 784 + nearer-taps rule"
LOOK_KEY = "models/plant-look-dinov2-small-784-5c2d4eeaf8b50af6.onnx"
LOOK_SHA256 = "5c2d4eeaf8b50af65b88a8f108d3c244b83535aca43a4a50c697636b1e35c84d"
_LOOK_PATH = "/tmp/ingabe_cache/models/plant-look-dinov2-small-784-5c2d4eeaf8b50af6.onnx"
_STORE_PREFIX = "my_crop/v1"
_MEAN = np.array([0.485, 0.456, 0.406], np.float32)
_STD = np.array([0.229, 0.224, 0.225], np.float32)
UNREADABLE, REFUSED, COUNTED = 0, 1, 2

MEASURED = (
    "An estimate, measured on one photo with taps drawn from two markers' dots: about 4 highlighted plants in 5 "
    "were the tapped crop and about 9 of its plants in 10 were highlighted. Where two or more crops grow together "
    "it is weaker (about 3 in 4). Not measured on tree crops or on other photos."
)


class TapProblem(ValueError):
    """The taps cannot be used; the message says why, in plain words."""


class NotReady(Exception):
    """A square of the field has no kept parts yet."""


def looks_of(session: Any, rgb: np.ndarray) -> np.ndarray:
    """The look of every patch of one square (GRID x GRID x 384)."""
    big = np.asarray(Image.fromarray(rgb).resize((SIDE, SIDE), Image.BICUBIC), np.float32)
    return session.run(None, {"x": ((big / 255 - _MEAN) / _STD).transpose(2, 0, 1)[None]})[0]


def look_at(looks: np.ndarray, x: float, y: float) -> np.ndarray:
    """The look of the plant centred at (x, y) in the square's pixels: the mean of the 3 x 3 patches around it, at
    unit length."""
    col = min(GRID - 1, max(0, int(x / plant_finder.SQUARE_PX * GRID)))
    row = min(GRID - 1, max(0, int(y / plant_finder.SQUARE_PX * GRID)))
    mean = looks[max(0, row - 1):row + 2, max(0, col - 1):col + 2].reshape(-1, looks.shape[-1]).astype(np.float32).mean(0)
    return mean / (np.linalg.norm(mean) + 1e-9)


def mine(box_looks: np.ndarray, crop_looks: Sequence[np.ndarray], other_looks: Sequence[np.ndarray]) -> np.ndarray:
    """Which boxes are the reader's crop: nearer the mean look of the crop taps than that of the other taps."""
    if not len(box_looks):
        return np.zeros(0, bool)
    return (box_looks @ np.mean(crop_looks, 0)) >= (box_looks @ np.mean(other_looks, 0))


def _square_key(photo_key: str, j: int, i: int) -> str:
    return f"{_STORE_PREFIX}/{hashlib.sha256(photo_key.encode()).hexdigest()[:32]}/{j}_{i}.npz"


def _load_square(s3: Any, bucket: str, photo_key: str, j: int, i: int) -> Optional[dict[str, np.ndarray]]:
    try:
        body = s3.get_object(Bucket=bucket, Key=_square_key(photo_key, j, i))["Body"].read()
    except s3.exceptions.NoSuchKey:
        return None
    with np.load(io.BytesIO(body)) as parts:
        return {name: parts[name] for name in parts.files}


def _has_square(s3: Any, bucket: str, photo_key: str, j: int, i: int) -> bool:
    """Whether a square's parts are kept, without fetching them."""
    try:
        s3.head_object(Bucket=bucket, Key=_square_key(photo_key, j, i))
    except ClientError as error:
        if error.response.get("Error", {}).get("Code") in ("404", "NoSuchKey", "NotFound"):
            return False
        raise
    return True


def square_parts(rgb: Optional[np.ndarray], metres_per_px: float, models: plant_finder.Models,
                 look: Any) -> dict[str, np.ndarray]:
    """What is kept of one square: its state, and where counted its plant boxes, the look of every patch and the
    green peaks (as plant_finder finds them, on the whole square)."""
    empty = {"boxes": np.zeros((0, 4), np.float32), "looks": np.zeros((0, 0, 0), np.float16),
             "peaks": np.zeros((0, 2), np.float32)}
    if rgb is None:
        return {"state": np.array(UNREADABLE), **empty}
    if plant_finder.picture_score(models, rgb) < plant_finder.PICTURE_CUT:
        return {"state": np.array(REFUSED), **empty}
    peaks, _ = plant_counts.crowns(rgb, np.ones(rgb.shape[:2], bool), metres_per_px)
    return {"state": np.array(COUNTED),
            "boxes": np.array(plant_finder.plant_boxes(models, rgb), np.float32).reshape(-1, 4),
            "looks": looks_of(look, rgb).astype(np.float16),
            "peaks": np.array([(col, row) for row, col in peaks], np.float32).reshape(-1, 2)}


def _look_session(s3: Any, bucket: str) -> Any:
    """The look model, fetched from object storage once and refused when it does not match its sha256."""
    import onnxruntime  # lazy: loaded only when a count is asked for

    options = onnxruntime.SessionOptions()
    options.intra_op_num_threads = 4
    return onnxruntime.InferenceSession(
        stored_files.ensure_local_copy(s3, bucket, LOOK_KEY, LOOK_SHA256, _LOOK_PATH, "plant look model"),
        options, providers=["CPUExecutionProvider"])


def prepare(cog_url: str, feature: dict[str, Any], s3: Any, bucket: str, photo_key: str,
            tick: Callable[[int, int], None]) -> None:
    """Make and keep the parts of every square of the field that has none yet; blocking, run it in a thread. `s3`
    is a synchronous boto3 client. The look model is loaded for the job and let go after it (about 900 MB while
    it runs)."""
    with rasterio.open(cog_url) as ds:
        placed = plant_finder.layout(ds, feature)
        todo = [key for key in placed.squares if not _has_square(s3, bucket, photo_key, *key)]
        done = len(placed.squares) - len(todo)
        tick(done, len(placed.squares))
        if not todo:
            return
        models = plant_finder.load_models(s3, bucket)
        look = _look_session(s3, bucket)
        for j, i in todo:
            parts = square_parts(plant_finder.read_square(ds, j, i), placed.metres_per_px, models, look)
            packed = io.BytesIO()
            np.savez_compressed(packed, state=parts["state"], boxes=parts["boxes"], looks=parts["looks"],
                                peaks=parts["peaks"])
            s3.put_object(Bucket=bucket, Key=_square_key(photo_key, j, i), Body=packed.getvalue(),
                          ContentType="application/octet-stream")
            done += 1
            tick(done, len(placed.squares))


def answer(cog_url: str, feature: dict[str, Any], s3: Any, bucket: str, photo_key: str,
           crop_taps: Sequence[tuple[float, float]], other_taps: Sequence[tuple[float, float]]) -> dict[str, Any]:
    """The reader's crop in one field from the kept parts; blocking, run it in a thread. Taps are (lon, lat).
    NotReady when a square has no parts yet; TapProblem when a tap cannot be used."""
    with rasterio.open(cog_url) as ds:
        placed = plant_finder.layout(ds, feature)
        to_photo = Transformer.from_crs("EPSG:4326", ds.crs, always_xy=True).transform
        to_wgs84 = Transformer.from_crs(ds.crs, "EPSG:4326", always_xy=True).transform
        transform = ds.transform
    squares: dict[tuple[int, int], dict[str, np.ndarray]] = {}
    for key in placed.squares:
        parts = _load_square(s3, bucket, photo_key, *key)
        if parts is None:
            raise NotReady()
        squares[key] = parts
    counted = {key: parts for key, parts in squares.items() if int(parts["state"]) == COUNTED}
    if not counted:
        raise plant_finder.CannotCount(
            "Plants cannot be counted in this field: in every part of it single plants cannot be told apart from "
            "above (closed canopy, pasture, bush or buildings).")
    inside = prep(placed.outline)
    n = plant_finder.SQUARE_PX

    def tapped(lon: float, lat: float) -> np.ndarray:
        x, y = ~transform * to_photo(lon, lat)
        key = (int(x // n), int(y // n))
        if not inside.contains(Point(x, y)):
            raise TapProblem("A tap lies outside the field. Tap plants inside its line.")
        if key not in squares:
            raise TapProblem("A tap lies at the very edge of the field. Tap a little further inside.")
        if key not in counted:
            raise TapProblem("A tap lies on ground where single plants cannot be told apart. Tap where each plant "
                             "shows on its own.")
        return look_at(counted[key]["looks"], x - key[0] * n, y - key[1] * n)

    crop_looks = [tapped(*tap) for tap in crop_taps]
    other_looks = [tapped(*tap) for tap in other_taps]
    places: dict[str, list[tuple[float, float]]] = {"my_crop": [], "other_plant": [], "other_green": []}
    for (j, i), parts in counted.items():
        boxes = parts["boxes"]
        centres = [((x1 + x2) / 2, (y1 + y2) / 2) for x1, y1, x2, y2 in boxes]
        box_looks = (np.array([look_at(parts["looks"], x, y) for x, y in centres]) if centres
                     else np.zeros((0, parts["looks"].shape[-1]), np.float32))
        for (x, y), is_mine in zip(centres, mine(box_looks, crop_looks, other_looks)):
            if inside.contains(Point(x + j * n, y + i * n)):
                places["my_crop" if is_mine else "other_plant"].append((x + j * n, y + i * n))
        places["other_green"] += [
            (float(col) + j * n, float(row) + i * n) for col, row in parts["peaks"]
            if inside.contains(Point(float(col) + j * n, float(row) + i * n))
            and not any(x1 <= col <= x2 and y1 <= row <= y2 for x1, y1, x2, y2 in boxes)]

    def lonlat(x: float, y: float) -> list[float]:
        lon, lat = to_wgs84(*(transform * (x, y)))
        return [round(float(lon), 8), round(float(lat), 8)]

    plot_area = placed.outline.area * placed.metres_per_px ** 2
    counted_area = sum(placed.covered[key] for key in counted) * n ** 2 * placed.metres_per_px ** 2
    states = [int(parts["state"]) for parts in squares.values()]
    return {
        "my_crop": len(places["my_crop"]), "other_plants": len(places["other_plant"]),
        "other_green": len(places["other_green"]),
        "plot_area_m2": round(plot_area, 1), "counted_area_m2": round(counted_area, 1),
        "counted_share": round(counted_area / plot_area, 3) if plot_area else 0.0,
        "per_ha": int(round(len(places["my_crop"]) / counted_area * 10_000, -1)) if counted_area else None,
        "squares": len(squares), "squares_counted": len(counted), "squares_refused": states.count(REFUSED),
        "squares_unreadable": states.count(UNREADABLE),
        "crop_taps": len(crop_looks), "other_taps": len(other_looks),
        "method": METHOD, "measured": MEASURED, "done_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "points": {"type": "FeatureCollection", "features": [
            {"type": "Feature", "geometry": {"type": "Point", "coordinates": lonlat(x, y)},
             "properties": {"kind": kind, "kind_code": code}}
            for code, kind in enumerate(("my_crop", "other_plant", "other_green")) for x, y in places[kind]]},
    }


_jobs: dict[str, dict[str, Any]] = {}  # field -> {"task", "done", "total", "error"}; in this process only
_work = asyncio.Lock()  # one field is prepared at a time in the whole app: the look model is heavy
MAX_WAITING = 5  # fields being read or waiting their turn; past it a new field is told to come back


class Busy(Exception):
    """Too many fields are being read; the message says so in plain words."""


def _job_key(photo_key: str, feature: dict[str, Any]) -> str:
    return hashlib.sha256(f"{photo_key}|{feature['geometry']}".encode()).hexdigest()


async def _prepare(key: str, cog_url: str, feature: dict[str, Any], s3: Any, bucket: str, photo_key: str) -> None:
    """Read one field's squares. A job that ends well takes its entry away; one that fails leaves only its reason
    in words, never the error itself (an error would hold the look model in memory until someone asked again)."""
    job = _jobs[key]

    def tick(done: int, total: int) -> None:
        job.update(done=done, total=total)

    try:
        async with _work:
            await asyncio.to_thread(prepare, cog_url, feature, s3, bucket, photo_key, tick)
    except plant_finder.CannotCount as why:
        job["error"] = str(why)
    except Exception:
        logger.exception("only my crop: the field could not be read")
        job["error"] = "The photo could not be read for this field. Try again in a moment."
    else:
        if _jobs.get(key) is job:
            del _jobs[key]


async def count(cog_url: str, feature: dict[str, Any], s3: Any, bucket: str, photo_key: str,
                crop_taps: Sequence[tuple[float, float]], other_taps: Sequence[tuple[float, float]]) -> dict[str, Any]:
    """The reader's crop in one field, or {"working": True, "done", "total"} while its squares are being read in
    the background: ask again with the same taps. `s3` is a synchronous boto3 client. TapProblem and
    plant_finder.CannotCount say why there is no answer; Busy that too many fields are being read."""
    if not MIN_TAPS <= len(crop_taps) <= MAX_TAPS or not MIN_TAPS <= len(other_taps) <= MAX_TAPS:
        raise TapProblem(f"Tap {MIN_TAPS} to {MAX_TAPS} plants of your crop and {MIN_TAPS} to {MAX_TAPS} green "
                         "things that are not.")
    key = _job_key(photo_key, feature)

    def running() -> Optional[dict[str, Any]]:
        job = _jobs.get(key)
        if job is None:
            return None
        if "error" in job:  # said once, then the next ask may try again
            del _jobs[key]
            raise plant_finder.CannotCount(job["error"])
        return {"working": True, "done": job["done"], "total": job["total"]}

    if (working := running()) is not None:
        return working
    try:
        return await asyncio.to_thread(answer, cog_url, feature, s3, bucket, photo_key, crop_taps, other_taps)
    except NotReady:
        if (working := running()) is not None:  # another ask started the reading while this one looked
            return working
        if sum("error" not in job for job in _jobs.values()) >= MAX_WAITING:
            raise Busy("Other fields are being read right now. Try this one again in a few minutes.") from None
        _jobs[key] = {"done": 0, "total": 0}
        _jobs[key]["task"] = asyncio.create_task(_prepare(key, cog_url, feature, s3, bucket, photo_key))
        return {"working": True, "done": 0, "total": 0}
