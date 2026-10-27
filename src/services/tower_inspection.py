"""Tower inspections: what a person does on each photo of a photo inspection, part by part, and what is counted from it.

A tower inspection is a photo inspection (src/services/photo_inspections.py) in a Telecom Towers project. On each
photo, for each part (tower_dish, tower_antenna, tower_radio_unit), a person:

1. marks a few of that part ("marks"); the other two parts' marks on the same photo are the part's look-alike marks
   when its measured settings use them (answer_levels: antennas and radio units, not dishes);
2. asks the example finder (src/services/example_finder.py) to find the rest ("find"): its finds are the
   suggestions;
3. reviews every suggestion: keeps it, rejects it, or fixes its box, and adds any the finder missed.

The answer level comes from answer_levels.decide for the photo's marks (FULL_REVIEW for every tower part today: the
finder's suggestions are never a count, a person checks each one). So `counted_by_you` (the marks, the kept, the
fixed and the added) exists only once every suggestion of that photo and part is reviewed; until then it is None
and `waiting` says how many are left. A person's work is kept per project in object storage
(towers/v1/<project>/<layer>/<sha256>.json), never shared across projects, even for byte-identical photos; the
finder keeps its finds per project too.

Across photos, the same dish seen from two places is counted on both, so an inspection's sum over photos is not the
number of dishes on the tower: `at_least` (the most counted on one photo) is what the photos prove.

Switch: TOWERS (off), read only by `enabled()`. Rules: docs/rules/TOWERS_RULES.md.
"""

from __future__ import annotations

import asyncio
import json
import logging
import math
import os
import statistics
from collections import OrderedDict
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from typing import Any, Optional, Sequence

from redis.exceptions import LockError

from src.services import answer_levels, example_finder, part_outlines, photo_inspections, stored_files
from src.services.example_finder_pictures import Box, FinderInputError, InspectionPhoto, PictureReadError
from src.services.photo_inspections import PhotoFacts
from src.services.shown_maps import SHOWN_MAPS_SQL

logger = logging.getLogger(__name__)

_SWITCH = "TOWERS"
PARTS = ("tower_dish", "tower_antenna", "tower_radio_unit")  # Telecom Towers: the tower cards count these
FARM_PARTS = ("maize_seedling",)  # Agriculture: marked and found on the photos; no cards yet
# Telecom Towers' defects: marked and found on close-up photos of the steel (trained detector only); not equipment, so
# the tower cards do not count them.
DEFECT_PARTS = ("tower_bolt_missing", "tower_bolt_loose")
ALL_PARTS = PARTS + FARM_PARTS + DEFECT_PARTS
PARTS_BY_INDUSTRY = {"telecom": PARTS + DEFECT_PARTS, "agriculture": FARM_PARTS}
# How the marking screen names each part (one, many), in the order it offers them.
PART_LABELS = {"tower_dish": ("Microwave dish", "microwave dishes"), "tower_antenna": ("Panel antenna", "panel antennas"),
               "tower_radio_unit": ("Radio unit", "radio units"), "maize_seedling": ("Maize plant", "maize plants"),
               "tower_bolt_missing": ("Missing bolt", "missing bolts"),
               "tower_bolt_loose": ("Loose bolt", "loose bolts")}
STORE_PREFIX = "towers/v1"
REDIS_PREFIX = "towers:v1"
_VERSION = 1
LOCK_TTL_S = 60  # one change to a photo's work holds its lock at most this long (starting a find reads the photo)
LOCK_WAIT_S = 10  # how long a change waits for another one on the same photo
MAX_ADDED = example_finder.MAX_DET  # boxes a person adds on one photo for one part
SAME_SITE_M = 100.0  # two inspections whose camera positions centre this close photographed the same site
_READ_CONCURRENCY = 16
_FACTS_CACHE_SIZE = 16  # inspections whose photo facts are kept in memory (they never change once uploaded)
COMPASS = ("north", "north-east", "east", "south-east", "south", "south-west", "west", "north-west")


def enabled() -> bool:
    """TOWERS: 1/true/yes turns the tower work on (default off: its routes answer 404 and Sage has no tower tools)."""
    return os.environ.get(_SWITCH, "0").strip().lower() in {"1", "true", "yes"}


class TowerInputError(FinderInputError):
    """A request that cannot be done as asked; the message says why in the reader's terms (422)."""


class TowerConflict(RuntimeError):
    """The photo's work is not in the state the request needs (a review before the finder has finished), or someone
    else is changing it right now (409)."""


def noun(part: str) -> str:
    """How a reader names the part: "dishes", "antennas", "radio units" (answer_levels' table)."""
    return answer_levels.target(part).noun


def parts_for(industry: Optional[str]) -> tuple[str, ...]:
    """The parts a person marks on a photo inspection of this industry (none for an industry without photo work)."""
    return PARTS_BY_INDUSTRY.get(industry or "", ())


def finder_measurements(parts: Sequence[str]) -> list[tuple[str, float, int]]:
    """(noun, AP50, test photos) of the finder this server runs for each part, as answer_levels' table measured it;
    a part nothing can find here (its detector switched off) is left out."""
    out = []
    for part in parts:
        try:
            model = example_finder.settings(part).model
        except FinderInputError:
            continue
        m = answer_levels.target_for(part, model).measured
        out.append((noun(part), float(m["value"]), int(m["n_images"])))
    return out


def check_part(part: Any, parts: Sequence[str] = ALL_PARTS) -> str:
    if part not in parts:
        raise TowerInputError(f"the part must be one of {list(parts)}, not {part!r}")
    return part


def _family(part: str) -> tuple[str, ...]:
    """The parts marked alongside this one (its industry's): only they can be its look-alike marks."""
    return next(family for family in (PARTS, FARM_PARTS, DEFECT_PARTS) if part in family)


# --------------------------------------------------------------------------- #
# What is kept for one photo
# --------------------------------------------------------------------------- #

