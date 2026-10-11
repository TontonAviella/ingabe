"""answer_levels: the measured target table, the size gate, countability and the level each answer gets."""

from __future__ import annotations

import json
from datetime import date

import pytest

from src.services import answer_levels as al
from src.services.answer_levels import Facts, Level, Verdict

PLANTED = date(2026, 9, 15)
TAKEN = date(2026, 10, 10)
OPEN_FIELD = Facts(planting_date=PLANTED, capture_date=TAKEN, green_cover_share=0.3)
BIG_MARKS = [(60, 60), (64, 64), (70, 70)]  # median 64 px across
TOWER_PARTS = ("tower_dish", "tower_antenna", "tower_radio_unit")
PLANT_TARGETS = ("maize_seedling", "row_crop_plant", "weed")


def _passing_gate() -> al.SizeGate:
    return al.size_gate(None, marked_box_px=BIG_MARKS)


def _best_facts() -> Facts:
    """The most favourable facts a picture can have for this target."""
    return Facts(planting_date=PLANTED, capture_date=TAKEN, green_cover_share=0.3, size_gate=_passing_gate())


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
# The target table
# --------------------------------------------------------------------------- #

def test_every_target_has_provenance_and_a_max_level():
    assert {t.name for t in al.targets()} == {
        "tower_dish", "tower_antenna", "tower_radio_unit", "building", "maize_seedling", "row_crop_plant",
        "weed", "tree_crown_forest", "tree_crown_savanna", "banana_mat"}
    for t in al.targets():
        assert set(al.PROVENANCE_KEYS) <= set(t.measured), t.name
        assert isinstance(t.max_level, Level), t.name
        assert t.kind in al.KINDS and t.noun, t.name
        value = t.measured["value"]
        if value is None:
            assert t.measured["note"], t.name  # untested says why
        else:
            assert 0 <= value <= 1, t.name
            assert t.measured["date"] == "2026-10-11" and t.measured["metric"].startswith("AP50"), t.name
            assert "not in this repository" in t.measured["where"], t.name  # the scripts live outside the repo
        for extra in t.also_measured:
            assert 0 <= extra.get("value", 0) <= 1, t.name


def test_the_size_floor_has_provenance():
    assert al.NEEDED_PX == 50
    assert al.SIZE_FLOOR_SOURCE["date"] == "2026-10-11"
    assert "not in this repository" in al.SIZE_FLOOR_SOURCE["where"]
    assert "25-30 px" in al.SIZE_FLOOR_SOURCE["note"]


@pytest.mark.parametrize("name,value,n_images,max_level", [
    ("tower_dish", 0.74, 20, Level.FULL_REVIEW),
    ("tower_antenna", 0.69, 20, Level.FULL_REVIEW),
    ("tower_radio_unit", 0.29, 14, Level.FULL_REVIEW),
    ("building", 0.53, 20, Level.ROUGH),
    ("maize_seedling", 0.79, 20, Level.ROUGH),
    ("row_crop_plant", 0.53, 20, Level.ROUGH),
    ("weed", 0.45, 20, Level.ROUGH),
    ("tree_crown_forest", 0.22, 20, Level.AREA),
    ("tree_crown_savanna", 0.06, 20, Level.AREA),
    ("banana_mat", None, None, Level.AREA),
])
def test_the_table_holds_the_measurements_of_2026_10_11(name, value, n_images, max_level):
    t = al.target(name)
    assert (t.measured["value"], t.measured["n_images"], t.max_level) == (value, n_images, max_level)


def test_the_finder_settings_measured_best():
    for t in al.targets():
        assert t.zoom == (2 if t.name in PLANT_TARGETS else 1), t.name
        assert t.look_alike_marks == (t.name in TOWER_PARTS), t.name
    assert al.target("tower_radio_unit").measured["image_px"] == 2560
    assert al.target("tower_radio_unit").also_measured[0]["value"] == 0.32
    assert al.target("tower_antenna").also_measured[0]["change"] == 0.02
    assert al.target("maize_seedling").also_measured[0]["value"] == 0.44
    assert "other pictures" in al.target("tree_crown_savanna").measured["protocol"].lower()


def test_no_size_is_guessed():
    # Nobody measured object sizes on our pictures: the gate uses the user's marks instead.
    assert all(t.object_size_m is None and t.object_size_note for t in al.targets())


