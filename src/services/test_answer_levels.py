"""answer_levels: the measured target table and its evidence, the size gate, countability and the level each answer
gets."""

from __future__ import annotations

import json
import math
import statistics
from datetime import date

import pytest

from src.services import answer_levels as al
from src.services import own_detector
from src.services.answer_levels import Facts, Level, Verdict

FINDER = "yoloe-11s-seg"
PLANTED = date(2026, 9, 15)
TAKEN = date(2026, 10, 10)
OPEN_FIELD = Facts(planting_date=PLANTED, capture_date=TAKEN, green_cover_share=0.3)
TOWER_PARTS = ("tower_dish", "tower_antenna", "tower_radio_unit")
ANSWERABLE = ("tower_dish", "tower_antenna", "tower_radio_unit", "building", "maize_seedling", "row_crop_plant", "weed")
CANT_TELL_KINDS = ("tree_crown_forest", "tree_crown_savanna", "banana_mat")
# Found only by a trained detector: the example finder was never measured on them (CAN'T TELL without it).
POWER_PARTS = ("power_pylon", "power_insulator", "power_nest")
DETECTED = (*TOWER_PARTS, *POWER_PARTS)
DEFECT_PARTS = ("tower_bolt_missing", "tower_bolt_loose")
ONLY_DETECTED = (*POWER_PARTS, *DEFECT_PARTS)  # never measured with the example finder
EVIDENCE = json.loads(al.EVIDENCE_PATH.read_text())


def _big_gate(name: str) -> al.SizeGate:
    """Marks of 100 px on a picture the finder reads at its own size: bigger than every target's floor."""
    return al.size_gate(name, al.target(name).finder_input_px or 640, marked_box_px=[(100, 100)] * 3)


def _answer(name: str, gate: al.SizeGate | None = None, facts: Facts = OPEN_FIELD,
            evidence: al.Evidence | None = None) -> al.Answer:
    gate = gate or _big_gate(name)
    return al.decide(name, FINDER, gate, al.countability(name, facts), evidence)


def _rows(selector: dict) -> list[dict]:
    rows = [r for r in EVIDENCE["scores"] if all(r.get(key) == value for key, value in selector.items())]
    assert rows, selector
    return rows


def _measured_sizes(name: str) -> list[tuple[float, list[list[float]]]]:
    """(picture long side, the 3 marks) for every picture behind the target's size floor."""
    return [(p["long_px"], p["marks_wh"]) for s in json.loads(al.TARGETS_PATH.read_text())["targets"][name]["size_evidence"]
            for p in EVIDENCE["marks"][s]["pictures"]]


# --------------------------------------------------------------------------- #
# Levels
# --------------------------------------------------------------------------- #

def test_levels_run_from_cant_tell_up_to_count():
    assert list(Level) == [Level.CANT_TELL, Level.AREA, Level.ROUGH, Level.ESTIMATE, Level.FULL_REVIEW, Level.COUNT]


@pytest.mark.parametrize("level", list(Level))
def test_every_level_has_a_plain_label_and_meaning(level):
    assert level.label and level.meaning.endswith(".")
    words = f"{level.label} {level.meaning}".lower()
    for jargon in ("ap50", "precision", "recall", "confidence", "model", "detector", "yoloe", "threshold"):
        assert jargon not in words


# --------------------------------------------------------------------------- #
# The target table and its evidence
# --------------------------------------------------------------------------- #

def test_every_target_has_provenance_and_a_max_level():
    assert {t.name for t in al.targets()} == set(ANSWERABLE) | set(CANT_TELL_KINDS) | set(ONLY_DETECTED)
    for t in al.targets():
        assert set(al.PROVENANCE_KEYS) <= set(t.measured), t.name
        assert isinstance(t.max_level, Level) and t.kind in al.KINDS and t.noun, t.name
        assert t.finder_model == FINDER, t.name
        if t.measured["value"] is None:
            assert t.measured["note"] and t.max_level is Level.CANT_TELL, t.name  # nothing measured: can't tell
        else:
            assert t.measured["date"] == "2026-10-11" and t.measured["metric"].startswith("AP50"), t.name
            assert "not in this repository" in t.measured["where"], t.name  # the scripts live outside the repo


