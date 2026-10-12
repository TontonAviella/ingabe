"""The example finder: the user marks a few objects of one kind on a picture ("mark 3"), and the finder finds the
rest of them on the same picture ("find the rest").

It reproduces the protocol the finder was measured with (scripts/finder_eval/run_sameimage.py and
run_lookalike_towers.py, 2026-10-11) exactly: YOLOE-11s visual prompts (example_finder_model), the reference
picture is the same picture, the prompts are the user's marks (class 0) plus look-alike marks (class 1) when the
target uses them, predict(conf CONF, imgsz = the target's finder_input_px, max_det MAX_DET, cpu); class 0 is kept
and finds overlapping one of the user's marks at IoU >= MARK_IOU are dropped. A picture larger than the target was
measured on (its image_px, the long side) is first shrunk so its long side is image_px, the marks with it; the finds
are mapped back to the picture's own pixels. For a target whose measured settings turn it on (answer_levels:
`suggestion_filter`, measured 2026-10-11 by scripts/finder_eval/run_postfilter.py) the finds are then filtered
(filter_suggestions): best first, a find is kept only when its area is 1/4 to 4 times the median area of the user's
marks and it does not overlap an already kept find at IoU >= FILTER_IOU, at most FILTER_MAX of them.

It runs only on pictures like the measured ones: taken the same way (answer_levels: oblique tower photos, or
pictures looking straight down), with the marked objects at least as big, as the finder reads them, as the smallest
the target was measured on (answer_levels.size_gate). Anything else is refused with the reason, never run.

Per-target settings (image_px, finder_input_px, look_alike_marks, view, suggestion_filter) are read from
answer_levels, which owns the measured table; none are copied here. How far an answer about the finds may go is
answer_levels' decision, not this module's.

Jobs: a request is identified by its content (`content_sha`: the picture, the marks, the look-alikes, the target,
the model and the settings). Its result is kept in object storage per project (finds/v1/<project>/<sha>.json) and
never computed twice. Every app worker shares the jobs through Redis: one worker runs a request, the others report
its status; jobs wait in one line for the whole app, first come first served, and only the one at the front runs
(the slot). The line is capped per user and for the whole app, and a waiting job keeps only the picture as the
finder will be given it (at most image_px a side). Switch: EXAMPLE_FINDER (off).
Rules: docs/rules/EXAMPLE_FINDER_RULES.md.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import math
import os
import statistics
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Optional, Sequence, Union

import numpy as np
from PIL import Image
from redis.exceptions import LockError

from src.services import answer_levels, boxes, example_finder_model, stored_files
from src.services.example_finder_model import MODEL_NAME, MODEL_SHA256, Runner
from src.services.example_finder_pictures import (
    LONLAT,
    Box,
    FinderInputError,
    MapFrame,
    PictureSource,
    check_pixels,
    check_view,
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
# The suggestion filter (scripts/finder_eval/run_postfilter.py), fixed before it was measured; used only for targets
# whose measured settings turn it on (Settings.suggestion_filter).
FILTER_AREA_RATIO = 4.0  # a find's area is 1/4 to 4 times the median area of the user's marks, or it is dropped
FILTER_IOU = 0.5  # a find overlapping an already kept (better scored) find this much is that find again
FILTER_MAX = 50  # at most this many finds are kept

MIN_MARKS = 1
MAX_MARKS = 20
MAX_LOOKALIKE_MARKS = 20
_EDGE_PX = 0.01  # a mark this close outside the picture (rounding of a reprojected box) is pulled onto the edge

# Job states, owned here: compare against these, never against a literal.
QUEUED = "queued"  # waiting in line for the app-wide slot
RUNNING = "running"
DONE = "done"
FAILED = "failed"

STORE_PREFIX = "finds/v1"
REDIS_PREFIX = "example_finder:v1"
LOCK_TTL_S = 60  # a job's lock and status live this long without a heartbeat (the worker running it died)
HEARTBEAT_S = 15
FAILED_KEEP_S = 3600  # a failure stays visible this long; starting the same request again retries it
QUEUE_POLL_S = 1.0  # how often a waiting job looks at the front of the line
# Jobs waiting or running at once. The slowest measured setting (radio units read at 2560 px, a square 1333 px
# picture) took 137 s in a new finder process and 89 s in a warm one, in the app image on the M1 Mac (2026-10-11),
# so a full line drains in at most about 23 minutes: inside QUEUE_LIMIT_S, which is only a backstop. One user can
# hold MAX_JOBS_PER_USER places, so no user waits behind more than MAX_JOBS - 1 others and none fills the line alone.
MAX_JOBS = 10
MAX_JOBS_PER_USER = 3
QUEUE_LIMIT_S = 1800  # a job waits this long at most for the slot before it fails as "busy"

_VIEW_WORDS = {answer_levels.OBLIQUE: "photos taken at an angle",
               answer_levels.NADIR: "pictures looking straight down (orthophotos)"}


def finder_enabled() -> bool:
    """EXAMPLE_FINDER: 1/true/yes turns the example finder on (default off: its routes answer 404)."""
    return os.environ.get("EXAMPLE_FINDER", "0").strip().lower() in {"1", "true", "yes"}


class FinderBusy(RuntimeError):
    """The line of finder jobs is full, for this user (`whole_app` False) or for everyone (True); nothing was
    queued. Asking again once a job is done works."""

    def __init__(self, message: str, whole_app: bool) -> None:
        super().__init__(message)
        self.whole_app = whole_app


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
    view: str  # answer_levels.OBLIQUE or NADIR: the way the measured pictures looked
    suggestion_filter: bool  # the finds are filtered (filter_suggestions): measured not to lower AP50 for the target

    def as_dict(self) -> dict[str, Any]:
        return {"target": self.target, "image_px": self.image_px, "finder_input_px": self.finder_input_px,
                "look_alike_marks": self.look_alike_marks, "view": self.view,
                "suggestion_filter": self.suggestion_filter}


def settings(target_name: str) -> Settings:
    """The finder's settings for a target, from the measured table; FinderInputError when there are none."""
    try:
        t = answer_levels.target(target_name)
    except ValueError as e:
        raise FinderInputError(str(e)) from None
    if t.finder_model != MODEL_NAME:
        raise FinderInputError(f"{t.noun} were measured with the {t.finder_model} finder, not {MODEL_NAME}")
    image_px, view = t.measured["image_px"], t.measured["view"]
    if t.finder_input_px is None or image_px is None or view is None:
        raise FinderInputError(f"the finder has never been measured on {t.noun}, so it has no settings to run with")
    return Settings(target=target_name, image_px=int(image_px), finder_input_px=int(t.finder_input_px),
                    look_alike_marks=t.look_alike_marks, view=view, suggestion_filter=t.suggestion_filter)


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


