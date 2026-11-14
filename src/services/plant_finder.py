"""Crop plants in one plot of a drone photo, found by their shape, with the other green around them flagged.

Colour finds green things but cannot say which are crop plants: weeds, grass and bushes are green too (Test 80:
the colour counter gave 1,503 where two markers counted 545 crop plants). Shape can. So three steps, each as it
was tested, on the photo's fixed grid of 12.5 m squares (SQUARE_PX pixels):

1. The picture check (DINOv2-small features, a small rule on top; cut PICTURE_CUT, Test 54) refuses a square where
   single plants cannot be told apart: closed canopy, pasture, bush, buildings. Refused squares are not counted and
   the answer says how much of the plot they are.
2. The plant finder ("the student": a small detector taught on 76 squares whose markers were told to mark
   crop plants and no weeds; Test 51, cut STUDENT_CUT, overlap STUDENT_OVERLAP) boxes each plant in
   the squares that are left. A box belongs to the plot when its centre lies inside the plot.
3. Other green: the peaks of green that colour finds (plant_counts.crowns) and that lie in a counted square outside
   every plant box. They are drawn apart from the plants. What each one is (weed, grass tuft, bush, litter) is
   not known and is not said.

Measured on Cyampirita (3.25 cm pixels), on 16 squares two blind markers fully counted, 545 plants: the total
came within 18% (Test 52); 81% of plant boxes sat on a marked crop plant and 90% of marked plants had a box;
95% of flagged other-green peaks were on no marked plant and they were 71% of all such peaks (Test 80). So a
count here is an estimate: about one dot in five is not a crop plant. Not measured: any other photo, crop mix
or pixel size, which is why a photo with other pixels is refused.

The models run on ONNX Runtime. Fetched from object storage once and checked by sha256; `scripts/
upload_plant_finder.py` puts them there. The plain ONNX path gives the tested student's counts on 56 of the 57
reference squares and 156 for 155 on the other; the picture scores agree to 0.0001.
"""

from __future__ import annotations

import hashlib
import json
import math
import threading
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from typing import Any, Optional, Sequence

import cv2
import numpy as np
import rasterio
from affine import Affine
from PIL import Image
from pyproj import Transformer
from rasterio.features import geometry_mask
from rasterio.windows import Window
from shapely.geometry import Point, box, shape
from shapely.ops import transform as reproject
from shapely.prepared import prep

from src.services import plant_counts, stored_files

SQUARE_PX = 384  # one square of the photo's grid: 12.5 m at the pixels the finder was taught on
PIXEL_M = (0.029, 0.036)  # the finder was taught and tested at 3.25 cm; other pixels are not measured
STUDENT_CUT = 0.15  # a box counts as a plant above this confidence (frozen in Test 51)
STUDENT_OVERLAP = 0.3  # of two boxes overlapping more than this, the surer one stays (frozen in Test 51)
PICTURE_CUT = 0.74  # a square scoring under this is refused (Test 54)
MIN_READABLE = 0.9  # a square with less of the photo than this is not read
MIN_PLOT_SHARE = 0.10  # a square the plot covers less of than this is left out: a sliver cannot be judged
MAX_PLOT_HA = 0.5  # about a second a square on the server: larger plots take too long to answer live
METHOD = "plant finder s1 + picture check 0.74 + other green v1"
_STORE_PREFIX = "plant_finder/v1"

STUDENT_KEY = "models/plant-student-s1-c6a0477530451b92.onnx"
STUDENT_SHA256 = "c6a0477530451b920d9fc1a5e3763721d03e7c17f1801435246eaea6b26a2791"
PICTURE_KEY = "models/picture-check-dinov2-small-18b57932b5e620a5.onnx"
PICTURE_SHA256 = "18b57932b5e620a504a915a49fda33c202da8131f88454304975372bd3419c73"
RULE_KEY = "models/picture-check-rule-0c1e114e53f37c2f.npz"
RULE_SHA256 = "0c1e114e53f37c2fbee8d19605d9bd0db12a93a6998c37f17209a0c54ed6b167"
_CACHE = "/tmp/ingabe_cache/models"
_MEAN = np.array([0.485, 0.456, 0.406], np.float32)
_STD = np.array([0.229, 0.224, 0.225], np.float32)