@pytest.mark.parametrize("name", [t.name for t in al.targets() if t.measured["value"] is not None])
def test_the_table_matches_its_evidence(name):
    """Every value is the mean of the rows its evidence selects; picture counts and finder sizes match those rows."""
    t = al.target(name)
    rows = _rows(t.measured["evidence"])
    assert round(statistics.mean(r["ap50"] for r in rows), 3) == t.measured["value"]
    assert {r["n_img"] for r in rows} == {t.measured["n_images"]}
    assert {r["imgsz"] for r in rows} == {t.finder_input_px}
    sizes = _measured_sizes(name)
    if sizes:
        assert max(long_px for long_px, _ in sizes) == t.measured["image_px"]
        main_set = EVIDENCE["marks"][json.loads(al.TARGETS_PATH.read_text())["targets"][name]["size_evidence"][0]]
        assert len(main_set["pictures"]) == t.measured["n_images"]
    for extra in t.also_measured:
        assert round(statistics.mean(r["ap50"] for r in _rows(extra["evidence"])), 3) == extra["value"], extra


@pytest.mark.parametrize("name,value,n_images,image_px,finder_input_px,max_level", [
    ("tower_dish", 0.745, 20, 1333, 1280, Level.FULL_REVIEW),
    ("tower_antenna", 0.695, 15, 1333, 1280, Level.FULL_REVIEW),
    ("tower_radio_unit", 0.291, 14, 1333, 2560, Level.FULL_REVIEW),
    ("building", 0.525, 20, 512, 640, Level.ROUGH),
    ("maize_seedling", 0.787, 20, 640, 1280, Level.ROUGH),
    ("row_crop_plant", 0.533, 20, 585, 640, Level.ROUGH),
    ("weed", 0.447, 20, 585, 896, Level.ROUGH),
    ("tree_crown_forest", 0.216, 20, 400, 640, Level.CANT_TELL),
    ("tree_crown_savanna", 0.059, 41, 400, 640, Level.CANT_TELL),
    ("banana_mat", None, None, None, None, Level.CANT_TELL),
])
def test_the_table_holds_the_measurements_of_2026_10_11(name, value, n_images, image_px, finder_input_px, max_level):
    t = al.target(name)
    assert (t.measured["value"], t.measured["n_images"], t.measured["image_px"], t.finder_input_px, t.max_level) == (
        value, n_images, image_px, finder_input_px, max_level)


def test_the_finder_settings_are_the_measured_ones():
    # Look-alike marks helped antennas (2 of 2 draws) and radio units (4 of 4 runs); not dishes or weeds.
    assert {t.name for t in al.targets() if t.look_alike_marks} == {"tower_antenna", "tower_radio_unit"}
    assert al.target("tower_dish").also_measured[1]["value"] < al.target("tower_dish").also_measured[2]["value"]
    rru = {e["setting"]: e["value"] for e in al.target("tower_radio_unit").also_measured}
    assert rru["read at 1280 px"] < al.target("tower_radio_unit").measured["value"] > rru["read at 3200 px"]
    assert "other pictures" in al.target("tree_crown_savanna").measured["protocol"].lower()


def _filter_entry(name: str) -> dict:
    return json.loads(al.TARGETS_PATH.read_text())["targets"][name]["suggestion_filter"]


@pytest.mark.parametrize("name", [t.name for t in al.targets()])
def test_the_suggestion_filter_is_on_only_where_it_was_measured_not_to_lower_ap50(name):
    """The flag follows its evidence: on only where the filtered AP50 recorded is at least the raw one, off where it
    is lower, and off for a target never measured with the filter. The raw row is the target's own measurement
    (the same pictures, marks and finder size), so the filter was measured on what the finder does for it."""
    t, entry = al.target(name), _filter_entry(name)
    if entry["evidence"] is None:
        assert t.suggestion_filter is False, name
        return
    assert entry["evidence"]["source"] == "results/postfilter.jsonl"
    (raw,) = _rows({**entry["evidence"], "method": "raw"})
    (filtered,) = _rows({**entry["evidence"], "method": "filtered"})
    assert entry["evidence"]["task"] == t.measured["evidence"]["task"] and raw["imgsz"] == t.finder_input_px
    assert raw["ap50"] == t.measured["value"]
    assert t.suggestion_filter is (filtered["ap50"] >= raw["ap50"]), name
    assert f"AP50 {raw['ap50']} without the filter, {filtered['ap50']} with it" in entry["note"]


