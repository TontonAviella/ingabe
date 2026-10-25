"""Drone question cards on a synthetic photo: a green field with one bare square of known size."""

from __future__ import annotations

import dataclasses
import json
import re
import time

import numpy as np
import pytest
import rasterio
from rasterio.transform import from_origin

from src.services import (
    background_jobs,
    drone_cards,
    drone_first_look,
    drone_plots,
    drone_vision,
    isdasoil_service,
    wapor_service,
)

PIXEL_M = 0.5
SIZE = 600  # 300 m x 300 m = 9 ha
BARE_PX = 120  # 60 m x 60 m = 0.36 ha


@pytest.fixture
def photo(tmp_path):
    """A 9 ha RGB photo in UTM 35S (Rwanda), green except one bare 0.36 ha square in the north-west."""
    red = np.full((SIZE, SIZE), 80, dtype="uint8")
    green = np.full((SIZE, SIZE), 150, dtype="uint8")
    blue = np.full((SIZE, SIZE), 60, dtype="uint8")
    red[60:60 + BARE_PX, 60:60 + BARE_PX] = 150
    green[60:60 + BARE_PX, 60:60 + BARE_PX] = 110
    path = tmp_path / "field.tif"
    with rasterio.open(path, "w", driver="GTiff", width=SIZE, height=SIZE, count=3, dtype="uint8",
                       crs="EPSG:32735", transform=from_origin(830000, 9810000, PIXEL_M, PIXEL_M)) as ds:
        ds.write(np.stack([red, green, blue]))
    return path


def _analysis(path, band_count=3):
    with rasterio.open(path) as ds:
        red = ds.read(1, masked=True)
        green = ds.read(2, masked=True)
        look = drone_first_look.FirstLook(layer_name="Test field", place="Test cell", area_ha=9.0,
                                          resolution_cm=PIXEL_M * 100,
                                          green=drone_first_look.measure_greenness(red, green))
        bare = drone_cards.measure_bare_ground(ds)
        zones = drone_cards.photo_zones(ds, look)
        bounds = list(rasterio.warp.transform_bounds(ds.crs, "EPSG:4326", *ds.bounds))
    return drone_cards.PhotoAnalysis(layer_id="Ltest", look=look, band_count=band_count, bounds=bounds,
                                     bare=bare, zones=zones)


def test_bare_square_is_one_patch_of_its_true_size(photo):
    bare = drone_cards.measure_bare_ground(rasterio.open(photo))
    assert bare.patches == 1
    assert bare.area_ha == pytest.approx(0.36, rel=0.25)
    feature = bare.geojson["features"][0]
    assert feature["properties"]["rank"] == 1
    lon, lat = feature["geometry"]["coordinates"][0][0]
    assert 29 < lon < 31 and -3 < lat < -1  # drawn in WGS84 over Rwanda


def test_a_field_without_bare_ground_has_no_patches(tmp_path):
    path = tmp_path / "green.tif"
    with rasterio.open(path, "w", driver="GTiff", width=200, height=200, count=3, dtype="uint8",
                       crs="EPSG:32735", transform=from_origin(830000, 9810000, PIXEL_M, PIXEL_M)) as ds:
        ds.write(np.stack([np.full((200, 200), v, dtype="uint8") for v in (80, 150, 60)]))
    bare = drone_cards.measure_bare_ground(rasterio.open(path))
    assert bare.patches == 0 and bare.area_ha == 0


def test_zones_follow_the_first_look_order(photo):
    analysis = _analysis(photo)
    names = [f["properties"]["name"] for f in analysis.zones["features"]]
    assert names == [zone.name for zone in analysis.look.green.zones]
    assert names[0] == "north-west"


def test_deck_shows_every_service_and_ends_with_a_lesson(photo):
    deck = drone_cards.build_deck(_analysis(photo), "farmer", drone_cards.Here(photos=1))
    assert [s["service"] for s in deck["services"]] == list(range(1, 12))
    assert all(s["cards"] for s in deck["services"])
    assert len(deck["for_you"]) == drone_cards.FOR_YOU_COUNT
    assert deck["for_you"][-1]["status"] == drone_cards.LEARN
    assert deck["photo"]["camera"] == "colour"


def test_ready_answers_come_first(photo):
    deck = drone_cards.build_deck(_analysis(photo), "farmer", drone_cards.Here(photos=1))
    statuses = [card["status"] for card in deck["for_you"][:-1]]
    assert statuses[:2] == [drone_cards.READY, drone_cards.READY]


def test_insurer_sees_planted_or_never_planted(photo):
    analysis = _analysis(photo)
    insurer = drone_cards.answer_card("bare_ground", analysis, "insurer", drone_cards.Here(photos=1))
    farmer = drone_cards.answer_card("bare_ground", analysis, "farmer", drone_cards.Here(photos=1))
    assert "never planted" in insurer["why"]
    assert insurer["why"] != farmer["why"]
    assert insurer["overlay"]["kind"] == "bare"
    assert len(insurer["overlay"]["geojson"]["features"]) == 1


def test_fertilizer_asks_for_a_special_camera_on_a_colour_photo(photo):
    analysis = _analysis(photo)
    assert drone_cards.answer_card("fertilizer", analysis, "agronomist", drone_cards.Here(photos=1))["status"] == drone_cards.NEEDS_CAMERA
    multispectral = _analysis(photo, band_count=5)
    assert drone_cards.answer_card("fertilizer", multispectral, "agronomist", drone_cards.Here(photos=1))["status"] == drone_cards.TO_BUILD


def test_growth_needs_a_second_flight_until_there_is_one(photo):
    analysis = _analysis(photo)
    assert drone_cards.answer_card("growth", analysis, "farmer", drone_cards.Here(photos=1))["status"] == drone_cards.NEEDS_FLIGHT
    assert drone_cards.answer_card("growth", analysis, "farmer", drone_cards.Here(photos=2))["status"] == drone_cards.TO_BUILD


def test_missing_water_data_is_said_not_shown_as_zero(photo, monkeypatch):
    monkeypatch.setattr(wapor_service, "query_et", lambda *a, **k: {"status": "error", "error": "no data"})
    answer = drone_cards.answer_card("water", _analysis(photo), "farmer", drone_cards.Here(photos=1))
    assert answer["facts"] == []
    assert "no satellite reading" in answer["what"]


