"""How far an answer about objects the picture finder found may go: a count, a count after checking, an estimate,
a rough number, an area, or "can't tell".

The finder is YOLOE-11s (yoloe-11s-seg) shown three marks the user drew on the picture. It was measured on
2026-10-11 for each kind of object; the results are in answer_levels_targets.json, and the rows and marks behind
them in answer_levels_evidence.json, beside this module. Each kind of object has the highest level that evidence
supports today; none is trusted to COUNT yet. The levels hold for this finder only: `decide` refuses another
finder's output. Two gates can lower the level for one picture:

* the size gate: objects smaller, as the finder reads them, than every picture the kind was measured on are
  outside the measurement;
* countability: for example, crop plants whose leaves touch cannot be told apart.

`decide` combines the table and the gates into one Answer whose reason is in data terms. Consumers read
`ladder_enabled()` (ANSWER_LADDER, off by default) before using any of this. Rules:
docs/rules/ANSWER_LEVELS_RULES.md.

src/services/plant_counts.py counts plants with another method (green crowns) and keeps its own floor, 5 cm per
pixel, checked on Cyampirita maize; see the rules doc.
"""

from __future__ import annotations

import json
import math
import os
import statistics
from dataclasses import dataclass
from datetime import date
from enum import Enum, IntEnum
from pathlib import Path
from typing import Any, Optional, Sequence


class Level(IntEnum):
    """Answer levels, lowest to highest: a higher level promises more about the number."""

    CANT_TELL = 0
    AREA = 1
    ROUGH = 2
    ESTIMATE = 3
    FULL_REVIEW = 4
    COUNT = 5

    @property
    def label(self) -> str:
        return _LEVEL_WORDS[self][0]

    @property
    def meaning(self) -> str:
        return _LEVEL_WORDS[self][1]


_LEVEL_WORDS: dict[Level, tuple[str, str]] = {
    Level.COUNT: ("Count", "A number you can use as it is: it was checked to be right on pictures like this one."),
    Level.FULL_REVIEW: ("Count after checking", "Every object found is marked on the picture; a person checks each "
                        "mark and adds any that were missed before the number is used."),
    Level.ESTIMATE: ("Estimate", "A number with a range around it, close enough to plan with, but not checked one "
                     "by one."),
    Level.ROUGH: ("Rough number", "Tells a few from many; single marks are often wrong or missed, so it is not a "
                  "count."),
    Level.AREA: ("Area only", "How much ground they cover, not how many there are."),
    Level.CANT_TELL: ("Can't tell", "This picture cannot answer the question; the reason says what would."),
}


class Verdict(str, Enum):
    """Whether objects of one kind can be counted one by one on this picture."""

    YES = "YES"
    MAYBE = "MAYBE"  # facts are missing (listed in `needs`) or counting this kind is untested
    NO = "NO"


# Kinds of object; each has its own countability rule (see `countability`).
STRUCTURE = "structure"  # tower parts, buildings: they do not grow or touch
ANNUAL_CROP = "annual_crop"  # planted each season: age and closed leaves matter
WEED = "weed"  # grows among the crop: closed leaves matter, there is no planting date
PERENNIAL_CROWN = "perennial_crown"  # tree crowns, banana mats: counting one by one is not proven
KINDS = (STRUCTURE, ANNUAL_CROP, WEED, PERENNIAL_CROWN)

# Green leaves covering this share of the ground or more: neighbouring plants touch and cannot be told apart.
# A cut-off set with the spec on 2026-10-11, not measured; the share itself is measured by the caller.
CLOSED_CANOPY_SHARE = 0.8

# Which way the measured pictures looked: a value holds only for pictures taken the same way.
OBLIQUE = "oblique"  # photos taken at an angle, from the side (the tower photos)
NADIR = "nadir"  # pictures looking straight down (orthophoto and UAV tiles)
VIEWS = (OBLIQUE, NADIR)

PROVENANCE_KEYS = ("dataset", "view", "protocol", "n_images", "image_px", "finder_input_px", "date", "metric",
                   "value", "evidence", "where", "note")
# A measured value must say what, seen which way, how, when, on how many pictures of what size, read at what size,
# from which rows.
_REQUIRED_WHEN_MEASURED = ("dataset", "view", "protocol", "n_images", "image_px", "finder_input_px", "date",
                           "metric", "evidence", "where")
_WHOLE_PX = ("n_images", "image_px", "finder_input_px")