def test_the_suggestion_filter_is_on_for_antennas_radio_units_and_maize():
    # Measured 2026-10-11: AP50 rose for antennas, radio units and Nerac maize; it fell for dishes and weeds.
    assert {t.name for t in al.targets() if t.suggestion_filter} == {"tower_antenna", "tower_radio_unit",
                                                                       "maize_seedling"}
    assert "Selommes was not measured with the filter" in _filter_entry("maize_seedling")["note"]
    measured = {r["task"] for r in EVIDENCE["scores"] if r["source"] == "results/postfilter.jsonl"}
    assert measured == {"tower_mw", "tower_antenna", "tower_rru", "maize_nerac", "field_weed"}


def test_the_antenna_count_is_the_one_the_run_recorded():
    # Only 15 test photos have 4 or more antennas; the same-picture run recorded n_img 15 itself (not filled in).
    row = _rows({"source": "results/sameimage.jsonl", "task": "tower_antenna", "method": "YOLOE marks"})[0]
    assert row["n_img"] == 15 and "n_img" not in row.get("filled", {})
    assert row["ap50"] == al.target("tower_antenna").measured["value"]


def test_the_weed_data_is_named():
    assert "cotton and weed UAV tiles" in al.target("weed").measured["dataset"]
    assert "cotton and weed UAV tiles" in al.target("row_crop_plant").measured["dataset"]


def test_every_measurement_says_which_way_its_pictures_looked():
    # As the datasets describe them: the tower photos are taken at an angle, every other set looks straight down.
    views = {t.name: t.measured["view"] for t in al.targets()}
    assert views == {"tower_dish": al.OBLIQUE, "tower_antenna": al.OBLIQUE, "tower_radio_unit": al.OBLIQUE,
                     "building": al.NADIR, "maize_seedling": al.NADIR, "row_crop_plant": al.NADIR, "weed": al.NADIR,
                     "tree_crown_forest": al.NADIR, "tree_crown_savanna": al.NADIR, "banana_mat": None,
                     **{name: None for name in ONLY_DETECTED}}  # never measured with the example finder
    assert {name: al.detector(name).measured["view"] for name in (*DETECTED, *DEFECT_PARTS)} == {
        name: al.OBLIQUE for name in (*DETECTED, *DEFECT_PARTS)}
    assert all("tower photos" in al.target(n).measured["dataset"] for n, v in views.items() if v == al.OBLIQUE)


def test_no_size_is_guessed():
    # Nobody measured object sizes on the ground: the gate uses the user's marks instead.
    assert all(t.object_size_m is None and t.object_size_note for t in al.targets())


def test_a_broken_table_fails_on_load(tmp_path):
    data = json.loads(al.TARGETS_PATH.read_text())
    good = tmp_path / "good.json"
    good.write_text(json.dumps(data))
    assert set(al.load_targets(good)) == set(ANSWERABLE) | set(CANT_TELL_KINDS) | set(ONLY_DETECTED)

    def broken(change, match: str) -> None:
        copy = json.loads(json.dumps(data))
        change(copy["targets"])
        path = tmp_path / "broken.json"
        path.write_text(json.dumps(copy))
        with pytest.raises(ValueError, match=match):
            al.load_targets(path)

    broken(lambda d: d["building"]["measured"].pop("where"), "provenance lacks")
    broken(lambda d: d["building"]["measured"].update(value=1.3), "not a share")
    broken(lambda d: d["building"]["measured"].update(date=None), "must say")
    broken(lambda d: d["weed"]["measured"].update(dataset=None), "must say")
    broken(lambda d: d["weed"]["measured"].update(view=None), "must say")
    broken(lambda d: d["weed"]["measured"].update(view="sideways"), "view 'sideways' is not one of")
    broken(lambda d: d["building"]["measured"].update(finder_input_px=None), "must say")
    broken(lambda d: d["building"]["measured"].update(n_images=0), "whole number")
    broken(lambda d: d["banana_mat"]["measured"].update(note=None), "no note")
    broken(lambda d: d["building"].update(kind="vehicle"), "kind")
    broken(lambda d: d["building"].update(object_size_m=0), "not positive")
    broken(lambda d: d["building"].update(object_size_note=None), "object_size_note")
    broken(lambda d: d["tower_radio_unit"]["also_measured"][0].update(value=-0.1), "not a share")
    broken(lambda d: d["building"].update(max_level="PERFECT"), "max level")
    broken(lambda d: d["tree_crown_forest"].update(max_level="AREA"), "area")  # no area answer is measured
    broken(lambda d: d["banana_mat"].update(max_level="ROUGH"), "needs a measured value")
    broken(lambda d: d["tree_crown_forest"]["measured"].update(note=None), "CANT_TELL needs a note")
    broken(lambda d: d["building"]["finder"].update(model=""), "finder")
    broken(lambda d: d["building"].update(size_evidence=[]), "size_evidence")
    broken(lambda d: d["building"].update(size_evidence=["nowhere"]), "no marked pictures")
    broken(lambda d: d["building"].update(size_evidence=["tower_mw"]), "read at 1280 px")
    broken(lambda d: d["weed"].pop("suggestion_filter"), "suggestion_filter must have exactly")
    broken(lambda d: d["weed"]["suggestion_filter"].pop("note"), "suggestion_filter must have exactly")
    broken(lambda d: d["weed"]["suggestion_filter"].update(on=1), "is not true or false")
    broken(lambda d: d["weed"]["suggestion_filter"].update(note=""), "needs a note")
    broken(lambda d: d["building"]["suggestion_filter"].update(on=True), "on without the measurement")