def check_lookalikes(given: Any, s: Settings) -> list[Box]:
    """Look-alike marks as given (any coordinates): at most MAX_LOOKALIKE_MARKS boxes, and only for a target whose
    measured settings use them (FinderInputError otherwise: refused, never ignored)."""
    looks = check_shape(given or [], "look-alike mark", 0, MAX_LOOKALIKE_MARKS)
    if looks and not s.look_alike_marks:
        raise FinderInputError(f"look-alike marks are not used for {answer_levels.target(s.target).noun}: they "
                               f"were measured not to help, or not tried")
    return looks


def check_picture(s: Settings, view: str, width: int, height: int,
                  marks_px: Sequence[Box]) -> answer_levels.SizeGate:
    """The picture is like the ones the target was measured on, or FinderInputError says how it is not: taken the
    same way (`view`), and the marked objects, as the finder reads this picture, at least as big as the smallest
    measured (answer_levels.size_gate, from the marks' sizes in the picture's pixels). A target with no measured
    size cannot be checked, so it is refused too. Returns the passing gate."""
    view = check_view(view)
    if view != s.view:
        raise FinderInputError(f"{answer_levels.target(s.target).noun} were measured only on {_VIEW_WORDS[s.view]}, "
                               f"not on {_VIEW_WORDS[view]}")
    gate = answer_levels.size_gate(s.target, max(width, height), [(x1 - x0, y1 - y0) for x0, y0, x1, y1 in marks_px])
    if gate.passes is not True:
        raise FinderInputError(gate.reason)
    return gate


# --------------------------------------------------------------------------- #
# Finding
# --------------------------------------------------------------------------- #

@dataclass(frozen=True)
class Find:
    box: Box  # in the picture's own pixels
    score: float  # the finder's confidence, 0-1 (not a measured probability of being right)