class CannotCount(ValueError):
    """The photo or plot cannot give a plant count; the message says why, in plain words."""


@dataclass(frozen=True)
class PlantsFound:
    plot: int
    plants: int  # plant boxes whose centre lies in the plot, in the counted squares
    other_green: int  # green peaks in the counted squares outside every plant box
    plot_area_m2: float
    counted_area_m2: float  # the part of the plot in squares the picture check let through
    counted_share: float
    per_ha: Optional[int]  # plants per hectare of the counted part; None when nothing was counted
    squares: int  # squares of the grid the plot covers at least MIN_PLOT_SHARE of
    squares_counted: int
    squares_refused: int  # the picture check: single plants cannot be told apart there
    squares_unreadable: int  # mostly outside the photo
    cm_per_px: float
    method: str
    done_at: str
    points: dict[str, Any]  # GeoJSON points, WGS84; properties.kind "plant" (kind_code 0) or "other_green" (1)


class Models:
    """The plant finder, the picture check and its rule, loaded once."""

    def __init__(self, student_path: str, picture_path: str, rule_path: str, threads: int = 4) -> None:
        import onnxruntime  # lazy: loaded only when a count is asked for

        options = onnxruntime.SessionOptions()
        options.intra_op_num_threads = threads
        self.student = onnxruntime.InferenceSession(student_path, options, providers=["CPUExecutionProvider"])
        self.picture = onnxruntime.InferenceSession(picture_path, options, providers=["CPUExecutionProvider"])
        with np.load(rule_path) as rule:
            self.rule = {name: rule[name] for name in rule.files}


_models: Optional[Models] = None
_models_lock = threading.Lock()


def load_models(s3: Any, bucket: str) -> Models:
    """The models, fetched from object storage once and refused when a file does not match its sha256. `s3` is a
    synchronous boto3 client: call this from a thread in async code."""
    global _models
    with _models_lock:
        if _models is None:
            paths = [stored_files.ensure_local_copy(s3, bucket, key, sha, f"{_CACHE}/{key.rsplit('/', 1)[1]}", what)
                     for key, sha, what in ((STUDENT_KEY, STUDENT_SHA256, "plant finder"),
                                            (PICTURE_KEY, PICTURE_SHA256, "picture check"),
                                            (RULE_KEY, RULE_SHA256, "picture check rule"))]
            _models = Models(*paths)
        return _models


def kept(confidence: Sequence[float], boxes: Sequence[Sequence[float]], cut: float = STUDENT_CUT,
         overlap: float = STUDENT_OVERLAP) -> list[int]:
    """Which boxes are plants: above the cut, and of two that overlap by more than `overlap` (shared area over
    total area) only the surer one. The order and rule of Test 51."""
    out: list[int] = []
    for i in sorted((i for i, c in enumerate(confidence) if c > cut), key=lambda i: -confidence[i]):
        a = boxes[i]
        clear = True
        for j in out:
            b = boxes[j]
            w = min(a[2], b[2]) - max(a[0], b[0])
            h = min(a[3], b[3]) - max(a[1], b[1])
            if w > 0 and h > 0 and w * h / ((a[2] - a[0]) * (a[3] - a[1]) + (b[2] - b[0]) * (b[3] - b[1]) - w * h) > overlap:
                clear = False
                break
        if clear:
            out.append(i)
    return out