@dataclass(frozen=True)
class Review:
    """A person's review of one part's suggestions on one photo; ids are suggestion ids."""

    kept: tuple[str, ...] = ()
    rejected: tuple[str, ...] = ()
    fixed: tuple[tuple[str, Box], ...] = ()  # (id, the box as the person drew it)
    added: tuple[Box, ...] = ()  # objects the finder missed

    def reviewed(self) -> set[str]:
        return set(self.kept) | set(self.rejected) | {sid for sid, _ in self.fixed}

    def as_dict(self) -> dict[str, Any]:
        return {"kept": list(self.kept), "rejected": list(self.rejected),
                "fixed": [{"id": sid, "box": list(box)} for sid, box in self.fixed],
                "added": [list(box) for box in self.added]}

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Review":
        return cls(kept=tuple(data["kept"]), rejected=tuple(data["rejected"]),
                   fixed=tuple((f["id"], _box(f["box"])) for f in data["fixed"]),
                   added=tuple(_box(b) for b in data["added"]))


@dataclass(frozen=True)
class PartRecord:
    marks: tuple[Box, ...] = ()
    own_lookalikes: tuple[Box, ...] = ()  # look-alike marks the person drew for this part
    job_id: Optional[str] = None  # the finder job run on these marks
    review: Review = Review()
    review_saved: bool = False  # the person saved a review of this job's suggestions (also when there were none)


@dataclass(frozen=True)
class PhotoRecord:
    sha256: str
    parts: dict[str, PartRecord]

    @classmethod
    def empty(cls, sha256: str) -> "PhotoRecord":
        return cls(sha256=sha256, parts={part: PartRecord() for part in ALL_PARTS})

    def with_part(self, part: str, record: PartRecord) -> "PhotoRecord":
        return replace(self, parts={**self.parts, part: record})


def _box(values: Sequence[float]) -> Box:
    x0, y0, x1, y1 = (float(v) for v in values)
    return (x0, y0, x1, y1)


def work_project(projects: Sequence[str]) -> Optional[str]:
    """The project a photo inspection's work is kept in, from the projects whose live maps show the layer
    (photo_context.projects_of): the one there is (None: none). TowerConflict when there are several: following the
    last edited map would move the work between projects, so the person removes the layer from all but one."""
    if len(projects) > 1:
        raise TowerConflict(f"this inspection is on maps of {len(projects)} projects; its tower work is kept in one "
                            f"project, so remove it from the maps of the others")
    return projects[0] if projects else None


def record_key(project_id: str, layer_id: str, sha256: str) -> str:
    for name, value in (("project", project_id), ("layer", layer_id)):
        if not value or "/" in value:
            raise ValueError(f"no tower work can be kept for {name} {value!r}")
    if not photo_inspections.is_sha256(sha256):
        raise ValueError(f"{sha256!r} is not a photo's SHA-256")
    return f"{_layer_prefix(project_id, layer_id)}{sha256}.json"


def _layer_prefix(project_id: str, layer_id: str) -> str:
    return f"{STORE_PREFIX}/{project_id}/{layer_id}/"


def _record_json(project_id: str, layer_id: str, record: PhotoRecord) -> bytes:
    parts = {part: {"marks": [list(b) for b in r.marks], "lookalike_marks": [list(b) for b in r.own_lookalikes],
                    "job_id": r.job_id, "reviewed": r.review.as_dict(), "review_saved": r.review_saved}
             for part, r in record.parts.items() if part in PARTS or r != PartRecord()}  # tower records as before
    return json.dumps({"version": _VERSION, "project_id": project_id, "layer_id": layer_id,
                       "sha256": record.sha256, "parts": parts,
                       "updated_at": datetime.now(timezone.utc).isoformat(timespec="seconds")}).encode()


def _record_from(data: dict[str, Any]) -> PhotoRecord:
    if data.get("version") != _VERSION:
        raise ValueError(f"tower work version {data.get('version')!r} is not {_VERSION}")
    parts = {part: PartRecord(marks=tuple(_box(b) for b in p["marks"]),
                              own_lookalikes=tuple(_box(b) for b in p["lookalike_marks"]),
                              job_id=p["job_id"], review=Review.from_dict(p["reviewed"]),
                              review_saved=p["review_saved"])
             for part, p in data["parts"].items()}
    return PhotoRecord(sha256=data["sha256"], parts={part: parts.get(part, PartRecord()) for part in ALL_PARTS})


async def load_record(s3: Any, bucket: str, project_id: str, layer_id: str, sha256: str) -> PhotoRecord:
    """The person's work on one photo in this project (empty when there is none)."""
    data = await stored_files.load_json(s3, bucket, record_key(project_id, layer_id, sha256))
    return PhotoRecord.empty(sha256) if data is None else _record_from(data)


async def _save_record(s3: Any, bucket: str, project_id: str, layer_id: str, record: PhotoRecord) -> None:
    await s3.put_object(Bucket=bucket, Key=record_key(project_id, layer_id, record.sha256),
                        Body=_record_json(project_id, layer_id, record), ContentType="application/json")


class _PhotoLock:
    """One change at a time to a photo's work, across every app worker (Redis)."""

    def __init__(self, redis: Any, project_id: str, layer_id: str, sha256: str) -> None:
        self._lock = redis.lock(f"{REDIS_PREFIX}:{project_id}:{layer_id}:{sha256}:lock", timeout=LOCK_TTL_S,
                                blocking_timeout=LOCK_WAIT_S, thread_local=False)

    async def __aenter__(self) -> None:
        if not await self._lock.acquire():
            raise TowerConflict("someone else is saving work on this photo; try again in a moment")

    async def __aexit__(self, *exc: Any) -> None:
        try:
            await self._lock.release()
        except LockError:
            logger.warning("tower work lock %s had expired before it was released", self._lock.name)


# --------------------------------------------------------------------------- #
# One part on one photo: marks, look-alikes, the finder's suggestions, the review, the count
# --------------------------------------------------------------------------- #

