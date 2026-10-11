"""Which crop grows in each plot, from what the plot looks like and the plots people checked on the same photo.

How: each plot is cut into up to MAX_SQUARES squares of SIDE_M metres lying fully inside it, each square becomes
a fingerprint of what it looks like (DINOv2-small, Meta, Apache-2.0; converted to TorchScript by
scripts/export_crop_fingerprint_model.py and kept in object storage, checked by sha256), and the plot's
fingerprint is their mean. A plot is given the crop of its NEIGHBOURS most similar checked plots, weighted by
similarity, only when that crop has at least MIN_EXAMPLES checked plots and the neighbours agree (share at
least MIN_SHARE); otherwise it stays "unsure", with the crops it is closest to as candidates. A checked plot's
own call is made with its check hidden, so the record of how often the calls are right is honest.

Measured on 2026-10-10 on Cyampirita (3.2 cm per pixel) against 34 plots checked from the drone pictures, each
hidden in turn (3 nearest, no abstaining): 25 right; maize 13 of 14, banana 5 of 5, grass 5 of 5, other 2 of 2;
cassava, fallow, vegetables and fruit trees 0 of 8, each with 1 to 3 examples. Always naming maize gets 14.
Eight squares a plot score as well as forty. The calls are only as good as the checks: crops nobody checked
can never be named.
"""

from __future__ import annotations

import asyncio
import hashlib
import io
import json
import logging
import math
import os
import random
import time
from collections import Counter
from dataclasses import dataclass, replace
from typing import Any, Callable, Optional

import numpy as np
import rasterio
from PIL import Image
from pyproj import Transformer
from rasterio.enums import Resampling
from rasterio.windows import from_bounds
from shapely.geometry import box, shape
from shapely.ops import transform as reproject

from src.services import background_jobs, drone_plots, drone_vision
from src.utils import get_s3_client

SIDE_M = 4.5  # metres across each square (the size of the Rwandan drone squares the method was tried on)
SQUARE_PX = 200  # pixels across each square as the model is shown it (2.25 cm per pixel)
MAX_SQUARES = 8  # squares per plot; 8 scored as well as 40 on Cyampirita
NEIGHBOURS = 3
MIN_EXAMPLES = 3  # checked plots a crop needs before it can be named
MIN_SHARE = 0.6  # share of the neighbours' (similarity-weighted) vote the named crop must have
MODEL_KEY = "models/dinov2_small_fingerprint-5ecef40ac402c21a.pt"
MODEL_SHA256 = "5ecef40ac402c21a3835c321c8df0617b3f524199da07153d2f3871f1337a533"
MODEL_NAME = "DINOv2-small fingerprints"
_MODEL_PATH = "/tmp/ingabe_cache/models/dinov2_small_fingerprint.pt"
_MEAN = np.array([0.485, 0.456, 0.406], np.float32)
_STD = np.array([0.229, 0.224, 0.225], np.float32)
_STORE_PREFIX = "crop_fingerprints/v1"
_BATCH = 16
CHECKPOINT_PLOTS = 50  # fingerprints so far are kept this often, so a failed run resumes
REOPEN_S = 1800  # a long run reopens the photo with a fresh link this often (links last an hour)
_LINK_SECONDS = 3600

logger = logging.getLogger(__name__)

_model: Any = None
_kept: dict[str, dict[int, np.ndarray]] = {}


@dataclass(frozen=True)
class CropCall:
    number: int
    crop: str  # "unsure" unless the rules above name one
    share: float  # the named (or leading) crop's share of the neighbours' vote
    candidates: tuple[str, ...]  # when unsure: the crops the plot is closest to, leading first
    neighbours: tuple[tuple[int, str, float], ...]  # (checked plot, its crop, similarity)


# --- The model ---------------------------------------------------------------------------------