TARGETS_PATH = Path(__file__).parent / "answer_levels_targets.json"
EVIDENCE_PATH = Path(__file__).parent / "answer_levels_evidence.json"


@dataclass(frozen=True)
class Target:
    """One kind of object the finder was measured on, with its settings and the highest level it supports."""

    name: str
    noun: str  # plural, as a reader would say it: "maize plants", "dishes"
    kind: str
    object_size_m: Optional[float]  # typical size on the ground; None when nobody measured it
    object_size_note: str  # where the size comes from, or why there is none
    finder_model: str  # the finder the levels were measured for; another finder's output gets no level
    finder_input_px: Optional[int]  # the finder reads a picture with its long side scaled to this, as measured
    look_alike_marks: bool  # also mark things that look alike but are not (see finder.note in the table)
    suggestion_filter: bool  # the example finder filters its suggestions: on only where measured not to lower AP50
    max_level: Level
    size_floor_px: Optional[int]  # smallest marked size, as the finder reads it, among the measured pictures
    measured: dict[str, Any]  # provenance: PROVENANCE_KEYS
    also_measured: tuple[dict[str, Any], ...]


def _check_unit_interval(where: str, value: Any) -> None:
    if not isinstance(value, (int, float)) or isinstance(value, bool) or not 0 <= value <= 1:
        raise ValueError(f"{where}: {value!r} is not a share between 0 and 1")


def _check_provenance(name: str, measured: dict[str, Any]) -> None:
    lacking = [key for key in PROVENANCE_KEYS if key not in measured]
    if lacking:
        raise ValueError(f"answer target {name}: provenance lacks {lacking}")
    if measured["value"] is None:
        if not measured["note"]:
            raise ValueError(f"answer target {name}: no measured value and no note saying why")
        return
    _check_unit_interval(f"answer target {name}", measured["value"])
    unsaid = [key for key in _REQUIRED_WHEN_MEASURED if measured[key] is None]
    if unsaid:
        raise ValueError(f"answer target {name}: a measured value must say {unsaid}")
    if measured["view"] not in VIEWS:
        raise ValueError(f"answer target {name}: view {measured['view']!r} is not one of {VIEWS}")
    for key in _WHOLE_PX:
        if not isinstance(measured[key], int) or isinstance(measured[key], bool) or measured[key] < 1:
            raise ValueError(f"answer target {name}: {key} {measured[key]!r} is not a whole number of 1 or more")


def box_px(width_px: float, height_px: float) -> float:
    """One box's size in pixels: the side of a square of the same area (objects are sized by area, as in COCO)."""
    if not (width_px > 0 and height_px > 0):
        raise ValueError(f"a marked box needs a positive width and height, got {width_px!r} x {height_px!r}")
    return math.sqrt(width_px * height_px)


def finder_px(picture_px: float, picture_long_px: float, finder_input_px: int) -> float:
    """A size in the picture's own pixels as the finder reads it: the finder scales the long side to its input."""
    if not picture_long_px > 0:
        raise ValueError(f"the picture's long side must be positive, got {picture_long_px!r} px")
    return picture_px * finder_input_px / picture_long_px


def _marked_px(marked_box_px: Sequence[Sequence[float]]) -> float:
    return statistics.median(box_px(w, h) for w, h in marked_box_px)


def _size_floor(name: str, sets: Sequence[str], finder_input: Optional[int],
                marks: dict[str, Any]) -> Optional[int]:
    """The smallest marked size, as the finder read it, among the pictures in `sets` (whole px, rounded down)."""
    sizes = []
    for set_name in sets:
        if set_name not in marks:
            raise ValueError(f"answer target {name}: no marked pictures named {set_name!r} in the evidence")
        if marks[set_name]["finder_input_px"] != finder_input:
            raise ValueError(f"answer target {name}: {set_name} was read at {marks[set_name]['finder_input_px']} px, "
                             f"not at the target's {finder_input} px")
        sizes += [finder_px(_marked_px(p["marks_wh"]), p["long_px"], finder_input) for p in marks[set_name]["pictures"]]
    return math.floor(min(sizes)) if sizes else None


_SUGGESTION_FILTER_KEYS = {"on", "evidence", "note"}