@dataclass(frozen=True)
class Suggestion:
    id: str  # names the finder job, so an id from older marks is refused, never taken for another suggestion
    box: Box  # in the photo's own pixels, as shown
    score: float  # the finder's confidence, 0-1 (not a measured chance of being right)
    outline: Optional[tuple[tuple[tuple[int, int], ...], ...]] = None  # part_outlines' outline; drawn, never counted


@dataclass(frozen=True)
class PartWork:
    part: str
    marks: tuple[Box, ...]
    lookalike_marks: tuple[Box, ...]
    job: Optional[example_finder.Job]
    suggestions: tuple[Suggestion, ...]
    review: Review
    counted_by_you: Optional[int]
    waiting: int  # suggestions not reviewed yet
    answer: Optional[answer_levels.Answer]  # how far an answer may go on this photo (None: nothing marked)
    review_saved: bool = False

    @property
    def unreviewed(self) -> bool:
        """The finder has finished and no review of its suggestions is saved yet (also when it found none)."""
        return self.job is not None and self.job.status == example_finder.DONE and not self.review_saved

    @property
    def finding(self) -> bool:
        return self.job is not None and self.job.status in (example_finder.QUEUED, example_finder.RUNNING)

    @property
    def not_found(self) -> bool:
        """Marked, but the finder has not run on these marks, or it failed."""
        return bool(self.marks) and (self.job is None or self.job.status == example_finder.FAILED)

    def as_dict(self) -> dict[str, Any]:
        return {
            "marks": [list(b) for b in self.marks],
            "lookalike_marks": [list(b) for b in self.lookalike_marks],
            "job": None if self.job is None else {"job_id": self.job.job_id, "status": self.job.status},
            "suggestions": [{"id": s.id, "box": list(s.box), "score": s.score,
                             **({"outline": [[list(p) for p in poly] for poly in s.outline]} if s.outline else {})}
                            for s in self.suggestions],
            "reviewed": self.review.as_dict(),
            "counted_by_you": self.counted_by_you,
            "waiting": self.waiting,
        }


def _others_marks(part: str, record: PhotoRecord) -> tuple[Box, ...]:
    """The other two parts' marks on the photo, when this part's measured settings use look-alikes (as measured);
    otherwise none."""
    try:
        uses_lookalikes = example_finder.settings(part).look_alike_marks
    except FinderInputError:  # a part only a switched-off detector finds: it has no finder settings here
        return ()
    if not uses_lookalikes:
        return ()
    return tuple(b for other in _family(part) if other != part for b in record.parts[other].marks)


def lookalike_marks(part: str, record: PhotoRecord) -> tuple[Box, ...]:
    """The look-alike marks the finder is given for this part: when the part's measured settings use look-alikes,
    the other two parts' marks on the photo (as measured), then the person's own, without repeats, at most
    MAX_LOOKALIKE_MARKS; otherwise none."""
    unique: list[Box] = []
    for b in _others_marks(part, record) + record.parts[part].own_lookalikes:
        if b not in unique:
            unique.append(b)
    return tuple(unique[:example_finder.MAX_LOOKALIKE_MARKS])


def own_lookalikes(part: str, record: PhotoRecord, given: Sequence[Box]) -> tuple[Box, ...]:
    """The person's own look-alike marks among the look-alike marks given for a part: a box that is another part's
    mark on the photo is a look-alike already (lookalike_marks adds it), so it is not kept as the person's own. So
    the list the work shows (`lookalike_marks`) can be sent back unchanged without changing anything."""
    others = set(_others_marks(part, record))
    own: list[Box] = []
    for b in given:
        if b not in others and b not in own:
            own.append(b)
    return tuple(own)


def level_answer(part: str, marks: Sequence[Box], photo: PhotoFacts,
                 found: Sequence[Box] = ()) -> Optional[answer_levels.Answer]:
    """How far an answer about this part may go on this photo, from answer_levels (the size gate on the marks, the
    part's countability, the measured table). A scan has no marks: the gate then reads the boxes it found and the
    person kept, fixed or added (`found`). None when there are neither."""
    marks = tuple(marks) or tuple(found)
    if not marks:
        return None
    try:  # the finder this server runs for the part: its trained detector, else YOLOE
        model = example_finder.settings(part).model
    except FinderInputError:  # a part whose detector cannot run here: nothing can be answered about it
        return None
    gate = answer_levels.size_gate(part, max(photo.upright_size), [(x1 - x0, y1 - y0) for x0, y0, x1, y1 in marks],
                                   finder_model=model)
    return answer_levels.decide(part, model, gate, answer_levels.countability(part, answer_levels.Facts()))


def suggestions_of(job: example_finder.Job, min_score: Optional[float] = None) -> tuple[Suggestion, ...]:
    """A finished job's finds as suggestions, best first, each with an id naming the job; only those from min_score
    up when one is given (ids keep their place among all the finds, so earlier reviews still match)."""
    return tuple(Suggestion(id=f"{job.job_id[:12]}-{i}", box=_box(find["box_px"]), score=find["score"])
                 for i, find in enumerate(job.finds or [], 1) if min_score is None or find["score"] >= min_score)


def _min_score(part: str) -> Optional[float]:
    """The score the part's detector shows boxes from, as measured (answer_levels); read when shown, so a job found
    before the floor was set is shown with it too."""
    try:
        return example_finder.settings(part).min_score
    except FinderInputError:
        return None


def counted(record: PartRecord, done: bool, waiting: int, answer: Optional[answer_levels.Answer]) -> Optional[int]:
    """What the person counted of this part on the photo: their marks, the kept, the fixed and the added. Only when
    the finder has finished, the person saved a review of its suggestions with none left waiting (a finder that
    found nothing still needs that review: it is where the missed ones are added), and the answer level allows a
    count after checking; else None (not counted yet, never 0)."""
    if (not done or waiting or not record.review_saved or answer is None
            or answer.level < answer_levels.Level.FULL_REVIEW):
        return None
    review = record.review
    return len(record.marks) + len(review.kept) + len(review.fixed) + len(review.added)


