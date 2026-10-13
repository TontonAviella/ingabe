"""The tower question cards (src/services/tower_cards.py): each card with and without data, the readers, the answer
shape the drone cards use, the finder's measured numbers read from answer_levels, and Sage's facts never giving a
count nobody checked. The inspections are built in memory with the domain's own counting rule
(test_tower_inspection._work)."""

from __future__ import annotations

import json
import re
from dataclasses import replace

import pytest

from src.services import answer_levels, example_finder, tower_cards, tower_inspection
from src.services.card_status import CANT_TELL, LEARN, NEEDS_FLIGHT, PARTLY, READY, WORKING
from src.services.test_tower_inspection import PHOTOS, _inspection, _work, facts
from src.services.tower_inspection import Earlier
from test_fixtures.wording import COLLECTION_INSTRUCTIONS

DISH, ANTENNA, RADIO = tower_inspection.PARTS
NO_EARLIER = Earlier(same_site=0, inspection=None)
# The keys of a drone card's answer (drone_cards._answer and answer_card), which the screen renders.
DRONE_ANSWER_KEYS = {"id", "question", "preview", "status", "status_label", "what", "why", "todo", "how_sure",
                     "overlay", "facts", "terms", "items", "downloads", "progress", "choices", "upload", "field_check",
                     "ask_sage"}


def parts(**kw):
    return {p: _work(p, **kw.get(p, {})) for p in tower_inspection.PARTS}


def here(work=None, earlier=NO_EARLIER, photos=PHOTOS) -> tower_cards.Here:
    return tower_cards.Here(inspection=_inspection(photos, work or {}), earlier=earlier)


COUNTED = {
    "a" * 64: parts(tower_dish=dict(marks=3, suggestions=4, reviewed=4),  # 7 dishes
                    tower_antenna=dict(marks=3, suggestions=2, reviewed=2)),  # 5 antennas
    "b" * 64: parts(tower_dish=dict(marks=3, suggestions=2, reviewed=2, added=1)),  # 6 dishes
}
OPEN = {**COUNTED, "c" * 64: parts(tower_dish=dict(marks=3, suggestions=5, reviewed=1))}  # 4 waiting
# Every part counted on every photo, nothing open: the only state the equipment answer is "ready" in.
EVERY_PART = dict(marks=3, suggestions=1, reviewed=1)
COMPLETE = {p.sha256: parts(tower_dish=EVERY_PART, tower_antenna=EVERY_PART, tower_radio_unit=EVERY_PART)
            for p in PHOTOS}


def _numbers(text: str) -> set[str]:
    return set(re.findall(r"\d+", text))


# --- The deck -------------------------------------------------------------------------------------------------------

@pytest.mark.parametrize("audience, first", [("owner", "equipment"), ("technician", "photos_to_check"),
                                             (None, "equipment")])
def test_the_deck_puts_each_readers_question_first_and_the_learning_card_last(audience, first):
    for state in (here(), here(OPEN)):
        cards = tower_cards.deck(state, audience)["cards"]
        assert [c["id"] for c in cards][0] == first and cards[-1]["id"] == "learn_review"
        assert all(set(c) == {"id", "title", "status", "preview"} for c in cards)
        assert {c["id"] for c in cards} == set(tower_cards.CARDS)
    with pytest.raises(tower_cards.UnknownAudience):
        tower_cards.deck(here(), "farmer")


def test_every_answer_has_the_drone_answer_shape():
    for card_id in tower_cards.CARDS:
        for state in (here(), here(OPEN)):
            answer = tower_cards.answer_card(card_id, state, "owner")
            assert DRONE_ANSWER_KEYS <= set(answer) and answer["title"] == tower_cards.CARDS[card_id]
            upcoming = (tower_cards.DEFAULT_ASSET.upcoming or {}).get(card_id)
            assert answer["status_label"] == (upcoming.label if upcoming else tower_cards.STATUS_LABELS[answer["status"]])
            assert set(answer["how_sure"]) == {"level", "label", "bars", "because", "surer"}
    with pytest.raises(KeyError):
        tower_cards.answer_card("no_such_card", here(), "owner")


# --- What equipment is on this tower? -------------------------------------------------------------------------------