def _suggestion_filter(name: str, entry: dict[str, Any]) -> bool:
    """Whether the example finder filters its suggestions for this target. On only with the run it was measured in
    (`evidence`, checked against the evidence file by the tests); every target says why in `note`."""
    given = entry.get("suggestion_filter")
    if not isinstance(given, dict) or set(given) != _SUGGESTION_FILTER_KEYS:
        raise ValueError(f"answer target {name}: suggestion_filter must have exactly {sorted(_SUGGESTION_FILTER_KEYS)}")
    if not isinstance(given["on"], bool):
        raise ValueError(f"answer target {name}: suggestion_filter on {given['on']!r} is not true or false")
    if not given["note"]:
        raise ValueError(f"answer target {name}: suggestion_filter needs a note saying what was measured, or that "
                         f"nothing was")
    if given["on"] and not given["evidence"]:
        raise ValueError(f"answer target {name}: the suggestion filter is on without the measurement it rests on")
    return given["on"]


def _target(name: str, entry: dict[str, Any], marks: dict[str, Any]) -> Target:
    measured = entry["measured"]
    _check_provenance(name, measured)
    for extra in entry["also_measured"]:
        _check_unit_interval(f"answer target {name} ({extra['setting']})", extra["value"])
    if entry["kind"] not in KINDS:
        raise ValueError(f"answer target {name}: kind {entry['kind']!r} is not one of {KINDS}")
    size = entry["object_size_m"]
    if size is not None and not size > 0:
        raise ValueError(f"answer target {name}: object size {size!r} m is not positive")
    if not entry.get("object_size_note"):
        raise ValueError(f"answer target {name}: object_size_note must say where the size comes from or why there is none")
    if entry["max_level"] not in Level.__members__:
        raise ValueError(f"answer target {name}: max level {entry['max_level']!r} is not one of {list(Level.__members__)}")
    level = Level[entry["max_level"]]
    if level is Level.AREA:
        raise ValueError(f"answer target {name}: no answer about the area objects cover has been measured, so no "
                         f"target can reach AREA")
    if level > Level.CANT_TELL and measured["value"] is None:
        raise ValueError(f"answer target {name}: {level.name} needs a measured value")
    if level is Level.CANT_TELL and not measured["note"]:
        raise ValueError(f"answer target {name}: CANT_TELL needs a note saying why")
    finder = entry["finder"]
    if not finder.get("model"):
        raise ValueError(f"answer target {name}: the finder the levels were measured for is not named")
    floor = _size_floor(name, entry["size_evidence"], measured["finder_input_px"], marks)
    if level > Level.CANT_TELL and floor is None:
        raise ValueError(f"answer target {name}: {level.name} needs size_evidence (marked pictures) for the size gate")
    return Target(name=name, noun=entry["noun"], kind=entry["kind"], object_size_m=size,
                  object_size_note=entry["object_size_note"], finder_model=finder["model"],
                  finder_input_px=measured["finder_input_px"], look_alike_marks=bool(finder["look_alike_marks"]),
                  suggestion_filter=_suggestion_filter(name, entry), max_level=level, size_floor_px=floor,
                  measured=dict(measured), also_measured=tuple(dict(e) for e in entry["also_measured"]))


def load_targets(path: Path = TARGETS_PATH, evidence_path: Path = EVIDENCE_PATH) -> dict[str, Target]:
    """Targets by name from the committed table and the marks behind it; raises if either is broken."""
    marks = json.loads(evidence_path.read_text())["marks"]
    return {name: _target(name, e, marks) for name, e in json.loads(path.read_text())["targets"].items()}


_TARGETS = load_targets()


def target(name: str) -> Target:
    try:
        return _TARGETS[name]
    except KeyError:
        raise ValueError(f"no answer target {name!r}; known: {sorted(_TARGETS)}") from None


def targets() -> tuple[Target, ...]:
    return tuple(_TARGETS.values())


def ladder_enabled() -> bool:
    """ANSWER_LADDER: 1/true/yes lets consumers show answer levels (default off: they behave as before)."""
    return os.environ.get("ANSWER_LADDER", "0").strip().lower() in {"1", "true", "yes"}


# --------------------------------------------------------------------------- #
# Size gate
# --------------------------------------------------------------------------- #

@dataclass(frozen=True)
class SizeGate:
    target: str
    object_px: Optional[float]  # one object's size as the finder reads it; None when it cannot be known
    needed_px: Optional[int]  # the target's size floor; None when no size was measured for it
    passes: Optional[bool]  # None: a size is unknown, so the gate cannot pass
    reason: str


