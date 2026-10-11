"""Tower question cards: the questions Ingabe asks about a tower inspection, answered from what a person counted.

Two readers: the tower owner or asset manager (what is on the tower, what changed) and the field technician (which
photos to check next, where the photos were taken from). Each answer comes in the drone cards' order (what, why,
what to do, how sure) and in their shape, so the screen renders both alike (src/services/drone_cards.py `_answer`).

Rules (TOWERS_RULES.md):
- A number of parts is only ever what a person counted (tower_inspection.counted_by_you): the finder's suggestions
  are never shown as a count, and a photo still being checked adds nothing.
- Photos overlap: the same dish seen from two sides is on two photos. The answer is "at least N", the most counted
  on one photo; the sum over photos is named as such and never called the number on the tower.
- How far an answer about the finder's results may go is answer_levels' (shown when ANSWER_LADDER is on); what the
  finder was measured to do is read from answer_levels' table, never copied.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Optional

from src.services import answer_levels, example_finder, tower_inspection
from src.services.card_status import CANT_TELL, LEARN, NEEDS_FLIGHT, PARTLY, READY, WORKING, how_sure
from src.services.photo_inspections import PhotoFacts
from src.services.tower_inspection import (
    CHECK_ORDER,
    CONFIRM,
    FINDING,
    NOT_FOUND,
    PARTS,
    UNMARKED,
    WAITING,
    Earlier,
    Inspection,
    PartInventory,
)

OWNER = "owner"
TECHNICIAN = "technician"
AUDIENCES = {OWNER: "Tower owner / asset manager", TECHNICIAN: "Field technician"}
DEFAULT_AUDIENCE = OWNER

STATUS_LABELS = {
    READY: "Answer ready",
    PARTLY: "Partly ready",
    WORKING: "Working on it",
    NEEDS_FLIGHT: "Needs a second inspection",
    CANT_TELL: "Can't tell yet",
    LEARN: "Learn · 1 minute",
}

CARDS = {
    "equipment": "What equipment is on this tower?",
    "photos_to_check": "Which photos still need checking?",
    "camera_positions": "Where were the photos taken from?",
    "changes": "What changed since the last inspection?",
    "learn_review": "Why does a person check every suggestion?",
}

# Readiness first, then the reader (as the drone cards rank theirs); the learning card always comes last.
_BASE_SCORE = {READY: 100, PARTLY: 60, WORKING: 55, NEEDS_FLIGHT: 40, CANT_TELL: 20, LEARN: 0}
# The owner's question comes first even before anything is counted (its answer says how to start); the technician's
# list of photos to check comes first for them.
_READER_BOOST = {
    OWNER: {"equipment": 90, "changes": 20},
    TECHNICIAN: {"photos_to_check": 45, "camera_positions": 15},
}
LISTED_PHOTOS = 20  # photos listed in one answer; the facts give the totals

_START = ("mark 3 {noun} on a photo where they are clear, press Find, then keep, fix or reject each suggestion and "
          "add any it missed")
NOTHING_COUNTED = ("Nothing on this inspection has been checked by a person yet, so there is no count. To start: "
                   + _START.format(noun="dishes (or antennas, or radio units)") + ". The count of a photo appears "
                   "once every suggestion on it is checked.")
NEVER_COUNT_YOURSELF = ("Give only the counts listed here. Never count equipment from a photo yourself, and never "
                        "present the finder's suggestions as a count: a person checks each one first.")


class UnknownAudience(ValueError):
    pass


def check_audience(audience: Optional[str]) -> str:
    if audience is None:
        return DEFAULT_AUDIENCE
    if audience not in AUDIENCES:
        raise UnknownAudience(f"the reader must be one of {list(AUDIENCES)}, not {audience!r}")
    return audience


@dataclass(frozen=True)
class Here:
    """What the cards answer from: the inspection with its work, and the same site inspected before."""

    inspection: Inspection
    earlier: Earlier

    @property
    def inventory(self) -> dict[str, PartInventory]:
        return tower_inspection.inventory(self.inspection)


# --- Words -------------------------------------------------------------------------------------------------------

def _cap(text: str) -> str:
    return text[:1].upper() + text[1:]


def _n(count: int, one: str, many: Optional[str] = None) -> str:
    return f"{count} {one if count == 1 else (many or one + 's')}"


def _names(names: list[str]) -> str:
    return names[0] if len(names) == 1 else ", ".join(names[:-1]) + " and " + names[-1]


def _photo_url(inspection: Inspection, photo: PhotoFacts) -> str:
    return f"/api/layer/{inspection.layer_id}/photos/{photo.sha256}"


def _item(inspection: Inspection, photo: PhotoFacts, detail: str) -> dict[str, Any]:
    """A photo to open, with its picture and where it was taken (None when it has no GPS position)."""
    return {"id": photo.sha256, "title": photo.filename, "detail": detail, "picture": _photo_url(inspection, photo),
            "lon": photo.lon, "lat": photo.lat}


def _metres(value: float) -> str:
    return f"{value:.0f} m"


def _degrees(value: float) -> str:
    return f"{value:.0f}°"


TERMS: dict[str, dict[str, Any]] = {
    "suggestion": {
        "word": "Suggestion",
        OWNER: ("A box the finder drew around something it thinks is the part you marked.",
                "It saves a technician from drawing every box, but it is a guess until someone keeps it."),
        TECHNICIAN: ("A box the finder drew after you marked 3 examples. Keep it, fix its box, or reject it.",
                     "Every suggestion on a photo must be checked before that photo's count exists."),
    },
    "counted_by_you": {
        "word": "Counted by you",
        OWNER: ("The parts a person marked, kept, fixed or added on a photo, once every suggestion on it was checked.",
                "It is the only number Ingabe shows: no count comes from the finder alone."),
        TECHNICIAN: ("Your marks, plus the suggestions you kept or fixed, plus the ones you added, on one photo.",
                     "It appears as soon as nothing on the photo is waiting for you."),
    },
    "at_least": {
        "word": "At least",
        OWNER: ("The most of a part counted on any one photo of the tower.",
                "Photos overlap, so adding photos up would count the same dish twice; one photo proves this many."),
        TECHNICIAN: ("The highest count of a part on one of your photos.",
                     "A photo from another side may show more: count it there too."),
    },
    "ap50": {
        "word": "AP50",
        OWNER: ("A score from 0 to 1 for a finder: 1 would mean every part found and every box right. A box counts "
                "as right when it covers the real part by half or more.",
                "It says how much the finder's suggestions can be trusted before anyone checks them."),
        TECHNICIAN: ("Average precision at 50% overlap, against boxes people drew on test photos.",
                     "A low score means more suggestions to reject and more missed parts to add."),
    },
    "look_alike": {
        "word": "Look-alike marks",
        OWNER: ("Boxes on things that look like the part but are not, shown to the finder so it leaves them out.",
                "They helped the finder on antennas and radio units when it was measured; not on dishes."),
        TECHNICIAN: ("Your marks of the other parts on the photo are given to the finder as things to leave out.",
                     "Mark dishes and antennas first, then radio units: the finder confuses them less."),
    },
    "height_above_takeoff": {
        "word": "Height above take-off",
        OWNER: ("How high the drone was above the place it took off from, as the drone recorded it.",
                "Photos from several heights show the equipment from above and below."),
        TECHNICIAN: ("DJI's relative altitude: metres above the take-off point, not above the ground at the tower.",
                     "If you took off below or above the tower's base, the heights are offset by that difference."),
    },
    "camera_tilt": {
        "word": "Camera tilt",
        OWNER: ("How far the camera looked down: 0° level, 90° straight down.",
                "The finder was measured on photos taken at an angle, not looking straight down."),
        TECHNICIAN: ("DJI's gimbal pitch. Within 10° of straight down a photo counts as looking straight down.",
                     "Tower parts can be found only on photos taken at an angle, as the finder was measured."),
    },
}


def _terms(ids: list[str], audience: str) -> list[dict[str, str]]:
    return [{"id": term, "word": TERMS[term]["word"], "simple": TERMS[term][audience][0],
             "why": TERMS[term][audience][1]} for term in ids]


# --- Status and preview --------------------------------------------------------------------------------------------

def _anything_counted(inventory: dict[str, PartInventory]) -> bool:
    return any(i.at_least is not None for i in inventory.values())


def _anything_marked(inventory: dict[str, PartInventory]) -> bool:
    return any(i.photos_marked for i in inventory.values())


def _open(inventory: dict[str, PartInventory]) -> bool:
    return any(i.photos_waiting or i.photos_to_confirm or i.photos_not_found or i.photos_finding
               for i in inventory.values())


def _uncounted(inventory: dict[str, PartInventory]) -> list[str]:
    """The parts counted on no photo yet, as the reader names them."""
    return [i.noun for i in inventory.values() if i.at_least is None]


def _complete(here: Here) -> bool:
    """Whether the equipment answer covers the whole inspection: every part counted on some photo, no marked photo
    still open, and something marked on every photo (a photo nothing is marked on was not looked at)."""
    inventory = here.inventory
    return (not _uncounted(inventory) and not _open(inventory)
            and not tower_inspection.photos_unmarked(here.inspection))


def _status(card_id: str, here: Here) -> str:
    inventory = here.inventory
    if card_id == "equipment":
        if not _anything_counted(inventory):
            return WORKING if any(i.photos_finding for i in inventory.values()) else CANT_TELL
        return READY if _complete(here) else PARTLY
    if card_id == "photos_to_check":
        return READY
    if card_id == "camera_positions":
        return READY if here.inspection.photos else CANT_TELL
    if card_id == "changes":
        if here.earlier.inspection is None:
            # An earlier inspection of the site with nothing counted on it: counting there answers it, no new one.
            return CANT_TELL if here.earlier.same_site else NEEDS_FLIGHT
        return READY if _anything_counted(inventory) else CANT_TELL
    return LEARN


def _part_phrase(i: PartInventory) -> str:
    return f"at least {i.at_least} {i.noun}"


def _preview(card_id: str, here: Here) -> str:
    inventory = here.inventory
    if card_id == "equipment":
        counted = [i for i in inventory.values() if i.at_least is not None]
        if counted:
            rest, unmarked = _uncounted(inventory), tower_inspection.photos_unmarked(here.inspection)
            text = _cap(", ".join(_part_phrase(i) for i in counted)) + ", counted by you."
            text += f" {_cap(_names(rest))} not counted yet." if rest else ""
            return text + (f" Nothing marked yet on {_n(unmarked, 'photo')}." if unmarked else "")
        waiting = sum(i.waiting for i in inventory.values())
        if waiting:
            return f"{_n(waiting, 'suggestion')} wait for checking; nothing is counted yet."
        finding = sum(i.photos_finding for i in inventory.values())
        if finding:
            return f"The finder is working on {_n(finding, 'photo')}; nothing is counted yet."
        if _anything_marked(inventory):
            return "Nothing counted yet: the marked photos are still being checked."
        return "Nothing counted yet: mark 3 of a part on a photo to start."
    if card_id == "photos_to_check":
        to_check = tower_inspection.photos_to_check(here.inspection)
        waiting = [c for c in to_check if c.reason == WAITING]
        if not to_check:
            return "Every photo has something marked and every marked part counted."
        text = f"{_n(len(to_check), 'photo')} to check"
        return text + (f", {len(waiting)} with suggestions waiting." if waiting else ".")
    if card_id == "camera_positions":
        facts = tower_inspection.camera_facts(here.inspection.photos)
        text = f"{_n(facts.photos, 'photo')}, {facts.with_gps} with GPS"
        if facts.height_m is not None:
            text += f", taken {_metres(facts.height_m.least)} to {_metres(facts.height_m.most)} above take-off"
        return text + "."
    if card_id == "changes":
        if here.earlier.inspection is None and here.earlier.same_site:
            return (f"{_n(here.earlier.same_site, 'earlier inspection')} of this site, nothing counted on "
                    f"{'it' if here.earlier.same_site == 1 else 'them'} yet.")
        if here.earlier.inspection is None:
            return "No earlier inspection of this site in this project: a second one shows what changed."
        if not _anything_counted(inventory):
            return f"{here.earlier.inspection.name} has counts; nothing is counted on this inspection yet."
        return f"Compared with {here.earlier.inspection.name}."
    return "The finder suggests, you decide: what it was measured to do."


def _card(card_id: str, here: Here) -> dict[str, Any]:
    status = _status(card_id, here)
    return {"id": card_id, "title": CARDS[card_id], "status": status, "preview": _preview(card_id, here)}


def deck(here: Here, audience: Optional[str]) -> dict[str, Any]:
    """The cards for this inspection and reader, the most useful first; the learning card last."""
    reader = check_audience(audience)
    cards = [_card(card_id, here) for card_id in CARDS]
    learn = [c for c in cards if c["status"] == LEARN]
    ranked = sorted((c for c in cards if c["status"] != LEARN),
                    key=lambda c: _BASE_SCORE[c["status"]] + _READER_BOOST[reader].get(c["id"], 0), reverse=True)
    return {"cards": ranked + learn}


# --- Answers -------------------------------------------------------------------------------------------------------

def _answer(card_id: str, here: Here, *, what: str, why: str, todo: str, sure: dict[str, Any], terms: list[str],
            audience: str, facts: Optional[list[dict[str, str]]] = None, items: Optional[list[dict[str, Any]]] = None,
            progress: Optional[dict[str, Any]] = None) -> dict[str, Any]:
    """One answer, in the drone cards' answer shape (drone_cards._answer)."""
    card = _card(card_id, here)
    return {**card, "question": card["title"], "status_label": STATUS_LABELS[card["status"]], "what": what,
            "why": why, "todo": todo, "how_sure": sure, "facts": facts or [], "terms": _terms(terms, audience),
            "items": items or [], "downloads": [], "progress": progress, "overlay": None, "choices": None,
            "upload": None, "field_check": None, "ask_sage": []}