def test_an_unknown_target_is_refused():
    with pytest.raises(ValueError, match="no answer target 'goats'"):
        al.target("goats")
    with pytest.raises(ValueError):
        al.countability("goats", Facts())
    with pytest.raises(ValueError):
        al.size_gate("goats", 640)


# --------------------------------------------------------------------------- #
# Size gate: sizes as the finder reads them
# --------------------------------------------------------------------------- #

def test_the_size_floors_come_from_the_measured_pictures():
    """Each floor is the smallest marked size (median of the 3 marks, as the finder read the picture) among the
    pictures the target was measured on."""
    for name in ANSWERABLE:
        t = al.target(name)
        sizes = [statistics.median(math.sqrt(w * h) for w, h in marks) * t.finder_input_px / long_px
                 for long_px, marks in _measured_sizes(name)]
        assert t.size_floor_px == math.floor(min(sizes)), name
    assert {name: al.target(name).size_floor_px for name in ANSWERABLE} == {
        "tower_dish": 17, "tower_antenna": 33, "tower_radio_unit": 21, "building": 29, "maize_seedling": 34,
        "row_crop_plant": 34, "weed": 31}


@pytest.mark.parametrize("name", ANSWERABLE)
def test_the_gate_never_refuses_the_targets_own_evidence(name):
    """Every picture a target was measured on passes its size gate and reaches its max level."""
    for long_px, marks in _measured_sizes(name):
        gate = al.size_gate(name, long_px, marked_box_px=marks)
        assert gate.passes is True, (name, long_px, marks, gate.reason)
        assert _answer(name, gate).level is al.target(name).max_level


def test_marks_are_read_at_the_finders_size():
    # Selommes maize: plants about 27 px on a 640 px tile, read at 1280 px: about 54 px to the finder (AP50 0.437).
    gate = al.size_gate("maize_seedling", 640, marked_box_px=[(27, 27)] * 3)
    assert gate.passes is True and gate.object_px == 54
    assert gate.reason == ("the marked maize plants are about 54 px wide as the finder reads them (27 px in this "
                           "640 px picture, read at 1280 px); maize plants were measured from 34 px wide")
    # Dishes about 30 px on a 1333 px tower photo read at 1280 px scored 0.745: they pass.
    assert al.size_gate("tower_dish", 1333, marked_box_px=[(30, 30)] * 3).passes is True


def test_a_large_photo_the_finder_shrinks_can_fail():
    # A 4000 px photo read at 1280 px: 40 px dishes are about 12.8 px to the finder, under the 17 px floor.
    gate = al.size_gate("tower_dish", 4000, marked_box_px=[(40, 40)] * 3)
    assert gate.passes is False and gate.object_px == pytest.approx(12.8)
    assert gate.reason == ("the marked dishes are about 12 px wide as the finder reads them (40 px in this 4000 px "
                           "picture, read at 1280 px); dishes were measured only from 17 px wide, so this picture is "
                           "outside the measurement")
    # The same marks on the 1333 px photos the dishes were measured on pass.
    assert al.size_gate("tower_dish", 1333, marked_box_px=[(40, 40)] * 3).passes is True


def test_just_under_the_floor_fails_and_is_never_shown_as_the_floor():
    side = 16.9 * 1333 / 1280  # 16.9 px to the finder, floor 17
    gate = al.size_gate("tower_dish", 1333, marked_box_px=[(side, side)] * 3)
    assert gate.passes is False and "about 16 px wide" in gate.reason
    exact = al.size_gate("tower_dish", 1280, marked_box_px=[(17, 17)] * 3)
    assert exact.passes is True and "about 17 px wide" in exact.reason