async def _job(redis: Any, s3: Any, bucket: str, project_id: str, job_id: Optional[str]) -> Optional[example_finder.Job]:
    if job_id is None:
        return None
    job = await example_finder.status(redis, s3, bucket, project_id, job_id)
    # Unknown: the worker running it stopped. Shown as failed, so the person starts it again.
    return job or example_finder.Job(job_id=job_id, status=example_finder.FAILED, error="the finder job stopped")


async def part_work(redis: Any, s3: Any, bucket: str, project_id: str, record: PhotoRecord, part: str,
                    photo: PhotoFacts) -> PartWork:
    own = record.parts[part]
    # Bolts found by a scan (no marks) are not shown: scan_parts says why.
    job = None if part in DEFECT_PARTS and not own.marks else await _job(redis, s3, bucket, project_id, own.job_id)
    done = job is not None and job.status == example_finder.DONE
    suggestions = suggestions_of(job, _min_score(part)) if done and job is not None else ()
    if suggestions and job is not None:
        outlines = await part_outlines.load(s3, bucket, project_id, job.job_id) or {}
        suggestions = tuple(replace(s, outline=tuple(tuple((int(x), int(y)) for x, y in poly) for poly in outlines[s.id]))
                            if outlines.get(s.id) else s for s in suggestions)
    reviewed = own.review.reviewed()
    waiting = sum(1 for s in suggestions if s.id not in reviewed)
    found = tuple(s.box for s in suggestions) + tuple(b for _, b in own.review.fixed) + own.review.added
    answer = level_answer(part, own.marks, photo, found)
    return PartWork(part=part, marks=own.marks, lookalike_marks=lookalike_marks(part, record), job=job,
                    suggestions=suggestions, review=own.review, counted_by_you=counted(own, done, waiting, answer),
                    waiting=waiting, answer=answer, review_saved=own.review_saved)


async def photo_work(redis: Any, s3: Any, bucket: str, project_id: str, layer_id: str,
                     photo: PhotoFacts, parts: Sequence[str] = PARTS) -> dict[str, PartWork]:
    """Every part's work on one photo (the tower parts unless the inspection's industry marks others)."""
    record = await load_record(s3, bucket, project_id, layer_id, photo.sha256)
    return await _record_work(redis, s3, bucket, project_id, record, photo, parts)


async def _record_work(redis: Any, s3: Any, bucket: str, project_id: str, record: PhotoRecord,
                       photo: PhotoFacts, parts: Sequence[str] = PARTS) -> dict[str, PartWork]:
    works = await asyncio.gather(*(part_work(redis, s3, bucket, project_id, record, p, photo) for p in parts))
    return dict(zip(parts, works))


def _in_photo(given: Any, what: str, most: int, photo: PhotoFacts) -> tuple[Box, ...]:
    width, height = photo.upright_size
    return tuple(example_finder.check_inside(example_finder.check_shape(given, what, 0, most), width, height, what))


async def set_marks(redis: Any, s3: Any, bucket: str, project_id: str, layer_id: str, photo: PhotoFacts, part: str,
                    marks: Any, lookalikes: Any) -> PartWork:
    """Save a part's marks (0 to MAX_MARKS boxes) and its look-alike marks (only for a part measured with
    look-alikes): the list the work shows, the other parts' marks and the person's own, of which only the person's
    own are kept (own_lookalikes). Changed marks or own look-alike marks drop the part's finder job and review: they
    were for the old ones."""
    check_part(part)
    marks_px = _in_photo(marks, "mark", example_finder.MAX_MARKS, photo)
    looks = example_finder.check_lookalikes(lookalikes, example_finder.settings(part))
    width, height = photo.upright_size
    given = example_finder.check_inside(looks, width, height, "look-alike mark")
    async with _PhotoLock(redis, project_id, layer_id, photo.sha256):
        record = await load_record(s3, bucket, project_id, layer_id, photo.sha256)
        own = own_lookalikes(part, record, given)
        current = record.parts[part]
        if (current.marks, current.own_lookalikes) != (marks_px, own):
            record = record.with_part(part, PartRecord(marks=marks_px, own_lookalikes=own))
            await _save_record(s3, bucket, project_id, layer_id, record)
        return await part_work(redis, s3, bucket, project_id, record, part, photo)


async def start_find(redis: Any, s3: Any, bucket: str, project_id: str, user_id: str, layer_id: str,
                     layer_metadata: dict[str, Any], photo: PhotoFacts, part: str) -> example_finder.Job:
    """Ask the example finder for the rest of this part on the photo, from the part's marks and look-alike marks.
    The photo is read from this inspection's own storage only (InspectionPhoto). Raises FinderInputError (a photo or
    marks unlike the measured ones), example_finder.FinderBusy and PictureReadError as example_finder.start does."""
    check_part(part)
    view = photo_inspections.view_of(photo)
    if view is None:
        raise TowerInputError(f"this photo does not say which way the camera looked (it has no DJI gimbal pitch), so "
                              f"it cannot be checked against the photos the finder was measured on for {noun(part)}")
    async with _PhotoLock(redis, project_id, layer_id, photo.sha256):
        record = await load_record(s3, bucket, project_id, layer_id, photo.sha256)
        own = record.parts[part]
        source = InspectionPhoto(s3=s3, bucket=bucket, layer_id=layer_id, layer_metadata=layer_metadata,
                                 sha256=photo.sha256, view=view)
        job = await example_finder.start(redis, s3, bucket, project_id, user_id, source, list(own.marks),
                                         list(lookalike_marks(part, record)), part)
        if own.job_id != job.job_id:  # a review was for the suggestions of another job
            record = record.with_part(part, PartRecord(marks=own.marks, own_lookalikes=own.own_lookalikes,
                                                       job_id=job.job_id))
            await _save_record(s3, bucket, project_id, layer_id, record)
    return job


# --------------------------------------------------------------------------- #
# Scanning: a trained detector looks at every photo by itself, no marks needed
# --------------------------------------------------------------------------- #