def _level_words(answer: Optional[answer_levels.Answer]) -> list[str]:
    """The answer level, as answer_levels words it, when ANSWER_LADDER is on."""
    if answer is None or not answer_levels.ladder_enabled():
        return []
    return [f"{answer.level.label}: {answer.level.meaning}"]


def _finder_progress(inspection: Inspection) -> Optional[dict[str, Any]]:
    """While the finder works on some photos: how many of the finder jobs on marked parts are done."""
    jobs = [w.job for parts in inspection.work.values() for w in parts.values() if w.marks and w.job is not None]
    if not any(j.status in (example_finder.QUEUED, example_finder.RUNNING) for j in jobs):
        return None
    return {"done": sum(1 for j in jobs if j.status == example_finder.DONE), "parts": len(jobs),
            "minutes_left": None}


def _equipment(here: Here, audience: str) -> dict[str, Any]:
    inspection, inventory = here.inspection, here.inventory
    total = len(inspection.photos)
    lines, facts, items = [], [], []
    for i in inventory.values():
        open_words = []
        if i.waiting:
            open_words.append(f"{_n(i.waiting, 'suggestion')} waiting on {_n(i.photos_waiting, 'photo')}")
        if i.photos_to_confirm:
            open_words.append(f"{_n(i.photos_to_confirm, 'photo')} where the finder found no more, to confirm")
        if i.photos_not_found:
            open_words.append(f"{_n(i.photos_not_found, 'marked photo')} not found yet")
        if i.photos_finding:
            open_words.append(f"the finder working on {_n(i.photos_finding, 'photo')}")
        still = f" Still open: {'; '.join(open_words)}." if open_words else ""
        if i.at_least is not None and i.at_least_photo is not None:
            lines.append(f"{_cap(i.noun)}: at least {i.at_least}, counted by you on {i.at_least_photo.filename} (the "
                         f"most on one photo); counted on {i.photos_counted} of {_n(total, 'photo')}.{still}")
            facts.append({"label": _cap(i.noun), "value": f"at least {i.at_least}"})
            facts.append({"label": f"{_cap(i.noun)} added up over photos",
                          "value": f"{i.counted_in_photos} on {_n(i.photos_counted, 'photo')} (a part seen on two "
                                   f"photos counts twice)"})
            items.append(_item(inspection, i.at_least_photo, f"{i.at_least} {i.noun} counted by you"))
        else:
            lines.append(f"{_cap(i.noun)}: not counted yet.{still}")
            facts.append({"label": _cap(i.noun), "value": "not counted yet"})
    marked_counted = tower_inspection.photos_with_marked_parts_counted(inspection)
    unmarked = tower_inspection.photos_unmarked(inspection)
    uncounted = _uncounted(inventory)
    waiting = sum(i.waiting for i in inventory.values())
    facts.append({"label": "Photos with every marked part counted", "value": f"{marked_counted} of {total}"})
    facts.append({"label": "Photos with nothing marked", "value": f"{unmarked} of {total}"})
    facts.append({"label": "Suggestions waiting", "value": str(waiting)})
    marked = _anything_marked(inventory)
    if not marked:
        what = NOTHING_COUNTED
    elif not _anything_counted(inventory):
        what = "Nothing is counted yet: " + (f"{_n(waiting, 'suggestion')} wait for a person to check them. "
                                             if waiting else "the marked photos are still being checked. ")
        what += " ".join(lines)
    else:
        what = " ".join(lines)
    if marked and unmarked:
        what += (f" Nothing is marked yet on {_n(unmarked, 'photo')} of {total}: parts seen only there are not "
                 "counted.")
    to_check = tower_inspection.photos_to_check(inspection)
    first = next((c for c in to_check if c.reason != UNMARKED), None)  # the first marked photo still open
    still_open = len([c for c in to_check if c.reason != UNMARKED])
    if audience == TECHNICIAN:
        why = ("A photo's count exists only once you have checked every suggestion on it. Photos overlap, so each "
               "number is the most you counted on one photo.")
        if first is not None and first.reason == WAITING:
            todo = f"Open {first.photo.filename} first: {_n(first.waiting, 'suggestion')} wait for you."
        elif first is not None:
            todo = f"Next, {first.photo.filename}: {_check_detail(first)}."
        elif not marked:
            todo = _cap(_START.format(noun="dishes (or antennas, or radio units)")) + "."
        elif uncounted:
            todo = _cap(_START.format(noun=" or ".join(uncounted))) + "."
        elif unmarked:
            todo = (f"Open the {_n(unmarked, 'photo')} with nothing marked: {'it' if unmarked == 1 else 'they'} "
                    "may show parts not counted yet.")
        else:
            todo = "Count the parts again on a photo from another side: it may show parts hidden on these."
    else:
        why = ("An asset register needs what is really on the tower. Each number was checked by a person, "
               "suggestion by suggestion; photos overlap, so it is the most seen on one photo, not a sum of photos.")
        if waiting:
            todo = f"Ask a technician to check the {_n(waiting, 'suggestion')} waiting before using these numbers."
        elif still_open:
            todo = (f"Ask a technician to finish the {_n(still_open, 'marked photo')} still open: their counts "
                    "appear once every suggestion on them is checked.")
        elif not marked:
            todo = "Ask a technician to " + _START.format(noun="dishes, antennas and radio units") + "."
        elif uncounted:
            todo = f"Ask a technician to count the {_names(uncounted)} too: until then this answer leaves them out."
        elif unmarked:
            todo = (f"Ask a technician to look at the {_n(unmarked, 'photo')} with nothing marked: "
                    f"{'it' if unmarked == 1 else 'they'} may show more.")
        else:
            todo = "Use these as the least on the tower; a photo from another side may show more."
    complete = _complete(here)
    best = next((i.answer for i in inventory.values() if i.answer is not None), None)
    gaps = ((["Some marked photos are still being checked: they add nothing yet"] if _open(inventory) else [])
            + ([f"{_cap(_names(uncounted))} not counted on any photo yet"] if uncounted else [])
            + ([f"Nothing marked on {_n(unmarked, 'photo')} of {total}: nobody has looked for parts there"]
               if unmarked else []))
    sure = how_sure("medium" if complete else "low", _level_words(best) + [
        "Counted by you: every suggestion on a counted photo was kept, fixed or rejected by a person",
        "Photos overlap: the same part on two photos counts on both, so only the most on one photo is given",
        "Parts hidden behind others on every photo cannot be counted",
    ] + gaps, "Check a photo taken from another side of the tower.")
    return _answer("equipment", here, what=what, why=why, todo=todo, sure=sure, audience=audience,
                   terms=["counted_by_you", "at_least", "suggestion"], facts=facts, items=items,
                   progress=_finder_progress(inspection))