def test_soil_values_show_the_likely_range(photo, monkeypatch):
    # The service's output at Cyampirita (see test_isdasoil_service.py).
    monkeypatch.setattr(isdasoil_service, "query_soil_point", lambda *a, **k: {"status": "success", "properties": {
        "nitrogen_total": {"value": 1.44, "likely_range": [1.15, 1.76], "unit": "g/kg", "label": "Total Nitrogen"},
        "phosphorous_extractable": {"value": 10.59, "likely_range": [9.27, 12.08], "unit": "ppm",
                                    "label": "Extractable Phosphorus"},
        "potassium_extractable": {"value": 166.67, "likely_range": [137.66, 201.76], "unit": "ppm",
                                  "label": "Extractable Potassium"},
        "ph": {"value": 5.81, "uncertainty": 0.1, "likely_range": [5.71, 5.92], "unit": "", "label": "Soil pH"},
    }})
    answer = drone_cards.answer_card("soil", _analysis(photo), "farmer", drone_cards.Here(photos=1))
    assert answer["facts"] == [
        {"label": "Total Nitrogen", "value": "1.44 g/kg (likely 1.15-1.76)"},
        {"label": "Extractable Phosphorus", "value": "10.6 ppm (likely 9.3-12.1)"},
        {"label": "Extractable Potassium", "value": "167 ppm (likely 138-202)"},
        {"label": "Soil pH", "value": "5.81 (likely 5.71-5.92)"},
    ]
    assert answer["how_sure"]["level"] == "low"


def test_soil_value_without_a_range_is_shown_alone(photo, monkeypatch):
    monkeypatch.setattr(isdasoil_service, "query_soil_point", lambda *a, **k: {"status": "success", "properties": {
        "nitrogen_total": {"value": 1.44, "unit": "g/kg", "label": "Total Nitrogen"},
    }})
    answer = drone_cards.answer_card("soil", _analysis(photo), "farmer", drone_cards.Here(photos=1))
    assert answer["facts"] == [{"label": "Total Nitrogen", "value": "1.44 g/kg"}]


def test_unknown_card(photo):
    with pytest.raises(KeyError):
        drone_cards.answer_card("nonsense", _analysis(photo), "farmer", drone_cards.Here(photos=1))


MONEY = re.compile(r"\b(RWF|FRW|USD|dollars?|francs?|money|price|cost|costs|profit|revenue|savings?)\b|\$", re.I)


@pytest.mark.parametrize("audience", ["farmer", "insurer", "agronomist", "scientist"])
def test_no_card_talks_about_money(photo, monkeypatch, audience):
    monkeypatch.setattr(isdasoil_service, "query_soil_point", lambda *a, **k: {"status": "error", "error": "offline"})
    monkeypatch.setattr(wapor_service, "query_et", lambda *a, **k: {"status": "error", "error": "offline"})
    analysis = _analysis(photo)
    deck = drone_cards.build_deck(analysis, audience, drone_cards.Here(photos=1))
    answers = [drone_cards.answer_card(card["id"], analysis, audience, drone_cards.Here(photos=1))
               for service in deck["services"] for card in service["cards"]]
    text = json.dumps([deck["for_you"], [{k: a[k] for k in ("what", "why", "todo", "terms")} for a in answers]])
    assert not MONEY.search(text)