@dataclass(frozen=True)
class Shown:
    """The picture as the finder is given it (shrunk to the target's measured size) and how to map back."""

    pixels: np.ndarray  # at most image_px a side
    sx: float  # picture pixels -> shown pixels
    sy: float
    width: int  # the picture's own size
    height: int
    marks: list[Box]  # in the shown pixels
    looks: list[Box]


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


def for_finder(pixels: np.ndarray, marks_px: Sequence[Box], looks_px: Sequence[Box], s: Settings) -> Shown:
    """The picture and marks as the finder is given them. Blocking (a resize): async code uses a thread."""
    height, width = pixels.shape[:2]
    small, sx, sy = _shrink(pixels, s.image_px)
    return Shown(pixels=small, sx=sx, sy=sy, width=width, height=height,
                 marks=[_scaled(b, sx, sy) for b in marks_px], looks=[_scaled(b, sx, sy) for b in looks_px])


def _find(picture: Shown, s: Settings, runner: Runner) -> list[Find]:
    rows = runner(picture.pixels, picture.marks + picture.looks,
                  [TARGET_CLASS] * len(picture.marks) + [LOOKALIKE_CLASS] * len(picture.looks),
                  s.finder_input_px, CONF, MAX_DET)
    finds = []
    for x0, y0, x1, y1, score, cls in rows:
        if cls != TARGET_CLASS:
            continue
        box = (x0, y0, x1, y1)
        if any(boxes.iou(box, mark) >= MARK_IOU for mark in picture.marks):
            continue
        bx0, by0, bx1, by1 = _scaled(box, 1 / picture.sx, 1 / picture.sy)
        finds.append(Find(box=(round(max(0.0, bx0), 2), round(max(0.0, by0), 2), round(min(picture.width, bx1), 2),
                               round(min(picture.height, by1), 2)), score=round(score, 4)))
    return sorted(finds, key=lambda f: -f.score)


def filter_suggestions(finds: Sequence[Find], marks_px: Sequence[Box], s: Settings) -> list[Find]:
    """The finds a person is shown: the finds as they are when the target's measured settings leave the suggestion
    filter off; otherwise, best score first, only finds whose area is 1/4 to 4 times (FILTER_AREA_RATIO) the median
    area of the user's marks and that do not overlap an already kept find at IoU >= FILTER_IOU, at most FILTER_MAX.

    `finds` and `marks_px` are in the picture's own pixels (the finds after the marks themselves were dropped).
    Fixed before it was measured (scripts/finder_eval/run_postfilter.py); on only where it did not lower AP50."""
    if not s.suggestion_filter:
        return list(finds)
    median = statistics.median(boxes.area(m) for m in marks_px)
    kept: list[Find] = []
    for f in sorted(finds, key=lambda g: -g.score):
        if not median / FILTER_AREA_RATIO <= boxes.area(f.box) <= median * FILTER_AREA_RATIO:
            continue
        if any(boxes.iou(f.box, k.box) >= FILTER_IOU for k in kept):
            continue
        kept.append(f)
        if len(kept) >= FILTER_MAX:
            break
    return kept


def _stored_model() -> Runner:
    """The finder with the weights from object storage (fetched once, checked by sha256). Blocking."""
    return example_finder_model.runner(example_finder_model.ensure_weights(get_s3_client(), get_bucket_name()))


def find(picture: Union[bytes, np.ndarray], marks: Any, lookalike_marks: Any, target: str, *, view: str,
         runner: Optional[Runner] = None) -> list[Find]:
    """The objects like the marked ones on the same picture, best first, in the picture's own pixels; filtered
    when the target's measured settings turn the suggestion filter on (filter_suggestions).

    `picture` is a photo file's bytes or RGB pixels (height x width x 3, uint8), taken the way `view` says
    (answer_levels.OBLIQUE or NADIR); `marks` 1 to 20 boxes (left, top, right, bottom) in its pixels;
    `lookalike_marks` up to 20 boxes of things that look alike but are not wanted, only for targets measured with
    them. Raises FinderInputError for anything else, including a picture unlike the measured ones (check_picture).
    Blocking (seconds on a CPU): async code calls it through asyncio.to_thread, or uses `start`.
    """
    s = settings(target)
    view = check_view(view)
    pixels = check_pixels(picture) if isinstance(picture, np.ndarray) else decode_photo(picture)
    height, width = pixels.shape[:2]
    marks_px = check_inside(check_shape(marks, "mark", MIN_MARKS, MAX_MARKS), width, height, "mark")
    looks_px = check_inside(check_lookalikes(lookalike_marks, s), width, height, "look-alike mark")
    check_picture(s, view, width, height, marks_px)
    return filter_suggestions(_find(for_finder(pixels, marks_px, looks_px, s), s, runner or _stored_model()),
                              marks_px, s)