def test_a_broken_table_fails_on_load(tmp_path):
    data = json.loads(al.TARGETS_PATH.read_text())
    good = tmp_path / "good.json"
    good.write_text(json.dumps(data))
    assert al.load_targets(good)[0] == 50

    def broken(change) -> None:
        copy = json.loads(json.dumps(data))
        change(copy)
        path = tmp_path / "broken.json"
        path.write_text(json.dumps(copy))
        with pytest.raises(ValueError):
            al.load_targets(path)

    broken(lambda d: d["targets"]["building"]["measured"].pop("where"))
    broken(lambda d: d["targets"]["building"]["measured"].update(value=1.3))
    broken(lambda d: d["targets"]["building"]["measured"].update(date=None))
    broken(lambda d: d["targets"]["banana_mat"]["measured"].update(note=None))
    broken(lambda d: d["targets"]["weed"]["measured"].update(note=None))  # no dataset and no note
    broken(lambda d: d["targets"]["building"].update(kind="vehicle"))
    broken(lambda d: d["targets"]["building"].update(object_size_m=0))
    broken(lambda d: d["targets"]["building"].update(object_size_note=None))
    broken(lambda d: d["targets"]["building"]["finder"].update(zoom=0))
    broken(lambda d: d["targets"]["tower_radio_unit"]["also_measured"][0].update(value=-0.1))
    broken(lambda d: d["size_floor"].update(needed_px=0))
    broken(lambda d: d["targets"]["building"].update(max_level="PERFECT"))


def test_an_unknown_target_is_refused():
    with pytest.raises(ValueError, match="no answer target 'goats'"):
        al.target("goats")
    with pytest.raises(ValueError):
        al.countability("goats", Facts())


# --------------------------------------------------------------------------- #
# Size gate
# --------------------------------------------------------------------------- #

def test_small_objects_fail_with_the_reason_in_data_terms():
    gate = al.size_gate(3.2, object_size_m=0.29)
    assert gate.passes is False and gate.object_px == pytest.approx(9.06, abs=0.01)
    assert gate.needed_px == 50 and not gate.from_marks
    assert gate.reason == "objects are about 9 px wide at 3.2 cm per pixel; at least 50 px are needed"


def test_fifty_px_is_enough_and_just_under_is_not():
    assert al.size_gate(1.0, object_size_m=0.5).passes is True  # exactly 50 px
    under = al.size_gate(1.0, object_size_m=0.496)  # 49.6 px
    assert under.passes is False
    assert "about 49 px" in under.reason  # never shown as "50 px" when it failed


def test_the_users_marks_win_over_a_size_on_the_ground():
    gate = al.size_gate(3.2, object_size_m=0.29, marked_box_px=BIG_MARKS)
    assert gate.from_marks and gate.object_px == 64 and gate.passes is True
    assert gate.reason == "the marked objects are about 64 px wide at 3.2 cm per pixel; 50 px or more is enough"


def test_a_marked_box_is_sized_by_its_area():
    assert al.box_px(20, 125) == 50  # a long thin box: the side of a square of the same area
    assert al.size_gate(None, marked_box_px=[(20, 125)]).passes is True
    assert al.size_gate(None, marked_box_px=[(10, 10), (40, 40), (100, 100)]).object_px == 40  # the median


def test_an_unknown_size_is_unknown_not_small():
    for gate in (al.size_gate(None), al.size_gate(3.2), al.size_gate(None, object_size_m=0.6),
                 al.size_gate(3.2, marked_box_px=[])):
        assert gate.passes is None and gate.object_px is None
        assert "not known" in gate.reason


@pytest.mark.parametrize("kwargs", [
    {"cm_per_px": 0}, {"cm_per_px": -1.0}, {"cm_per_px": 3.2, "object_size_m": 0},
    {"cm_per_px": None, "marked_box_px": [(0, 10)]}, {"cm_per_px": None, "marked_box_px": [(10, -5)]},
])
def test_impossible_sizes_are_refused(kwargs):
    with pytest.raises(ValueError):
        al.size_gate(**kwargs)


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
def test_structures_are_countable_when_the_size_gate_passes(name):
    assert al.countability(name, Facts(size_gate=_passing_gate())).verdict is Verdict.YES
    small = al.countability(name, Facts(size_gate=al.size_gate(None, marked_box_px=[(20, 20)])))
    assert small.verdict is Verdict.NO and small.reason_code == "too_small"
    assert small.reason_text.startswith(f"the marked {al.target(name).noun} are about 20 px wide")
    for unknown in (Facts(), Facts(size_gate=al.size_gate(None))):
        verdict = al.countability(name, unknown)
        assert verdict.verdict is Verdict.MAYBE and verdict.needs == ("size_gate",)


@pytest.mark.parametrize("name", ["tree_crown_forest", "tree_crown_savanna", "banana_mat"])
def test_counting_crowns_is_untested(name):
    verdict = al.countability(name, _best_facts())
    assert verdict.verdict is Verdict.MAYBE and verdict.reason_code == "count_untested"