def _sha256(path: str) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as f:
        for block in iter(lambda: f.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def ensure_model(s3: Any, bucket: str) -> str:
    """The local path of the fingerprint model, fetched from object storage once and checked by sha256."""
    if not (os.path.exists(_MODEL_PATH) and _sha256(_MODEL_PATH) == MODEL_SHA256):
        os.makedirs(os.path.dirname(_MODEL_PATH), exist_ok=True)
        partial = _MODEL_PATH + ".part"
        s3.download_file(bucket, MODEL_KEY, partial)
        if _sha256(partial) != MODEL_SHA256:
            os.remove(partial)
            raise RuntimeError(f"the fingerprint model in storage ({MODEL_KEY}) does not match its sha256")
        os.replace(partial, _MODEL_PATH)
    return _MODEL_PATH


def _load_model(path: str) -> Any:
    global _model
    if _model is None:
        import torch  # lazy: torch takes seconds to import; only the fingerprint job needs it

        torch.set_num_threads(max(1, min(4, os.cpu_count() or 1)))
        _model = torch.jit.load(path, map_location="cpu").eval()
    return _model


def _prepare(square: np.ndarray) -> np.ndarray:
    """A square as the model expects it: 256 px (bicubic), the middle 224 px, normalised (as DINOv2's own
    image processor does; checked equal to within 0.0003 on 16 Cyampirita squares)."""
    image = Image.fromarray(square).resize((256, 256), Image.BICUBIC).crop((16, 16, 240, 240))
    pixels = (np.asarray(image, dtype=np.float32) / 255.0 - _MEAN) / _STD
    return pixels.transpose(2, 0, 1)


def fingerprint(squares: list[np.ndarray], model: Any) -> np.ndarray:
    """One fingerprint for the squares of one plot: the mean of theirs, of length 1."""
    import torch  # lazy: torch takes seconds to import; only the fingerprint job needs it

    rows = []
    with torch.no_grad():
        for start in range(0, len(squares), _BATCH):
            batch = torch.from_numpy(np.stack([_prepare(s) for s in squares[start:start + _BATCH]]))
            rows.append(model(batch).numpy())
    mean = np.concatenate(rows).mean(axis=0)
    return mean / (np.linalg.norm(mean) or 1.0)


# --- Squares from the photo ----------------------------------------------------------------------

def _ground_scale(ds: Any, lat: float) -> float:
    """Photo units per ground metre: Web Mercator units are larger than ground metres by 1 / cos(latitude)."""
    return 1 / math.cos(math.radians(lat)) if ds.crs and ds.crs.to_epsg() == 3857 else 1.0


def plot_squares(ds: Any, outline: Any, number: int, lat: float) -> list[np.ndarray]:
    """Up to MAX_SQUARES RGB squares of SIDE_M metres lying fully inside a plot (outline in the photo's CRS),
    picked the same way every time for the same plot."""
    side = SIDE_M * _ground_scale(ds, lat)
    x0, y0, x1, y1 = outline.bounds
    cells = []
    y = y0
    while y + side <= y1:
        x = x0
        while x + side <= x1:
            cell = box(x, y, x + side, y + side)
            if outline.contains(cell):
                cells.append(cell)
            x += side
        y += side
    random.Random(number).shuffle(cells)
    squares = []
    for cell in cells[:MAX_SQUARES]:
        window = from_bounds(*cell.bounds, ds.transform)
        bands = ds.read([1, 2, 3], window=window, out_shape=(3, SQUARE_PX, SQUARE_PX),
                        resampling=Resampling.bilinear, boundless=True, fill_value=0)
        squares.append(np.ascontiguousarray(np.transpose(bands, (1, 2, 0))).astype("uint8"))
    return squares


def fingerprint_plots(photo_url: Callable[[], str], plots: drone_plots.PlotSet, model_path: str,
                      progress: background_jobs.Progress, done: Optional[dict[int, np.ndarray]] = None,
                      checkpoint: Callable[[dict[int, np.ndarray]], None] = lambda prints: None
                      ) -> dict[int, np.ndarray]:
    """The fingerprint of every plot that holds at least one whole square (blocking: minutes per photo). Plots in
    `done` are kept as they are; `checkpoint` gets the fingerprints so far every CHECKPOINT_PLOTS plots, and the
    photo is reopened with a fresh link (`photo_url`) every REOPEN_S seconds, so a long run neither loses its work
    nor outlives its link."""
    model = _load_model(model_path)
    features = plots.geojson["features"]
    prints: dict[int, np.ndarray] = dict(done or {})
    started = opened = time.monotonic()
    squares_seen = 0
    ds = rasterio.open(photo_url())
    try:
        to_photo = Transformer.from_crs("EPSG:4326", ds.crs, always_xy=True).transform
        for count, feature in enumerate(features, 1):
            number = feature["properties"]["number"]
            if number not in prints:
                if time.monotonic() - opened > REOPEN_S:
                    ds.close()
                    ds, opened = rasterio.open(photo_url()), time.monotonic()
                outline_wgs84 = shape(feature["geometry"])
                squares = plot_squares(ds, reproject(to_photo, outline_wgs84), number, outline_wgs84.centroid.y)
                if squares:
                    prints[number] = fingerprint(squares, model)
                    squares_seen += len(squares)
            if count % CHECKPOINT_PLOTS == 0:
                checkpoint(prints)
                logger.info("crop fingerprints: %d of %d plots, %.2f squares/s", count, len(features),
                            squares_seen / max(time.monotonic() - started, 1e-6))
            progress(count, len(features))
    finally:
        ds.close()
    return prints


# --- Calls -------------------------------------------------------------------------------------

def calls(prints: dict[int, np.ndarray], checks: dict[int, str]) -> dict[int, CropCall]:
    """The crop call for every plot with a fingerprint, from the checked plots (a checked plot's own check
    hidden). Crops the checks name fewer than MIN_EXAMPLES times are never named."""
    checked = {n: c for n, c in checks.items() if n in prints and c != "unsure"}
    out: dict[int, CropCall] = {}
    for number, vector in prints.items():
        others = {n: c for n, c in checked.items() if n != number}
        if not others:
            continue
        examples = Counter(others.values())
        near = sorted(((float(vector @ prints[n]), n) for n in others), reverse=True)[:NEIGHBOURS]
        votes: Counter[str] = Counter()
        for similarity, n in near:
            votes[others[n]] += max(similarity, 0.0)
        total = sum(votes.values()) or 1.0
        ranked = [(crop, weight / total) for crop, weight in votes.most_common()]
        leading, share = ranked[0]
        named = leading if share >= MIN_SHARE and examples[leading] >= MIN_EXAMPLES else "unsure"
        out[number] = CropCall(
            number=number, crop=named, share=round(share, 2),
            candidates=() if named != "unsure" else tuple(c for c, _ in ranked[:2]),
            neighbours=tuple((n, others[n], round(s, 3)) for s, n in near))
    return out


def apply(survey: Optional[drone_vision.Survey], crop_calls: dict[int, CropCall]) -> Optional[drone_vision.Survey]:
    """The survey with each plot's crop taken from its call instead of the vision model's looks. Once a photo has
    calls, a plot without one (too small for a whole square) is "unsure", not left to the looks, so every crop
    on the photo comes from one method."""
    if survey is None or not crop_calls:
        return survey
    looks = dict(survey.looks)
    for number, look in survey.looks.items():
        call = crop_calls.get(number)
        if call is None:
            looks[number] = replace(look, main_crop="unsure", candidates=(), confidence="low", source="fingerprints")
            continue
        looks[number] = replace(look, main_crop=call.crop, candidates=call.candidates,
                                confidence="medium" if call.crop != "unsure" and call.share >= 0.9 else "low",
                                other_crops=[c for c in look.other_crops if c != call.crop], source="fingerprints")
    return replace(survey, looks=looks)


def looked(survey: Optional[drone_vision.Survey], prints: Optional[dict[int, np.ndarray]],
           checks: dict[int, str]) -> Optional[drone_vision.Survey]:
    """The survey with crops from the calls when there are fingerprints and checks; as it was otherwise. The one
    way the cards and Sage take a plot's crop (checks are applied on top by field_checks.apply)."""
    return apply(survey, calls(prints, checks)) if prints else survey


# --- Plots people named from the drone pictures ------------------------------------------------

_EXAMPLES_PREFIX = "crop_examples/v1"


def _examples_key(project_id: str, plot_set: str) -> str:
    return f"{_EXAMPLES_PREFIX}/{project_id}/{hashlib.sha256(plot_set.encode()).hexdigest()[:32]}.json"


async def load_examples(s3: Any, bucket: str, project_id: Optional[str], plot_set: str) -> dict[int, str]:
    """Crops people named by looking at the drone pictures (not in the field), per project and plot set. They
    teach the calls but are never shown or counted as checks on the ground."""
    if not project_id:
        return {}
    try:
        response = await s3.get_object(Bucket=bucket, Key=_examples_key(project_id, plot_set))
    except s3.exceptions.NoSuchKey:
        return {}
    async with response["Body"] as body:
        data = json.loads(await body.read())
    return {int(n): c for n, c in data.get("examples", {}).items() if c in drone_vision.CROPS and c != "unsure"}


async def save_examples(s3: Any, bucket: str, project_id: str, plot_set: str, examples: dict[int, str],
                        source: str) -> None:
    """Keep the crops people named from the pictures ("unsure" answers are left out); `source` says who and how."""
    kept = {str(n): c for n, c in sorted(examples.items()) if c in drone_vision.CROPS and c != "unsure"}
    body = json.dumps({"examples": kept, "source": source}).encode()
    await s3.put_object(Bucket=bucket, Key=_examples_key(project_id, plot_set), Body=body,
                        ContentType="application/json")


# --- Kept fingerprints and running jobs -------------------------------------------------------

def store_key(photo_key: str, plots: drone_plots.PlotSet) -> str:
    """One set of fingerprints per photo, plot set and model."""
    return f"{_STORE_PREFIX}|{photo_key}|{plots.source}|{plots.found_at}|{MODEL_SHA256[:16]}"


def _object_key(key: str) -> str:
    return f"{_STORE_PREFIX}/{hashlib.sha256(key.encode()).hexdigest()[:32]}.npz"


def _to_bytes(prints: dict[int, np.ndarray]) -> bytes:
    out = io.BytesIO()
    numbers = np.array(sorted(prints), dtype=np.int64)
    vectors = np.stack([prints[n] for n in numbers]).astype(np.float32) if len(numbers) else np.zeros((0, 768), np.float32)
    np.savez_compressed(out, numbers=numbers, vectors=vectors)
    return out.getvalue()


def _from_bytes(raw: bytes) -> dict[int, np.ndarray]:
    data = np.load(io.BytesIO(raw))
    return {int(n): v for n, v in zip(data["numbers"], data["vectors"])}


async def load(s3: Any, bucket: str, key: str) -> Optional[dict[int, np.ndarray]]:
    if key in _kept:
        return _kept[key]
    try:
        response = await s3.get_object(Bucket=bucket, Key=_object_key(key))
    except s3.exceptions.NoSuchKey:
        return None
    async with response["Body"] as body:
        prints = _from_bytes(await body.read())
    _kept[key] = prints
    return prints


def job(key: str) -> Optional[background_jobs.Job]:
    return background_jobs.status(f"fingerprints:{key}")


def _partial_key(key: str) -> str:
    return _object_key(key + "|partial")


def start(s3: Any, bucket: str, key: str, cog_key: str, plots: drone_plots.PlotSet) -> background_jobs.Job:
    """Fingerprint every plot in the background, once, resuming from the last checkpoint of a failed run; the
    running or failed job is returned."""
    store = get_s3_client()

    def photo_url() -> str:
        return store.generate_presigned_url("get_object", Params={"Bucket": bucket, "Key": cog_key},
                                            ExpiresIn=_LINK_SECONDS)

    def earlier() -> dict[int, np.ndarray]:
        try:
            return _from_bytes(store.get_object(Bucket=bucket, Key=_partial_key(key))["Body"].read())
        except store.exceptions.NoSuchKey:
            return {}

    def checkpoint(prints: dict[int, np.ndarray]) -> None:
        store.put_object(Bucket=bucket, Key=_partial_key(key), Body=_to_bytes(prints),
                         ContentType="application/octet-stream")

    async def work(progress: background_jobs.Progress) -> None:
        path = await asyncio.to_thread(ensure_model, store, bucket)
        done = await asyncio.to_thread(earlier)
        prints = await asyncio.to_thread(fingerprint_plots, photo_url, plots, path, progress, done, checkpoint)
        await s3.put_object(Bucket=bucket, Key=_object_key(key), Body=_to_bytes(prints),
                            ContentType="application/octet-stream")
        _kept[key] = prints

    return background_jobs.start(f"fingerprints:{key}", work)