def test_equipment_without_a_checked_photo_gives_no_number():
    answer = tower_cards.answer_card("equipment", here(), "owner")
    assert answer["status"] == CANT_TELL and answer["what"] == tower_cards.NOTHING_COUNTED
    assert "at least" not in answer["what"] and answer["items"] == []
    assert {f["label"]: f["value"] for f in answer["facts"]}[_cap("dishes")] == "not counted yet"

    waiting = {"c" * 64: parts(tower_dish=dict(marks=3, suggestions=5, reviewed=1))}
    pending = tower_cards.answer_card("equipment", here(waiting), "technician")
    assert pending["status"] == CANT_TELL and "at least" not in pending["what"]
    assert pending["what"].startswith("Nothing is counted yet: 4 suggestions wait for a person")
    assert pending["todo"] == "Open DJI_0003.JPG first: 4 suggestions wait for you."
    finding = {"d" * 64: parts(tower_radio_unit=dict(marks=3, done=False))}
    working = tower_cards.answer_card("equipment", here(finding), "owner")
    assert working["status"] == WORKING and working["progress"] == {"done": 0, "parts": 1, "minutes_left": None}


def _marked_never_found(part):
    return {p: replace(_work(p), marks=((0.0, 0.0, 60.0, 60.0),) * 3) if p == part else _work(p)
            for p in tower_inspection.PARTS}


@pytest.mark.parametrize("state, technician_todo", [
    # The finder working, a finder that found no more (the review saving the missed ones is due), marks never found.
    ({"d" * 64: parts(tower_radio_unit=dict(marks=3, done=False))},
     "Next, DJI_0004.JPG: the finder is working on radio units."),
    ({"a" * 64: parts(tower_dish=dict(marks=3, suggestions=0))},
     "Next, DJI_0001.JPG: the finder found no more dishes: add any it missed, then save."),
    ({"b" * 64: _marked_never_found(ANTENNA)}, "Next, DJI_0002.JPG: antennas marked; press Find."),
])
def test_equipment_with_marked_photos_open_says_what_is_open_not_how_to_start(state, technician_todo):
    """Started work is never answered with "mark a few to start": the answer says what is open and what comes next."""
    for audience in ("owner", "technician"):
        answer = tower_cards.answer_card("equipment", here(state), audience)
        assert answer["what"] != tower_cards.NOTHING_COUNTED and "mark a few" not in answer["todo"].lower()
        assert answer["what"].startswith("Nothing is counted yet: the marked photos are still being checked.")
        assert "Still open:" in answer["what"] and "at least" not in answer["what"]
        assert "Some marked photos are still being checked: they add nothing yet" in answer["how_sure"]["because"]
    assert tower_cards.answer_card("equipment", here(state), "technician")["todo"] == technician_todo
    assert tower_cards.answer_card("equipment", here(state), "owner")["todo"] == (
        "Ask a technician to finish the 1 marked photo still open: their counts appear once every suggestion on "
        "them is checked.")
    preview = tower_cards.deck(here(state), "owner")["cards"][0]
    assert preview["id"] == "equipment" and "mark a few" not in preview["preview"]
    sage = tower_cards.findings(here(state))
    assert sage["answer"] != tower_cards.NOTHING_COUNTED and "mark a few" not in sage["what_to_do"].lower()


def _cap(text: str) -> str:
    return text[:1].upper() + text[1:]


def test_equipment_is_at_least_the_most_counted_on_one_photo_never_the_sum():
    answer = tower_cards.answer_card("equipment", here(COUNTED), "owner")
    assert "Dishes: at least 7, counted by you on DJI_0001.JPG (the most on one photo)" in answer["what"]
    assert "Antennas: at least 5" in answer["what"] and "Radio units: not counted yet." in answer["what"]
    assert "13" not in _numbers(answer["what"])  # 7 + 6 dishes over two photos is not the number on the tower
    facts_ = {f["label"]: f["value"] for f in answer["facts"]}
    assert facts_["Dishes"] == "at least 7"
    assert facts_["Dishes added up over photos"] == "13 on 2 photos (a part seen on two photos counts twice)"
    # What it measures: a photo with only dishes counted is not checked for antennas or radio units.
    assert facts_["Photos with every marked part counted"] == "2 of 5" and "Photos checked" not in facts_
    assert facts_["Photos with nothing marked"] == "3 of 5"
    assert [i["title"] for i in answer["items"]] == ["DJI_0001.JPG", "DJI_0001.JPG"]
    assert answer["items"][0]["picture"] == f"/api/layer/L/photos/{'a' * 64}"

    still_open = tower_cards.answer_card("equipment", here(OPEN), "owner")
    assert still_open["status"] == PARTLY and still_open["how_sure"]["level"] == "low"
    assert "Still open: 4 suggestions waiting on 1 photo." in still_open["what"]
    assert still_open["todo"] == "Ask a technician to check the 4 suggestions waiting before using these numbers."


