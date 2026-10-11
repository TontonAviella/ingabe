"""The example finder: the user marks a few objects of one kind on a picture ("mark 3"), and the finder finds the
rest of them on the same picture ("find the rest").

It reproduces the protocol the finder was measured with (scripts/finder_eval/run_sameimage.py and
run_lookalike_towers.py, 2026-10-11) exactly: YOLOE-11s visual prompts (example_finder_model), the reference
picture is the same picture, the prompts are the user's marks (class 0) plus look-alike marks (class 1) when the
target uses them, predict(conf CONF, imgsz = the target's finder_input_px, max_det MAX_DET, cpu); class 0 is kept
and finds overlapping one of the user's marks at IoU >= MARK_IOU are dropped. A picture larger than the target was
measured on (its image_px, the long side) is first shrunk so its long side is image_px, the marks with it; the finds
are mapped back to the picture's own pixels.

Per-target settings (image_px, finder_input_px, look_alike_marks) are read from answer_levels, which owns the
measured table; none are copied here. How far an answer about the finds may go is answer_levels' decision, not this
module's.

Jobs: a request is identified by its content (`content_sha`: the picture, the marks, the look-alikes, the target,
the model and the settings). Its result is kept in object storage per project (finds/v1/<project>/<sha>.json) and
never computed twice. Every app worker shares the jobs through Redis: one worker runs a request, the others report
its status; and only one picture is run at a time in the whole app (the slot). Switch: EXAMPLE_FINDER (off).
Rules: docs/rules/EXAMPLE_FINDER_RULES.md.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import math
import os
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Optional, Sequence, Union

import numpy as np
from PIL import Image
from redis.exceptions import LockError

from src.services import answer_levels, boxes, example_finder_model
from src.services.example_finder_model import MODEL_NAME, MODEL_SHA256, Runner
from src.services.example_finder_pictures import (
    LONLAT,
    Box,
    FinderInputError,
    Picture,
    PictureSource,
    check_pixels,
    decode_photo,
)
from src.utils import get_bucket_name, get_s3_client

logger = logging.getLogger(__name__)

# The measured protocol (scripts/finder_eval/run_sameimage.py), not tunables: changing one is a new measurement.
CONF = 0.01
MAX_DET = 300
MARK_IOU = 0.3  # a find overlapping one of the user's marks this much is that mark again, not another object
TARGET_CLASS = 0
LOOKALIKE_CLASS = 1

MIN_MARKS = 1
MAX_MARKS = 20
MAX_LOOKALIKE_MARKS = 20
_EDGE_PX = 0.01  # a mark this close outside the picture (rounding of a reprojected box) is pulled onto the edge

# Job states, owned here: compare against these, never against a literal.
QUEUED = "queued"  # waiting for the app-wide slot
RUNNING = "running"
DONE = "done"
FAILED = "failed"

STORE_PREFIX = "finds/v1"
REDIS_PREFIX = "example_finder:v1"
LOCK_TTL_S = 60  # a job's lock and status live this long without a heartbeat (the worker running it died)
HEARTBEAT_S = 15
FAILED_KEEP_S = 3600  # a failure stays visible this long; starting the same request again retries it
QUEUE_LIMIT_S = 1800  # a job waits this long for the slot before it fails as "busy"


def finder_enabled() -> bool:
    """EXAMPLE_FINDER: 1/true/yes turns the example finder on (default off: its routes answer 404)."""
    return os.environ.get("EXAMPLE_FINDER", "0").strip().lower() in {"1", "true", "yes"}


# --------------------------------------------------------------------------- #
# Settings and inputs
# --------------------------------------------------------------------------- #

@dataclass(frozen=True)
class Settings:
    """How the finder runs for one target, as that target was measured (answer_levels)."""

    target: str
    image_px: int  # long side of the measured pictures: a larger picture is shrunk to it first
    finder_input_px: int  # the finder reads the picture with its long side scaled to this (imgsz)
    look_alike_marks: bool  # the target's measured settings use look-alike marks

    def as_dict(self) -> dict[str, Any]:
        return {"target": self.target, "image_px": self.image_px, "finder_input_px": self.finder_input_px,
                "look_alike_marks": self.look_alike_marks}


def settings(target_name: str) -> Settings:
    """The finder's settings for a target, from the measured table; FinderInputError when there are none."""
    try:
        t = answer_levels.target(target_name)
    except ValueError as e:
        raise FinderInputError(str(e)) from None
    if t.finder_model != MODEL_NAME:
        raise FinderInputError(f"{t.noun} were measured with the {t.finder_model} finder, not {MODEL_NAME}")
    image_px = t.measured["image_px"]
    if t.finder_input_px is None or image_px is None:
        raise FinderInputError(f"the finder has never been measured on {t.noun}, so it has no settings to run with")
    return Settings(target=target_name, image_px=int(image_px), finder_input_px=int(t.finder_input_px),
                    look_alike_marks=t.look_alike_marks)


