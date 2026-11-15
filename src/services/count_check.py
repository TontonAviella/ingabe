"""Check a square: the reader checks the plant count on their own field.

The app picks one square of a kept field (the reader does not choose it, so the easy corner cannot be picked),
shows the plants the finder ringed there, and the reader taps the rings that are not plants and the plants that got
no ring. What is kept is what the reader said, square by square; what is reported is those squares alone: "the app
counted 38 here, you counted 41". Nothing is scaled up to the field: one square says how the count did in that
square, and how far that carries across the field was not measured.

The plants shown are exactly the ones the count is made of (plant_finder.plant_boxes, centre inside the field), on
the same grid of squares, in ground the picture check lets through.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import math
import threading
from collections import OrderedDict
from datetime import datetime, timezone
from typing import Any, Optional, Sequence

import rasterio
from pyproj import Transformer
from shapely.geometry import MultiPolygon, Point, box, mapping, shape
from shapely.ops import transform as reproject

from src.services import plant_finder, stored_files
from src.services.gdal_http import GDAL_HTTP_TIMEOUTS
from src.services.plant_finder import SQUARE_PX, CannotCount

_STORE_PREFIX = "count_check/v1"
WHOLE_ENOUGH = 0.5  # squares with at least this share inside the field are offered first: more plants to check
MAX_MISSED = 400  # plants a reader may add in one square; far above the densest square counted
SAME_PLANT_M = 0.15  # an added plant this close to another added one, or to a ring left standing, is that plant
_KEPT_FIELDS = 64  # fields whose uncountable squares are remembered
_KEPT_SQUARES = 16  # squares whose plants are held in memory between showing one and saving its check

MEASURED = (
    "A person's own count on parts of the field that the app chose, in an order nobody picks; a part cannot be "
    "passed over. It tells you how the app's count did in the parts checked, on this field: how many of its rings "
    "were not plants, and how many plants it missed. It is a judgement from the photo, not a count on the ground. "
    "It is not scaled up: how well a few parts stand for the whole field was not measured. Each part is checked by "
    "itself, so a plant ringed twice across the line between two parts does not show in a check."
)


class CheckProblem(ValueError):
    """The check cannot be shown or kept as asked; the message says why, in plain words."""


def order_key(field_id: str, square: tuple[int, int]) -> str:
    """The squares of a field in an order nobody chooses: the same every time for one line of one field (`field_id`
    names the line, so keeping the same outline again does not deal a new order), unrelated to where the square
    lies or what grows in it."""
    return hashlib.sha256(f"{field_id}:{square[0]}:{square[1]}".encode()).hexdigest()


def in_order(field_id: str, covered: dict[tuple[int, int], float], squares: Sequence[tuple[int, int]]) -> list[tuple[int, int]]:
    """The squares to offer: those mostly inside the field first, each group in the order nobody chooses."""
    return sorted(squares, key=lambda s: (covered[s] < WHOLE_ENOUGH, order_key(field_id, s)))


def summed(checks: Sequence[dict[str, Any]], squares_in_field: Optional[int] = None) -> Optional[dict[str, Any]]:
    """What the reader's checks say together, or None when there are none."""
    if not checks:
        return None
    app = sum(c["app"] for c in checks)
    wrong = sum(c["wrong"] for c in checks)
    missed = sum(c["missed"] for c in checks)
    you = app - wrong + missed
    parts = len(checks)
    of = f" of {squares_in_field}" if squares_in_field else ""
    said = (f"In the {parts}{of} part{'s' if parts != 1 else ''} of this field checked by hand, the app counted "
            f"{app:,} plants and the person checking counted {you:,}. {wrong:,} of the app's rings "
            f"{'was' if wrong == 1 else 'were'} not {'a plant' if wrong == 1 else 'plants'}, and it missed {missed:,}.")
    return {"parts": parts, "app": app, "you": you, "wrong": wrong, "missed": missed, "said": said,
            "last_at": max(c["at"] for c in checks)}


def _geometry_mark(geometry: dict[str, Any]) -> str:
    """Changes when the field's line changes: a check made on another line is not this field's check."""
    return hashlib.sha256(json.dumps(geometry, sort_keys=True).encode()).hexdigest()[:16]


def _key(photo_key: str, layer_id: str, field_id: str) -> str:
    place = hashlib.sha256(f"{photo_key}:{layer_id}".encode()).hexdigest()[:32]
    return f"{_STORE_PREFIX}/{place}/{field_id}.json"


