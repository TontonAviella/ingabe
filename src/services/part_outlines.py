"""Outlines of parts on inspection photos, from FastSAM (fastsam_model): around each suggestion a person is shown (a
box prompt on a padded crop), and around a part a person taps (a point prompt). They only change how a part is
drawn: the box stays what is reviewed and counted.

Eye-checked 2026-10-14 on one Louisville tower photo (5472 x 3648 px, 64 m up), not measured: 11 of 12 shown
suggestions (6 panel antennas, 6 radio units) were outlined along the part, one antenna as a sliver; 12 boxes took
4.2 s on the server's CPU. Of three taps, two traced the antenna tapped and one the building behind it, so a traced
outline is always shown to the person before it is kept, and one covering most of the window it was traced in is
refused (MAX_TRACE_SHARE, chosen by eye, not measured).

The outlines of a finder job's suggestions are worked out once, in the background, and kept in storage by job."""

from __future__ import annotations

import asyncio
import json
import logging
import os
from dataclasses import dataclass
from typing import Any, Optional, Sequence

import cv2
import numpy as np
from redis.exceptions import LockError

from src.services import fastsam_model, photo_inspections, stored_files
from src.services.example_finder_pictures import Box, FinderInputError, InspectionPhoto, PictureReadError
from src.services.photo_inspections import PhotoFacts

logger = logging.getLogger(__name__)

METHOD = "fastsam-s/box-crop-640/v1"  # changes when the method does: outlines of another method are not reused
STORE_PREFIX = "towers/outlines/v1"
IMGSZ = 640  # FastSAM's input, as eye-checked
PAD = 0.15  # each box is outlined in a crop this much larger on every side (plus PAD_PX), as eye-checked
PAD_PX = 20
SIMPLIFY_PX = 2.0  # outline points dropped within this many pixels of the line (cv2.approxPolyDP)
MIN_SHARE_OF_BOX = 0.02  # pieces smaller than this share of their box are dropped
MAX_OUTLINED = 60  # suggestions outlined per job, best first
TRACE_WINDOW_SHARE = 1 / 6  # a tap is traced in a square this share of the photo's long side
TRACE_WINDOW_MIN_PX = 600
MAX_TRACE_SHARE = 0.6  # a traced outline covering more of its window than this is the background, not a part
LOCK_WAIT_S = 30.0  # how long a call waits for FastSAM (one run at a time) before answering busy
JOB_LOCK_TTL_S = 300

Polygon = list[list[int]]


def enabled() -> bool:
    """Suggestions are outlined here: PART_OUTLINES is not 0 and the FastSAM weights are present."""
    return os.environ.get("PART_OUTLINES", "1").strip() != "0" and bool(fastsam_model.weights_status()["available"])


class OutlinerUnavailable(RuntimeError):
    """FastSAM cannot run here (no weights) or is busy."""


@dataclass(frozen=True)
class Traced:
    polygons: list[Polygon]
    box: Box  # around the outline, in the photo's own pixels