def protocol() -> dict[str, Any]:
    """The measured protocol's values: part of a request's identity (content_sha) and kept with its finds."""
    return {"conf": CONF, "max_det": MAX_DET, "mark_iou": MARK_IOU,
            "suggestion_filter": {"area_ratio": FILTER_AREA_RATIO, "iou": FILTER_IOU, "max": FILTER_MAX}}


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
        "protocol": protocol(),
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


@dataclass(frozen=True)
class _Request:
    """What a job keeps while it waits and runs: the picture only as the finder will be given it (`shown`, at most
    image_px a side), never the whole window."""

    project_id: str
    job_id: str
    user_id: str
    settings: Settings
    shown: Shown
    picture_sha256: str
    frame: Optional[MapFrame]
    marks_px: list[Box]  # in the picture's own pixels
    looks_px: list[Box]
    gate: answer_levels.SizeGate

    @property
    def place(self) -> str:
        """This job's entry in the line."""
        return json.dumps([self.user_id, self.project_id, self.job_id], separators=(",", ":"))


def _status_key(project_id: str, job_id: str) -> str:
    return f"{REDIS_PREFIX}:{project_id}:{job_id}:status"


def _lock_key(project_id: str, job_id: str) -> str:
    return f"{REDIS_PREFIX}:{project_id}:{job_id}:lock"


def slot_key() -> str:
    """The app-wide slot: one picture at a time in the whole app, whichever worker runs it."""
    return f"{REDIS_PREFIX}:slot"


def queue_key() -> str:
    """The line of jobs waiting or running (a sorted set: members are places, scores are tickets in arrival order)."""
    return f"{REDIS_PREFIX}:queue"


def _ticket_key() -> str:
    return f"{REDIS_PREFIX}:ticket"


def _text(value: Any) -> str:
    return value.decode() if isinstance(value, bytes) else value


# Join the back of the line unless the user (ARGV[3]) or everyone (ARGV[4]) already holds the most places allowed
# (the user's own count first: it is the one they can do something about). Counting and joining are one step, so
# two workers cannot both take the last place. 0 joined, 1 the user's places are taken, 2 the app's are.
_JOIN = """
local places = redis.call('ZRANGE', KEYS[1], 0, -1)
local mine = 0
for _, place in ipairs(places) do
  if cjson.decode(place)[1] == ARGV[2] then mine = mine + 1 end
end
if mine >= tonumber(ARGV[3]) then return 1 end
if #places >= tonumber(ARGV[4]) then return 2 end
redis.call('ZADD', KEYS[1], redis.call('INCR', KEYS[2]), ARGV[1])
return 0
"""


async def _leave_if_gone(redis: Any, place: Any) -> bool:
    """Removes a place whose job is gone (its lock expired: the worker running it died); True when removed. A live
    job holds its lock from before it joins until after it leaves."""
    _, project_id, job_id = json.loads(_text(place))
    if await redis.exists(_lock_key(project_id, job_id)):
        return False
    await redis.zrem(queue_key(), place)
    logger.warning("example finder job %s left the line: its worker stopped", job_id)
    return True


async def _join(redis: Any, request: _Request) -> None:
    """Takes a place at the back of the line, or FinderBusy when the user's or the app's places are taken."""
    for place in await redis.zrange(queue_key(), 0, -1):
        await _leave_if_gone(redis, place)
    joined = await redis.eval(_JOIN, 2, queue_key(), _ticket_key(), request.place, request.user_id,
                              MAX_JOBS_PER_USER, MAX_JOBS)
    if joined == 1:
        raise FinderBusy(f"you already have {MAX_JOBS_PER_USER} finder jobs waiting or running; start another when "
                         f"one is done", whole_app=False)
    if joined == 2:
        raise FinderBusy(f"the finder already has {MAX_JOBS} pictures waiting or running; try again in a few "
                         f"minutes", whole_app=True)