def _plot_set(n=10):
    """n square plots in rows of 5 on the test photo; plot 3 is the least green."""
    features = []
    for i in range(n):
        lon, lat = 29.9658 + (i % 5) * 0.0005, -1.7168 - (i // 5) * 0.0005
        greenness = 0.02 if i == 2 else 0.1 + i * 0.01
        features.append({"type": "Feature",
                         "geometry": {"type": "Polygon", "coordinates": [[[lon, lat], [lon + 0.0004, lat],
                                                                          [lon + 0.0004, lat - 0.0004], [lon, lat - 0.0004],
                                                                          [lon, lat]]]},
                         "properties": {"number": i + 1, "area_ha": 0.79, "greenness": greenness,
                                        "bare_share": 0.4 if i == 2 else 0.0, "confidence": 0.5,
                                        "lon": lon + 0.0002, "lat": lat - 0.0002}})
    groups = drone_plots.plot_groups([(f["properties"]["greenness"], f["properties"]["bare_share"]) for f in features])
    for f, group in zip(features, groups):
        f["properties"]["group"] = group
    return drone_plots.PlotSet(geojson={"type": "FeatureCollection", "features": features},
                               found_at="2026-10-06T12:00:00+00:00", seconds=1.0, source="found", read_m_per_px=0.5)


def test_plot_cards_wait_while_the_plots_are_found(photo):
    job = background_jobs.Job(state="running", parts_done=2, parts=8, started=0.0)
    here = drone_cards.Here(photos=1, plot_job=job)
    answer = drone_cards.answer_card("plots_green", _analysis(photo), "farmer", here)
    assert answer["status"] == drone_cards.WORKING
    assert answer["progress"] == {"done": 2, "parts": 8, "minutes_left": job.minutes_left}
    assert answer["downloads"] == []


def test_least_green_plots_are_named_and_drawn(photo):
    here = drone_cards.Here(photos=1, plots=_plot_set())
    answer = drone_cards.answer_card("plots_green", _analysis(photo), "farmer", here)
    assert answer["status"] == drone_cards.READY
    assert answer["items"][0]["title"] == "Plot 3"
    assert answer["what"].startswith("Of the 10 plots with a crop") and "plots 3 and 1" in answer["what"]
    assert answer["overlay"]["kind"] == "plot_groups"
    assert {d["label"] for d in answer["downloads"]} == {"Excel table", "Shapefile", "GeoJSON"}


def test_plot_cards_offer_the_readers_own_maps(photo):
    plot_map = drone_plots.PlotMap(layer_id="Lmap", name="Cooperative blocks", shapes=12)
    here = drone_cards.Here(photos=1, plots=_plot_set(), plot_maps=(plot_map,))
    answer = drone_cards.answer_card("field_outlines", _analysis(photo), "farmer", here)
    options = answer["choices"]["options"]
    assert [(o["id"], o["selected"]) for o in options] == [("found", True), ("Lmap", False)]
    assert options[1]["detail"] == "Your map · 12 shapes"
    assert answer["choices"]["href"] == "/api/layer/Ltest/plots/source"
    assert "Add data" not in answer["todo"]  # a map is already there to choose


def test_without_a_plot_map_the_card_says_how_to_add_one(photo):
    answer = drone_cards.answer_card("field_outlines", _analysis(photo), "farmer", drone_cards.Here(plots=_plot_set()))
    assert answer["choices"] is None and "Add data" in answer["todo"]


def test_plots_from_the_readers_map_go_by_their_names(photo):
    plots = _plot_set()
    for f in plots.geojson["features"]:
        f["properties"]["name"] = f"B-{f['properties']['number']:02d}"
    own = drone_plots.PlotSet(geojson=plots.geojson, found_at=plots.found_at, seconds=1.0,
                              source="Cooperative blocks", read_m_per_px=0.5)
    here = drone_cards.Here(plots=own, plot_maps=(drone_plots.PlotMap("Lmap", "Cooperative blocks", 10),),
                            plot_map="Lmap")
    green = drone_cards.answer_card("plots_green", _analysis(photo), "farmer", here)
    assert "plots B-03 and B-01" in green["what"] and green["items"][0]["title"] == "B-03 · plot 3"
    outlines = drone_cards.answer_card("field_outlines", _analysis(photo), "farmer", here)
    assert outlines["what"].startswith("Your map Cooperative blocks has 10 plots")
    assert [o["selected"] for o in outlines["choices"]["options"]] == [False, True]


def test_a_map_that_cannot_be_read_is_said_so(photo):
    here = drone_cards.Here(plots=_plot_set(), plot_maps=(drone_plots.PlotMap("Lmap", "Blocks", 3),),
                            plot_map_error="Blocks could not be read (bad file)")
    answer = drone_cards.answer_card("field_outlines", _analysis(photo), "farmer", here)
    assert answer["what"].startswith("Your map Blocks could not be read (bad file), so these are the plots Ingabe found.")


def test_field_outlines_count_and_measure_the_plots(photo):
    here = drone_cards.Here(photos=1, plots=_plot_set())
    answer = drone_cards.answer_card("field_outlines", _analysis(photo), "insurer", here)
    assert answer["what"].startswith("Ingabe found 10 plots")
    assert answer["overlay"]["kind"] == "plots"
    assert answer["overlay"]["geojson"]["features"][0]["properties"]["label"] == "Plot 1 · 0.79 ha"


@pytest.mark.parametrize("audience", ["farmer", "insurer", "agronomist", "scientist"])
def test_no_plot_card_talks_about_money(photo, audience):
    here = drone_cards.Here(photos=1, plots=_plot_set())
    analysis = _analysis(photo)
    answers = [drone_cards.answer_card(card_id, analysis, audience, here) for card_id in ("plots_green", "field_outlines")]
    text = json.dumps([{k: a[k] for k in ("what", "why", "todo", "items", "facts")} for a in answers])
    assert not MONEY.search(text)


def _survey(plots, crops, overrides=None):
    """A vision survey of the test plots: crops[i] for plot i + 1, every plot young unless overridden."""
    looks = {}
    for i, crop in enumerate(crops):
        n = i + 1
        answer = {"main_crop": crop, "other_crops": [], "stage": "young", "crop_cover_percent": 40, "weeds": "few",
                  "problems": [], "note": f"Plot {n} note.", "confidence": "medium"} | (overrides or {}).get(n, {})
        looks[n] = drone_vision._look(n, answer)
    return drone_vision.Survey(looks=looks, plots=len(crops), model="openai/gpt-6-luna",
                               done_at="2026-10-06T17:00:00+00:00", cost_usd=0.002)


def test_vision_cards_wait_for_the_plots_and_the_looks(photo):
    job = background_jobs.Job(state="running", parts_done=3, parts=10, started=0.0)
    here = drone_cards.Here(plots=_plot_set(), survey_job=job)
    answer = drone_cards.answer_card("crop_types", _analysis(photo), "farmer", here)
    assert answer["status"] == drone_cards.WORKING and answer["progress"]["done"] == 3
    assert "3 of 10 done" in answer["what"]


def test_crop_types_name_the_crops_and_count_what_is_not_sure(photo):
    plots = _plot_set()
    crops = ["maize"] * 5 + ["beans"] * 3 + ["unsure"] * 2
    here = drone_cards.Here(plots=plots, survey=_survey(plots, crops, {1: {"other_crops": ["beans"]}}))
    answer = drone_cards.answer_card("crop_types", _analysis(photo), "insurer", here)
    assert answer["status"] == drone_cards.READY
    assert answer["what"].startswith("The crop is named in 8 of 10 plots: maize in 5 plots")
    assert "2 plots are 'not sure' rather than a guess" in answer["what"] and "1 plot looks intercropped" in answer["what"]
    assert [i["key"] for i in answer["overlay"]["legend_items"]] == ["maize", "beans", "unsure"]
    assert answer["how_sure"]["level"] == "low"


def test_plots_behind_their_crop_are_found(photo):
    plots = _plot_set()
    crops = ["maize"] * 6 + ["beans"] * 2 + ["unsure"] * 2
    overrides = {n: {"stage": "growing"} for n in range(1, 7)} | {4: {"stage": "just_planted"}}
    here = drone_cards.Here(plots=plots, survey=_survey(plots, crops, overrides))
    answer = drone_cards.answer_card("plot_stage", _analysis(photo), "farmer", here)
    assert answer["what"].startswith("Of 6 plots compared within their crop, 1 look younger")
    assert answer["items"][0]["title"] == "Plot 4"
    flags = {f["properties"]["number"]: f["properties"]["flag"] for f in answer["overlay"]["geojson"]["features"]}
    assert flags[4] is True and flags[1] is False


def test_weedy_plots_are_listed_largest_first(photo):
    plots = _plot_set()
    here = drone_cards.Here(plots=plots, survey=_survey(plots, ["maize"] * 10, {2: {"weeds": "many"}, 7: {"weeds": "many"}}))
    answer = drone_cards.answer_card("weeds", _analysis(photo), "farmer", here)
    assert answer["what"].startswith("2 of 10 plots with a crop look weedy")
    assert {i["title"] for i in answer["items"]} == {"Plot 2", "Plot 7"}


def test_problems_seen_from_the_air_are_ranked(photo):
    plots = _plot_set()
    overrides = {3: {"problems": ["gaps", "yellowing"]}, 5: {"problems": ["standing_water"]}}
    here = drone_cards.Here(plots=plots, survey=_survey(plots, ["maize"] * 10, overrides))
    answer = drone_cards.answer_card("plot_problems", _analysis(photo), "agronomist", here)
    assert "possible problems in 2 of 10 plots" in answer["what"]
    assert answer["items"][0]["title"] == "Plot 3"


@pytest.mark.parametrize("audience", ["farmer", "insurer", "agronomist", "scientist"])
def test_no_vision_card_talks_about_money(photo, audience):
    plots = _plot_set()
    here = drone_cards.Here(plots=plots, survey=_survey(plots, ["maize"] * 10, {2: {"weeds": "many", "problems": ["gaps"]}}))
    answers = [drone_cards.answer_card(c, _analysis(photo), audience, here)
               for c in ("crop_types", "plot_problems", "plot_stage", "weeds")]
    text = json.dumps([{k: a[k] for k in ("what", "why", "todo", "items", "facts")} for a in answers])
    assert not MONEY.search(text)


def test_weeding_leaves_out_fallow_plots(photo):
    plots = _plot_set()
    crops = ["maize"] * 8 + ["fallow_or_bare"] * 2
    overrides = {9: {"weeds": "many"}, 10: {"weeds": "many"}, 1: {"weeds": "many"}}
    here = drone_cards.Here(plots=plots, survey=_survey(plots, crops, overrides))
    answer = drone_cards.answer_card("weeds", _analysis(photo), "farmer", here)
    assert answer["what"].startswith("1 of 8 plots with a crop look weedy")
    assert [i["title"] for i in answer["items"]] == ["Plot 1"]


def test_a_seed_changes_the_wording_and_the_mix_but_seed_0_is_fixed(photo):
    plots = _plot_set()
    analysis = _analysis(photo)
    fixed = drone_cards.build_deck(analysis, "farmer", drone_cards.Here(plots=plots))
    assert fixed == drone_cards.build_deck(analysis, "farmer", drone_cards.Here(plots=plots))
    decks = [drone_cards.build_deck(analysis, "farmer", drone_cards.Here(plots=plots, seed=s)) for s in range(1, 30)]
    questions = {card["question"] for deck in decks for card in deck["for_you"]}
    mixes = {tuple(card["id"] for card in deck["for_you"]) for deck in decks}
    assert len(questions) > len(fixed["for_you"]) and len(mixes) > 1
    assert all(deck["for_you"][-1]["status"] == drone_cards.LEARN for deck in decks)
    # the answer uses the same words as the card the reader tapped
    card = decks[0]["for_you"][0]
    answer = drone_cards.answer_card(card["id"], analysis, "farmer", drone_cards.Here(plots=plots, seed=1))
    assert answer["question"] == card["question"]


def test_answers_suggest_questions_for_sage_with_the_photo_named(photo):
    plots = _plot_set()
    here = drone_cards.Here(plots=plots, survey=_survey(plots, ["maize"] * 10, {3: {"problems": ["gaps"]}}))
    answer = drone_cards.answer_card("plot_problems", _analysis(photo), "farmer", here)
    labels = [a["label"] for a in answer["ask_sage"]]
    assert labels[0] == "What could cause this in Plot 3?" and "maize" in labels[1]
    assert all(a["prompt"].startswith('On my drone photo "Test field" (Test cell)') for a in answer["ask_sage"])
    deck = drone_cards.build_deck(_analysis(photo), "farmer", here)
    assert 1 <= len(deck["ask_sage"]) <= drone_cards.ASKS_PER_DECK


def test_flagged_plots_are_badged_and_the_rest_dimmed(photo):
    plots = _plot_set()
    here = drone_cards.Here(plots=plots, survey=_survey(plots, ["maize"] * 10, {2: {"weeds": "many"}}))
    overlay = drone_cards.answer_card("weeds", _analysis(photo), "farmer", here)["overlay"]
    badges = {f["properties"]["number"]: f["properties"].get("badge") for f in overlay["geojson"]["features"]}
    assert badges[2] == "Weeds" and badges[1] is None
    dimmed = overlay["spotlight"]["features"][0]["geometry"]
    assert dimmed["type"] == "Polygon" and len(dimmed["coordinates"]) == 2  # the photo with one hole


def test_problem_answers_show_bare_spots_inside_plots_with_gaps(photo):
    plots = _plot_set()
    here = drone_cards.Here(plots=plots, survey=_survey(plots, ["maize"] * 10, {3: {"problems": ["gaps"]}, 5: {"problems": ["yellowing"]}}))
    assert [f["properties"]["number"] for f in drone_cards.plots_to_measure_spots("plot_problems", here)] == [3]
    assert drone_cards.plots_to_measure_spots("weeds", here) == []
    spots = {"type": "FeatureCollection", "measured_plots": [3],
             "features": [{"type": "Feature", "properties": {"number": 3, "area_m2": 900.0},
                           "geometry": plots.geojson["features"][2]["geometry"]}]}
    answer = drone_cards.answer_card("plot_problems", _analysis(photo), "farmer", _replace(here, spots))
    assert answer["overlay"]["spots"]["features"] == spots["features"] and "1 spots of 1 m² or more" in answer["what"]
    assert "open soil covers 10% or more of 1 of them" in answer["what"]


def test_gaps_count_only_where_measured_open_soil_confirms_them(photo):
    """Seeing the photo at full detail, the model called the soil between young plants 'gaps'."""
    plots = _plot_set()
    gaps = {3: {"problems": ["gaps"]}, 4: {"problems": ["gaps"]}, 5: {"problems": ["yellowing"]}}
    here = drone_cards.Here(plots=plots, survey=_survey(plots, ["maize"] * 10, gaps))
    spots = {"type": "FeatureCollection", "measured_plots": [3, 4], "features": [
        {"type": "Feature", "properties": {"number": 3, "area_m2": 900.0}, "geometry": plots.geojson["features"][2]["geometry"]},
        {"type": "Feature", "properties": {"number": 4, "area_m2": 20.0}, "geometry": plots.geojson["features"][3]["geometry"]}]}
    answer = drone_cards.answer_card("plot_problems", _analysis(photo), "farmer", _replace(here, spots))
    assert [i["title"] for i in answer["items"]] == ["Plot 3", "Plot 5"]  # plot 4: 20 m² of 7,900 is not a gap
    assert "flagged gaps in 2 plots; measured open soil covers 10% or more of 1 of them" in answer["what"]
    assert [f["properties"]["number"] for f in answer["overlay"]["spots"]["features"]] == [3]


def test_while_open_soil_is_measured_the_problems_answer_says_so(photo):
    plots = _plot_set()
    here = drone_cards.Here(plots=plots, survey=_survey(plots, ["maize"] * 10, {3: {"problems": ["gaps"]}}),
                            spots_job=background_jobs.Job(state="running", parts_done=40, parts=222, started=0.0))
    answer = drone_cards.answer_card("plot_problems", _analysis(photo), "farmer", here)
    assert answer["progress"]["done"] == 40 and "being measured to confirm them" in answer["what"]


def _replace(here, spots):
    import dataclasses

    return dataclasses.replace(here, spots=spots)


def _records():
    from src.services import farm_records

    soil = farm_records.FarmDocument(
        id="s", filename="report.pdf", file_key="k", kind=farm_records.SOIL_REPORT, title="Soil report",
        source="Example Soil Laboratory; Client: Test cooperative", date="26/09/2026", added_at="2026-10-07T00:00:00+00:00",
        soil_samples=[farm_records.SoilSample("S1", "Plot 3", "0-20 cm", 4.9, 0.95, 0.08, 4.1, "Bray II", 0.12, "Sandy loam"),
                      farm_records.SoilSample("S2", "Plot 5", "0-20 cm", 6.1, 1.9, 0.16, 17.5, "Bray II", 0.42, "Clay loam")])
    harvest = farm_records.FarmDocument(
        id="h", filename="register.jpg", file_key="k2", kind=farm_records.HARVEST_RECORDS, title="Harvest register",
        source=None, date=None, added_at="2026-10-07T00:00:00+00:00",
        harvests=[farm_records.Harvest("3", "Test farmer 3", "maize", "2026A", None, 190, 0.10),
                  farm_records.Harvest("5", "Test farmer 5", "maize", "2026A", None, 640, 0.15)])
    return (soil, harvest)


def test_the_soil_card_answers_from_the_lab_report(photo):
    here = drone_cards.Here(plots=_plot_set(), records=_records())
    answer = drone_cards.answer_card("soil", _analysis(photo), "farmer", here)
    assert answer["status"] == drone_cards.READY
    assert answer["what"].startswith("Your lab report (Example Soil Laboratory, 26/09/2026) has 2 samples.")
    assert "lime Plot 3" in answer["todo"] and answer["items"][0]["title"] == "Plot 3"
    assert answer["facts"][0]["value"] == "pH 4.9 · N 0.08 % · P 4.1 mg/kg · K 0.12 cmol/kg"
    assert answer["ask_sage"][0]["label"] == "How much lime for pH 4.9 in Plot 3?"
    assert "1 low phosphorus" in answer["what"] and "low p," not in answer["what"]
    assert answer["how_sure"]["level"] == "medium"  # numbers copied by a model from a document


def test_the_history_card_answers_from_harvests_and_links_the_soil(photo):
    here = drone_cards.Here(plots=_plot_set(), records=_records())
    answer = drone_cards.answer_card("history", _analysis(photo), "farmer", here)
    assert answer["status"] == drone_cards.READY
    assert "Best: 5 (4.3 t/ha); lowest: 3 (1.9 t/ha)" in answer["what"]
    assert "3 also tested acidic, low phosphorus" in answer["what"]
    assert answer["how_sure"]["level"] == "medium"
    assert answer["upload"]["href"] == "/api/layer/Ltest/records"


def test_without_records_the_soil_and_history_cards_offer_to_read_a_document(photo, monkeypatch):
    monkeypatch.setattr(isdasoil_service, "query_soil_point", lambda *a, **k: {"status": "error", "error": "offline"})
    for card in ("soil", "history"):
        answer = drone_cards.answer_card(card, _analysis(photo), "farmer", drone_cards.Here())
        assert answer["upload"]["accept"].startswith(".pdf")


def test_soil_and_history_only_promise_a_test_or_harvests_once_documents_are_added(photo):
    analysis = _analysis(photo)
    for seed in range(1, 9):
        bare = drone_cards.build_deck(analysis, "farmer", drone_cards.Here(seed=seed))
        with_docs = drone_cards.build_deck(analysis, "farmer", drone_cards.Here(seed=seed, records=_records()))
        wordings = {c["id"]: c["question"] for s in bare["services"] for c in s["cards"]}
        doc_wordings = {c["id"]: c["question"] for s in with_docs["services"] for c in s["cards"]}
        assert wordings["soil"] not in ("What does the soil test say?", "What does my lab report show?")
        assert "harvest" not in wordings["history"]
        assert doc_wordings["soil"] in ("What does the soil test say?", "What does my lab report show?")
        assert "harvest" in doc_wordings["history"]


def test_disputed_plots_are_offered_for_a_field_check_first(photo):
    plots = _plot_set()
    survey = _survey(plots, ["maize"] * 6 + ["beans"] * 2 + ["unsure"] * 2)
    disputed = dataclasses.replace(survey.looks[9], candidates=("cassava", "maize"))
    survey = dataclasses.replace(survey, looks={**survey.looks, 9: disputed})
    answer = drone_cards.answer_card("crop_types", _analysis(photo), "farmer", drone_cards.Here(plots=plots, survey=survey))
    check = answer["field_check"]
    assert check["plots"][0]["number"] == 9 and check["plots"][0]["detail"].startswith("Cassava or maize?")
    assert {p["number"] for p in check["plots"][1:]} >= {1, 7}  # the largest plot of each named crop
    assert check["href"].endswith("/plots/{number}/check")
    assert "Not yet checked against what is really in the plots" in answer["how_sure"]["because"]


def test_field_checks_replace_the_model_and_say_how_often_it_was_right(photo):
    from src.services import field_checks

    plots = _plot_set()
    survey = _survey(plots, ["maize"] * 10)
    checks = {n: field_checks.Check(number=n, crop="maize" if n <= 9 else "cassava", at="2026-10-07") for n in range(1, 11)}
    here = drone_cards.Here(plots=plots, survey=field_checks.apply(survey, checks), checked=frozenset(checks),
                            record=field_checks.model_record(survey, checks))
    answer = drone_cards.answer_card("crop_types", _analysis(photo), "farmer", here)
    assert "Checked on the ground: the model was right in 9 of 10 plots" in answer["how_sure"]["because"]
    assert answer["how_sure"]["level"] == "medium"  # 10 checks, 90% right
    badges = {f["properties"]["number"]: f["properties"].get("badge") for f in answer["overlay"]["geojson"]["features"]}
    assert badges[10] == "✓ Cassava" and badges[1] == "✓ Maize"
    assert answer["field_check"] is None  # every plot checked


def test_crop_names_from_checked_plots_say_so():
    """A crop named from the checked plots (crop_fingerprints) is explained that way, not as the vision looks."""
    look = drone_vision.PlotLook(number=1, main_crop="maize", other_crops=[], stage="young", crop_cover_percent=50,
                                 weeds="none", problems=[], note="", confidence="low", source="fingerprints")
    survey = drone_vision.Survey(looks={1: look, 2: dataclasses.replace(look, number=2, main_crop="unsure",
                                                                         candidates=("cassava",))},
                                 plots=2, model="claude-haiku-5-5", done_at="t", cost_usd=0.0)
    sure = drone_cards._vision_how_sure(survey, 1, [], drone_cards.Here(photos=1), crops=True)
    assert "compared with the plots people named" in sure["because"][0]
    assert "too few plots of their crop" in sure["because"][1]
    pictures = drone_cards.Here(photos=1, picture_record=drone_cards.field_checks.Record(right=22, named=26, unsure=8))
    because = drone_cards._vision_how_sure(survey, 1, [], pictures, crops=True)["because"]
    assert any("22 of 26" in b and "not a check in the field" in b for b in because)
    vision = dataclasses.replace(survey, looks={1: dataclasses.replace(look, source="vision")})
    assert "Two separate looks" in drone_cards._vision_how_sure(vision, 0, [], drone_cards.Here(photos=1),
                                                                crops=True)["because"][0]


@pytest.mark.parametrize("model, base_url, name", [
    ("claude-haiku-5-5", "https://api.anthropic.com/v1/", "Claude Haiku 5.5"),
    ("openai/gpt-6-luna", "https://openrouter.ai/api/v1", "GPT-6 Luna"),
    ("some-lab/new-vision-1", "https://example.org/v1", "new-vision-1"),
])
def test_the_vision_model_is_named_from_its_setting(photo, monkeypatch, model, base_url, name):
    """The cards named GPT-6 Luna whatever DRONE_VISION_MODEL said (claude-haiku-5-5 since 2026-10-10)."""
    monkeypatch.setenv("DRONE_VISION_MODEL", model)
    monkeypatch.setenv("OPENAI_BASE_URL", base_url)
    plots = _plot_set()
    here = drone_cards.Here(plots=plots, survey=_survey(plots, ["maize"] * 10))
    for audience in ("farmer", "scientist"):
        answer = drone_cards.answer_card("crop_types", _analysis(photo), audience, here)
        term = next(t for t in answer["terms"] if t["id"] == "vision_model")
        assert name in term["simple"] and "VISION_MODEL_NAME" not in term["simple"]
        if name != "GPT-6 Luna":
            assert "Luna" not in term["simple"] and "OpenRouter" not in term["simple"]
    # how sure names the model that made the looks, by its name
    assert "(GPT-6 Luna)" in answer["how_sure"]["because"][0]


# --- With the vision survey switched off (DRONE_VISION_SURVEY=0) -------------------------------------------------

LOOK_ONLY = ("plot_problems", "plot_stage", "weeds")


def _no_looks(plots, crops=None, checks=None, *, fingerprinted=None, job=None):
    """The looks with the survey off: crops named by fingerprint calls (crops[i] for plot i + 1; "unsure" for none;
    every plot then has a fingerprint) and then by ground checks, as photo_context.plot_looks makes them. Without
    calls, `fingerprinted` (plots with a kept fingerprint; None: none kept) and `job` (the fingerprint job) say
    where the fingerprints stand."""
    from src.services import crop_fingerprints, field_checks

    survey = drone_vision.no_looks(plots)
    if crops:
        calls = {i + 1: crop_fingerprints.CropCall(number=i + 1, crop=c, share=1.0 if c != "unsure" else 0.5,
                                                   candidates=() if c != "unsure" else ("cassava", "maize"),
                                                   neighbours=())
                 for i, c in enumerate(crops)}
        survey = crop_fingerprints.apply(survey, calls)
        fingerprinted = len(crops)
    checks = {n: field_checks.Check(number=n, crop=c, at="2026-10-11") for n, c in (checks or {}).items()}
    return drone_cards.Here(plots=plots, survey=field_checks.apply(survey, checks), vision_looks=False,
                            checked=frozenset(checks),
                            record=field_checks.model_record(survey, checks) if checks else None,
                            fingerprinted=fingerprinted, fingerprint_job=job)


def test_with_the_looks_off_crop_types_are_named_from_the_fingerprints(photo):
    plots = _plot_set()
    here = _no_looks(plots, ["maize"] * 5 + ["beans"] * 3 + ["unsure"] * 2)
    answer = drone_cards.answer_card("crop_types", _analysis(photo), "farmer", here)
    assert answer["status"] == drone_cards.READY  # not "Partly ready" forever
    assert answer["what"].startswith("The crop is named in 8 of 10 plots: maize in 5 plots")
    assert "compared with the plots people named" in answer["how_sure"]["because"][0]
    assert [t["id"] for t in answer["terms"]] == []  # no vision model looked
    labels = [f["properties"]["label"] for f in answer["overlay"]["geojson"]["features"]]
    assert labels[0] == "Plot 1 · Maize · 0.79 ha"  # no stage: none was seen
    assert drone_cards.build_deck(_analysis(photo), "farmer", here)["for_you"]


@pytest.mark.parametrize("plots_found", [True, False])
def test_with_the_looks_off_stage_weeds_and_problems_say_they_cannot_tell(photo, plots_found):
    plots = _plot_set()
    job = background_jobs.Job(state="running", parts_done=2, parts=8, started=0.0)
    here = _no_looks(plots, ["maize"] * 10) if plots_found else drone_cards.Here(plot_job=job, vision_looks=False)
    deck = drone_cards.build_deck(_analysis(photo), "farmer", here)
    cards = {c["id"]: c for s in deck["services"] for c in s["cards"]}
    for card_id in LOOK_ONLY:
        assert cards[card_id]["status"] == drone_cards.CANT_TELL and cards[card_id]["status_label"] == "Can't tell yet"
        assert cards[card_id]["preview"] == "Can't tell yet from this photo."
        answer = drone_cards.answer_card(card_id, _analysis(photo), "farmer", here)
        assert answer["status"] == drone_cards.CANT_TELL
        assert answer["what"].startswith("Can't tell yet from this photo: ")
        assert "turned off" in answer["what"] and "has not looked" not in answer["what"]
        assert answer["how_sure"]["level"] == "low" and answer["items"] == [] and answer["progress"] is None
    if not plots_found:  # the crop card waits for the plots without promising a look
        crop = drone_cards.answer_card("crop_types", _analysis(photo), "farmer", here)
        assert crop["status"] == drone_cards.WORKING and "looked at" not in crop["what"]
        assert crop["terms"] == []


def _fingerprint_job(state, parts_done=3, parts=10):
    """The fingerprint job as background_jobs keeps it: running a minute so far, or failed as ensure_model fails."""
    return background_jobs.Job(state=state, parts_done=parts_done, parts=parts, started=time.time() - 60,
                               error=None if state == "running" else
                               "the fingerprint model in storage (models/x.pt) does not match its sha256")


def test_with_the_looks_off_while_the_fingerprints_are_made_only_checked_plots_are_named(photo):
    plots = _plot_set()
    for f in plots.geojson["features"]:
        p = f["properties"]
        if p["number"] == 3:  # the largest plot, but mostly soil showing
            p["group"], p["area_ha"] = drone_plots.MOSTLY_SOIL, 2.0
        if p["number"] == 10:
            p["area_ha"] = 1.5
    here = _no_looks(plots, checks={1: "maize", 2: "beans"}, job=_fingerprint_job("running"))
    answer = drone_cards.answer_card("crop_types", _analysis(photo), "farmer", here)
    assert answer["status"] == drone_cards.WORKING
    assert answer["what"].startswith("The crop is named in 2 of 10 plots: maize in 1 plot")
    assert answer["how_sure"]["level"] == "high"
    assert answer["how_sure"]["because"][0] == "Named only in the 2 plots checked on the ground"
    check = answer["field_check"]  # the plots to name next: unchecked ones, plots with a crop first
    assert len(check["plots"]) == drone_cards.FIELD_CHECK_PLOTS and {1, 2}.isdisjoint(p["number"] for p in check["plots"])
    assert check["plots"][0]["number"] == 10 and check["plots"][-1]["number"] == 3  # mostly soil: offered last
    assert all(p["detail"] == "Not named yet." for p in check["plots"]) and "3 plots of a crop" in check["detail"]


def test_with_the_looks_off_and_nothing_named_the_crop_card_asks_for_names(photo):
    plots = _plot_set()
    here = _no_looks(plots, fingerprinted=10)  # every plot fingerprinted, none checked: nothing to name them from
    answer = drone_cards.answer_card("crop_types", _analysis(photo), "farmer", here)
    assert answer["status"] == drone_cards.READY and answer["progress"] is None
    assert answer["what"] == ("No crop is named yet in the 10 plots: Ingabe names a plot's crop from the plots people "
                              "check on the ground, and then the plots that look like them.")
    assert answer["how_sure"]["level"] == "low" and answer["how_sure"]["because"][0] == "No plot is checked on the ground yet"
    assert answer["field_check"]["plots"]
    deck = drone_cards.build_deck(_analysis(photo), "farmer", here)
    preview = next(c for s in deck["services"] for c in s["cards"] if c["id"] == "crop_types")["preview"]
    assert preview == "No crop named yet: say what grows in a few plots."


PROMISES = ("look like them", "look alike")  # the promise that plots like the named ones get named


def _crop_card(photo, here):
    answer = drone_cards.answer_card("crop_types", _analysis(photo), "farmer", here)
    card = next(c for s in drone_cards.build_deck(_analysis(photo), "farmer", here)["services"] for c in s["cards"]
                if c["id"] == "crop_types")
    promised = json.dumps([answer["what"], answer["todo"], answer["how_sure"], answer["field_check"]])
    return answer, card, any(words in promised for words in PROMISES)


def test_with_the_looks_off_the_crop_card_shows_the_fingerprints_being_made(photo):
    here = _no_looks(_plot_set(), checks={1: "maize", 2: "beans"}, job=_fingerprint_job("running"))
    answer, card, promised = _crop_card(photo, here)
    words = "Ingabe is comparing how each plot looks (3 of 10 plots done, about 2 minutes left)."
    assert answer["status"] == card["status"] == drone_cards.WORKING  # the deck and the answer refresh themselves
    assert card["preview"] == words and answer["what"].endswith(" " + words)
    assert answer["progress"] == {"done": 3, "parts": 10, "minutes_left": 2}
    assert promised and answer["how_sure"]["because"][1].startswith("The other plots are named once 3 plots")
    assert not answer["todo"].startswith("Open this question again")


def test_with_the_looks_off_the_crop_card_says_the_fingerprints_just_started(photo):
    here = _no_looks(_plot_set(), checks={1: "maize"}, job=_fingerprint_job("running", parts_done=0, parts=1))
    answer, card, promised = _crop_card(photo, here)
    assert card["preview"] == "Ingabe is comparing how each plot looks. It takes a few minutes."
    assert answer["status"] == drone_cards.WORKING and promised and "0 of 1" not in answer["what"]


def test_with_the_looks_off_a_failed_fingerprint_job_is_said_plainly_and_nothing_is_promised(photo):
    here = _no_looks(_plot_set(), checks={1: "maize", 2: "beans"}, job=_fingerprint_job("failed"))
    answer, card, promised = _crop_card(photo, here)
    failed = "Naming plots from their look did not work this time."
    assert answer["status"] == card["status"] == drone_cards.PARTLY  # never "Answer ready" with naming broken
    assert card["preview"] == failed and answer["what"].endswith(" " + failed)
    assert answer["what"].startswith("The crop is named in 2 of 10 plots")  # the checked plots still show
    assert not promised
    assert answer["how_sure"]["because"][:2] == ["Named only in the 2 plots checked on the ground", failed.rstrip(".")]
    assert answer["todo"] == "Open this question again to try once more. Walk to the plots listed below and tap what you find."
    assert answer["progress"] is None
    assert answer["field_check"]["detail"] == "Tap the crop you find in each plot."
    assert answer["field_check"]["plots"]  # people can still name plots


def test_with_the_looks_off_and_nothing_named_a_failed_job_is_not_hidden(photo):
    here = _no_looks(_plot_set(), job=_fingerprint_job("failed"))
    answer, _, promised = _crop_card(photo, here)
    assert answer["what"] == ("No crop is named yet in the 10 plots: Ingabe names a plot's crop from the plots people "
                              "check on the ground. Naming plots from their look did not work this time.")
    assert not promised and answer["status"] == drone_cards.PARTLY


@pytest.mark.parametrize("fingerprinted, words", [
    (None, "Ingabe has not compared how the plots look yet."),  # Sage reads the cards without starting jobs
    (0, "No plot holds a whole 4.5 m square, so none can be compared by how it looks."),
])
def test_with_the_looks_off_and_no_fingerprints_to_come_nothing_is_promised(photo, fingerprinted, words):
    here = _no_looks(_plot_set(), checks={1: "maize", 2: "maize"}, fingerprinted=fingerprinted)
    answer, card, promised = _crop_card(photo, here)
    assert answer["status"] == card["status"] == drone_cards.PARTLY
    assert answer["what"].endswith(" " + words) and not promised and answer["progress"] is None
    assert card["preview"] == "Mostly maize (2 plots)."  # no job to report on: what is named
    assert not answer["todo"].startswith("Open this question again")


def test_with_the_looks_off_and_the_fingerprints_kept_the_card_is_ready_without_job_words(photo):
    here = _no_looks(_plot_set(), ["maize"] * 5 + ["unsure"] * 5, checks={1: "maize"})
    answer, card, _ = _crop_card(photo, here)
    assert answer["status"] == card["status"] == drone_cards.READY and answer["progress"] is None
    assert not any(w in answer["what"] for w in ("comparing", "did not work", "has not compared"))


def test_with_the_looks_on_the_fingerprint_job_changes_nothing(photo):
    plots = _plot_set()
    here = drone_cards.Here(plots=plots, survey=_survey(plots, ["maize"] * 6 + ["unsure"] * 4))
    analysis = _analysis(photo)
    for job in (_fingerprint_job("failed"), _fingerprint_job("running")):
        with_job = dataclasses.replace(here, fingerprint_job=job)
        for card_id in ("crop_types",) + LOOK_ONLY:
            assert drone_cards.answer_card(card_id, analysis, "farmer", with_job) == \
                drone_cards.answer_card(card_id, analysis, "farmer", here)
        assert drone_cards.build_deck(analysis, "farmer", with_job) == drone_cards.build_deck(analysis, "farmer", here)


def test_with_the_looks_on_nothing_changes_for_the_stage_weeds_and_problems_cards(photo):
    plots = _plot_set()
    here = drone_cards.Here(plots=plots, survey=_survey(plots, ["maize"] * 10, {2: {"weeds": "many"}}))
    assert here.vision_looks is True
    assert [drone_cards.answer_card(c, _analysis(photo), "farmer", here)["status"] for c in LOOK_ONLY] == \
        [drone_cards.READY] * 3


@pytest.mark.parametrize("audience", ["farmer", "insurer", "agronomist", "scientist"])
def test_no_card_talks_about_money_with_the_looks_off(photo, audience):
    plots = _plot_set()
    here = _no_looks(plots, ["maize"] * 6 + ["unsure"] * 4, checks={1: "maize"})
    answers = [drone_cards.answer_card(c, _analysis(photo), audience, here) for c in ("crop_types",) + LOOK_ONLY]
    text = json.dumps([{k: a[k] for k in ("what", "why", "todo", "items", "facts")} for a in answers])
    assert not MONEY.search(text)


# --- Each role its own questions; the season, what changed and a new set each day (Roger, 2026-10-25) --------------

def _for_you(photo, role, here=None):
    return drone_cards.build_deck(_analysis(photo), role, here or drone_cards.Here(photos=1))


def test_each_role_is_asked_its_own_questions(photo):
    farmer = {c["id"]: c["question"] for s in _for_you(photo, "farmer")["services"] for c in s["cards"]}
    insurer = {c["id"]: c["question"] for s in _for_you(photo, "insurer")["services"] for c in s["cards"]}
    government = _for_you(photo, "government")
    assert farmer["bare_ground"] == "Which of my fields are still empty?"
    assert insurer["bare_ground"] == "Is this land planted, or still bare?"
    assert {c["id"]: c["question"] for s in government["services"] for c in s["cards"]}["bare_ground"] == \
        "How much of this area is planted this season?"
    assert (government["audience"], government["audience_label"]) == ("government", "Government / district")
    assert [o["id"] for o in government["audiences"]] == [
        "farmer", "cooperative", "agronomist", "insurance", "scientist", "government", "buyer"]
    # A role with no words of its own for a card is asked the card's question.
    assert {c["id"]: c["question"] for s in _for_you(photo, "buyer")["services"] for c in s["cards"]}["weeds"] == \
        "Which plots need weeding first?"
    # A role from another industry reads as the default.
    assert _for_you(photo, "technician")["audience"] == "agronomist"


def test_every_role_wording_belongs_to_a_real_card():
    assert set(drone_cards._ROLE_WORDS) <= set(drone_cards.reader_roles.AGRICULTURE)
    for words in drone_cards._ROLE_WORDS.values():
        assert set(words) <= set(drone_cards._CARD_BY_ID)


def test_an_answer_keeps_the_roles_question_and_its_voice(photo):
    analysis = _analysis(photo)
    coop = drone_cards.answer_card("bare_ground", analysis, "cooperative", drone_cards.Here(photos=1))
    farmer = drone_cards.answer_card("bare_ground", analysis, "farmer", drone_cards.Here(photos=1))
    assert coop["question"] in ("Which plots are not planted yet?", "How much of our land is still bare?")
    assert coop["what"] == farmer["what"]  # a cooperative manager reads the farmer's plain words


def test_the_season_leads_with_what_matters_now(photo):
    planting = [c["id"] for c in _for_you(photo, "farmer", drone_cards.Here(photos=1, moment="planting"))["for_you"]]
    growing = [c["id"] for c in _for_you(photo, "farmer", drone_cards.Here(photos=1, moment="growing"))["for_you"]]
    assert planting != growing
    assert planting.index("bare_ground") < growing.index("bare_ground") if "bare_ground" in growing else True


def test_a_card_that_changed_comes_first_and_one_opened_today_gives_way(photo):
    usual = _for_you(photo, "farmer")["for_you"]
    second = usual[1]["id"]
    fresh = _for_you(photo, "farmer", drone_cards.Here(photos=1, changed=frozenset({second})))["for_you"]
    assert fresh[0]["id"] == second and fresh[0]["new"] is True
    opened = _for_you(photo, "farmer", drone_cards.Here(photos=1, seen=frozenset({usual[0]["id"]})))["for_you"]
    assert opened[0]["id"] != usual[0]["id"]


def test_the_days_set_is_the_same_all_day_and_new_the_next():
    from datetime import date

    from src.services import card_memory

    monday, tuesday = date(2026, 10, 26), date(2026, 10, 27)
    assert card_memory.daily_seed("u1", "L1", monday) == card_memory.daily_seed("u1", "L1", monday)
    assert card_memory.daily_seed("u1", "L1", monday) != card_memory.daily_seed("u1", "L1", tuesday)
    assert card_memory.daily_seed("u1", "L1", monday) != card_memory.daily_seed("u2", "L1", monday)
    assert all(card_memory.daily_seed("u", f"L{i}", monday) > 0 for i in range(50))


def test_only_answers_that_became_ready_count_as_changed():
    from src.services import card_memory

    before = {"a": "working", "b": "ready", "c": "to_build", "d": "partly", "f": "ready", "g": "cant_tell"}
    now = {"a": "ready", "b": "ready", "c": "to_build", "d": "ready", "e": "ready", "f": "partly", "g": "working"}
    # "e" is new to the deck, not changed; "f" got worse; "g" improved but cannot be read yet
    assert card_memory.changed(before, now) == {"a", "d"}
    assert card_memory.changed({}, now) == frozenset()  # a first look marks nothing new


class _FakeRedis:
    def __init__(self):
        self.hash: dict[str, str] = {}

    async def hsetnx(self, key, field, value):
        self.hash.setdefault(field, value)

    async def hset(self, key, mapping):
        self.hash.update(mapping)

    async def expire(self, key, seconds):
        pass

    async def hgetall(self, key):
        return dict(self.hash)


def test_a_new_mark_stays_until_the_reader_opens_the_card():
    import asyncio

    from src.services import card_memory

    redis = _FakeRedis()
    asyncio.run(card_memory.remember_deck(redis, "u", "L", {"a": "working"}))
    asyncio.run(card_memory.remember_deck(redis, "u", "L", {"a": "ready"}))  # another look does not move the baseline
    _, before = asyncio.run(card_memory.recall(redis, "u", "L"))
    assert card_memory.changed(before, {"a": "ready"}) == {"a"}
    asyncio.run(card_memory.remember_opened(redis, "u", "L", "a", "ready"))
    seen, before = asyncio.run(card_memory.recall(redis, "u", "L"))
    assert "a" in seen and card_memory.changed(before, {"a": "ready"}) == frozenset()


def test_a_role_or_the_season_never_lifts_a_question_ingabe_cannot_answer_yet(photo):
    analysis = _analysis(photo)
    card = {"id": "yield", "status": drone_cards.TO_BUILD}
    assert drone_cards._READER_BOOST["buyer"]["yield"] and drone_cards._SEASON_BOOST["harvest"]["yield"]
    assert (drone_cards._score(card, analysis, "buyer", drone_cards.Here(moment="harvest"))
            == drone_cards._score(card, analysis, "farmer", drone_cards.Here()))
    ready = {"id": "yield", "status": drone_cards.READY}
    assert (drone_cards._score(ready, analysis, "buyer", drone_cards.Here(moment="harvest"))
            > drone_cards._score(ready, analysis, "farmer", drone_cards.Here()))


def test_season_moments_follow_the_calendar():
    from datetime import datetime

    from src.services import crop_calendar

    moments = {d: crop_calendar.season_moment(datetime.fromisoformat(d)) for d in
               ("2026-09-20", "2026-10-25", "2027-01-05", "2027-02-20", "2027-07-25")}
    assert moments == {"2026-09-20": "planting", "2026-10-25": "growing", "2027-01-05": "harvest",
                       "2027-02-20": "planting", "2027-07-25": "between"}


def test_roles_read_in_their_voices():
    from src.services import insurance_engine, reader_roles

    assert reader_roles.for_industry("telecom", "farmer") == "owner"
    assert reader_roles.for_industry("agriculture", "Farm manager") == "cooperative"
    assert [reader_roles.voice(r) for r in ("cooperative", "government", "buyer", "manager")] == \
        ["farmer", "insurance", "farmer", "owner"]
    assert insurance_engine.normalize_audience("cooperative") == "farmer"  # reports keep their four voices
    assert insurance_engine.normalize_audience("technician") == insurance_engine.DEFAULT_AUDIENCE