def _is_number(v: Any) -> bool:
    return isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v)


def check_shape(given: Any, what: str, least: int, most: int) -> list[Box]:
    """Boxes as given (any coordinates): how many there are, four numbers each, a positive width and height."""
    if not isinstance(given, (list, tuple)):
        raise FinderInputError(f"the {what}s must be a list of boxes")
    if not least <= len(given) <= most:
        wanted = f"at most {most}" if least == 0 else f"{least} to {most}"
        raise FinderInputError(f"give {wanted} {what}s, not {len(given)}")
    out: list[Box] = []
    for i, box in enumerate(given, 1):
        if not isinstance(box, (list, tuple)) or len(box) != 4 or not all(_is_number(v) for v in box):
            raise FinderInputError(f"{what} {i} must be four numbers: left, top, right, bottom")
        x0, y0, x1, y1 = (float(v) for v in box)
        if not (x0 < x1 and y0 < y1):
            raise FinderInputError(f"{what} {i} has no width or no height")
        out.append((x0, y0, x1, y1))
    return out


def check_inside(boxes_px: Sequence[Box], width: int, height: int, what: str) -> list[Box]:
    """Boxes in the picture's pixels must lie inside it; returned rounded to 0.001 px."""
    out: list[Box] = []
    for i, (x0, y0, x1, y1) in enumerate(boxes_px, 1):
        if x0 < -_EDGE_PX or y0 < -_EDGE_PX or x1 > width + _EDGE_PX or y1 > height + _EDGE_PX:
            raise FinderInputError(f"{what} {i} is not inside the picture ({width} x {height} px)")
        box = (max(0.0, x0), max(0.0, y0), min(float(width), x1), min(float(height), y1))
        if not (box[0] < box[2] and box[1] < box[3]):
            raise FinderInputError(f"{what} {i} has no width or no height in the picture's pixels")
        out.append(tuple(round(v, 3) for v in box))  # type: ignore[arg-type]
    return out


def _lookalike_shape(given: Any, s: Settings) -> list[Box]:
    looks = check_shape(given or [], "look-alike mark", 0, MAX_LOOKALIKE_MARKS)
    if looks and not s.look_alike_marks:
        raise FinderInputError(f"look-alike marks are not used for {answer_levels.target(s.target).noun}: they "
                               f"were measured not to help, or not tried")
    return looks


# --------------------------------------------------------------------------- #
# Finding
# --------------------------------------------------------------------------- #

@dataclass(frozen=True)
class Find:
    box: Box  # in the picture's own pixels
    score: float  # the finder's confidence, 0-1 (not a measured probability of being right)


def _shrink(pixels: np.ndarray, image_px: int) -> tuple[np.ndarray, float, float]:
    """The picture with its long side at most image_px (area averaging), and the x and y scale applied."""
    height, width = pixels.shape[:2]
    if max(width, height) <= image_px:
        return pixels, 1.0, 1.0
    if width >= height:
        new_w, new_h = image_px, max(1, round(height * image_px / width))
    else:
        new_w, new_h = max(1, round(width * image_px / height)), image_px
    small = np.asarray(Image.fromarray(pixels).resize((new_w, new_h), Image.Resampling.BOX))
    return small, new_w / width, new_h / height


def _scaled(box: Sequence[float], sx: float, sy: float) -> Box:
    return (box[0] * sx, box[1] * sy, box[2] * sx, box[3] * sy)


