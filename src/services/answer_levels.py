"""How far an answer about objects in a picture may go: a count, a count after checking, an estimate, a rough
number, an area, or "can't tell".

The picture finder (YOLOE-11s, shown three marks the user drew on the picture) was measured on 2026-10-11 for
each kind of object. The results, and where each came from, are in answer_levels_targets.json beside this module.
Each kind of object has the highest level that evidence supports today; none is trusted to COUNT yet. Two gates
can lower the level for one picture:

* the size gate: objects under 50 px across were not found reliably (below about 25-30 px every kind failed);
* countability: for example, crop plants whose leaves touch cannot be told apart.

`decide` combines the table and the gates into one Answer whose reason is in data terms. Consumers read
`ladder_enabled()` (ANSWER_LADDER, off by default) before using any of this. Rules:
docs/rules/ANSWER_LEVELS_RULES.md.

This module owns the finder's size floor only. src/services/plant_counts.py counts plants with another method
(green crowns) and keeps its own floor, 5 cm per pixel, checked on Cyampirita maize; see the rules doc.
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

PROVENANCE_KEYS = ("dataset", "protocol", "n_images", "image_px", "zoom", "date", "metric", "value", "where", "note")
# A measured value must say how, when, on how many pictures and where it was measured.
_REQUIRED_WHEN_MEASURED = ("protocol", "n_images", "date", "metric", "where")

TARGETS_PATH = Path(__file__).parent / "answer_levels_targets.json"


@dataclass(frozen=True)
class Target:
    """One kind of object the finder was measured on, with its settings and the highest level it supports."""

    name: str
    noun: str  # plural, as a reader would say it: "maize plants", "dishes"
    kind: str
    object_size_m: Optional[float]  # typical size on the ground; None when nobody measured it
    object_size_note: str  # where the size comes from, or why there is none
    zoom: int  # finder setting measured best: 2 for plants, 1 for the rest
    look_alike_marks: bool  # finder setting measured best: also mark things that look alike but are not
    max_level: Level
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
    else:
        _check_unit_interval(f"answer target {name}", measured["value"])
        unsaid = [key for key in _REQUIRED_WHEN_MEASURED if measured[key] is None]
        if unsaid:
            raise ValueError(f"answer target {name}: a measured value must say {unsaid}")
        if measured["dataset"] is None and not measured["note"]:
            raise ValueError(f"answer target {name}: no dataset and no note saying why")


def _target(name: str, entry: dict[str, Any]) -> Target:
    measured = entry["measured"]
    _check_provenance(name, measured)
    for extra in entry["also_measured"]:
        if "value" in extra:
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
    zoom = entry["finder"]["zoom"]
    if not isinstance(zoom, int) or zoom < 1:
        raise ValueError(f"answer target {name}: zoom {zoom!r} is not a whole number of 1 or more")
    return Target(name=name, noun=entry["noun"], kind=entry["kind"], object_size_m=size,
                  object_size_note=entry["object_size_note"], zoom=zoom,
                  look_alike_marks=bool(entry["finder"]["look_alike_marks"]), max_level=Level[entry["max_level"]],
                  measured=dict(measured), also_measured=tuple(dict(e) for e in entry["also_measured"]))


def load_targets(path: Path = TARGETS_PATH) -> tuple[int, dict[str, Any], dict[str, Target]]:
    """(needed px, the size floor's provenance, targets by name) from the committed table; raises if it is broken."""
    data = json.loads(path.read_text())
    floor = data["size_floor"]
    needed_px = floor["needed_px"]
    if not isinstance(needed_px, int) or needed_px < 1:
        raise ValueError(f"size floor {needed_px!r} px is not a whole number of 1 or more")
    return needed_px, dict(floor["measured"]), {name: _target(name, e) for name, e in data["targets"].items()}


NEEDED_PX, SIZE_FLOOR_SOURCE, _TARGETS = load_targets()


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
    object_px: Optional[float]  # one object's size in the picture; None when it cannot be known
    needed_px: int
    passes: Optional[bool]  # None: the size is unknown, so the gate cannot pass
    reason: str
    cm_per_px: Optional[float]
    from_marks: bool  # the size came from the boxes the user drew


def box_px(width_px: float, height_px: float) -> float:
    """One box's size in pixels: the side of a square of the same area (objects are sized by area, as in COCO)."""
    if not (width_px > 0 and height_px > 0):
        raise ValueError(f"a marked box needs a positive width and height, got {width_px!r} x {height_px!r}")
    return math.sqrt(width_px * height_px)


def _size_text(noun: str, object_px: Optional[float], cm_per_px: Optional[float], from_marks: bool) -> str:
    """The size gate's reason in data terms, e.g. "plants are about 9 px wide at 3.2 cm per pixel; ..."."""
    if object_px is None:
        return (f"the size of the {noun} in this picture is not known; mark two or three of them on the picture "
                f"so it can be measured ({NEEDED_PX} px across or more is needed)")
    fails = object_px < NEEDED_PX
    shown = math.floor(object_px) if fails else round(object_px)  # never print "50 px" for 49.6 px that failed
    who = f"the marked {noun}" if from_marks else noun
    scale = f" at {cm_per_px:.1f} cm per pixel" if cm_per_px is not None else ""
    enough = f"at least {NEEDED_PX} px are needed" if fails else f"{NEEDED_PX} px or more is enough"
    return f"{who} are about {shown} px wide{scale}; {enough}"


def size_gate(cm_per_px: Optional[float], object_size_m: Optional[float] = None,
              marked_box_px: Optional[Sequence[tuple[float, float]]] = None) -> SizeGate:
    """Whether objects are big enough in this picture for the finder: NEEDED_PX across or more.

    `marked_box_px` is the (width, height) in pixels of each box the user drew; their median size is used when
    given, before `object_size_m` (a size on the ground, needing `cm_per_px`).
    """
    if cm_per_px is not None and not cm_per_px > 0:
        raise ValueError(f"cm per pixel must be positive, got {cm_per_px!r}")
    if object_size_m is not None and not object_size_m > 0:
        raise ValueError(f"object size must be positive, got {object_size_m!r} m")
    from_marks = bool(marked_box_px)
    if from_marks:
        object_px: Optional[float] = statistics.median(box_px(w, h) for w, h in marked_box_px or ())
    elif object_size_m is not None and cm_per_px is not None:
        object_px = object_size_m * 100 / cm_per_px
    else:
        object_px = None
    passes = None if object_px is None else object_px >= NEEDED_PX
    return SizeGate(object_px=object_px, needed_px=NEEDED_PX, passes=passes,
                    reason=_size_text("objects", object_px, cm_per_px, from_marks), cm_per_px=cm_per_px,
                    from_marks=from_marks)


# --------------------------------------------------------------------------- #
# Countability
# --------------------------------------------------------------------------- #

@dataclass(frozen=True)
class Facts:
    """What the caller knows about the picture and the field. None means not known, never "no"."""

    planting_date: Optional[date] = None
    capture_date: Optional[date] = None
    green_cover_share: Optional[float] = None  # share of the ground under green leaves, 0-1, measured by the caller
    size_gate: Optional[SizeGate] = None


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
    "size_gate": "how big one of them is in the picture (mark two or three)",
}