_REASON_WORDS = {
    WAITING: "{waiting} waiting for you",
    CONFIRM: "the finder found no more {parts}: add any it missed, then save",
    NOT_FOUND: "{parts} marked; press Find",
    FINDING: "the finder is working on {parts}",
    UNMARKED: "nothing marked yet",
}


def _check_detail(check: tower_inspection.PhotoToCheck) -> str:
    parts = _names([tower_inspection.noun(p) for p in check.parts]) if check.parts else ""
    return _REASON_WORDS[check.reason].format(waiting=_n(check.waiting, "suggestion"), parts=parts)


def _photos_to_check(here: Here, audience: str) -> dict[str, Any]:
    inspection = here.inspection
    to_check = tower_inspection.photos_to_check(inspection)
    by_reason = {reason: [c for c in to_check if c.reason == reason] for reason in CHECK_ORDER}
    waiting = sum(c.waiting for c in by_reason[WAITING])
    facts = [{"label": "Photos to check", "value": f"{len(to_check)} of {len(inspection.photos)}"},
             {"label": "Suggestions waiting", "value": f"{waiting} on {_n(len(by_reason[WAITING]), 'photo')}"},
             {"label": "Found no more, to confirm", "value": _n(len(by_reason[CONFIRM]), "photo")},
             {"label": "Marked, not found yet", "value": _n(len(by_reason[NOT_FOUND]), "photo")},
             {"label": "Finder working", "value": _n(len(by_reason[FINDING]), "photo")},
             {"label": "Nothing marked", "value": _n(len(by_reason[UNMARKED]), "photo")}]
    if not to_check:
        what = ("Every photo has something marked and every part marked on it is counted. A part nobody marked on "
                "a photo was not looked for there.")
        todo = "Nothing to check." if audience == TECHNICIAN else "The equipment counts include every marked photo."
    else:
        parts = []
        if by_reason[WAITING]:
            parts.append(f"{_n(len(by_reason[WAITING]), 'photo')} with {_n(waiting, 'suggestion')} waiting")
        if by_reason[CONFIRM]:
            parts.append(f"{_n(len(by_reason[CONFIRM]), 'photo')} where the finder found no more, to confirm")
        if by_reason[NOT_FOUND]:
            parts.append(f"{_n(len(by_reason[NOT_FOUND]), 'photo')} marked but not found yet")
        if by_reason[FINDING]:
            parts.append(f"{_n(len(by_reason[FINDING]), 'photo')} the finder is working on")
        if by_reason[UNMARKED]:
            parts.append(f"{_n(len(by_reason[UNMARKED]), 'photo')} with nothing marked")
        what = (f"{_n(len(to_check), 'photo')} of {len(inspection.photos)} still need checking: {_names(parts)}. "
                "Listed in the order to work on them: nearly done first, then in the order they were taken.")
        first = to_check[0]
        todo = (f"Start with {first.photo.filename}: {_check_detail(first)}." if audience == TECHNICIAN else
                "Ask the technician to work down this list; the first photos are nearly done.")
    why = ("A photo's count exists only once every suggestion on it is checked; until then it adds nothing to the "
           "equipment answer." if audience == TECHNICIAN else
           "Until these photos are checked, the equipment answer leaves them out.")
    items = [_item(inspection, c.photo, _check_detail(c)) for c in to_check[:LISTED_PHOTOS]]
    sure = how_sure("high", ["From the work saved on each photo of this inspection"], None)
    return _answer("photos_to_check", here, what=what, why=why, todo=todo, sure=sure, audience=audience,
                   terms=["suggestion", "counted_by_you"], facts=facts, items=items,
                   progress=_finder_progress(inspection))