SCAN_LOCK_TTL_S = 120  # the scan's lock, renewed while it runs: another worker takes over if this one stops
SCAN_WAIT_S = 3.0  # how often the scan checks a job it started, or the finder's line when it was full
SCAN_JOB_LIMIT_S = 900.0  # a job still not done after this is left: the next scan starts it again


def scan_parts(parts: Sequence[str]) -> tuple[str, ...]:
    """The parts this server finds with a trained detector: they are scanned without marks. The others (the example
    finder) still need a person's marks first. Bolts are never scanned: on a wide photo a bolt is a few pixels and the
    bolt models boxed cars and bare steel (2026-10-14, a Louisville photo 64 m up); they run on a close-up a person
    marked bolts on."""
    out = []
    for part in parts:
        if part in DEFECT_PARTS:
            continue
        try:
            if example_finder.settings(part).uses_detector:
                out.append(part)
        except FinderInputError:  # nothing can find it here (its detector switched off)
            continue
    return tuple(out)


def _needs_scan(record: PhotoRecord, part: str) -> bool:
    own = record.parts[part]
    return not own.marks and own.job_id is None


async def start_scan(redis: Any, s3: Any, bucket: str, project_id: str, user_id: str, layer_id: str,
                     layer_metadata: dict[str, Any], photo: PhotoFacts, part: str) -> Optional[example_finder.Job]:
    """Start the trained detector on the whole photo for one part, when nobody marked it and nothing ran on it yet;
    None when there is nothing to start. The job becomes the part's job, as a find from marks does."""
    view = photo_inspections.view_of(photo)
    if view is None:
        return None
    async with _PhotoLock(redis, project_id, layer_id, photo.sha256):
        record = await load_record(s3, bucket, project_id, layer_id, photo.sha256)
        if not _needs_scan(record, part):
            return None
        source = InspectionPhoto(s3=s3, bucket=bucket, layer_id=layer_id, layer_metadata=layer_metadata,
                                 sha256=photo.sha256, view=view)
        job = await example_finder.start(redis, s3, bucket, project_id, user_id, source, [], [], part)
        await _save_record(s3, bucket, project_id, layer_id,
                           record.with_part(part, PartRecord(job_id=job.job_id)))
    return job


async def _until_done(redis: Any, s3: Any, bucket: str, project_id: str, job: example_finder.Job) -> None:
    waited = 0.0
    while job.status in (example_finder.QUEUED, example_finder.RUNNING) and waited < SCAN_JOB_LIMIT_S:
        await asyncio.sleep(SCAN_WAIT_S)
        waited += SCAN_WAIT_S
        job = await example_finder.status(redis, s3, bucket, project_id, job.job_id) or replace(
            job, status=example_finder.FAILED)


async def scan(redis: Any, s3: Any, bucket: str, project_id: str, user_id: str, layer_id: str,
               layer_metadata: dict[str, Any], photos: Sequence[PhotoFacts], parts: Sequence[str]) -> int:
    """Scan every photo, in flight order, for every part a trained detector finds; one job at a time, so the
    finder's line stays open to people. Returns how many jobs it started. A photo that cannot be scanned (no camera
    angle, unreadable, unlike the measured photos) is left for a person, with the reason in the log."""
    started = 0
    for photo in sorted(photos, key=_flight_order):
        for part in scan_parts(parts):
            while True:
                try:
                    job = await start_scan(redis, s3, bucket, project_id, user_id, layer_id, layer_metadata, photo,
                                           part)
                    break
                except example_finder.FinderBusy:
                    await asyncio.sleep(SCAN_WAIT_S)
                except (FinderInputError, PictureReadError, TowerConflict) as e:
                    logger.info("scan of %s left %s on %s for a person: %s", layer_id, part, photo.filename, e)
                    job = None
                    break
            if job is not None:
                started += 1
                await _until_done(redis, s3, bucket, project_id, job)
    return started


_scans: set[asyncio.Task] = set()


def _scan_lock(redis: Any, project_id: str, layer_id: str) -> Any:
    return redis.lock(f"{REDIS_PREFIX}:{project_id}:{layer_id}:scan", timeout=SCAN_LOCK_TTL_S, thread_local=False)


async def ensure_scan(redis: Any, s3: Any, bucket: str, project_id: str, user_id: str, layer_id: str,
                      layer_metadata: dict[str, Any], photos: Sequence[PhotoFacts], parts: Sequence[str]) -> bool:
    """Start scanning the inspection in the background unless a scan of it already runs (any worker). Returns
    whether a scan runs now. Safe to call on every visit: finished photos are skipped, so a stopped scan resumes."""
    if not scan_parts(parts):
        return False
    lock = _scan_lock(redis, project_id, layer_id)
    if not await lock.acquire(blocking=False):
        return True

    async def run() -> None:
        async def keep() -> None:
            while True:
                await asyncio.sleep(SCAN_LOCK_TTL_S / 3)
                try:
                    await lock.reacquire()
                except LockError:
                    logger.warning("scan of %s lost its lock; another worker may take over", layer_id)
                    return

        keeper = asyncio.create_task(keep())
        try:
            started = await scan(redis, s3, bucket, project_id, user_id, layer_id, layer_metadata, photos, parts)
            logger.info("scan of %s finished: %d finder jobs started", layer_id, started)
        except Exception:
            logger.exception("scan of %s stopped", layer_id)
        finally:
            keeper.cancel()
            try:
                await lock.release()
            except LockError:
                pass

    task = asyncio.get_running_loop().create_task(run())
    _scans.add(task)
    task.add_done_callback(_scans.discard)
    return True


async def scan_running(redis: Any, project_id: str, layer_id: str) -> bool:
    """Whether a scan of the inspection runs now, in any worker."""
    return bool(await _scan_lock(redis, project_id, layer_id).locked())