def test_equipment_is_ready_only_when_every_part_and_every_photo_is_covered():
    """A part nobody counted, or a photo nobody marked anything on, leaves the answer partly ready and not sure,
    and says which."""
    some = tower_cards.answer_card("equipment", here(COUNTED), "owner")  # 2 of 5 photos; radio units never counted
    assert some["status"] == PARTLY and some["status_label"] == "Partly ready"
    assert some["how_sure"]["level"] == "low"
    assert "Radio units not counted on any photo yet" in some["how_sure"]["because"]
    assert "Nothing marked on 3 photos of 5: nobody has looked for parts there" in some["how_sure"]["because"]
    assert some["what"].endswith("Nothing is marked yet on 3 photos of 5: parts seen only there are not counted.")
    assert some["todo"] == "Ask a technician to count the radio units too: until then this answer leaves them out."
    technician = tower_cards.answer_card("equipment", here(COUNTED), "technician")
    assert technician["todo"] == ("Mark a few radio units on a photo where they are clear, press Find, then keep, fix "
                                  "or reject each suggestion and add any it missed.")
    deck = {c["id"]: c for c in tower_cards.deck(here(COUNTED), "owner")["cards"]}
    assert deck["equipment"]["status"] == PARTLY
    assert deck["equipment"]["preview"].endswith("Radio units not counted yet. Nothing marked yet on 3 photos.")

    every_part_some_photos = {sha: work for sha, work in COMPLETE.items() if sha != "e" * 64}
    unopened = tower_cards.answer_card("equipment", here(every_part_some_photos), "owner")
    assert unopened["status"] == PARTLY and unopened["how_sure"]["level"] == "low"
    assert unopened["todo"] == "Ask a technician to look at the 1 photo with nothing marked: it may show more."

    ready = tower_cards.answer_card("equipment", here(COMPLETE), "owner")
    assert ready["status"] == READY and ready["how_sure"]["level"] == "medium"
    assert not any(b.startswith(("Some marked", "Nothing marked")) or "not counted" in b
                   for b in ready["how_sure"]["because"])
    assert ready["todo"] == "Use these as the least on the tower; a photo from another side may show more."
    assert tower_cards.findings(here(COMPLETE))["complete"] is True
    assert tower_cards.findings(here(COUNTED))["complete"] is False


def test_the_answer_level_shows_only_with_the_ladder_on(monkeypatch):
    monkeypatch.setenv("ANSWER_LADDER", "0")
    assert not any("Count after checking" in b for b in
                   tower_cards.answer_card("equipment", here(COUNTED), "owner")["how_sure"]["because"])
    monkeypatch.setenv("ANSWER_LADDER", "1")
    level = answer_levels.Level.FULL_REVIEW
    because = tower_cards.answer_card("equipment", here(COUNTED), "owner")["how_sure"]["because"]
    assert because[0] == f"{level.label}: {level.meaning}"  # answer_levels' words, not this module's
    learn = tower_cards.answer_card("learn_review", here(), "owner")["how_sure"]["because"]
    assert learn[0] == f"{level.label}: {level.meaning}"


# --- Which photos still need checking? ------------------------------------------------------------------------------

def test_photos_to_check_with_and_without_work():
    untouched = tower_cards.answer_card("photos_to_check", here(), "technician")
    assert untouched["status"] == READY and [i["title"] for i in untouched["items"]] == [p.filename for p in PHOTOS]
    assert untouched["todo"] == "Start with DJI_0001.JPG: nothing marked yet."
    worked = tower_cards.answer_card("photos_to_check", here(OPEN), "technician")
    assert [i["title"] for i in worked["items"]] == ["DJI_0003.JPG", "DJI_0004.JPG", "DJI_0005.JPG"]
    assert worked["items"][0]["detail"] == "4 suggestions waiting for you"
    assert worked["what"].startswith("3 photos of 5 still need checking: 1 photo with 4 suggestions waiting and "
                                     "2 photos with nothing marked.")
    everything = {p.sha256: parts(tower_dish=dict(marks=3, suggestions=1, reviewed=1)) for p in PHOTOS}
    done = tower_cards.answer_card("photos_to_check", here(everything), "owner")
    assert done["items"] == [] and done["what"].startswith(
        "Every photo has something marked and every part marked on it is counted.")


