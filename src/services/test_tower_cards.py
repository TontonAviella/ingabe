"""The tower question cards (src/services/tower_cards.py): each card with and without data, the readers, the answer
shape the drone cards use, the finder's measured numbers read from answer_levels, and Sage's facts never giving a
count nobody checked. The inspections are built in memory with the domain's own counting rule
(test_tower_inspection._work)."""

from __future__ import annotations

import re

import pytest

from src.services import answer_levels, tower_cards, tower_inspection
from src.services.card_status import CANT_TELL, LEARN, NEEDS_FLIGHT, PARTLY, READY, WORKING
from src.services.test_tower_inspection import PHOTOS, _inspection, _work, facts
from src.services.tower_inspection import Earlier

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
            assert answer["status_label"] == tower_cards.STATUS_LABELS[answer["status"]]
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


def _cap(text: str) -> str:
    return text[:1].upper() + text[1:]


def test_equipment_is_at_least_the_most_counted_on_one_photo_never_the_sum():
    answer = tower_cards.answer_card("equipment", here(COUNTED), "owner")
    assert answer["status"] == READY
    assert "Dishes: at least 7, counted by you on DJI_0001.JPG (the most on one photo)" in answer["what"]
    assert "Antennas: at least 5" in answer["what"] and "Radio units: not counted yet." in answer["what"]
    assert "13" not in _numbers(answer["what"])  # 7 + 6 dishes over two photos is not the number on the tower
    facts_ = {f["label"]: f["value"] for f in answer["facts"]}
    assert facts_["Dishes"] == "at least 7"
    assert facts_["Dishes added up over photos"] == "13 on 2 photos (a part seen on two photos counts twice)"
    assert facts_["Photos checked"] == "2 of 5"
    assert [i["title"] for i in answer["items"]] == ["DJI_0001.JPG", "DJI_0001.JPG"]
    assert answer["items"][0]["picture"] == f"/api/layer/L/photos/{'a' * 64}"
    assert answer["how_sure"]["level"] == "medium"

    still_open = tower_cards.answer_card("equipment", here(OPEN), "owner")
    assert still_open["status"] == PARTLY and still_open["how_sure"]["level"] == "low"
    assert "Still open: 4 suggestions waiting on 1 photo." in still_open["what"]
    assert still_open["todo"] == "Ask a technician to check the 4 suggestions waiting before using these numbers."


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
    assert done["items"] == [] and done["what"].startswith("Every photo is checked")


# --- Where were the photos taken from? ------------------------------------------------------------------------------

def test_camera_positions_from_the_photos_own_data():
    answer = tower_cards.answer_card("camera_positions", here(), "technician")
    assert answer["status"] == READY
    assert "5 photos, 5 with a GPS position." in answer["what"]
    assert "Taken from 45 m to 45 m above the take-off point" in answer["what"]
    assert "Facing north 1, east 1, south 1, west 1." in answer["what"]
    assert "1 photo without a direction" in answer["what"]
    assert answer["todo"].startswith("Next visit, also take photos facing north-east, south-east, south-west and "
                                     "north-west")
    assert answer["items"] == []
    no_gps = PHOTOS + [facts(sha="f" * 64, name="phone.jpg", lat=None, lon=None, pitch=None, gimbal_yaw=None,
                             relative_altitude_m=None)]
    listed = tower_cards.answer_card("camera_positions", here(photos=no_gps), "owner")
    assert "No GPS position: phone.jpg." in listed["what"]
    assert listed["items"][0]["title"] == "phone.jpg" and listed["items"][0]["lon"] is None
    bare = [facts(sha="e" * 64, name="plain.jpg", pitch=None, gimbal_yaw=None, relative_altitude_m=None)]
    plain = tower_cards.answer_card("camera_positions", here(photos=bare), "owner")
    assert "above the take-off" not in plain["what"] and plain["todo"].startswith("These photos carry no DJI")


# --- What changed since the last inspection? ------------------------------------------------------------------------

def test_changes_need_an_earlier_inspection_with_counts():
    alone = tower_cards.answer_card("changes", here(COUNTED), "owner")
    assert alone["status"] == NEEDS_FLIGHT and alone["status_label"] == "Needs a second inspection"
    assert "only inspection of this site" in alone["what"] and "at least" not in alone["what"]
    uncounted = tower_cards.answer_card("changes", here(COUNTED, Earlier(same_site=2, inspection=None)), "owner")
    assert uncounted["what"].startswith("2 earlier inspections of this site, but nothing is counted on them")

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
                                "compared: counted on only one of the two).")
    assert [i["title"] for i in compared["items"]] == ["OLD_0001.JPG", "DJI_0001.JPG", "DJI_0001.JPG"]


# --- Why does a person check every suggestion? ----------------------------------------------------------------------

def test_the_learn_card_reads_what_the_finder_was_measured_to_do():
    answer = tower_cards.answer_card("learn_review", here(), "owner")
    assert answer["status"] == LEARN
    for part in tower_inspection.PARTS:
        m = answer_levels.target(part).measured
        assert f"{m['value']:.3f} on {m['n_images']} public test photos" in answer["what"]  # read, never copied
    assert "0.745 on 20 public test photos" in answer["what"]  # dishes, as answer_levels' table says today
    assert not any("your checks" in f["label"] for f in answer["facts"])
    mine = tower_cards.answer_card("learn_review", here(OPEN), "technician")
    yours = {f["label"]: f["value"] for f in mine["facts"]}
    assert yours["Dishes, your checks"] == ("on your photos, 7 of 7 suggestions checked were right (kept or fixed); "
                                            "1 it missed")


# --- For Sage -------------------------------------------------------------------------------------------------------

def test_sage_gets_no_count_before_a_person_checked_one():
    nothing = tower_cards.findings(here())
    assert nothing["counted"] is False and nothing["answer"] == tower_cards.NOTHING_COUNTED
    assert all(p["at_least"] is None and p["added_up_over_photos"] is None for p in nothing["equipment"].values())
    assert nothing["rule"] == tower_cards.NEVER_COUNT_YOURSELF and "mark 3" in nothing["what_to_do"]
    waiting = tower_cards.findings(here({"c" * 64: parts(tower_dish=dict(marks=3, suggestions=5))}))
    assert waiting["counted"] is False and waiting["equipment"][DISH]["suggestions_waiting"] == 5
    assert waiting["equipment"][DISH]["at_least"] is None  # 3 marks and 5 suggestions are not a count

    counted = tower_cards.findings(here(COUNTED))
    assert counted["counted"] is True and counted["equipment"][DISH]["at_least"] == 7
    assert counted["equipment"][DISH]["counted_on_photo"] == "DJI_0001.JPG"
    assert counted["equipment"][RADIO]["at_least"] is None
    assert counted["camera"]["facts"][0] == {"label": "Photos", "value": "5"}

    review = tower_cards.photos_needing_review(here(OPEN))
    assert review["photos_to_check"] == 3 and review["first_photos"][0] == {
        "photo": "DJI_0003.JPG", "why": "4 suggestions waiting for you"}