def scan_progress(inspection: "Inspection", parts: Sequence[str]) -> dict[str, int]:
    """How far the scan is: photos where every scanned part has a finished job, of the photos it can scan."""
    scanned = scan_parts(parts)
    can = [p for p in inspection.photos if photo_inspections.view_of(p) is not None]
    done = sum(1 for p in can if scanned and all(
        (w := (inspection.work.get(p.sha256) or {}).get(part)) is not None and w.job is not None
        and w.job.status in (example_finder.DONE, example_finder.FAILED) for part in scanned))
    return {"photos": len(can), "scanned": done if scanned else 0}


def _ids(given: Any, what: str) -> tuple[str, ...]:
    if not isinstance(given, (list, tuple)) or not all(isinstance(i, str) for i in given):
        raise TowerInputError(f"the {what} suggestions must be a list of suggestion ids")
    return tuple(given)


def _fixed(given: Any, photo: PhotoFacts) -> tuple[tuple[str, Box], ...]:
    if not isinstance(given, (list, tuple)) or not all(isinstance(f, dict) and set(f) == {"id", "box"} for f in given):
        raise TowerInputError("each fixed suggestion is {\"id\", \"box\"}")
    boxes = _in_photo([f["box"] for f in given], "fixed box", MAX_ADDED, photo)
    return tuple((sid, box) for sid, box in zip(_ids([f["id"] for f in given], "fixed"), boxes))


async def set_review(redis: Any, s3: Any, bucket: str, project_id: str, layer_id: str, photo: PhotoFacts, part: str,
                     kept: Any, rejected: Any, fixed: Any, added: Any) -> PartWork:
    """Save the person's review of a part's suggestions on the photo (it replaces the last one). Every id must be a
    suggestion of the part's current finder job, reviewed once; boxes lie in the photo. TowerConflict before the
    finder has finished."""
    check_part(part)
    review = Review(kept=_ids(kept, "kept"), rejected=_ids(rejected, "rejected"), fixed=_fixed(fixed, photo),
                    added=_in_photo(added, "added box", MAX_ADDED, photo))
    given = list(review.kept) + list(review.rejected) + [sid for sid, _ in review.fixed]
    twice = sorted({sid for sid in given if given.count(sid) > 1})
    if twice:
        raise TowerInputError(f"each suggestion is kept, rejected or fixed once; {twice[0]} is given more than once")
    async with _PhotoLock(redis, project_id, layer_id, photo.sha256):
        record = await load_record(s3, bucket, project_id, layer_id, photo.sha256)
        work = await part_work(redis, s3, bucket, project_id, record, part, photo)
        if work.job is None or work.job.status != example_finder.DONE:
            raise TowerConflict(f"the finder has not found {noun(part)} on this photo yet; review its suggestions "
                                f"once it has")
        ids = {s.id for s in work.suggestions}
        unknown = [sid for sid in given if sid not in ids]
        if unknown:
            raise TowerInputError(f"{unknown[0]} is not one of this photo's suggestions for {noun(part)} (the "
                                  f"suggestions change when the marks do)")
        record = record.with_part(part, replace(record.parts[part], review=review, review_saved=True))
        await _save_record(s3, bucket, project_id, layer_id, record)
        return await part_work(redis, s3, bucket, project_id, record, part, photo)


# --------------------------------------------------------------------------- #
# A whole inspection
# --------------------------------------------------------------------------- #

_facts_cache: "OrderedDict[str, tuple[PhotoFacts, ...]]" = OrderedDict()


def unique_photos(facts: Sequence[PhotoFacts]) -> tuple[PhotoFacts, ...]:
    """One entry per stored photo, in upload order: a photo uploaded twice (same bytes) is stored once and keeps its
    first name."""
    seen: set[str] = set()
    out = []
    for f in facts:
        if f.sha256 not in seen:
            seen.add(f.sha256)
            out.append(f)
    return tuple(out)


async def inspection_photos(s3: Any, bucket: str, prefix: str) -> Optional[tuple[PhotoFacts, ...]]:
    """The inspection's photos (photo_inspections.load_facts, one per stored photo); None when its facts were not
    kept (uploaded before 2026-10-11). Kept in memory: an inspection's photos never change once uploaded."""
    if prefix in _facts_cache:
        _facts_cache.move_to_end(prefix)
        return _facts_cache[prefix]
    facts = await photo_inspections.load_facts(s3, bucket, prefix)
    if facts is None:
        return None
    photos = unique_photos(facts)
    _facts_cache[prefix] = photos
    while len(_facts_cache) > _FACTS_CACHE_SIZE:
        _facts_cache.popitem(last=False)
    return photos


def photo_named(photos: Sequence[PhotoFacts], sha256: str) -> Optional[PhotoFacts]:
    return next((p for p in photos if p.sha256 == sha256), None)


@dataclass(frozen=True)
class Inspection:
    project_id: str
    layer_id: str
    name: str
    photos: tuple[PhotoFacts, ...]
    work: dict[str, dict[str, PartWork]]  # sha256 -> part -> work, for the photos someone worked on


async def _keys_under(s3: Any, bucket: str, prefix: str) -> list[str]:
    keys: list[str] = []
    page_from: dict[str, Any] = {}
    while True:
        page = await s3.list_objects_v2(Bucket=bucket, Prefix=prefix, **page_from)
        keys += [item["Key"] for item in page.get("Contents", [])]
        if not page.get("IsTruncated"):
            return keys
        page_from = {"ContinuationToken": page["NextContinuationToken"]}


async def load_inspection(redis: Any, s3: Any, bucket: str, project_id: str, layer_id: str, name: str,
                          photos: Sequence[PhotoFacts], parts: Sequence[str] = PARTS) -> Inspection:
    """The inspection's photos and the work kept on them in this project, for these parts (its industry's)."""
    by_sha = {p.sha256: p for p in photos}
    prefix = _layer_prefix(project_id, layer_id)
    shas = [k[len(prefix):-len(".json")] for k in await _keys_under(s3, bucket, prefix) if k.endswith(".json")]
    limit = asyncio.Semaphore(_READ_CONCURRENCY)

    async def one(sha: str) -> tuple[str, dict[str, PartWork]]:
        async with limit:
            return sha, await photo_work(redis, s3, bucket, project_id, layer_id, by_sha[sha], parts)

    work = dict(await asyncio.gather(*(one(sha) for sha in shas if sha in by_sha)))
    return Inspection(project_id=project_id, layer_id=layer_id, name=name, photos=tuple(photos), work=work)