# --- Where were the photos taken from? ------------------------------------------------------------------------------

def test_camera_positions_from_the_photos_own_data():
    answer = tower_cards.answer_card("camera_positions", here(), "technician")
    assert answer["status"] == READY
    assert "5 photos, 5 with a GPS position." in answer["what"]
    assert "Taken from 45 m to 45 m above the take-off point" in answer["what"]
    assert "Facing north 1, east 1, south 1, west 1." in answer["what"]
    assert "1 photo without a direction" in answer["what"]
    assert "no photo of this inspection faces north-east, south-east, south-west and north-west" in answer["todo"]
    assert answer["items"] == []
    no_gps = PHOTOS + [facts(sha="f" * 64, name="phone.jpg", lat=None, lon=None, pitch=None, gimbal_yaw=None,
                             relative_altitude_m=None)]
    listed = tower_cards.answer_card("camera_positions", here(photos=no_gps), "owner")
    assert "No GPS position: phone.jpg." in listed["what"]
    assert listed["items"][0]["title"] == "phone.jpg" and listed["items"][0]["lon"] is None
    bare = [facts(sha="e" * 64, name="plain.jpg", pitch=None, gimbal_yaw=None, relative_altitude_m=None)]
    plain = tower_cards.answer_card("camera_positions", here(photos=bare), "owner")
    assert "above the take-off" not in plain["what"] and plain["todo"].startswith("These photos carry no camera tilt or direction")


# --- What changed since the last inspection? ------------------------------------------------------------------------

def test_changes_need_an_earlier_inspection_with_counts():
    alone = tower_cards.answer_card("changes", here(COUNTED), "owner")
    assert alone["status"] == NEEDS_FLIGHT and alone["status_label"] == "Needs a second inspection"
    assert "only inspection of this site" in alone["what"] and "at least" not in alone["what"]
    # An earlier inspection of the site exists, with nothing counted on it: counting there answers it, not a new one.
    uncounted = tower_cards.answer_card("changes", here(COUNTED, Earlier(same_site=2, inspection=None)), "owner")
    assert uncounted["status"] == CANT_TELL and uncounted["status_label"] == "Can't tell yet"
    assert uncounted["what"].startswith("2 earlier inspections of this site, but nothing is counted on them")
    assert uncounted["todo"] == "Count parts on the earlier inspection too, then come back here."
    one_before = here(COUNTED, Earlier(same_site=1, inspection=None))
    card = {c["id"]: c for c in tower_cards.deck(one_before, "owner")["cards"]}
    assert card["changes"]["status"] == CANT_TELL
    assert card["changes"]["preview"] == "1 earlier inspection of this site, nothing counted on it yet."

    before_photos = [facts(sha="9" * 64, name="OLD_0001.JPG", taken="2026-04-01T10:00:00")]
    before = tower_inspection.Inspection(project_id="P", layer_id="L0", name="Rebero April", photos=tuple(before_photos),
                                         work={"9" * 64: parts(tower_dish=dict(marks=3, suggestions=1, reviewed=1))})
    earlier = Earlier(same_site=1, inspection=before)
    nothing_now = tower_cards.answer_card("changes", here({}, earlier), "owner")
    assert nothing_now["status"] == CANT_TELL
    compared = tower_cards.answer_card("changes", here(COUNTED, earlier), "owner")
    assert compared["status"] == READY
    assert compared["what"] == ("Compared with Rebero April (taken 2026-04-01): Dishes: at least 4 then, at least 7 "
                                "now (3 more counted now). Antennas: not counted then, at least 5 now (cannot be "
                                "compared: counted on only one of the two). "
                                + tower_cards.ASSETS["telecom"].changes_gaps)
    assert [i["title"] for i in compared["items"]] == ["OLD_0001.JPG", "DJI_0001.JPG", "DJI_0001.JPG"]


# --- Why does a person check every suggestion? ----------------------------------------------------------------------

