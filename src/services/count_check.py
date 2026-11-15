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
import threading
from collections import OrderedDict
from datetime import datetime, timezone
from typing import Any, Optional, Sequence

import rasterio
from pyproj import Transformer
from shapely.geometry import Point, box, mapping, shape
from shapely.ops import transform as reproject

from src.services import plant_finder, stored_files
from src.services.gdal_http import GDAL_HTTP_TIMEOUTS
from src.services.plant_finder import SQUARE_PX, CannotCount

_STORE_PREFIX = "count_check/v1"
WHOLE_ENOUGH = 0.5  # squares with at least this share inside the field are offered first: more plants to check
MAX_MISSED = 400  # plants a reader may add in one square; far above the densest square counted
MAX_SEEN = 200  # squares one request may name as already shown
_KEPT_SQUARES = 16  # squares whose plants are held in memory between showing one and saving its check

MEASURED = (
    "This is your own count, on a part of your field the app chose. It tells you how the app's count did in the "
    "parts you checked, on this field: how many of its rings were not plants, and how many plants it missed. It is "
    "not scaled up: how well one part stands for the whole field was not measured. The more parts you check, the "
    "more of the field your count covers."
)


class CheckProblem(ValueError):
    """The check cannot be shown or kept as asked; the message says why, in plain words."""


def order_key(field_id: str, square: tuple[int, int]) -> str:
    """The squares of a field in an order nobody chooses: the same every time for one field, unrelated to where
    the square lies or what grows in it."""
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
    said = (f"In the {parts}{of} part{'s' if parts != 1 else ''} you checked, the app counted {app:,} plants and "
            f"you counted {you:,}. {wrong:,} of its rings {'was' if wrong == 1 else 'were'} not "
            f"{'a plant' if wrong == 1 else 'plants'}, and it missed {missed:,}.")
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
    return [c for c in record if c["line"] == mark and c["method"] == plant_finder.METHOD]


async def summary(s3: Any, bucket: str, photo_key: str, layer_id: str, row: dict[str, Any]) -> Optional[dict[str, Any]]:
    return summed(await checks(s3, bucket, photo_key, layer_id, row))


_squares: OrderedDict[tuple[str, str, int, int], dict[str, Any]] = OrderedDict()
_squares_lock = threading.Lock()


def _feature(row: dict[str, Any]) -> dict[str, Any]:
    return {"type": "Feature", "geometry": row["geometry"], "properties": {"area_ha": row.get("area_ha", 0.0)}}


def _plants_in(ds: Any, placed: plant_finder.Layout, square: tuple[int, int],
               models: plant_finder.Models) -> Optional[list[tuple[float, float]]]:
    """The plants the count holds in one square, as photo pixels; None when the square is not counted (mostly
    outside the photo, or ground where single plants cannot be told apart)."""
    j, i = square
    rgb = plant_finder.read_square(ds, j, i)
    if rgb is None or plant_finder.picture_score(models, rgb) < plant_finder.PICTURE_CUT:
        return None
    centres = [((x1 + x2) / 2 + j * SQUARE_PX, (y1 + y2) / 2 + i * SQUARE_PX)
               for x1, y1, x2, y2 in plant_finder.plant_boxes(models, rgb)]
    return [(x, y) for x, y in centres if placed.outline.contains(Point(x, y))]