@dataclass(frozen=True)
class PartInventory:
    """One part across the inspection's photos."""

    part: str
    noun: str
    photos_marked: int  # photos where this part is marked
    photos_counted: int  # photos where it is counted by you
    photos_waiting: int  # photos with suggestions waiting for a person's review
    waiting: int  # suggestions waiting for review, all photos
    photos_to_confirm: int  # photos where the finder found no more and no review is saved yet
    photos_finding: int  # photos where the finder is still working
    photos_not_found: int  # photos marked where the finder has not run (or failed)
    counted_in_photos: int  # the counts added over photos: one object seen in two photos counts twice
    at_least: Optional[int]  # the most counted on one photo: there are at least this many (None: none counted)
    at_least_photo: Optional[PhotoFacts]
    answer: Optional[answer_levels.Answer]  # the answer level on that photo


def inventory(inspection: Inspection, parts: Sequence[str] = PARTS) -> dict[str, PartInventory]:
    """What is counted of each part across the inspection, and what is still open (the tower equipment unless the
    caller names other parts: the defects)."""
    out = {}
    photos = {p.sha256: p for p in inspection.photos}
    for part in parts:
        works = [(sha, parts[part]) for sha, parts in inspection.work.items() if part in parts]
        counted_photos = [(sha, w) for sha, w in works if w.counted_by_you is not None]
        best = max(counted_photos, key=lambda item: item[1].counted_by_you, default=None)  # type: ignore[arg-type]
        out[part] = PartInventory(
            part=part, noun=noun(part),
            photos_marked=sum(1 for _, w in works if w.marks),
            photos_counted=len(counted_photos),
            photos_waiting=sum(1 for _, w in works if w.waiting),
            waiting=sum(w.waiting for _, w in works),
            photos_to_confirm=sum(1 for _, w in works if w.unreviewed and not w.waiting),
            photos_finding=sum(1 for _, w in works if w.finding),
            photos_not_found=sum(1 for _, w in works if w.not_found),
            counted_in_photos=sum(w.counted_by_you or 0 for _, w in counted_photos),
            at_least=best[1].counted_by_you if best else None,
            at_least_photo=photos[best[0]] if best else None,
            answer=best[1].answer if best else None,
        )
    return out


def _anything_marked(parts: Optional[dict[str, PartWork]]) -> bool:
    return parts is not None and any(w.marks for w in parts.values())


def photos_with_marked_parts_counted(inspection: Inspection) -> int:
    """Photos where something is marked and every marked part is counted. Not "checked for every part": a part
    nobody marked on a photo was not looked for there (there is no "none on this photo")."""
    done = 0
    for parts in inspection.work.values():
        marked = [w for w in parts.values() if w.marks]
        if marked and all(w.counted_by_you is not None for w in marked):
            done += 1
    return done


def photos_unmarked(inspection: Inspection) -> int:
    """Photos nothing is marked on yet: nobody has looked for parts on them."""
    return sum(1 for p in inspection.photos if not _anything_marked(inspection.work.get(p.sha256)))


# Why a photo is on the technician's list, in the order they are listed.
WAITING = "waiting"  # suggestions to review
CONFIRM = "confirm"  # the finder found no more: add any it missed and save the review
NOT_FOUND = "not_found"  # marked, the finder has not run on the marks (or it failed)
FINDING = "finding"  # the finder is working; review once it has finished
UNMARKED = "unmarked"  # nothing marked on it yet
CHECK_ORDER = (WAITING, CONFIRM, NOT_FOUND, FINDING, UNMARKED)


@dataclass(frozen=True)
class PhotoToCheck:
    photo: PhotoFacts
    reason: str  # one of CHECK_ORDER
    waiting: int  # suggestions waiting, all parts
    parts: tuple[str, ...]  # the parts the reason is about (none for UNMARKED)


def _flight_order(photo: PhotoFacts) -> tuple[bool, str, str]:
    """Photos in the order they were taken (the camera's local time), then by name; photos with no time last."""
    return (photo.taken_at is None, (photo.taken_at or "")[:19], photo.filename)


def photos_to_check(inspection: Inspection) -> list[PhotoToCheck]:
    """The photos a person still has to work on, in the order to work on them: suggestions waiting (most first), a
    finder that found no more (to confirm), marked but not found, the finder still working, then photos nothing is
    marked on (in flight order). A photo where something is counted and nothing marked is open is checked, and not
    listed."""
    out = []
    for photo in sorted(inspection.photos, key=_flight_order):
        parts = inspection.work.get(photo.sha256)
        if parts is None or not _anything_marked(parts):
            out.append(PhotoToCheck(photo=photo, reason=UNMARKED, waiting=0, parts=()))
            continue
        waiting = sum(w.waiting for w in parts.values())
        for reason, test in ((WAITING, lambda w: w.waiting > 0), (CONFIRM, lambda w: w.unreviewed),
                             (NOT_FOUND, lambda w: w.not_found), (FINDING, lambda w: w.finding)):
            hit = tuple(p for p, w in parts.items() if test(w))
            if hit:
                out.append(PhotoToCheck(photo=photo, reason=reason, waiting=waiting, parts=hit))
                break
    return sorted(out, key=lambda c: (CHECK_ORDER.index(c.reason), -c.waiting))  # stable: flight order within


# --------------------------------------------------------------------------- #
# Where the photos were taken from (the photos' own EXIF and DJI XMP facts)
# --------------------------------------------------------------------------- #

@dataclass(frozen=True)
class Spread:
    least: float
    middle: float  # the median
    most: float


def _spread(values: Sequence[Optional[float]]) -> Optional[Spread]:
    known = [v for v in values if v is not None]
    if not known:
        return None
    return Spread(least=min(known), middle=statistics.median(known), most=max(known))