def test_the_learn_card_reads_what_the_finder_was_measured_to_do():
    answer = tower_cards.answer_card("learn_review", here(), "owner")
    assert answer["status"] == LEARN
    for part in tower_inspection.PARTS + tower_inspection.DEFECT_PARTS:  # as the finder that runs was measured
        m = answer_levels.target_for(part, example_finder.settings(part).model).measured
        assert f"{m['value']:.3f} on {m['n_images']} public test photos" in answer["what"]  # read, never copied
    assert "0.889 on 60 public test photos" in answer["what"]  # dishes, by our own model, as the table says today
    assert "a model trained on this part (RF-DETR" in answer["what"] and "YOLOE" not in answer["what"]
    assert not any("your checks" in f["label"] for f in answer["facts"])
    mine = tower_cards.answer_card("learn_review", here(OPEN), "technician")
    yours = {f["label"]: f["value"] for f in mine["facts"]}
    assert yours["Dishes, your checks"] == ("on your photos, 7 of 7 suggestions checked were right (kept or fixed); "
                                            "1 it missed")


# --- For Sage -------------------------------------------------------------------------------------------------------

def test_sage_gets_no_count_before_a_person_checked_one():
    nothing = tower_cards.findings(here())
    assert nothing["counted"] is False and nothing["answer"] == tower_cards.NOTHING_COUNTED
    assert all(p["at_least"] is None for p in nothing["equipment"].values())
    assert nothing["rule"] == tower_cards.NEVER_COUNT_YOURSELF and "mark a few" in nothing["what_to_do"]
    waiting = tower_cards.findings(here({"c" * 64: parts(tower_dish=dict(marks=3, suggestions=5))}))
    assert waiting["counted"] is False and waiting["equipment"][DISH]["suggestions_waiting"] == 5
    assert waiting["equipment"][DISH]["at_least"] is None  # 3 marks and 5 suggestions are not a count

    counted = tower_cards.findings(here(COUNTED))
    assert counted["counted"] is True and counted["equipment"][DISH]["at_least"] == 7
    assert counted["equipment"][DISH]["counted_on_photo"] == "DJI_0001.JPG"
    assert counted["equipment"][RADIO]["at_least"] is None
    assert counted["camera"]["facts"][0] == {"label": "Photos", "value": "5"}

    assert counted["photos_with_every_marked_part_counted"] == 2 and counted["photos_with_nothing_marked"] == 3

    review = tower_cards.photos_needing_review(here(OPEN))
    assert review["photos_to_check"] == 3 and review["first_photos"][0] == {
        "photo": "DJI_0003.JPG", "why": "4 suggestions waiting for you"}


def test_sage_never_gets_the_sum_over_photos():
    """7 and 6 dishes on two photos add up to 13, which counts the dishes seen on both twice: Sage gets "at least 7"
    and never the 13, so it cannot give the sum as the number on the tower (TOWERS_RULES.md)."""
    counted = tower_cards.findings(here(COUNTED))
    assert all(set(p) == {"part", "at_least", "counted_on_photo", "photos_counted", "photos_marked",
                          "suggestions_waiting", "photos_with_suggestions_waiting", "photos_to_confirm",
                          "photos_marked_not_found", "photos_being_found"} for p in counted["equipment"].values())
    shown = json.dumps({k: v for k, v in counted.items() if k != "camera"})
    assert "13" not in _numbers(shown) and "added up" not in shown


def test_no_card_tells_anyone_how_or_when_to_fly():
    """Roger (2026-10-11): the app works with whatever data arrives and says in data terms what it supports; it never
    gives flight or data-collection instructions (feedback memory: data, not collection)."""
    banned = COLLECTION_INSTRUCTIONS
    no_gps = PHOTOS + [facts(sha="f" * 64, name="phone.jpg", lat=None, lon=None, pitch=None, gimbal_yaw=None,
                             relative_altitude_m=None)]
    for photos in (PHOTOS, no_gps):
        for audience in ("owner", "technician"):
            for card in tower_cards.deck(here(photos=photos), audience)["cards"]:
                text = json.dumps(tower_cards.answer_card(card["id"], here(photos=photos), audience))
                assert not banned.search(text), (card["id"], banned.search(text).group(0))


# --- Defects, and each industry's own questions -------------------------------------------------------------------

def _with(part_sets, **kw):
    """A photo's work for these parts (an industry's), with the work given per part."""
    return {p: _work(p, **kw.get(p, {})) for p in part_sets}