def test_a_size_on_the_ground_is_read_at_the_finders_size():
    gate = al.size_gate("building", 512, cm_per_px=8.8, object_size_m=1.0)
    assert gate.passes is False and gate.object_px == pytest.approx(100 / 8.8 * 640 / 512)
    assert gate.reason == ("buildings are about 14 px wide as the finder reads them (1 m at 8.8 cm per pixel is 11 px "
                           "in this 512 px picture, read at 640 px); buildings were measured only from 29 px wide, so "
                           "this picture is outside the measurement")


def test_the_users_marks_win_over_a_size_on_the_ground():
    gate = al.size_gate("building", 640, marked_box_px=[(60, 60), (64, 64), (70, 70)], cm_per_px=8.8,
                        object_size_m=1.0)
    assert gate.object_px == 64 and gate.passes is True and gate.reason.startswith("the marked buildings")


def test_a_marked_box_is_sized_by_its_area():
    assert al.box_px(20, 125) == 50  # a long thin box: the side of a square of the same area
    gate = al.size_gate("building", 640, marked_box_px=[(10, 10), (40, 40), (100, 100)])
    assert gate.object_px == 40  # the median


def test_an_unknown_size_is_unknown_not_small():
    for gate in (al.size_gate("tower_dish", 1333), al.size_gate("tower_dish", 1333, cm_per_px=3.2),
                 al.size_gate("tower_dish", 1333, object_size_m=0.6),
                 al.size_gate("tower_dish", 1333, marked_box_px=[])):
        assert gate.passes is None and gate.object_px is None and gate.needed_px == 17
        assert "not known" in gate.reason and "mark two or three" in gate.reason


def test_a_kind_with_no_measured_size_has_no_floor():
    gate = al.size_gate("banana_mat", 1000, marked_box_px=[(80, 80)] * 3)
    assert gate.passes is None and gate.needed_px is None and "no object size was measured" in gate.reason


@pytest.mark.parametrize("kwargs", [
    {"picture_long_px": 0}, {"picture_long_px": 640, "cm_per_px": 0}, {"picture_long_px": 640, "cm_per_px": -1.0},
    {"picture_long_px": 640, "cm_per_px": 3.2, "object_size_m": 0},
    {"picture_long_px": 640, "marked_box_px": [(0, 10)]}, {"picture_long_px": 640, "marked_box_px": [(10, -5)]},
])
def test_impossible_sizes_are_refused(kwargs):
    with pytest.raises(ValueError):
        al.size_gate("building", **kwargs)


# --------------------------------------------------------------------------- #
# Countability
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize("name", ["maize_seedling", "row_crop_plant"])
def test_annual_crops_in_an_open_field_are_countable(name):
    verdict = al.countability(name, OPEN_FIELD)
    assert verdict.verdict is Verdict.YES and verdict.needs == ()


def test_missing_facts_give_maybe_and_are_listed():
    verdict = al.countability("maize_seedling", Facts())
    assert verdict.verdict is Verdict.MAYBE and verdict.reason_code == "needs_facts"
    assert verdict.needs == ("planting_date", "capture_date", "green_cover_share")
    assert verdict.reason_text == ("to count maize plants this needs the planting date, the date the picture was "
                                   "taken and how much of the ground green leaves cover")
    only_date = al.countability("maize_seedling", Facts(planting_date=PLANTED, green_cover_share=0.3))
    assert only_date.needs == ("capture_date",)
    assert only_date.reason_text == "to count maize plants this needs the date the picture was taken"


def test_closed_leaves_say_no_even_when_dates_are_missing():
    verdict = al.countability("maize_seedling", Facts(green_cover_share=0.8))
    assert verdict.verdict is Verdict.NO and verdict.reason_code == "plants_touch"
    assert "plants touch" in verdict.reason_text and "80%" in verdict.reason_text
    assert al.countability("maize_seedling", Facts(planting_date=PLANTED, capture_date=TAKEN,
                                                   green_cover_share=0.79)).verdict is Verdict.YES


def test_zero_green_cover_is_a_value_not_missing():
    verdict = al.countability("maize_seedling", Facts(planting_date=PLANTED, capture_date=TAKEN, green_cover_share=0.0))
    assert verdict.verdict is Verdict.YES


def test_a_picture_taken_before_planting_has_nothing_to_count():
    verdict = al.countability("maize_seedling", Facts(planting_date=TAKEN, capture_date=PLANTED, green_cover_share=0.1))
    assert verdict.verdict is Verdict.NO and verdict.reason_code == "before_planting"
    assert "25 days before planting" in verdict.reason_text