def compass(yaw: float) -> str:
    """The direction a camera faced, as one of eight compass words, from DJI's gimbal yaw (degrees from north)."""
    return COMPASS[round((yaw % 360) / 45) % len(COMPASS)]


@dataclass(frozen=True)
class CameraFacts:
    photos: int
    with_gps: int
    without_gps: tuple[str, ...]  # file names
    height_m: Optional[Spread]  # above the take-off point (DJI's relative altitude)
    pitch_deg: Optional[Spread]  # gimbal pitch: 0 level, -90 straight down
    at_an_angle: int  # photo_inspections.view_of: OBLIQUE
    straight_down: int  # NADIR
    no_pitch: int  # the photo does not say which way the camera looked
    facing: dict[str, int]  # compass word -> photos, in compass order, only words with photos
    no_direction: int
    first_taken: Optional[str]
    last_taken: Optional[str]
    cameras: tuple[str, ...]  # make and model, as the photos name them


def camera_facts(photos: Sequence[PhotoFacts]) -> CameraFacts:
    views = [photo_inspections.view_of(p) for p in photos]
    facing = [compass(p.gimbal_yaw) for p in photos if p.gimbal_yaw is not None]
    times = sorted(p.taken_at[:19] for p in photos if p.taken_at)
    cameras = sorted({" ".join(x for x in (p.make, p.model) if x) for p in photos if p.make or p.model})
    return CameraFacts(
        photos=len(photos), with_gps=sum(1 for p in photos if p.has_position),
        without_gps=tuple(p.filename for p in photos if not p.has_position),
        height_m=_spread([p.relative_altitude_m for p in photos]), pitch_deg=_spread([p.gimbal_pitch for p in photos]),
        at_an_angle=views.count(answer_levels.OBLIQUE), straight_down=views.count(answer_levels.NADIR),
        no_pitch=views.count(None),
        facing={word: facing.count(word) for word in COMPASS if word in facing},
        no_direction=len(photos) - len(facing),
        first_taken=times[0] if times else None, last_taken=times[-1] if times else None,
        cameras=tuple(cameras))


# --------------------------------------------------------------------------- #
# The same site inspected before
# --------------------------------------------------------------------------- #

def site_centre(photos: Sequence[PhotoFacts]) -> Optional[tuple[float, float]]:
    """(lat, lon): the median camera position of the photos with GPS; None when none has one."""
    placed = [p for p in photos if p.has_position]
    if not placed:
        return None
    return statistics.median(p.lat for p in placed), statistics.median(p.lon for p in placed)  # type: ignore[type-var]


def distance_m(a: tuple[float, float], b: tuple[float, float]) -> float:
    """Great-circle distance between two (lat, lon) points (haversine, mean Earth radius)."""
    lat1, lon1, lat2, lon2 = (math.radians(v) for v in (*a, *b))
    h = math.sin((lat2 - lat1) / 2) ** 2 + math.cos(lat1) * math.cos(lat2) * math.sin((lon2 - lon1) / 2) ** 2
    return 2 * 6_371_008.8 * math.asin(math.sqrt(h))


def _taken_span(photos: Sequence[PhotoFacts]) -> Optional[tuple[str, str]]:
    times = sorted(p.taken_at[:19] for p in photos if p.taken_at)
    return (times[0], times[-1]) if times else None


def is_earlier_same_site(current: Sequence[PhotoFacts], other: Sequence[PhotoFacts]) -> bool:
    """Whether `other` photographed the same site (camera positions centred within SAME_SITE_M) and was finished
    before `current` began (its last photo taken before this one's first). Unknown positions or times: False."""
    here, there = site_centre(current), site_centre(other)
    now, then = _taken_span(current), _taken_span(other)
    if here is None or there is None or now is None or then is None:
        return False
    return distance_m(here, there) <= SAME_SITE_M and then[1] < now[0]


# The project's other photo inspections on the maps it shows (shown_maps, the rule the tower routes use): one removed
# from the map, though still on an older version, is not an earlier inspection of the project any more.
_INSPECTIONS_SQL = f"""
SELECT DISTINCT l.layer_id, l.name, l.metadata
  FROM map_layers l
  JOIN ({SHOWN_MAPS_SQL}) s ON l.layer_id = ANY(s.layers)
 WHERE s.project_id = $1 AND l.type = 'vector'
   AND l.metadata ? 'photo_inspection' AND l.layer_id <> $2
"""


@dataclass(frozen=True)
class Earlier:
    """The same site inspected before, in this project."""

    same_site: int  # earlier inspections of the same site (positions and times known)
    inspection: Optional[Inspection]  # the latest of them with something counted by you; None when none has


async def earlier_inspection(conn: Any, redis: Any, s3: Any, bucket: str, current: Inspection,
                             parts: Sequence[str] = PARTS) -> Earlier:
    """The latest earlier inspection of the same site in the current inspection's project that has something counted
    by you. `conn` sees what the reader may see (row security)."""
    candidates = []
    for row in await conn.fetch(_INSPECTIONS_SQL, current.project_id, current.layer_id):
        metadata = json.loads(row["metadata"]) if isinstance(row["metadata"], str) else dict(row["metadata"] or {})
        prefix = photo_inspections.stored_prefix(metadata, row["layer_id"])
        photos = None if prefix is None else await inspection_photos(s3, bucket, prefix)
        if photos is not None and is_earlier_same_site(current.photos, photos):
            candidates.append((_taken_span(photos), row["layer_id"], row["name"], photos))
    for _, layer_id, name, photos in sorted(candidates, key=lambda c: c[0], reverse=True):
        earlier = await load_inspection(redis, s3, bucket, current.project_id, layer_id, name, photos, parts)
        if any(i.at_least is not None for i in inventory(earlier, parts).values()):
            return Earlier(same_site=len(candidates), inspection=earlier)
    return Earlier(same_site=len(candidates), inspection=None)