def _and_list(items: Sequence[str]) -> str:
    return items[0] if len(items) == 1 else f"{', '.join(items[:-1])} and {items[-1]}"


def _needs(noun: str, missing: Sequence[str]) -> Countability:
    words = _and_list([_FACT_WORDS[name] for name in missing])
    return Countability(Verdict.MAYBE, "needs_facts", f"to count {noun} this needs {words}", tuple(missing))


def _structure_countability(t: Target, facts: Facts) -> Countability:
    gate = facts.size_gate
    if gate is None or gate.passes is None:
        return _needs(t.noun, ["size_gate"])
    if not gate.passes:
        return Countability(Verdict.NO, "too_small", _size_text(t.noun, gate.object_px, gate.cm_per_px,
                                                                gate.from_marks))
    return Countability(Verdict.YES, "countable", f"{t.noun} are big enough in this picture to be found one by one")


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
    missing, or untested) or NO (with the reason). A NO that the known facts already prove wins over missing ones."""
    t = target(target_name)
    if t.kind == STRUCTURE:
        return _structure_countability(t, facts)
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
    m = t.measured
    if m["value"] is None:
        return f"for {t.noun} the answer stops at \"{t.max_level.label}\" today: {m['note']}"
    return (f"for {t.noun} the answer stops at \"{t.max_level.label}\" today: measured on {m['n_images']} "
            f"pictures ({m['dataset'] or 'dataset not named'}, {m['date']})")


def decide(target_name: str, gate: SizeGate, verdict: Countability, evidence: Optional[Evidence] = None) -> Answer:
    """The highest level up to the target's max_level that the size gate, countability and any evidence allow.
    CAN'T TELL whenever the size gate does not pass or countability is NO."""
    t = target(target_name)
    if gate.passes is not True:
        code = "size_unknown" if gate.passes is None else "too_small"
        return Answer(Level.CANT_TELL, code, _size_text(t.noun, gate.object_px, gate.cm_per_px, gate.from_marks))
    if verdict.verdict is Verdict.NO:
        return Answer(Level.CANT_TELL, verdict.reason_code, verdict.reason_text)
    level, code, text = t.max_level, "measured_limit", _measured_text(t)
    if verdict.verdict is Verdict.MAYBE:
        level = min(level, Level.AREA)
        code, text = verdict.reason_code, f"{verdict.reason_text}; the area they cover is offered instead"
    if evidence is not None and evidence.max_level < level:
        level, code, text = evidence.max_level, "evidence_limit", evidence.reason_text
    return Answer(level, code, text)