@pytest.mark.parametrize("share", [-0.1, 1.2, "most"])
def test_an_impossible_green_cover_is_refused(share):
    with pytest.raises(ValueError):
        al.countability("maize_seedling", Facts(green_cover_share=share))


def test_weeds_need_only_the_green_cover():
    assert al.countability("weed", Facts()).needs == ("green_cover_share",)
    assert al.countability("weed", Facts(green_cover_share=0.3)).verdict is Verdict.YES
    assert al.countability("weed", Facts(green_cover_share=0.9)).reason_code == "plants_touch"


@pytest.mark.parametrize("name", [*TOWER_PARTS, "building"])
def test_structures_are_countable_by_kind(name):
    """Size is not a countability fact: decide applies the size gate, once."""
    verdict = al.countability(name, Facts())
    assert verdict.verdict is Verdict.YES and verdict.reason_code == "countable"


@pytest.mark.parametrize("name", CANT_TELL_KINDS)
def test_counting_crowns_is_untested(name):
    verdict = al.countability(name, OPEN_FIELD)
    assert verdict.verdict is Verdict.MAYBE and verdict.reason_code == "count_untested"


# --------------------------------------------------------------------------- #
# Decision
# --------------------------------------------------------------------------- #

def test_cant_tell_when_the_plants_are_too_small():
    # A 4000 px drone photo given whole: 29 px plants are about 9 px to the finder.
    gate = al.size_gate("maize_seedling", 4000, marked_box_px=[(29, 29)] * 3)
    answer = _answer("maize_seedling", gate)
    assert answer.level is Level.CANT_TELL and answer.reason_code == "too_small"
    assert answer.reason_text == ("the marked maize plants are about 9 px wide as the finder reads them (29 px in this "
                                  "4000 px picture, read at 1280 px); maize plants were measured only from 34 px "
                                  "wide, so this picture is outside the measurement")


def test_cant_tell_when_the_size_is_unknown():
    answer = _answer("tower_dish", al.size_gate("tower_dish", 1333), Facts())
    assert answer.level is Level.CANT_TELL and answer.reason_code == "size_unknown"
    assert "mark two or three" in answer.reason_text and "dishes" in answer.reason_text


def test_cant_tell_when_countability_is_no():
    closed = Facts(planting_date=PLANTED, capture_date=TAKEN, green_cover_share=0.95)
    answer = _answer("maize_seedling", facts=closed)
    assert answer.level is Level.CANT_TELL and answer.reason_code == "plants_touch"
    assert "95%" in answer.reason_text


def test_the_size_gate_speaks_first():
    small = al.size_gate("maize_seedling", 4000, marked_box_px=[(29, 29)] * 3)
    assert _answer("maize_seedling", small, Facts(green_cover_share=0.95)).reason_code == "too_small"


@pytest.mark.parametrize("name", TOWER_PARTS)
def test_a_tower_part_needs_only_the_passing_gate(name):
    """The size gate goes in once: a passing gate with no other facts reaches the measured level."""
    answer = _answer(name, facts=Facts())
    assert answer.level is Level.FULL_REVIEW and answer.reason_code == "measured_limit"


@pytest.mark.parametrize("name,level", [
    ("tower_dish", Level.FULL_REVIEW), ("tower_antenna", Level.FULL_REVIEW), ("tower_radio_unit", Level.FULL_REVIEW),
    ("building", Level.ROUGH), ("maize_seedling", Level.ROUGH), ("row_crop_plant", Level.ROUGH), ("weed", Level.ROUGH),
    ("tree_crown_forest", Level.CANT_TELL), ("tree_crown_savanna", Level.CANT_TELL), ("banana_mat", Level.CANT_TELL),
])
def test_with_every_gate_open_the_answer_is_the_measured_max(name, level):
    assert _answer(name).level is level


def test_the_measured_limit_says_where_it_was_measured():
    assert _answer("tower_radio_unit").reason_text == (
        'for radio units the answer stops at "Count after checking" today: measured on 14 pictures on 2026-10-11; '
        "data: celltower test set (celltower_1333, 1333 x 750 px tower photos)")
    assert "measured on 15 pictures" in _answer("tower_antenna").reason_text


def test_missing_facts_say_what_is_needed_and_offer_no_area():
    answer = _answer("maize_seedling", facts=Facts(green_cover_share=0.2))
    assert answer.level is Level.CANT_TELL and answer.reason_code == "needs_facts"
    assert answer.reason_text == "to count maize plants this needs the planting date and the date the picture was taken"