def _polygons(mask: np.ndarray, x0: int, y0: int, min_area: float) -> list[Polygon]:
    contours, _ = cv2.findContours(mask.astype(np.uint8), cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    out = []
    for contour in sorted(contours, key=cv2.contourArea, reverse=True):
        if cv2.contourArea(contour) < min_area:
            continue
        simple = cv2.approxPolyDP(contour, SIMPLIFY_PX, True)
        if len(simple) >= 3:
            out.append([[int(p[0][0]) + x0, int(p[0][1]) + y0] for p in simple])
    return out


def _first_mask(result: Any) -> Optional[np.ndarray]:
    if not result or result[0].masks is None or not len(result[0].masks.data):
        return None
    return result[0].masks.data[0].cpu().numpy() > 0.5


def _model() -> Any:
    status = fastsam_model.weights_status()
    if not status["available"]:
        raise OutlinerUnavailable(status["reason"])
    return fastsam_model.load(status["path"])


class _Run:
    """FastSAM held for one call (one run at a time across the app)."""

    def __enter__(self) -> Any:
        model = _model()
        if not fastsam_model.INFERENCE_LOCK.acquire(timeout=LOCK_WAIT_S):
            raise OutlinerUnavailable("FastSAM is busy with another photo; try again shortly")
        return model

    def __exit__(self, *exc: Any) -> None:
        fastsam_model.INFERENCE_LOCK.release()


def outline_boxes(rgb: np.ndarray, boxes: Sequence[Box]) -> list[list[Polygon]]:
    """Blocking: the outline of what is in each box (no outline: an empty list), in the photo's own pixels."""
    height, width = rgb.shape[:2]
    out: list[list[Polygon]] = []
    with _Run() as model:
        for x0, y0, x1, y1 in boxes:
            bw, bh = x1 - x0, y1 - y0
            cx0, cy0 = max(0, int(x0 - bw * PAD - PAD_PX)), max(0, int(y0 - bh * PAD - PAD_PX))
            cx1, cy1 = min(width, int(x1 + bw * PAD + PAD_PX)), min(height, int(y1 + bh * PAD + PAD_PX))
            if cx1 - cx0 < 4 or cy1 - cy0 < 4:
                out.append([])
                continue
            result = model(rgb[cy0:cy1, cx0:cx1], bboxes=[[x0 - cx0, y0 - cy0, x1 - cx0, y1 - cy0]],
                           retina_masks=True, imgsz=IMGSZ, device=fastsam_model.device(), verbose=False)
            mask = _first_mask(result)
            out.append([] if mask is None else _polygons(mask, cx0, cy0, MIN_SHARE_OF_BOX * bw * bh))
    return out


def trace(rgb: np.ndarray, x: float, y: float) -> Optional[Traced]:
    """Blocking: the outline of the part at (x, y), in the photo's own pixels; None when nothing was traced or the
    outline covers most of its window (the background around a missed tap)."""
    height, width = rgb.shape[:2]
    half = int(max(TRACE_WINDOW_MIN_PX, max(width, height) * TRACE_WINDOW_SHARE) / 2)
    cx0, cy0 = max(0, int(x) - half), max(0, int(y) - half)
    crop = rgb[cy0:min(height, int(y) + half), cx0:min(width, int(x) + half)]
    with _Run() as model:
        result = model(crop, points=[[x - cx0, y - cy0]], labels=[1], retina_masks=True, imgsz=IMGSZ,
                       device=fastsam_model.device(), verbose=False)
    mask = _first_mask(result)
    if mask is None or mask.mean() > MAX_TRACE_SHARE:
        return None
    polygons = _polygons(mask, cx0, cy0, 0.001 * mask.size)
    if not polygons:
        return None
    xs = [p[0] for poly in polygons for p in poly]
    ys = [p[1] for poly in polygons for p in poly]
    return Traced(polygons=polygons, box=(float(min(xs)), float(min(ys)), float(max(xs)), float(max(ys))))


async def trace_photo(s3: Any, bucket: str, layer_id: str, layer_metadata: dict[str, Any], photo: PhotoFacts,
                      x: float, y: float) -> Optional[Traced]:
    """The outline of the part tapped at (x, y) on an inspection photo (trace). FinderInputError for a tap outside
    the photo or a photo that is not read (no camera angle)."""
    width, height = photo.upright_size
    if not (0 <= x < width and 0 <= y < height):
        raise FinderInputError("The tap is outside the photo")
    view = photo_inspections.view_of(photo)
    if view is None:
        raise FinderInputError("This photo has no camera angle, so it is not read")
    picture = await InspectionPhoto(s3=s3, bucket=bucket, layer_id=layer_id, layer_metadata=layer_metadata,
                                    sha256=photo.sha256, view=view).read()
    return await asyncio.to_thread(trace, picture.pixels, x, y)


def _key(project_id: str, job_id: str) -> str:
    return f"{STORE_PREFIX}/{project_id}/{job_id}.json"


async def load(s3: Any, bucket: str, project_id: str, job_id: str) -> Optional[dict[str, list[Polygon]]]:
    """The outlines kept for a finder job's suggestions, by suggestion id; None when none are kept yet."""
    data = await stored_files.load_json(s3, bucket, _key(project_id, job_id))
    if data is None or data.get("method") != METHOD:
        return None
    return data["outlines"]


_tasks: set[asyncio.Task] = set()


async def ensure(redis: Any, s3: Any, bucket: str, project_id: str, layer_id: str, layer_metadata: dict[str, Any],
                 sha256: str, view: str, job_id: str, suggestions: Sequence[tuple[str, Box]]) -> None:
    """Outline a finished job's suggestions (best first, at most MAX_OUTLINED) in the background, once per job across
    workers. Nothing runs while FastSAM cannot (no weights)."""
    if not suggestions or not enabled():
        return
    lock = redis.lock(f"towers:v1:{project_id}:outlines:{job_id}", timeout=JOB_LOCK_TTL_S, thread_local=False)
    if not await lock.acquire(blocking=False):
        return

    async def run() -> None:
        try:
            if await load(s3, bucket, project_id, job_id) is not None:
                return
            picture = await InspectionPhoto(s3=s3, bucket=bucket, layer_id=layer_id, layer_metadata=layer_metadata,
                                            sha256=sha256, view=view).read()
            chosen = list(suggestions)[:MAX_OUTLINED]
            outlines = await asyncio.to_thread(outline_boxes, picture.pixels, [box for _, box in chosen])
            body = {"method": METHOD, "outlines": {sid: polys for (sid, _), polys in zip(chosen, outlines)}}
            await s3.put_object(Bucket=bucket, Key=_key(project_id, job_id), Body=json.dumps(body).encode(),
                                ContentType="application/json")
        except (OutlinerUnavailable, PictureReadError) as e:
            logger.info("outlines of job %s left: %s", job_id, e)
        except Exception:
            logger.exception("outlines of job %s stopped", job_id)
        finally:
            try:
                await lock.release()
            except LockError:
                pass

    task = asyncio.get_running_loop().create_task(run())
    _tasks.add(task)
    task.add_done_callback(_tasks.discard)