def plant_boxes(models: Models, rgb: np.ndarray) -> list[tuple[float, float, float, float]]:
    """The plants in one square (SQUARE_PX x SQUARE_PX x 3, 0..255) as (x1, y1, x2, y2) in the square's pixels."""
    size = models.student.get_inputs()[0].shape[2]
    scaled = cv2.resize(rgb, (size, size), interpolation=cv2.INTER_LINEAR).astype(np.float32).transpose(2, 0, 1)[None] / 255.0
    found = models.student.run(None, {"images": np.ascontiguousarray(scaled)})[0][0]
    back = rgb.shape[0] / size
    boxes = [[float(v) * back for v in row[:4]] for row in found]
    return [(boxes[i][0], boxes[i][1], boxes[i][2], boxes[i][3]) for i in kept([float(c) for c in found[:, 4]], boxes)]


def picture_score(models: Models, rgb: np.ndarray) -> float:
    """How much one square looks like ground where single plants can be counted (0..1)."""
    small = np.asarray(Image.fromarray(rgb).resize((392, 392), Image.BICUBIC), np.float32)
    vector = models.picture.run(None, {"x": ((small / 255 - _MEAN) / _STD).transpose(2, 0, 1)[None]})[0]
    rule = models.rule
    z = ((vector - rule["mean"]) / rule["scale"] - rule["pca_mean"]) @ rule["pca"].T @ rule["coef"] + rule["intercept"][0]
    return float(1 / (1 + np.exp(-z)))


def _ground_metres_per_px(ds: Any, lat: float) -> float:
    """A pixel's ground size: Web Mercator metres shrink by cos(latitude) on the ground."""
    size = abs(ds.transform.a)
    return size * math.cos(math.radians(lat)) if ds.crs and ds.crs.to_epsg() == 3857 else size