@pytest.mark.parametrize("name", CANT_TELL_KINDS)
def test_tree_crowns_and_banana_mats_cant_be_told_and_say_why(name):
    answer = _answer(name, al.size_gate(name, 400, marked_box_px=[(80, 80)] * 3))
    assert answer.level is Level.CANT_TELL and answer.reason_code == "measured_limit"
    assert ("no answer key" if name == "banana_mat" else "no answer about the area they cover has been measured") in (
        answer.reason_text)


def test_no_answer_is_area_or_count_today():
    """No area answer and no count was measured, so no combination of gates and evidence gives either."""
    gates = {t.name: [_big_gate(t.name), al.size_gate(t.name, 4000, marked_box_px=[(20, 20)] * 3)] for t in al.targets()}
    facts = [Facts(), OPEN_FIELD, Facts(green_cover_share=0.9), Facts(green_cover_share=0.2)]
    for t in al.targets():
        assert t.max_level not in (Level.AREA, Level.COUNT), t.name
        for gate in gates[t.name]:
            for f in facts:
                for evidence in (None, al.Evidence(Level.COUNT, "x"), al.Evidence(Level.ESTIMATE, "y")):
                    assert _answer(t.name, gate, f, evidence).level not in (Level.AREA, Level.COUNT), t.name


def test_evidence_can_lower_the_level_but_never_raise_it():
    lower = _answer("tower_dish", evidence=al.Evidence(Level.ESTIMATE, "half the picture is cloud"))
    assert (lower.level, lower.reason_code, lower.reason_text) == (Level.ESTIMATE, "evidence_limit",
                                                                   "half the picture is cloud")
    higher = _answer("building", evidence=al.Evidence(Level.COUNT, "a checked count"))
    assert higher.level is Level.ROUGH and higher.reason_code == "measured_limit"


def test_evidence_limited_to_the_area_gives_cant_tell():
    """No area answer is measured, so a limit at AREA leaves nothing to offer."""
    answer = _answer("building", evidence=al.Evidence(Level.AREA, "half the picture is cloud"))
    assert (answer.level, answer.reason_code, answer.reason_text) == (Level.CANT_TELL, "evidence_limit",
                                                                      "half the picture is cloud")


def test_another_finders_output_gets_no_level():
    gate = _big_gate("building")
    with pytest.raises(ValueError, match="measured for the yoloe-11s-seg finder, not 'fastsam-s'"):
        al.decide("building", "fastsam-s", gate, al.countability("building", Facts()))


def test_a_size_gate_for_another_target_is_refused():
    with pytest.raises(ValueError, match="size gate was worked out for 'tower_dish'"):
        al.decide("building", FINDER, _big_gate("tower_dish"), al.countability("building", Facts()))


def test_an_answer_carries_its_words():
    shown = _answer("building").as_dict()
    assert shown["level"] == "ROUGH" and shown["label"] == "Rough number"
    assert shown["meaning"] == Level.ROUGH.meaning and shown["reason_code"] == "measured_limit"
    json.dumps(shown)  # plain values, ready for an API response


# --------------------------------------------------------------------------- #
# Switch
# --------------------------------------------------------------------------- #

def test_the_ladder_is_off_by_default(monkeypatch):
    monkeypatch.delenv("ANSWER_LADDER", raising=False)
    assert al.ladder_enabled() is False
    for value in ("0", "false", "no", "", "off"):
        monkeypatch.setenv("ANSWER_LADDER", value)
        assert al.ladder_enabled() is False, value


def test_the_ladder_turns_on(monkeypatch):
    for value in ("1", "true", "YES", " yes "):
        monkeypatch.setenv("ANSWER_LADDER", value)
        assert al.ladder_enabled() is True, value


def test_the_rules_do_not_depend_on_the_switch(monkeypatch):
    """The switch is for consumers; the domain answers the same with it on or off."""
    def answers():
        return [_answer(t.name) for t in al.targets()]

    monkeypatch.setenv("ANSWER_LADDER", "0")
    off = answers()
    monkeypatch.setenv("ANSWER_LADDER", "1")
    assert answers() == off