def _size_text(t: Target, floor: int, finder_input: int, object_px: float, picture_px: float,
               picture_long_px: float, scale: str, from_marks: bool) -> str:
    """The size gate's reason in data terms, e.g. "the marked dishes are about 12 px wide as the finder reads
    them (40 px in this 4000 px picture, read at 1280 px); dishes were measured only from 17 px wide, ..."."""
    fails = object_px < floor
    shown = math.floor(object_px) if fails else round(object_px)  # never print "17 px" for a 16.6 px that failed
    who = f"the marked {t.noun}" if from_marks else t.noun
    where = f"{scale}{picture_px:.0f} px in this {picture_long_px:.0f} px picture, read at {finder_input} px"
    head = f"{who} are about {shown} px wide as the finder reads them ({where})"
    if fails:
        return f"{head}; {t.noun} were measured only from {floor} px wide, so this picture is outside the measurement"
    return f"{head}; {t.noun} were measured from {floor} px wide"


def size_gate(target_name: str, picture_long_px: float, marked_box_px: Optional[Sequence[Sequence[float]]] = None,
              cm_per_px: Optional[float] = None, object_size_m: Optional[float] = None) -> SizeGate:
    """Whether objects of this kind are, as the finder reads this picture, at least as big as on the pictures the
    kind was measured on (the target's size floor).

    `picture_long_px` is the long side, in pixels, of the picture (or tile) given to the finder; the finder reads
    it at the target's measured `finder_input_px`. `marked_box_px` is the (width, height) in the picture's pixels
    of each box the user drew; their median size is used when given, before `object_size_m` (a size on the ground,
    needing `cm_per_px`).
    """
    t = target(target_name)
    if not picture_long_px > 0:
        raise ValueError(f"the picture's long side must be positive, got {picture_long_px!r} px")
    if cm_per_px is not None and not cm_per_px > 0:
        raise ValueError(f"cm per pixel must be positive, got {cm_per_px!r}")
    if object_size_m is not None and not object_size_m > 0:
        raise ValueError(f"object size must be positive, got {object_size_m!r} m")
    floor, finder_input = t.size_floor_px, t.finder_input_px
    if floor is None or finder_input is None:
        return SizeGate(target_name, None, None, None,
                        f"no object size was measured for {t.noun}, so this picture cannot be checked against one")
    if marked_box_px:
        picture_px, scale, from_marks = _marked_px(marked_box_px), "", True
    elif object_size_m is not None and cm_per_px is not None:
        picture_px, from_marks = object_size_m * 100 / cm_per_px, False
        scale = f"{object_size_m:g} m at {cm_per_px:.1f} cm per pixel is "
    else:
        return SizeGate(target_name, None, floor, None,
                        f"the size of the {t.noun} in this picture is not known; mark two or three of them on the "
                        f"picture so it can be measured")
    object_px = finder_px(picture_px, picture_long_px, finder_input)
    return SizeGate(target_name, object_px, floor, object_px >= floor,
                    _size_text(t, floor, finder_input, object_px, picture_px, picture_long_px, scale, from_marks))


# --------------------------------------------------------------------------- #
# Countability
# --------------------------------------------------------------------------- #

@dataclass(frozen=True)
class Facts:
    """What the caller knows about the picture and the field. None means not known, never "no"."""

    planting_date: Optional[date] = None
    capture_date: Optional[date] = None
    green_cover_share: Optional[float] = None  # share of the ground under green leaves, 0-1, measured by the caller


@dataclass(frozen=True)
class Countability:
    verdict: Verdict
    reason_code: str
    reason_text: str
    needs: tuple[str, ...] = ()  # names of the Facts fields that are missing (MAYBE only)


_FACT_WORDS = {
    "planting_date": "the planting date",
    "capture_date": "the date the picture was taken",
    "green_cover_share": "how much of the ground green leaves cover",
}


def _and_list(items: Sequence[str]) -> str:
    return items[0] if len(items) == 1 else f"{', '.join(items[:-1])} and {items[-1]}"


def _needs(noun: str, missing: Sequence[str]) -> Countability:
    words = _and_list([_FACT_WORDS[name] for name in missing])
    return Countability(Verdict.MAYBE, "needs_facts", f"to count {noun} this needs {words}", tuple(missing))