# --------------------------------------------------------------------------- #
# Decision
# --------------------------------------------------------------------------- #

def test_cant_tell_when_the_plants_are_too_small():
    gate = al.size_gate(3.2, object_size_m=0.29)
    answer = al.decide("maize_seedling", gate, al.countability("maize_seedling", OPEN_FIELD))
    assert answer.level is Level.CANT_TELL and answer.reason_code == "too_small"
    assert answer.reason_text == "maize plants are about 9 px wide at 3.2 cm per pixel; at least 50 px are needed"


def test_cant_tell_when_the_size_is_unknown():
    answer = al.decide("tower_dish", al.size_gate(None), al.countability("tower_dish", Facts()))
    assert answer.level is Level.CANT_TELL and answer.reason_code == "size_unknown"
    assert "mark two or three" in answer.reason_text and "dishes" in answer.reason_text


def test_cant_tell_when_countability_is_no():
    closed = Facts(planting_date=PLANTED, capture_date=TAKEN, green_cover_share=0.95)
    answer = al.decide("maize_seedling", _passing_gate(), al.countability("maize_seedling", closed))
    assert answer.level is Level.CANT_TELL and answer.reason_code == "plants_touch"
    assert "95%" in answer.reason_text


def test_the_size_gate_speaks_first():
    small = al.size_gate(3.2, object_size_m=0.29)
    no = al.countability("maize_seedling", Facts(green_cover_share=0.95))
    assert al.decide("maize_seedling", small, no).reason_code == "too_small"


@pytest.mark.parametrize("name,level", [
    ("tower_dish", Level.FULL_REVIEW), ("tower_antenna", Level.FULL_REVIEW), ("tower_radio_unit", Level.FULL_REVIEW),
    ("building", Level.ROUGH), ("maize_seedling", Level.ROUGH), ("row_crop_plant", Level.ROUGH),
    ("weed", Level.ROUGH), ("tree_crown_forest", Level.AREA), ("tree_crown_savanna", Level.AREA),
    ("banana_mat", Level.AREA),
])
def test_with_every_gate_open_the_answer_is_the_measured_max(name, level):
    facts = _best_facts()
    answer = al.decide(name, facts.size_gate, al.countability(name, facts))
    assert answer.level is level


def test_the_measured_limit_says_where_it_was_measured():
    facts = _best_facts()
    answer = al.decide("tower_radio_unit", facts.size_gate, al.countability("tower_radio_unit", facts))
    assert answer.reason_code == "measured_limit"
    assert answer.reason_text == ('for radio units the answer stops at "Count after checking" today: measured on 14 '
                                  "pictures (celltower test set, 2026-10-11)")


def test_missing_facts_offer_the_area_and_list_what_is_needed():
    answer = al.decide("maize_seedling", _passing_gate(), al.countability("maize_seedling", Facts(green_cover_share=0.2)))
    assert answer.level is Level.AREA and answer.reason_code == "needs_facts"
    assert answer.reason_text == ("to count maize plants this needs the planting date and the date the picture was "
                                  "taken; the area they cover is offered instead")


def test_evidence_can_lower_the_level_but_never_raise_it():
    facts = _best_facts()
    verdict = al.countability("building", facts)
    lower = al.decide("building", facts.size_gate, verdict, al.Evidence(Level.AREA, "half the picture is cloud"))
    assert (lower.level, lower.reason_code, lower.reason_text) == (Level.AREA, "evidence_limit",
                                                                   "half the picture is cloud")
    higher = al.decide("building", facts.size_gate, verdict, al.Evidence(Level.COUNT, "a checked count"))
    assert higher.level is Level.ROUGH and higher.reason_code == "measured_limit"


def test_no_target_reaches_count_today():
    assert all(t.max_level < Level.COUNT for t in al.targets())
    for t in al.targets():
        facts = _best_facts()
        answer = al.decide(t.name, facts.size_gate, al.countability(t.name, facts), al.Evidence(Level.COUNT, "x"))
        assert answer.level is not Level.COUNT, t.name


def test_an_answer_carries_its_words():
    facts = _best_facts()
    shown = al.decide("building", facts.size_gate, al.countability("building", facts)).as_dict()
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
    facts = _best_facts()

    def answers():
        return [al.decide(t.name, facts.size_gate, al.countability(t.name, facts)) for t in al.targets()]

    monkeypatch.setenv("ANSWER_LADDER", "0")
    off = answers()
    monkeypatch.setenv("ANSWER_LADDER", "1")
    assert answers() == off