def _camera_positions(here: Here, audience: str) -> dict[str, Any]:
    inspection = here.inspection
    c = tower_inspection.camera_facts(inspection.photos)
    sentences = [f"{_n(c.photos, 'photo')}, {c.with_gps} with a GPS position."]
    facts = [{"label": "Photos", "value": str(c.photos)}, {"label": "With GPS", "value": str(c.with_gps)}]
    if c.height_m is not None:
        sentences.append(f"Taken from {_metres(c.height_m.least)} to {_metres(c.height_m.most)} above the take-off "
                         f"point (middle {_metres(c.height_m.middle)}).")
        facts.append({"label": "Height above take-off",
                      "value": f"{_metres(c.height_m.least)} to {_metres(c.height_m.most)}, middle "
                               f"{_metres(c.height_m.middle)}"})
    if c.pitch_deg is not None:
        tilt = f"{_degrees(c.pitch_deg.least)} to {_degrees(c.pitch_deg.most)}"
        sentences.append(f"Camera tilt {tilt} (0° level, -90° straight down): {c.at_an_angle} at an angle, "
                         f"{c.straight_down} straight down.")
        facts.append({"label": "Camera tilt", "value": f"{tilt}, middle {_degrees(c.pitch_deg.middle)}"})
    if c.facing:
        sentences.append("Facing " + ", ".join(f"{word} {n}" for word, n in c.facing.items()) + ".")
        facts.append({"label": "Facing", "value": ", ".join(f"{word} {n}" for word, n in c.facing.items())})
    unknown = [text for text, n in ((f"{_n(c.no_pitch, 'photo')} without a camera tilt", c.no_pitch),
                                    (f"{_n(c.no_direction, 'photo')} without a direction", c.no_direction)) if n]
    if unknown:
        sentences.append(_cap(_names(unknown)) + " (no DJI flight data in them).")
    if c.without_gps:
        listed = list(c.without_gps[:LISTED_PHOTOS])
        more = len(c.without_gps) - len(listed)
        sentences.append(f"No GPS position: {_names(listed)}" + (f" and {more} more" if more else "") + ".")
    if c.first_taken:
        facts.append({"label": "Taken", "value": c.first_taken if c.first_taken == c.last_taken
                      else f"{c.first_taken} to {c.last_taken} (camera time)"})
    if c.cameras:
        facts.append({"label": "Camera", "value": ", ".join(c.cameras)})
    unseen = [word for word in tower_inspection.COMPASS if word not in c.facing] if c.facing else []
    if audience == TECHNICIAN:
        why = ("Gaps in heights or directions are where to fly or shoot again on the next visit; tower parts can be "
               "found only on photos taken at an angle.")
    else:
        why = ("Photos from every side and from above and below the equipment are what let parts be counted; a side "
               "no photo looks at is a side nobody can count.")
    if c.photos and unseen:
        todo = f"Next visit, also take photos facing {_names(unseen)}: no photo of this inspection does."
    elif c.no_pitch == c.photos:
        todo = "These photos carry no DJI flight data: use a drone that records it so parts can be found on them."
    else:
        todo = "Every direction is covered; keep flying the same way next time so inspections compare."
    items = [_item(inspection, p, "no GPS position") for p in inspection.photos if not p.has_position][:LISTED_PHOTOS]
    sure = how_sure("high", ["Read from each photo's own EXIF and DJI flight data",
                             "Heights are above the take-off point, not the ground at the tower"], None)
    return _answer("camera_positions", here, what=" ".join(sentences), why=why, todo=todo, sure=sure,
                   audience=audience, terms=["height_above_takeoff", "camera_tilt"], facts=facts, items=items)


