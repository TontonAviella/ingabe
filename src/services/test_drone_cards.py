"""Drone question cards on a synthetic photo: a green field with one bare square of known size."""

from __future__ import annotations

import json
import re

import numpy as np
import pytest
import rasterio
from rasterio.transform import from_origin

from src.services import drone_cards, drone_first_look, isdasoil_service, wapor_service

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
    deck = drone_cards.build_deck(_analysis(photo), "farmer", photos_here=1)
    assert [s["service"] for s in deck["services"]] == list(range(1, 12))
    assert all(s["cards"] for s in deck["services"])
    assert len(deck["for_you"]) == drone_cards.FOR_YOU_COUNT
    assert deck["for_you"][-1]["status"] == drone_cards.LEARN
    assert deck["photo"]["camera"] == "colour"


def test_ready_answers_come_first(photo):
    deck = drone_cards.build_deck(_analysis(photo), "farmer", photos_here=1)
    statuses = [card["status"] for card in deck["for_you"][:-1]]
    assert statuses[:2] == [drone_cards.READY, drone_cards.READY]


def test_insurer_sees_planted_or_never_planted(photo):
    analysis = _analysis(photo)
    insurer = drone_cards.answer_card("bare_ground", analysis, "insurer", photos_here=1)
    farmer = drone_cards.answer_card("bare_ground", analysis, "farmer", photos_here=1)
    assert "never planted" in insurer["why"]
    assert insurer["why"] != farmer["why"]
    assert insurer["overlay"]["kind"] == "bare"
    assert len(insurer["overlay"]["geojson"]["features"]) == 1


def test_fertilizer_asks_for_a_special_camera_on_a_colour_photo(photo):
    analysis = _analysis(photo)
    assert drone_cards.answer_card("fertilizer", analysis, "agronomist", 1)["status"] == drone_cards.NEEDS_CAMERA
    multispectral = _analysis(photo, band_count=5)
    assert drone_cards.answer_card("fertilizer", multispectral, "agronomist", 1)["status"] == drone_cards.TO_BUILD


def test_growth_needs_a_second_flight_until_there_is_one(photo):
    analysis = _analysis(photo)
    assert drone_cards.answer_card("growth", analysis, "farmer", 1)["status"] == drone_cards.NEEDS_FLIGHT
    assert drone_cards.answer_card("growth", analysis, "farmer", 2)["status"] == drone_cards.TO_BUILD


def test_missing_water_data_is_said_not_shown_as_zero(photo, monkeypatch):
    monkeypatch.setattr(wapor_service, "query_et", lambda *a, **k: {"status": "error", "error": "no data"})
    answer = drone_cards.answer_card("water", _analysis(photo), "farmer", 1)
    assert answer["facts"] == []
    assert "no satellite reading" in answer["what"]


def test_soil_values_without_a_false_spread(photo, monkeypatch):
    monkeypatch.setattr(isdasoil_service, "query_soil_point", lambda *a, **k: {"status": "success", "properties": {
        "nitrogen_total": {"value": 1.44, "uncertainty": 0.13, "unit": "g/kg", "label": "Total Nitrogen"},
        "ph": {"value": 5.81, "uncertainty": 0.1, "unit": "", "label": "Soil pH"},
    }})
    answer = drone_cards.answer_card("soil", _analysis(photo), "farmer", 1)
    assert {"label": "Total Nitrogen", "value": "1.44 g/kg"} in answer["facts"]
    assert "±" not in json.dumps(answer["facts"])  # the service's spread is not a real range yet
    assert answer["how_sure"]["level"] == "low"


def test_unknown_card(photo):
    with pytest.raises(KeyError):
        drone_cards.answer_card("nonsense", _analysis(photo), "farmer", 1)


MONEY = re.compile(r"\b(RWF|FRW|USD|dollars?|francs?|money|price|cost|costs|profit|revenue|savings?)\b|\$", re.I)


@pytest.mark.parametrize("audience", ["farmer", "insurer", "agronomist", "scientist"])
def test_no_card_talks_about_money(photo, monkeypatch, audience):
    monkeypatch.setattr(isdasoil_service, "query_soil_point", lambda *a, **k: {"status": "error", "error": "offline"})
    monkeypatch.setattr(wapor_service, "query_et", lambda *a, **k: {"status": "error", "error": "offline"})
    analysis = _analysis(photo)
    deck = drone_cards.build_deck(analysis, audience, 1)
    answers = [drone_cards.answer_card(card["id"], analysis, audience, 1)
               for service in deck["services"] for card in service["cards"]]
    text = json.dumps([deck["for_you"], [{k: a[k] for k in ("what", "why", "todo", "terms")} for a in answers]])
    assert not MONEY.search(text)