def find_plants(cog_url: str, feature: dict[str, Any], models: Models) -> PlantsFound:
    """The plants and the other green in one plot feature (WGS84, with `number` and `area_ha`); blocking, run it
    in a thread. CannotCount when the photo or plot is outside what was measured."""
    props = feature["properties"]
    if props.get("area_ha", 0) > MAX_PLOT_HA:
        raise CannotCount(f"This plot is {props['area_ha']:.1f} ha; plants are counted live in plots up to "
                          f"{MAX_PLOT_HA} ha. Pick a smaller plot.")
    with rasterio.open(cog_url) as ds:
        outline_wgs84 = shape(feature["geometry"])
        metres_per_px = _ground_metres_per_px(ds, outline_wgs84.centroid.y)
        if not PIXEL_M[0] <= metres_per_px <= PIXEL_M[1]:
            raise CannotCount(f"This photo has {metres_per_px * 100:.1f} cm per pixel; the plant finder was taught and "
                              "tested on 3 cm pixels only.")
        to_photo = Transformer.from_crs("EPSG:4326", ds.crs, always_xy=True).transform
        to_wgs84 = Transformer.from_crs(ds.crs, "EPSG:4326", always_xy=True).transform
        to_pixel = ~ds.transform
        outline = reproject(lambda x, y, z=None: to_pixel * (x, y), reproject(to_photo, outline_wgs84)).buffer(0)
        inside = prep(outline)
        west, north, east, south = outline.bounds  # pixel units: x right, y down
        grid = [(j, i) for i in range(int(north // SQUARE_PX), int(south // SQUARE_PX) + 1)
                for j in range(int(west // SQUARE_PX), int(east // SQUARE_PX) + 1)]
        covered = {(j, i): outline.intersection(box(j * SQUARE_PX, i * SQUARE_PX, (j + 1) * SQUARE_PX,
                                                    (i + 1) * SQUARE_PX)).area / SQUARE_PX ** 2 for j, i in grid}
        squares = [key for key in grid if covered[key] >= MIN_PLOT_SHARE]
        plants: list[tuple[float, float]] = []
        boxes: list[tuple[float, float, float, float]] = []
        other: list[tuple[float, float]] = []
        counted = refused = unreadable = 0
        counted_area_px = 0.0
        for j, i in squares:
            window = Window(j * SQUARE_PX, i * SQUARE_PX, SQUARE_PX, SQUARE_PX)
            if ds.count < 3 or (ds.read_masks(1, window=window, boundless=True) > 0).mean() < MIN_READABLE:
                unreadable += 1
                continue
            rgb = np.ascontiguousarray(ds.read([1, 2, 3], window=window, boundless=True, fill_value=0).transpose(1, 2, 0))
            if picture_score(models, rgb) < PICTURE_CUT:
                refused += 1
                continue
            counted += 1
            counted_area_px += covered[(j, i)] * SQUARE_PX ** 2
            here = [(x1 + j * SQUARE_PX, y1 + i * SQUARE_PX, x2 + j * SQUARE_PX, y2 + i * SQUARE_PX)
                    for x1, y1, x2, y2 in plant_boxes(models, rgb)]
            boxes += here
            plants += [((x1 + x2) / 2, (y1 + y2) / 2) for x1, y1, x2, y2 in here
                       if inside.contains(Point((x1 + x2) / 2, (y1 + y2) / 2))]
            # green peaks of this square that lie in the plot and outside every plant box of this square
            in_plot = ~geometry_mask([outline], out_shape=(SQUARE_PX, SQUARE_PX),
                                     transform=Affine.translation(j * SQUARE_PX, i * SQUARE_PX))
            peaks, _ = plant_counts.crowns(rgb, in_plot, metres_per_px)
            other += [(j * SQUARE_PX + float(col), i * SQUARE_PX + float(row)) for row, col in peaks
                      if not any(x1 <= j * SQUARE_PX + col <= x2 and y1 <= i * SQUARE_PX + row <= y2
                                 for x1, y1, x2, y2 in here)]
        transform = ds.transform

    def lonlat(x: float, y: float) -> list[float]:
        lon, lat = to_wgs84(*(transform * (x, y)))
        return [round(float(lon), 8), round(float(lat), 8)]

    plot_area = outline.area * metres_per_px ** 2
    counted_area = counted_area_px * metres_per_px ** 2
    features = [{"type": "Feature", "geometry": {"type": "Point", "coordinates": lonlat(x, y)},
                 "properties": {"plot": props["number"], "kind": kind, "kind_code": code}}
                for code, (kind, places) in enumerate((("plant", plants), ("other_green", other))) for x, y in places]
    return PlantsFound(
        plot=props["number"], plants=len(plants), other_green=len(other), plot_area_m2=round(plot_area, 1),
        counted_area_m2=round(counted_area, 1), counted_share=round(counted_area / plot_area, 3) if plot_area else 0.0,
        per_ha=int(round(len(plants) / counted_area * 10_000, -1)) if counted_area else None,
        squares=len(squares), squares_counted=counted, squares_refused=refused, squares_unreadable=unreadable,
        cm_per_px=round(metres_per_px * 100, 1), method=METHOD,
        done_at=datetime.now(timezone.utc).isoformat(timespec="seconds"),
        points={"type": "FeatureCollection", "features": features})


def _store_key(photo_key: str, feature: dict[str, Any]) -> str:
    """One answer per photo, plot outline and method."""
    raw = json.dumps([photo_key, feature["geometry"], METHOD], sort_keys=True)
    return f"{_STORE_PREFIX}/{hashlib.sha256(raw.encode()).hexdigest()[:32]}.json"


async def load_found(s3: Any, bucket: str, photo_key: str, feature: dict[str, Any]) -> Optional[PlantsFound]:
    record = await stored_files.load_json(s3, bucket, _store_key(photo_key, feature))
    return None if record is None else PlantsFound(**record)


async def save_found(s3: Any, bucket: str, photo_key: str, feature: dict[str, Any], found: PlantsFound) -> None:
    await s3.put_object(Bucket=bucket, Key=_store_key(photo_key, feature), Body=json.dumps(asdict(found)).encode(),
                        ContentType="application/json")