def _at_least(count: Optional[int]) -> str:
    return "not counted" if count is None else f"at least {count}"


def _change_words(then: Optional[int], now: Optional[int]) -> str:
    if then is None or now is None:
        return "cannot be compared: counted on only one of the two"
    if now == then:
        return "the same"
    return f"{abs(now - then)} {'more' if now > then else 'fewer'} counted now"


def _changes(here: Here, audience: str) -> dict[str, Any]:
    earlier, inventory = here.earlier, here.inventory
    if earlier.inspection is None:
        what = ("This is the only inspection of this site in the project with parts counted by you"
                if earlier.same_site == 0 else
                f"{_n(earlier.same_site, 'earlier inspection')} of this site, but nothing is counted on "
                f"{'it' if earlier.same_site == 1 else 'them'}") + ", so there is nothing to compare with yet."
        todo = ("Count parts on the earlier inspection too, then come back here." if earlier.same_site else
                "Upload the next inspection of this site to this project; this card then shows what changed.")
        sure = how_sure("low", ["Needs parts counted by you on an earlier inspection of the same site"
                                if earlier.same_site else
                                "Needs a second inspection of the same site with parts counted by you"], None)
        return _answer("changes", here, what=what, why="A change can only be seen between two inspections.",
                       todo=todo, sure=sure, audience=audience, terms=["at_least"])
    before = tower_inspection.inventory(earlier.inspection)
    lines, facts, items = [], [], []
    for part in PARTS:
        now, then = inventory[part], before[part]
        if now.at_least is None and then.at_least is None:
            continue
        lines.append(f"{_cap(now.noun)}: {_at_least(then.at_least)} then, {_at_least(now.at_least)} now "
                     f"({_change_words(then.at_least, now.at_least)}).")
        facts.append({"label": _cap(now.noun), "value": _change_words(then.at_least, now.at_least)})
        for inv, inspection, when in ((then, earlier.inspection, "then"), (now, here.inspection, "now")):
            if inv.at_least_photo is not None:
                items.append(_item(inspection, inv.at_least_photo, f"{inv.at_least} {inv.noun} counted ({when})"))
    span_then = tower_inspection.camera_facts(earlier.inspection.photos)
    what = f"Compared with {earlier.inspection.name}" + (
        f" (taken {span_then.first_taken[:10]})" if span_then.first_taken else "") + ": " + " ".join(lines)
    why = ("More counted can be new equipment, or photos that show more of the tower; fewer can be removed equipment, "
           "or parts hidden on these photos. Look at both photos before deciding.")
    todo = ("Open the two photos of each part side by side before recording a change." if audience == OWNER else
            "Where the counts differ, photograph that part from the same side as last time.")
    sure = how_sure("low", ["Each number is the most counted on one photo of each inspection",
                            "Different photos see different sides of the tower"],
                    "Photos of each side from the same places as last time make the counts comparable.")
    return _answer("changes", here, what=what, why=why, todo=todo, sure=sure, audience=audience,
                   terms=["at_least"], facts=facts, items=items)