async def checks(s3: Any, bucket: str, photo_key: str, layer_id: str, row: dict[str, Any]) -> list[dict[str, Any]]:
    """The checks kept for this field as its line is now, oldest first."""
    record = await stored_files.load_json(s3, bucket, _key(photo_key, layer_id, row["id"])) or []
    mark = _geometry_mark(row["geometry"])
    return [c for c in record if isinstance(c, dict) and c.get("line") == mark and c.get("method") == plant_finder.METHOD
            and all(isinstance(c.get(k), int) for k in ("app", "wrong", "missed")) and c.get("square") and c.get("at")]


async def summary(s3: Any, bucket: str, photo_key: str, layer_id: str, row: dict[str, Any]) -> Optional[dict[str, Any]]:
    return summed(await checks(s3, bucket, photo_key, layer_id, row))


class Busy(Exception):
    """Too many parts are being read at once; the message says so in plain words."""


MAX_WAITING = 4  # requests reading a part or waiting their turn; past it a new one is told to come back
_squares: OrderedDict[tuple[str, str, str, int, int], dict[str, Any]] = OrderedDict()
_refused: OrderedDict[tuple[str, str, str], set[tuple[int, int]]] = OrderedDict()  # squares found not countable
_memory_lock = threading.Lock()
_reading = threading.Lock()  # one read of the photo with the models at a time
_waiting = 0


def _feature(row: dict[str, Any]) -> dict[str, Any]:
    return {"type": "Feature", "geometry": row["geometry"], "properties": {"area_ha": row.get("area_ha", 0.0)}}


def _polygons(geometry: Any) -> list[Any]:
    """The polygons of a geometry: an outline that only touches a square's edge leaves lines and points behind."""
    return [g for g in getattr(geometry, "geoms", [geometry]) if g.geom_type == "Polygon" and not g.is_empty and g.area > 0]


def _plants_in(ds: Any, placed: plant_finder.Layout, square: tuple[int, int],
               models: plant_finder.Models) -> Optional[list[tuple[float, float]]]:
    """The plants the count holds in one square, as photo pixels, top to bottom then left to right (an order that
    does not depend on the model's own); None when the square is not counted (mostly outside the photo, or ground
    where single plants cannot be told apart)."""
    j, i = square
    rgb = plant_finder.read_square(ds, j, i)
    if rgb is None or plant_finder.picture_score(models, rgb) < plant_finder.PICTURE_CUT:
        return None
    centres = [((x1 + x2) / 2 + j * SQUARE_PX, (y1 + y2) / 2 + i * SQUARE_PX)
               for x1, y1, x2, y2 in plant_finder.plant_boxes(models, rgb)]
    return sorted(((x, y) for x, y in centres if placed.outline.contains(Point(x, y))), key=lambda p: (p[1], p[0]))


def plants_mark(plants: Sequence[Sequence[float]]) -> str:
    """Names one list of plants: a check is kept only against the list the reader was shown."""
    return hashlib.sha256(json.dumps([list(p) for p in plants]).encode()).hexdigest()[:16]


def _read(cog_url: str, photo_key: str, row: dict[str, Any], models: plant_finder.Models,
          skip: set[tuple[int, int]]) -> dict[str, Any]:
    """The part of the field to check next, with its plants: the first countable square, in the order nobody
    chooses, that is not in `skip` (the squares already checked). Blocking; one at a time."""
    field = (photo_key, _geometry_mark(row["geometry"]), plant_finder.METHOD)
    with _reading, rasterio.Env(**GDAL_HTTP_TIMEOUTS), rasterio.open(cog_url) as ds:
        placed = plant_finder.layout(ds, _feature(row))
        to_wgs84 = Transformer.from_crs(ds.crs, "EPSG:4326", always_xy=True).transform
        with _memory_lock:
            refused = set(_refused.get(field, ()))
        for square in in_order(field[1], placed.covered, placed.squares):
            if square in skip or square in refused:
                continue
            with _memory_lock:
                held = _squares.get((*field, *square))
            if held is not None:
                return held
            j, i = square
            part = _polygons(placed.outline.intersection(box(j * SQUARE_PX, i * SQUARE_PX, (j + 1) * SQUARE_PX,
                                                             (i + 1) * SQUARE_PX)))
            plants = _plants_in(ds, placed, square, models) if part else None
            if plants is None:
                with _memory_lock:
                    _refused.setdefault(field, set()).add(square)
                    while len(_refused) > _KEPT_FIELDS:
                        _refused.popitem(last=False)
                continue
            lonlat = [[round(float(v), 8) for v in to_wgs84(*(ds.transform * p))] for p in plants]
            to_map = lambda x, y, z=None: to_wgs84(*(ds.transform * (x, y)))  # noqa: E731
            found = {"square": [j, i], "plants": lonlat, "app": len(lonlat), "plants_mark": plants_mark(lonlat),
                     # the part of this square that lies in the field: plants are counted there and nowhere else
                     "part": mapping(MultiPolygon([reproject(to_map, g) for g in part])),
                     "side_m": round(SQUARE_PX * placed.metres_per_px, 1), "squares_in_field": len(placed.squares)}
            with _memory_lock:
                _squares[(*field, *square)] = found
                while len(_squares) > _KEPT_SQUARES:
                    _squares.popitem(last=False)
            return found
    raise CheckProblem("You have checked every part of this field where plants are counted.")