def _plant_countability(t: Target, facts: Facts) -> Countability:
    share = facts.green_cover_share
    if share is not None:
        _check_unit_interval("green cover share", share)
        if share >= CLOSED_CANOPY_SHARE:
            return Countability(Verdict.NO, "plants_touch",
                                f"green leaves cover {share:.0%} of the ground ({CLOSED_CANOPY_SHARE:.0%} or more): "
                                f"plants touch, so one cannot be told from the next")
    needed = ("planting_date", "capture_date", "green_cover_share") if t.kind == ANNUAL_CROP else ("green_cover_share",)
    if t.kind == ANNUAL_CROP and facts.planting_date is not None and facts.capture_date is not None:
        days = (facts.planting_date - facts.capture_date).days
        if days > 0:
            return Countability(Verdict.NO, "before_planting",
                                f"the picture was taken {days} days before planting, so there are no {t.noun} to count")
    missing = [name for name in needed if getattr(facts, name) is None]
    if missing:
        return _needs(t.noun, missing)
    return Countability(Verdict.YES, "countable", f"{t.noun} stand apart: green leaves cover {share:.0%} of the ground")


def countability(target_name: str, facts: Facts) -> Countability:
    """Whether this kind of object can be counted one by one on this picture: YES, MAYBE (listing what is
    missing, or untested) or NO (with the reason). A NO that the known facts already prove wins over missing ones.
    Size is not a fact here: `decide` applies the size gate."""
    t = target(target_name)
    if t.kind == STRUCTURE:
        return Countability(Verdict.YES, "countable", f"{t.noun} do not grow or touch, so each can be found on its own")
    if t.kind == PERENNIAL_CROWN:
        return Countability(Verdict.MAYBE, "count_untested", f"counting {t.noun} one by one is not proven yet")
    return _plant_countability(t, facts)


# --------------------------------------------------------------------------- #
# Decision
# --------------------------------------------------------------------------- #

@dataclass(frozen=True)
class Evidence:
    """Something known about this one picture that limits the answer further. It can only lower the level:
    raising one needs a new measurement in answer_levels_targets.json."""

    max_level: Level
    reason_text: str


@dataclass(frozen=True)
class Answer:
    level: Level
    reason_code: str
    reason_text: str

    def as_dict(self) -> dict[str, str]:
        return {"level": self.level.name, "label": self.level.label, "meaning": self.level.meaning,
                "reason_code": self.reason_code, "reason_text": self.reason_text}


def _measured_text(t: Target) -> str:
    """Why the answer stops at the target's max level, from recorded values only."""
    m = t.measured
    head = f"for {t.noun} the answer stops at \"{t.max_level.label}\" today"
    if m["value"] is None:
        return f"{head}: {m['note']}"
    text = f"{head}: measured on {m['n_images']} pictures on {m['date']}; data: {m['dataset']}"
    return f"{text}; {m['note']}" if t.max_level is Level.CANT_TELL else text


def decide(target_name: str, finder_model: str, gate: SizeGate, verdict: Countability,
           evidence: Optional[Evidence] = None) -> Answer:
    """The highest level up to the target's max_level that the size gate, countability and any evidence allow.
    CAN'T TELL whenever the table says so, the size gate does not pass, or countability is not YES (the reason
    then says what is missing). `finder_model` names the finder that found the objects: the levels were measured
    for one finder, and another finder's output is refused (ValueError), not given its level."""
    t = target(target_name)
    if finder_model != t.finder_model:
        raise ValueError(f"levels for {t.noun} were measured for the {t.finder_model} finder, not {finder_model!r}")
    if gate.target != target_name:
        raise ValueError(f"the size gate was worked out for {gate.target!r}, not {target_name!r}")
    if t.max_level is Level.CANT_TELL:
        return Answer(Level.CANT_TELL, "measured_limit", _measured_text(t))
    if gate.passes is not True:
        return Answer(Level.CANT_TELL, "size_unknown" if gate.passes is None else "too_small", gate.reason)
    if verdict.verdict is not Verdict.YES:
        return Answer(Level.CANT_TELL, verdict.reason_code, verdict.reason_text)
    if evidence is not None and evidence.max_level < t.max_level:
        # No answer about the area objects cover has been measured, so a limit at AREA leaves nothing to offer.
        level = Level.CANT_TELL if evidence.max_level is Level.AREA else evidence.max_level
        return Answer(level, "evidence_limit", evidence.reason_text)
    return Answer(t.max_level, "measured_limit", _measured_text(t))