def _on_your_photos(here: Here, part: str) -> Optional[str]:
    """What the person's own reviews say about the finder on this part: suggestions kept or fixed of those reviewed,
    and parts it missed (added). None before any review."""
    kept = reviewed = added = 0
    for parts in here.inspection.work.values():
        work = parts[part]
        if work.job is None or work.job.status != example_finder.DONE:
            continue
        ids = {s.id for s in work.suggestions}
        kept += len([i for i in work.review.kept if i in ids]) + len([i for i, _ in work.review.fixed if i in ids])
        reviewed += len(work.review.reviewed() & ids)
        added += len(work.review.added)
    if not reviewed and not added:
        return None
    return f"on your photos, {kept} of {reviewed} suggestions checked were right (kept or fixed); {added} it missed"


def _learn_review(here: Here, audience: str) -> dict[str, Any]:
    lines, facts = [], []
    for part in PARTS:
        target = answer_levels.target(part)
        m = target.measured
        lines.append(f"{_cap(target.noun)}: {m['value']:.3f} on {m['n_images']} public test photos.")
        facts.append({"label": _cap(target.noun),
                      "value": f"{m['value']:.3f} AP50, {m['n_images']} photos ({m['dataset']}, {m['date']})"})
        own = _on_your_photos(here, part)
        if own:
            facts.append({"label": f"{_cap(target.noun)}, your checks", "value": own})
    what = ("You mark 3 of a part on a photo; the finder (YOLOE, shown your marks) draws boxes around the rest. It was "
            "measured on public tower photos, scored with AP50 from 0 to 1 (1: every part found and every box "
            "right): " + " ".join(lines) + " No score is high enough to use the boxes as a count, so a person keeps, "
            "fixes or rejects each one and adds what it missed; only then does the photo have a count.")
    why = ("A count used for an asset register or a maintenance order must be right; a finder that misses parts or "
           "boxes the wrong thing would make it wrong without anyone noticing." if audience == OWNER else
           "Checking every box is what makes the count yours: a missed radio unit or a wrong box changes it.")
    todo = ("Mark dishes and antennas before radio units: the finder is told the other parts' marks are not what it "
            "looks for, which helped on antennas and radio units when it was measured.")
    levels = sorted({answer_levels.target(part).max_level for part in PARTS}) if answer_levels.ladder_enabled() else []
    sure = how_sure("medium", [f"{level.label}: {level.meaning}" for level in levels] + [
        f"Measured on {answer_levels.target(PARTS[0]).measured['date']} on public tower photos, not on yours",
        "The measurement's photos and scripts are not in this repository",
    ], "Your own checks (kept, fixed, rejected, added) show how the finder does on your towers.")
    return _answer("learn_review", here, what=what, why=why, todo=todo, sure=sure, audience=audience,
                   terms=["ap50", "suggestion", "look_alike"], facts=facts)