def _find(pixels: np.ndarray, marks_px: Sequence[Box], looks_px: Sequence[Box], s: Settings,
          runner: Runner) -> list[Find]:
    height, width = pixels.shape[:2]
    shown, sx, sy = _shrink(pixels, s.image_px)
    marks = [_scaled(b, sx, sy) for b in marks_px]
    looks = [_scaled(b, sx, sy) for b in looks_px]
    rows = runner(shown, marks + looks, [TARGET_CLASS] * len(marks) + [LOOKALIKE_CLASS] * len(looks),
                  s.finder_input_px, CONF, MAX_DET)
    finds = []
    for x0, y0, x1, y1, score, cls in rows:
        if cls != TARGET_CLASS:
            continue
        box = (x0, y0, x1, y1)
        if any(boxes.iou(box, mark) >= MARK_IOU for mark in marks):
            continue
        bx0, by0, bx1, by1 = _scaled(box, 1 / sx, 1 / sy)
        finds.append(Find(box=(round(max(0.0, bx0), 2), round(max(0.0, by0), 2), round(min(width, bx1), 2),
                               round(min(height, by1), 2)), score=round(score, 4)))
    return sorted(finds, key=lambda f: -f.score)


def _stored_model() -> Runner:
    """The finder with the weights from object storage (fetched once, checked by sha256). Blocking."""
    return example_finder_model.runner(example_finder_model.ensure_weights(get_s3_client(), get_bucket_name()))


def find(picture: Union[bytes, np.ndarray], marks: Any, lookalike_marks: Any, target: str, *,
         runner: Optional[Runner] = None) -> list[Find]:
    """The objects like the marked ones on the same picture, best first, in the picture's own pixels.

    `picture` is a photo file's bytes or RGB pixels (height x width x 3, uint8); `marks` 1 to 20 boxes
    (left, top, right, bottom) in its pixels; `lookalike_marks` up to 20 boxes of things that look alike but are
    not wanted, only for targets measured with them. Raises FinderInputError for anything else. Blocking (seconds
    on a CPU): async code calls it through asyncio.to_thread, or uses `start`.
    """
    s = settings(target)
    pixels = check_pixels(picture) if isinstance(picture, np.ndarray) else decode_photo(picture)
    height, width = pixels.shape[:2]
    marks_px = check_inside(check_shape(marks, "mark", MIN_MARKS, MAX_MARKS), width, height, "mark")
    looks_px = check_inside(_lookalike_shape(lookalike_marks, s), width, height, "look-alike mark")
    return _find(pixels, marks_px, looks_px, s, runner or _stored_model())