def _read(cog_url: str, photo_key: str, row: dict[str, Any], models: plant_finder.Models,
          wanted: Optional[tuple[int, int]], skip: set[tuple[int, int]]) -> dict[str, Any]:
    """One square of the field with its plants: `wanted`, or the first countable one in the order nobody chooses
    that is not in `skip`. Blocking."""
    with rasterio.Env(**GDAL_HTTP_TIMEOUTS), rasterio.open(cog_url) as ds:
        placed = plant_finder.layout(ds, _feature(row))
        if not placed.squares:
            raise CannotCount("This field is too thin to count plants in.")
        if wanted is not None and wanted not in placed.squares:
            raise CheckProblem("That part is not in this field.")
        offered = [wanted] if wanted is not None else [s for s in in_order(row["id"], placed.covered, placed.squares)
                                                      if s not in skip]
        to_wgs84 = Transformer.from_crs(ds.crs, "EPSG:4326", always_xy=True).transform
        for square in offered:
            mark = (photo_key, _geometry_mark(row["geometry"]), square[0], square[1])
            with _squares_lock:
                held = _squares.get(mark)
            if held is not None:
                return held
            plants = _plants_in(ds, placed, square, models)
            if plants is None:
                continue
            j, i = square
            lonlat = [[round(float(v), 8) for v in to_wgs84(*(ds.transform * p))] for p in plants]
            part = placed.outline.intersection(box(j * SQUARE_PX, i * SQUARE_PX, (j + 1) * SQUARE_PX, (i + 1) * SQUARE_PX))
            found = {"square": [j, i], "plants": lonlat, "app": len(lonlat),
                     # the part of this square that lies in the field: plants are counted there and nowhere else
                     "part": mapping(reproject(lambda x, y, z=None: to_wgs84(*(ds.transform * (x, y))), part)),
                     "side_m": round(SQUARE_PX * placed.metres_per_px, 1), "squares_in_field": len(placed.squares)}
            with _squares_lock:
                _squares[mark] = found
                while len(_squares) > _KEPT_SQUARES:
                    _squares.popitem(last=False)
            return found
    if wanted is not None:
        raise CheckProblem("Plants are not counted in that part of the field.")
    raise CheckProblem("Every part of this field where plants are counted has been shown. "
                       "Close this and open it again to see them once more.")


async def square(cog_url: str, s3: Any, sync_s3: Any, bucket: str, photo_key: str, layer_id: str, row: dict[str, Any],
                 seen: Sequence[tuple[int, int]]) -> dict[str, Any]:
    """The next square of this field to check, with the plants the count holds there. Squares already checked, and
    those in `seen`, are passed over."""
    done = await checks(s3, bucket, photo_key, layer_id, row)
    skip = {(int(c["square"][0]), int(c["square"][1])) for c in done} | {(int(j), int(i)) for j, i in seen}
    models = await asyncio.to_thread(plant_finder.load_models, sync_s3, bucket)
    found = await asyncio.to_thread(_read, cog_url, photo_key, row, models, None, skip)
    return {**found, "checked": summed(done, found["squares_in_field"]), "measured": MEASURED}


_locks: dict[str, asyncio.Lock] = {}


async def keep(cog_url: str, s3: Any, sync_s3: Any, bucket: str, photo_key: str, layer_id: str, row: dict[str, Any],
               at: tuple[int, int], wrong: Sequence[int], missed: Sequence[tuple[float, float]],
               user_id: str) -> dict[str, Any]:
    """Keep the reader's check of one square: which of the app's rings are not plants (their place in the square's
    list) and where the plants it missed are. The square's plants are read again here, never taken from the
    browser. A square checked before is replaced."""
    models = await asyncio.to_thread(plant_finder.load_models, sync_s3, bucket)
    found = await asyncio.to_thread(_read, cog_url, photo_key, row, models, at, set())
    struck = sorted(set(wrong))
    if any(not 0 <= n < found["app"] for n in struck):
        raise CheckProblem("One of the rings marked is not in this part. Open the part again.")
    if len(missed) > MAX_MISSED:
        raise CheckProblem(f"At most {MAX_MISSED} plants can be added in one part.")
    part = shape(found["part"]).buffer(1e-7)  # a tap on the line itself counts as inside
    if any(not part.contains(Point(lon, lat)) for lon, lat in missed):
        raise CheckProblem("A plant you added lies outside this part of the field. Take it away and save again.")
    check = {"square": found["square"], "app": found["app"], "wrong": len(struck), "missed": len(missed),
             "you": found["app"] - len(struck) + len(missed), "struck": struck,
             "added": [[round(lon, 8), round(lat, 8)] for lon, lat in missed],
             "line": _geometry_mark(row["geometry"]), "method": plant_finder.METHOD, "by": user_id,
             "at": datetime.now(timezone.utc).isoformat(timespec="seconds")}
    key = _key(photo_key, layer_id, row["id"])
    async with _locks.setdefault(key, asyncio.Lock()):
        record = await stored_files.load_json(s3, bucket, key) or []
        record = [c for c in record if not (c["square"] == check["square"] and c["line"] == check["line"]
                                            and c["method"] == check["method"])] + [check]
        await s3.put_object(Bucket=bucket, Key=key, Body=json.dumps(record).encode(), ContentType="application/json")
    done = [c for c in record if c["line"] == check["line"] and c["method"] == check["method"]]
    return {"here": {"app": check["app"], "you": check["you"], "wrong": check["wrong"], "missed": check["missed"]},
            "checked": summed(done, found["squares_in_field"]), "measured": MEASURED}