async def _take_slot(redis: Any, request: _Request, held: list[Any]) -> None:
    """Waits in line, first come first served, and takes the app-wide slot once this job is at the front."""
    slot = redis.lock(slot_key(), timeout=LOCK_TTL_S, thread_local=False)
    deadline = time.monotonic() + QUEUE_LIMIT_S
    while True:
        rank = await redis.zrank(queue_key(), request.place)
        if rank is None:
            raise RuntimeError("the job lost its place in line (its lock expired while it waited)")
        if rank == 0:
            if await slot.acquire(blocking=False):
                held.append(slot)
                return
        else:
            front = await redis.zrange(queue_key(), 0, 0)
            if front and await _leave_if_gone(redis, front[0]):
                continue
        if time.monotonic() >= deadline:
            raise TimeoutError(f"the finder was busy with other pictures for {QUEUE_LIMIT_S // 60} minutes")
        await asyncio.sleep(QUEUE_POLL_S)


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
    stored = await stored_files.load_json(s3, bucket, result_key(project_id, job_id))
    if stored is not None:
        return _done(job_id, stored)
    raw = await redis.get(_status_key(project_id, job_id))
    if raw is None:
        return None
    data = json.loads(raw)
    return Job(job_id=job_id, status=data["status"], error=data.get("error"))


_tasks: set[asyncio.Task] = set()  # running jobs, kept so they are not garbage-collected mid-run
_places_here: set[str] = set()  # places in line of the jobs this app worker runs


async def start(redis: Any, s3: Any, bucket: str, project_id: str, user_id: str, source: PictureSource, marks: Any,
                lookalike_marks: Any, target: str, *, runner: Optional[Runner] = None) -> Job:
    """Find the objects like the marked ones in the source's picture, once per content and project.

    Returns at once: DONE with the finds when this project asked the same before; otherwise the job, run in the
    background by this worker (QUEUED, then RUNNING) or already running in another worker. `marks` and
    `lookalike_marks` are boxes in the picture source's coordinates; `user_id` is who asks (places in line are
    counted per user). FinderInputError for a bad request or a picture unlike the measured ones, PictureReadError
    when the picture could not be read, FinderBusy when the line is full (nothing is queued then).
    """
    s = settings(target)
    given_marks = check_shape(marks, "mark", MIN_MARKS, MAX_MARKS)
    given_looks = check_lookalikes(lookalike_marks, s)
    picture = await source.read()
    marks_px = check_inside([picture.to_pixels(b) for b in given_marks], picture.width, picture.height, "mark")
    looks_px = check_inside([picture.to_pixels(b) for b in given_looks], picture.width, picture.height,
                            "look-alike mark")
    gate = check_picture(s, picture.view, picture.width, picture.height, marks_px)
    job_id = content_sha(picture.sha256, marks_px, looks_px, s)
    stored = await stored_files.load_json(s3, bucket, result_key(project_id, job_id))
    if stored is not None:
        return _done(job_id, stored)
    request = _Request(project_id=project_id, job_id=job_id, user_id=user_id, settings=s,
                       shown=await asyncio.to_thread(for_finder, picture.pixels, marks_px, looks_px, s),
                       picture_sha256=picture.sha256, frame=picture.frame, marks_px=marks_px, looks_px=looks_px,
                       gate=gate)
    lock = redis.lock(_lock_key(project_id, job_id), timeout=LOCK_TTL_S, thread_local=False)
    if not await lock.acquire(blocking=False):  # another worker runs it
        return await status(redis, s3, bucket, project_id, job_id) or Job(job_id=job_id, status=QUEUED)
    try:
        await _join(redis, request)
    except BaseException:
        await lock.release()
        raise
    _places_here.add(request.place)
    await _set_status(redis, project_id, job_id, QUEUED, LOCK_TTL_S)
    task = asyncio.get_running_loop().create_task(_run(redis, s3, bucket, request, runner, lock))
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