_ANSWERS = {
    "equipment": _equipment,
    "photos_to_check": _photos_to_check,
    "camera_positions": _camera_positions,
    "changes": _changes,
    "learn_review": _learn_review,
}


def answer_card(card_id: str, here: Here, audience: Optional[str]) -> dict[str, Any]:
    """One card's answer; KeyError for an unknown card, UnknownAudience for an unknown reader."""
    reader = check_audience(audience)
    if card_id not in _ANSWERS:
        raise KeyError(card_id)
    return _ANSWERS[card_id](here, reader)


# --- For Sage: the same facts as the cards -------------------------------------------------------------------------

def findings(here: Here) -> dict[str, Any]:
    """What a person counted on the inspection, what is still open, and where the photos were taken from: the
    equipment and camera cards' facts, for Sage. Only "at least N" per part: the sum over photos counts a part seen on
    two photos twice, so it is not given (TOWERS_RULES.md). `complete` is the equipment card's "ready": every part
    counted, nothing marked still open, something marked on every photo."""
    inventory = here.inventory
    equipment = _equipment(here, OWNER)
    camera = _camera_positions(here, TECHNICIAN)
    parts = {
        part: {"part": i.noun, "at_least": i.at_least,
               "counted_on_photo": i.at_least_photo.filename if i.at_least_photo else None,
               "photos_counted": i.photos_counted, "photos_marked": i.photos_marked,
               "suggestions_waiting": i.waiting, "photos_with_suggestions_waiting": i.photos_waiting,
               "photos_to_confirm": i.photos_to_confirm,
               "photos_marked_not_found": i.photos_not_found, "photos_being_found": i.photos_finding}
        for part, i in inventory.items()
    }
    return {
        "inspection": here.inspection.name,
        "photos": len(here.inspection.photos),
        "photos_with_every_marked_part_counted": tower_inspection.photos_with_marked_parts_counted(here.inspection),
        "photos_with_nothing_marked": tower_inspection.photos_unmarked(here.inspection),
        "counted": _anything_counted(inventory),
        "complete": _complete(here),
        "equipment": parts,
        "answer": equipment["what"],
        "what_to_do": equipment["todo"],
        "how_sure": equipment["how_sure"]["label"],
        "because": equipment["how_sure"]["because"],
        "camera": {"answer": camera["what"], "facts": camera["facts"]},
        "rule": NEVER_COUNT_YOURSELF,
    }


def photos_needing_review(here: Here) -> dict[str, Any]:
    """The photos still to check, in the technician's order, for Sage."""
    answer = _photos_to_check(here, TECHNICIAN)
    to_check = tower_inspection.photos_to_check(here.inspection)
    return {
        "inspection": here.inspection.name,
        "photos": len(here.inspection.photos),
        "photos_to_check": len(to_check),
        "answer": answer["what"],
        "what_to_do": answer["todo"],
        "first_photos": [{"photo": c.photo.filename, "why": _check_detail(c)} for c in to_check[:LISTED_PHOTOS]],
        "counted": _anything_counted(here.inventory),
        "rule": NEVER_COUNT_YOURSELF,
    }


async def load_here(conn: Any, redis: Any, s3: Any, bucket: str, project_id: str, layer_id: str, name: str,
                    photos: tuple[PhotoFacts, ...]) -> Here:
    """What the cards and Sage answer from: the inspection with the work kept on it in this project, and the same
    site inspected before in this project. `conn` sees what the reader may see."""
    inspection = await tower_inspection.load_inspection(redis, s3, bucket, project_id, layer_id, name, photos)
    return Here(inspection=inspection,
                earlier=await tower_inspection.earlier_inspection(conn, redis, s3, bucket, inspection))