def content_sha(picture_sha: str, marks_px: Sequence[Box], looks_px: Sequence[Box], s: Settings) -> str:
    """What identifies one request: the same picture, marks, look-alikes, target, model and settings give the same
    finds, so they are found once."""
    payload = {
        "version": 1,
        "picture": picture_sha,
        "marks_px": [list(b) for b in marks_px],
        "lookalike_marks_px": [list(b) for b in looks_px],
        "target": s.target,
        "model": MODEL_SHA256,
        "settings": s.as_dict(),
        "protocol": {"conf": CONF, "max_det": MAX_DET, "mark_iou": MARK_IOU},
    }
    return hashlib.sha256(json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def is_job_id(text: str) -> bool:
    """A job id is a content_sha: 64 lowercase hex characters."""
    return len(text) == 64 and all(c in "0123456789abcdef" for c in text)


def result_key(project_id: str, job_id: str) -> str:
    if not project_id or "/" in project_id or not is_job_id(job_id):
        raise ValueError(f"no finds can be kept for project {project_id!r} and job {job_id!r}")
    return f"{STORE_PREFIX}/{project_id}/{job_id}.json"


# --------------------------------------------------------------------------- #
# Jobs shared by every app worker (Redis) with results kept per project (object storage)
# --------------------------------------------------------------------------- #

@dataclass(frozen=True)
class Job:
    job_id: str
    status: str  # QUEUED, RUNNING, DONE or FAILED
    finds: Optional[list[dict[str, Any]]] = None  # DONE: [{"box", "box_px", "score"}], best first
    error: Optional[str] = None  # FAILED: what went wrong

    def as_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {"job_id": self.job_id, "status": self.status}
        if self.status == DONE:
            out["finds"] = self.finds
        if self.status == FAILED:
            out["error"] = self.error
        return out


def _status_key(project_id: str, job_id: str) -> str:
    return f"{REDIS_PREFIX}:{project_id}:{job_id}:status"


def _lock_key(project_id: str, job_id: str) -> str:
    return f"{REDIS_PREFIX}:{project_id}:{job_id}:lock"


def slot_key() -> str:
    """The app-wide slot: one picture at a time in the whole app, whichever worker runs it."""
    return f"{REDIS_PREFIX}:slot"


async def _load(s3: Any, bucket: str, key: str) -> Optional[dict[str, Any]]:
    try:
        response = await s3.get_object(Bucket=bucket, Key=key)
    except s3.exceptions.NoSuchKey:
        return None
    async with response["Body"] as body:
        return json.loads(await body.read())


def _done(job_id: str, stored: dict[str, Any]) -> Job:
    return Job(job_id=job_id, status=DONE, finds=stored["finds"])


async def _set_status(redis: Any, project_id: str, job_id: str, state: str, ttl_s: int,
                      error: Optional[str] = None) -> None:
    value = json.dumps({"status": state, "error": error, "at": time.time()})
    await redis.set(_status_key(project_id, job_id), value, ex=ttl_s)


async def status(redis: Any, s3: Any, bucket: str, project_id: str, job_id: str) -> Optional[Job]:
    """The job in this project: DONE with its finds, or its status while it runs or after it failed; None when
    there is no such job here (never started, or the worker running it stopped: starting it again is safe)."""
    if not is_job_id(job_id):
        return None
    stored = await _load(s3, bucket, result_key(project_id, job_id))
    if stored is not None:
        return _done(job_id, stored)
    raw = await redis.get(_status_key(project_id, job_id))
    if raw is None:
        return None
    data = json.loads(raw)
    return Job(job_id=job_id, status=data["status"], error=data.get("error"))


_tasks: set[asyncio.Task] = set()  # running jobs, kept so they are not garbage-collected mid-run


async def start(redis: Any, s3: Any, bucket: str, project_id: str, source: PictureSource, marks: Any,
                lookalike_marks: Any, target: str, *, runner: Optional[Runner] = None) -> Job:
    """Find the objects like the marked ones in the source's picture, once per content and project.

    Returns at once: DONE with the finds when this project asked the same before; otherwise the job, run in the
    background by this worker (QUEUED, then RUNNING) or already running in another worker. `marks` and
    `lookalike_marks` are boxes in the picture source's coordinates. FinderInputError for a bad request,
    PictureReadError when the picture could not be read.
    """
    s = settings(target)
    given_marks = check_shape(marks, "mark", MIN_MARKS, MAX_MARKS)
    given_looks = _lookalike_shape(lookalike_marks, s)
    picture = await source.read()
    marks_px = check_inside([picture.to_pixels(b) for b in given_marks], picture.width, picture.height, "mark")
    looks_px = check_inside([picture.to_pixels(b) for b in given_looks], picture.width, picture.height,
                            "look-alike mark")
    job_id = content_sha(picture.sha256, marks_px, looks_px, s)
    stored = await _load(s3, bucket, result_key(project_id, job_id))
    if stored is not None:
        return _done(job_id, stored)
    lock = redis.lock(_lock_key(project_id, job_id), timeout=LOCK_TTL_S, thread_local=False)
    if not await lock.acquire(blocking=False):  # another worker runs it
        return await status(redis, s3, bucket, project_id, job_id) or Job(job_id=job_id, status=QUEUED)
    await _set_status(redis, project_id, job_id, QUEUED, LOCK_TTL_S)
    task = asyncio.get_running_loop().create_task(
        _run(redis, s3, bucket, project_id, job_id, picture, marks_px, looks_px, s, runner, lock))
    _tasks.add(task)
    task.add_done_callback(_tasks.discard)
    return Job(job_id=job_id, status=QUEUED)


async def _heartbeat(redis: Any, project_id: str, job_id: str, held: list[Any]) -> None:
    """Keeps the job's lock, the slot (once held) and the status alive while the job runs."""
    while True:
        await asyncio.sleep(HEARTBEAT_S)
        for lock in list(held):
            try:
                await lock.reacquire()
            except LockError:
                logger.warning("example finder job %s lost the lock %s; another worker may start it", job_id,
                               lock.name)
                held.remove(lock)
        try:
            await redis.expire(_status_key(project_id, job_id), LOCK_TTL_S)
        except Exception:
            logger.warning("example finder job %s: could not renew its status", job_id, exc_info=True)


def _round_box(box: Sequence[float], digits: int) -> list[float]:
    return [round(float(v), digits) for v in box]


def _record(job_id: str, picture: Picture, marks_px: Sequence[Box], looks_px: Sequence[Box], s: Settings,
            finds: Sequence[Find], seconds: float) -> dict[str, Any]:
    """What is kept for one request: the finds in the picture's pixels and in the caller's coordinates, and every
    input that made them."""
    digits = 2 if picture.frame is None else (8 if picture.frame.crs == LONLAT else 3)
    return {
        "version": 1,
        "job_id": job_id,
        "target": s.target,
        "model": {"name": MODEL_NAME, "sha256": MODEL_SHA256},
        "settings": s.as_dict(),
        "protocol": {"conf": CONF, "max_det": MAX_DET, "mark_iou": MARK_IOU},
        "picture": {"sha256": picture.sha256, "width": picture.width, "height": picture.height,
                    "frame": picture.frame.as_dict() if picture.frame else None},
        "marks_px": [list(b) for b in marks_px],
        "lookalike_marks_px": [list(b) for b in looks_px],
        "finds": [{"box": _round_box(picture.from_pixels(f.box), digits), "box_px": list(f.box), "score": f.score}
                  for f in finds],
        "seconds": round(seconds, 2),
        "made_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    }


async def _work(redis: Any, s3: Any, bucket: str, project_id: str, job_id: str, picture: Picture,
                marks_px: Sequence[Box], looks_px: Sequence[Box], s: Settings, runner: Optional[Runner],
                held: list[Any]) -> None:
    key = result_key(project_id, job_id)
    if await _load(s3, bucket, key) is not None:  # another worker finished it between the check and the lock
        return
    slot = redis.lock(slot_key(), timeout=LOCK_TTL_S, sleep=1.0, blocking_timeout=QUEUE_LIMIT_S,
                      thread_local=False)
    if not await slot.acquire():
        raise TimeoutError(f"the finder was busy with other pictures for {QUEUE_LIMIT_S // 60} minutes")
    held.append(slot)
    await _set_status(redis, project_id, job_id, RUNNING, LOCK_TTL_S)
    run_model = runner or await asyncio.to_thread(_stored_model)
    began = time.monotonic()
    finds = await asyncio.to_thread(_find, picture.pixels, marks_px, looks_px, s, run_model)
    seconds = time.monotonic() - began
    record = await asyncio.to_thread(_record, job_id, picture, marks_px, looks_px, s, finds, seconds)
    await s3.put_object(Bucket=bucket, Key=key, Body=json.dumps(record).encode(), ContentType="application/json")
    await redis.delete(_status_key(project_id, job_id))
    logger.info("example finder job %s (%s, project %s): %d finds in %.1f s", job_id, s.target, project_id,
                len(finds), seconds)


async def _run(redis: Any, s3: Any, bucket: str, project_id: str, job_id: str, picture: Picture,
               marks_px: Sequence[Box], looks_px: Sequence[Box], s: Settings, runner: Optional[Runner],
               lock: Any) -> None:
    held = [lock]
    beat = asyncio.get_running_loop().create_task(_heartbeat(redis, project_id, job_id, held))
    failure: Optional[BaseException] = None
    try:
        await _work(redis, s3, bucket, project_id, job_id, picture, marks_px, looks_px, s, runner, held)
    except Exception as e:  # shown to the caller as a FAILED job; the next start of the same request retries
        logger.exception("example finder job %s failed", job_id)
        failure = e
    finally:
        beat.cancel()
    try:
        if failure is not None:
            await _set_status(redis, project_id, job_id, FAILED, FAILED_KEEP_S, error=str(failure)[:300])
    finally:
        for held_lock in held:
            try:
                await held_lock.release()
            except LockError:
                logger.warning("example finder job %s: the lock %s had already expired", job_id, held_lock.name)