TELECOM_PARTS = tower_inspection.parts_for("telecom")
POWER = tower_cards.ASSETS["power_grid"]
BOLTS = {"a" * 64: _with(TELECOM_PARTS, tower_bolt_missing=dict(marks=1, suggestions=2, reviewed=2))}  # 3 missing


def test_counted_bolts_are_listed_with_their_photo_and_the_owner_is_told_what_to_do():
    answer = tower_cards.answer_card("defects", here(BOLTS), "owner")
    assert answer["status"] == READY and answer["question"] == "Is the tower structurally safe?"
    assert "Rust, cracked welds, bent or broken steel" in answer["what"]  # what is not measured, said plainly
    assert answer["what"].startswith("Missing bolts: at least 3 on one photo (DJI_0001.JPG), counted on 1 photo.")
    assert [i["title"] for i in answer["items"]] == ["DJI_0001.JPG"]
    assert answer["items"][0]["detail"] == "3 missing bolts counted by you"
    assert answer["todo"] == tower_cards.ASSETS["telecom"].defect_todo
    assert "Loose bolts" in {f["label"] for f in answer["facts"]}  # none counted, never "none there"


def test_no_defect_marked_never_reads_as_no_defects():
    answer = tower_cards.answer_card("defects", here(), "owner")
    assert answer["status"] == CANT_TELL
    assert "does not mean there are none" in answer["what"]
    sage = tower_cards.findings(here())
    assert "does not mean there are none" in sage["defects"]["answer"] and sage["defects"]["photos"] == []
    assert "never say there are none" in sage["rule"]


def test_power_lines_ask_about_the_line_and_its_nests_never_tower_equipment():
    work = _with(tower_inspection.POWER_PARTS, power_nest=dict(marks=1, suggestions=1, reviewed=1),
                 power_pylon=dict(marks=1, suggestions=0))
    # The helper's 60 px marks are smaller than the nests were measured on (the size gate, tested in answer_levels),
    # so the counts a person reached are set here.
    work["power_nest"] = replace(work["power_nest"], counted_by_you=2)
    work["power_pylon"] = replace(work["power_pylon"], counted_by_you=1, review_saved=True)
    nests = {"a" * 64: work}
    power = tower_cards.Here(inspection=_inspection(PHOTOS, nests), earlier=NO_EARLIER, asset=POWER)
    deck = {c["id"]: c for c in tower_cards.deck(power, "owner")["cards"]}
    assert deck["equipment"]["title"] == "What is on this line?"
    assert deck["defects"]["title"] == "Are there bird nests on the line?" and deck["defects"]["status"] == READY
    equipment = tower_cards.answer_card("equipment", power, "owner")
    assert "Pylons" in equipment["what"] and "ishes" not in equipment["what"]  # no dishes, antennas or radio units
    nest = tower_cards.answer_card("defects", power, "owner")
    assert nest["what"].startswith("Bird nests: at least 2 on one photo (DJI_0001.JPG)")
    assert nest["todo"] == POWER.defect_todo and "insulator" in nest["why"]
    learn = tower_cards.answer_card("learn_review", power, "owner")
    assert "public line photos" in learn["what"] and "0.146" in learn["what"]  # pylons, as measured at new sites
    with pytest.raises(KeyError):
        tower_cards.answer_card("camera", power, "owner")


def test_every_worry_is_a_card_and_the_ones_not_measured_say_so_without_a_number():
    """The deck asks what a tower owner worries about. A question nothing measures yet still shows, with its own
    status (coming soon, a thermal camera, a night flight), what it will tell and what it needs, and no number."""
    deck = {c["id"]: c for c in tower_cards.deck(here(), "owner")["cards"]}
    assert {"defects", "changes", "alignment", "equipment", "damage", "hot_spots", "obstruction_light",
            "surroundings"} <= set(deck)
    assert deck["hot_spots"]["status"] == "needs_camera" and deck["alignment"]["status"] == "to_build"
    light = tower_cards.answer_card("obstruction_light", here(), "owner")
    assert light["status_label"] == "Needs night photos" and light["what"].startswith("Not answered yet.")
    assert light["todo"].startswith("What it needs: ") and light["how_sure"]["level"] == "low"
    for card_id in ("alignment", "damage", "hot_spots", "obstruction_light"):
        answer = tower_cards.answer_card(card_id, here(COUNTED), "owner")
        assert not re.search(r"\d", answer["what"]), (card_id, answer["what"])  # no number for what is not measured
    ranked = [c["id"] for c in tower_cards.deck(here(BOLTS), "owner")["cards"]]
    assert ranked.index("defects") < ranked.index("alignment")  # answers before what is coming
    power = {c["id"] for c in tower_cards.deck(tower_cards.Here(inspection=_inspection(PHOTOS, {}),
                                                                 earlier=NO_EARLIER, asset=POWER), "owner")["cards"]}
    assert {"right_of_way", "insulators", "sag", "steel", "hot_spots"} <= power and "alignment" not in power