# --------------------------------------------------------------------------- #
# Trained detectors (2026-10-12)
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize("name", DETECTED)
def test_a_detector_matches_its_evidence(name):
    """Each detector's value, picture count and size are the recorded run's; its size floor comes from the test
    pictures' own boxes, read as they are (no rescale)."""
    d = al.detector(name)
    (row,) = _rows(d.measured["evidence"])
    assert (row["ap50"], row["n_img"], row["imgsz"]) == (d.measured["value"], d.measured["n_images"], d.finder_input_px)
    assert row["model"] == d.finder_model and row["class"] == d.detector_class
    assert d.finder_input_px == d.measured["image_px"] and d.max_level is Level.FULL_REVIEW
    assert d.measured["date"] == "2026-10-13" and "same sites" in d.measured["note"]
    model = own_detector.MODELS[d.finder_model]  # our own model, its weights named by key and sha256
    assert d.detector_class in model.classes and model.sha256[:16] in model.key
    assert d.size_floor_px is not None and d.size_floor_px > 0


@pytest.mark.parametrize("name,value", [("tower_dish", 0.889), ("tower_antenna", 0.952), ("tower_radio_unit", 0.562),
                                        ("power_pylon", 0.989), ("power_insulator", 0.869), ("power_nest", 0.958)])
def test_the_detectors_hold_the_measurements_of_2026_10_13(name, value):
    assert al.detector(name).measured["value"] == value


def test_only_tower_power_and_maize_parts_have_a_detector():
    assert {t.name for t in al.targets() if al.detector(t.name)} == set(DETECTED) | set(DEFECT_PARTS) | {"maize_seedling"}


@pytest.mark.parametrize("name,cls,value", [("tower_bolt_missing", "missing bolt", 0.61),
                                            ("tower_bolt_loose", "loose bolt", 0.542)])
def test_bolt_defects_are_found_by_our_bolt_model_as_measured(name, cls, value):
    """Our bolt model, measured with its ONNX file on the bolts_v2 test split (156 close-ups, a whole DJI flight never
    used in training among them); a person checks every box."""
    d = al.detector(name)
    (row,) = _rows(d.measured["evidence"])
    assert (d.finder_model, d.detector_class) == ("own:rfdetr-small-bolts-2026-10-13", cls) == (row["model"], row["class"])
    assert (row["ap50"], row["n_img"], row["imgsz"]) == (value, 156, 640) == (d.measured["value"], d.measured["n_images"],
                                                                               d.finder_input_px)
    assert d.max_level is Level.FULL_REVIEW and d.size_floor_px is not None and d.size_floor_px > 0
    assert cls in own_detector.MODELS[d.finder_model].classes


def test_maize_is_found_by_sam3_with_its_measured_prompt():
    d = al.detector("maize_seedling")
    (row,) = _rows(d.measured["evidence"])
    assert (d.finder_model, d.detector_class) == ("roboflow:sam3/sam3_final", "seedling") == (row["model"], row["class"])
    assert (row["ap50"], row["n_img"], row["imgsz"]) == (d.measured["value"], 36, 640) and d.measured["value"] == 0.741
    assert d.measured["view"] == al.NADIR and d.max_level is Level.FULL_REVIEW
    assert len(EVIDENCE["marks"]["rf_maize_seedling"]["pictures"]) == 36


def test_power_parts_cannot_be_answered_by_the_example_finder():
    for name in ONLY_DETECTED:
        assert al.target(name).max_level is Level.CANT_TELL and al.target(name).finder_model == FINDER
        gate = al.size_gate(name, 640, marked_box_px=[(300, 300)] * 3, finder_model=al.detector(name).finder_model)
        assert al.decide(name, FINDER, _big_gate(name), al.countability(name, Facts())).level is Level.CANT_TELL
        assert al.decide(name, al.detector(name).finder_model, gate, al.countability(name, Facts())).level is (
            Level.FULL_REVIEW)


def test_a_detector_reads_the_picture_at_its_own_size():
    d = al.detector("tower_dish")
    gate = al.size_gate("tower_dish", 2666, marked_box_px=[(2 * d.size_floor_px, 2 * d.size_floor_px)] * 3,
                        finder_model=d.finder_model)
    assert gate.passes is True and gate.object_px == pytest.approx(d.size_floor_px)  # 2666 px read at 1333 px
    small = al.size_gate("tower_dish", 2666, marked_box_px=[(d.size_floor_px, d.size_floor_px)] * 3,
                         finder_model=d.finder_model)
    assert small.passes is False


def test_another_model_s_output_gets_no_level():
    with pytest.raises(ValueError, match="detector"):
        al.decide("tower_dish", "some-other-model", _big_gate("tower_dish"), al.countability("tower_dish", Facts()))