async def _next(cog_url: str, sync_s3: Any, bucket: str, photo_key: str, row: dict[str, Any],
                done: Sequence[dict[str, Any]]) -> dict[str, Any]:
    """The part to check after the ones in `done`. Busy when too many requests are already reading."""
    global _waiting
    if _waiting >= MAX_WAITING:
        raise Busy("Other parts are being read right now. Try again in a minute.")
    _waiting += 1
    try:
        skip = {(int(c["square"][0]), int(c["square"][1])) for c in done}
        models = await asyncio.to_thread(plant_finder.load_models, sync_s3, bucket)
        return await asyncio.to_thread(_read, cog_url, photo_key, row, models, skip)
    finally:
        _waiting -= 1


async def square(cog_url: str, s3: Any, sync_s3: Any, bucket: str, photo_key: str, layer_id: str,
                 row: dict[str, Any]) -> dict[str, Any]:
    """The next part of this field to check, with the plants the count holds there. There is no passing over a
    part: the next one comes when this one's check is kept, so the parts checked are the app's choice, in order."""
    done = await checks(s3, bucket, photo_key, layer_id, row)
    found = await _next(cog_url, sync_s3, bucket, photo_key, row, done)
    return {**found, "checked": summed(done, found["squares_in_field"]), "measured": MEASURED}


def added_plants(missed: Sequence[Sequence[float]], standing: Sequence[Sequence[float]]) -> list[tuple[float, float]]:
    """The plants a reader added, each once: a tap within SAME_PLANT_M of a ring left standing is the plant that
    ring already counts, and of two taps that close together the second is the first again."""
    out: list[tuple[float, float]] = []
    for lon, lat in missed:
        per_lon = 111_320 * math.cos(math.radians(lat))
        if not any(math.hypot((p[0] - lon) * per_lon, (p[1] - lat) * 111_320) < SAME_PLANT_M for p in (*standing, *out)):
            out.append((lon, lat))
    return out


_locks: dict[str, asyncio.Lock] = {}


async def keep(cog_url: str, s3: Any, sync_s3: Any, bucket: str, photo_key: str, layer_id: str, row: dict[str, Any],
               at: tuple[int, int], shown: str, wrong: Sequence[int], missed: Sequence[tuple[float, float]],
               user_id: str) -> dict[str, Any]:
    """Keep the reader's check of the part they were shown: which of the app's rings are not plants (their place in
    the part's list) and where the plants it missed are. Only the part that is next for this field is taken, and
    only against the list of plants the reader saw (`shown`, its plants_mark): the plants are read again here,
    never taken from the browser."""
    key = _key(photo_key, layer_id, row["id"])
    async with _locks.setdefault(key, asyncio.Lock()):
        done = await checks(s3, bucket, photo_key, layer_id, row)
        found = await _next(cog_url, sync_s3, bucket, photo_key, row, done)
        if tuple(found["square"]) != tuple(at) or found["plants_mark"] != shown:
            raise CheckProblem("This is not the part to check now. Close this and open Check the count again.")
        struck = sorted(set(wrong))
        if any(not 0 <= n < found["app"] for n in struck):
            raise CheckProblem("One of the rings marked is not in this part. Close this and open it again.")
        part = shape(found["part"]).buffer(1e-7)  # a tap on the line itself counts as inside
        if any(not part.contains(Point(lon, lat)) for lon, lat in missed):
            raise CheckProblem("A plant you added lies outside this part of the field. Take it away and save again.")
        missed = added_plants(missed, [p for n, p in enumerate(found["plants"]) if n not in set(struck)])
        check = {"square": found["square"], "app": found["app"], "wrong": len(struck), "missed": len(missed),
                 "you": found["app"] - len(struck) + len(missed), "struck": struck,
                 "added": [[round(lon, 8), round(lat, 8)] for lon, lat in missed],
                 "line": _geometry_mark(row["geometry"]), "method": plant_finder.METHOD, "by": user_id,
                 "at": datetime.now(timezone.utc).isoformat(timespec="seconds")}
        record = (await stored_files.load_json(s3, bucket, key) or []) + [check]
        await s3.put_object(Bucket=bucket, Key=key, Body=json.dumps(record).encode(), ContentType="application/json")
    return {"here": {"app": check["app"], "you": check["you"], "wrong": check["wrong"], "missed": check["missed"]},
            "checked": summed([*done, check], found["squares_in_field"]), "measured": MEASURED}