def _record(request: _Request, finds: Sequence[Find], seconds: float) -> dict[str, Any]:
    """What is kept for one request: the finds in the picture's pixels and in the caller's coordinates, and every
    input that made them."""
    frame, s, gate = request.frame, request.settings, request.gate
    digits = 2 if frame is None else (8 if frame.crs == LONLAT else 3)

    def caller_box(box: Box) -> list[float]:
        return _round_box(box if frame is None else frame.from_pixels(box), digits)

    return {
        "version": 1,
        "job_id": request.job_id,
        "target": s.target,
        "model": {"name": MODEL_NAME, "sha256": MODEL_SHA256},
        "settings": s.as_dict(),
        "protocol": protocol(),
        "picture": {"sha256": request.picture_sha256, "width": request.shown.width, "height": request.shown.height,
                    "frame": frame.as_dict() if frame else None},
        "size_gate": {"object_px": round(gate.object_px, 1) if gate.object_px is not None else None,
                      "needed_px": gate.needed_px, "reason": gate.reason},
        "marks_px": [list(b) for b in request.marks_px],
        "lookalike_marks_px": [list(b) for b in request.looks_px],
        "finds": [{"box": caller_box(f.box), "box_px": list(f.box), "score": f.score} for f in finds],
        "seconds": round(seconds, 2),
        "made_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    }


async def _work(redis: Any, s3: Any, bucket: str, request: _Request, runner: Optional[Runner],
                held: list[Any]) -> None:
    key = result_key(request.project_id, request.job_id)
    if await stored_files.load_json(s3, bucket, key) is not None:  # another worker finished it between the check and the lock
        return
    await _take_slot(redis, request, held)
    await _set_status(redis, request.project_id, request.job_id, RUNNING, LOCK_TTL_S)
    run_model = runner or await asyncio.to_thread(_stored_model)
    began = time.monotonic()
    finds = await asyncio.to_thread(_find, request.shown, request.settings, run_model)
    seconds = time.monotonic() - began
    finds = filter_suggestions(finds, request.marks_px, request.settings)  # at most MAX_DET finds: milliseconds
    await _hand_over(redis, request)
    await redis.zrem(queue_key(), request.place)  # out of the line before the finds show, so it no longer counts
    record = await asyncio.to_thread(_record, request, finds, seconds)
    await s3.put_object(Bucket=bucket, Key=key, Body=json.dumps(record).encode(), ContentType="application/json")
    await redis.delete(_status_key(request.project_id, request.job_id))
    logger.info("example finder job %s (%s, project %s): %d finds in %.1f s", request.job_id, request.settings.target,
                request.project_id, len(finds), seconds)


async def _next_runs_here(redis: Any, request: _Request) -> bool:
    """Whether the next job in line after `request` (which just ran, at the front) is one of this worker's."""
    for place in await redis.zrange(queue_key(), 0, 1):
        if _text(place) != request.place:
            return _text(place) in _places_here
    return False


async def _hand_over(redis: Any, request: _Request) -> None:
    """After a picture, with the slot still held: the finder process stops unless this worker runs the next job, so
    only one finder process (one model's memory) lives in the whole app at a time. Never raises: a failure here
    must not fail the job that just ran."""
    try:
        keep = await _next_runs_here(redis, request)
    except Exception:
        logger.warning("example finder: could not read the line; stopping the finder process", exc_info=True)
        keep = False
    if not keep:
        try:
            await asyncio.to_thread(example_finder_model.stop)
        except Exception:  # it also stops by itself once idle (IDLE_EXIT_S)
            logger.warning("example finder: could not stop the finder process after job %s", request.job_id,
                           exc_info=True)


async def _run(redis: Any, s3: Any, bucket: str, request: _Request, runner: Optional[Runner], lock: Any) -> None:
    held = [lock]
    beat = asyncio.get_running_loop().create_task(_heartbeat(redis, request.project_id, request.job_id, held))
    failure: Optional[BaseException] = None
    try:
        await _work(redis, s3, bucket, request, runner, held)
    except Exception as e:  # shown to the caller as a FAILED job; the next start of the same request retries
        logger.exception("example finder job %s failed", request.job_id)
        failure = e
    finally:
        beat.cancel()
    try:
        if failure is not None:
            if any(held_lock.name == slot_key() for held_lock in held):
                await _hand_over(redis, request)
            await _set_status(redis, request.project_id, request.job_id, FAILED, FAILED_KEEP_S,
                              error=str(failure)[:300])
    finally:
        _places_here.discard(request.place)
        try:
            await redis.zrem(queue_key(), request.place)  # before the lock goes: a place without a lock is a dead job
        finally:
            for held_lock in held:
                try:
                    await held_lock.release()
                except LockError:
                    logger.warning("example finder job %s: the lock %s had already expired", request.job_id,
                                   held_lock.name)