def test_the_surroundings_card_reads_what_sam3_and_the_green_rule_found_on_photos_looking_straight_down(monkeypatch):
    """Trees (SAM 3) and other vegetation (green pixels outside them), as shares of each photo looking straight down,
    with the measurement behind them; nothing without such photos, and nothing invented while they are analysed."""
    from src.services import site_surroundings

    down = [replace(p, gimbal_pitch=-90.0) for p in PHOTOS[:2]] + list(PHOTOS[2:])
    none = tower_cards.answer_card("surroundings", here(), "owner")
    assert none["status"] == CANT_TELL and "No photo of this inspection looks straight down" in none["what"]
    monkeypatch.setattr(site_surroundings, "enabled", lambda: True)
    waiting = tower_cards.Here(inspection=_inspection(down, {}), earlier=NO_EARLIER, surroundings={})
    assert tower_cards.answer_card("surroundings", waiting, "owner")["status"] == WORKING
    results = {down[0].sha256: {"method": site_surroundings.METHOD, "trees": 0.3, "other_vegetation": 0.2,
                                "trees_found": 4},
               down[1].sha256: {"method": site_surroundings.METHOD, "trees": 0.1, "other_vegetation": 0.4,
                                "trees_found": 1}}
    done = tower_cards.Here(inspection=_inspection(down, {}), earlier=NO_EARLIER, surroundings=results)
    answer = tower_cards.answer_card("surroundings", done, "owner")
    assert answer["status"] == READY and answer["status_label"] == "Answer ready"
    assert answer["what"].startswith("On 2 photos looking straight down, trees cover 20% of the ground in view and "
                                     "other vegetation (grass, bush, crops) 30%.")
    assert [i["title"] for i in answer["items"]] == [down[0].filename, down[1].filename]  # most trees first
    assert "Buildings, erosion and the access road are not measured yet." in answer["what"]
    assert any("0.62" in b for b in answer["how_sure"]["because"])  # the measurement, read from site_surroundings


def test_the_conductors_card_lists_the_photos_sam3_outlined_conductors_on(monkeypatch):
    """Line checks: where SAM 3 ("power line") outlined conductors, photo by photo, with the measurement behind it;
    nothing invented while the photos are looked at."""
    from src.services import line_conductors

    monkeypatch.setattr(line_conductors, "enabled", lambda: True)
    waiting = tower_cards.Here(inspection=_inspection(PHOTOS, {}), earlier=NO_EARLIER, asset=POWER, conductors={})
    assert "conductors" in {c["id"] for c in tower_cards.deck(waiting, "owner")["cards"]}
    assert tower_cards.answer_card("conductors", waiting, "owner")["status"] == WORKING
    two = [[0, 0], [10, 0], [10, 2]]
    results = {PHOTOS[0].sha256: {"method": line_conductors.METHOD, "outlines": [two]},
               PHOTOS[1].sha256: {"method": line_conductors.METHOD, "outlines": [two, two]},
               PHOTOS[2].sha256: {"method": line_conductors.METHOD, "outlines": []}}
    done = tower_cards.Here(inspection=_inspection(PHOTOS[:3], {}), earlier=NO_EARLIER, asset=POWER,
                            conductors=results)
    answer = tower_cards.answer_card("conductors", done, "owner")
    assert answer["status"] == READY
    assert answer["what"].startswith("SAM 3 outlined conductors on 2 of the 3 photos looked at; 1 photo shows none.")
    assert [i["title"] for i in answer["items"]] == [PHOTOS[1].filename, PHOTOS[0].filename]  # most outlines first
    assert "Sag, clearance to the ground" in answer["what"]
    assert any("97%" in b and "83%" in b for b in answer["how_sure"]["because"])
    telecom = {c["id"] for c in tower_cards.deck(here(), "owner")["cards"]}
    assert "conductors" not in telecom
