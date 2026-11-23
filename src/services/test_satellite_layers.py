"""The satellite view as two layers of the map: their colours, legends and dates, and one layer of each kind a photo."""

from __future__ import annotations

from datetime import date, timedelta
from typing import Any

import numpy as np
import pytest

from src.services import ndvi_classes, satellite_layers as sl, satellite_view as sv
from src.services.h3_layer_persistence import SavedLayer

TODAY = date(2026, 11, 23)
BOX = (30.41749, -1.7009, 30.43148, -1.69289)


def _plants(old_part: bool = True) -> sv.Plants:
    ndvi = np.full((4, 4), 0.55, dtype=np.float32)
    seen = np.full((4, 4), (TODAY - timedelta(days=4)).toordinal(), dtype=np.int32)
    if old_part:
        seen[:, 3] = (TODAY - timedelta(days=31)).toordinal()
    return sv.Plants(sv.Grid("EPSG:32736", (10.0, 0.0, 212650.0, 0.0, -10.0, 9812700.0), (4, 4)), ndvi, seen,
                     np.zeros((4, 4), bool), None, 3)


def _soil(first: bool = True) -> sv.Soil:
    return sv.Soil(period="2026-11-D1", percent=np.array([[62.0, 68.3]], dtype=np.float32), west=30.4169922,
                   north=-1.6923828, step=0.0009765625, first_estimate=first, centre=69.0, year_low=40.0, year_high=75.0)


def _view(plants: Any = "yes", soil: Any = "yes") -> sv.View:
    return sv.View(bbox=BOX, today=TODAY, plants=_plants() if plants == "yes" else plants, plants_missing=None,
                   soil=_soil() if soil == "yes" else soil, soil_missing=None)


def test_the_plants_layer_uses_the_shared_ndvi_classes_fades_older_squares_and_dates_its_legend():
    legend = sl.plants_legend(_plants(), 10)
    assert legend["title"] == "Plants from the satellite (NDVI), 10 m squares, seen 19 Nov 2026"
    assert [item["label"] for item in legend["items"]] == [
        "Bare soil", "Sparse vegetation", "Cropland or moderate vegetation", "Dense vegetation",
        "Faded: an older look (tap a square for its day)"]
    assert [item["color"] for item in legend["items"][:4]] == [c.color for c in ndvi_classes.NDVI_CLASSES]
    assert len(sl.plants_legend(_plants(old_part=False), 10)["items"]) == 4  # nothing faded: no such line
    (fill,) = sl.plants_style("L1", legend)
    assert fill["source"] == "L1" and fill["metadata"]["mundi:legend"] == legend
    assert fill["paint"]["fill-color"] == ["step", ["coalesce", ["get", "ndvi"], -1], "#d73027", 0.1, "#fc8d59", 0.3,
                                           "#a6d96a", 0.6, "#1a9850"]
    assert fill["paint"]["fill-opacity"] == ["case", ["==", ["get", "faded"], True], 0.3, 0.72]


def test_the_soil_layer_is_blue_bands_with_outlined_squares_their_reading_and_its_ten_days_in_the_legend():
    legend = sl.soil_legend(_soil())
    assert legend["title"] == "Soil moisture, 1-10 Nov 2026 (FAO estimate, 100 m squares), first estimate"
    assert [item["label"] for item in legend["items"]] == ["Below 45%", "45-55%", "55-65%", "65-75%", "75% and above"]
    assert sl.soil_legend(_soil(first=False))["title"] == "Soil moisture, 1-10 Nov 2026 (FAO estimate, 100 m squares)"
    fill, outline, label = sl.soil_style("L2", legend)
    assert outline["type"] == "line" and label["type"] == "symbol"
    assert fill["paint"]["fill-color"][:3] == ["step", ["coalesce", ["get", "moisture"], 0], "#eff3ff"]
    assert fill["paint"]["fill-opacity"] == 0.3  # the plants underneath keep their greens
    assert label["minzoom"] == 15  # readings appear once a square is wide enough to hold one
    assert label["layout"]["text-field"] == ["concat", ["to-string", ["round", ["get", "moisture"]]], "%"]
    assert {layer["source"] for layer in (fill, outline, label)} == {"L2"}


def test_the_stamps_change_only_when_the_satellites_have_something_newer():
    assert sl.plants_stamp(_plants()) == "2026-10-23,2026-11-19"
    assert sl.plants_stamp(_plants(old_part=False)) == "2026-11-19"
    assert sl.soil_stamp(_soil()) == "2026-11-D1:first" and sl.soil_stamp(_soil(first=False)) == "2026-11-D1:final"


@pytest.fixture
def saving(monkeypatch):
    """What is already on the map, and every layer saved."""
    there: dict[str, tuple[str, str, str, Any]] = {}
    saved: list[dict[str, Any]] = []

    async def already(map_id, photo_layer_id):
        return there

    async def save(**kwargs):
        saved.append(kwargs)
        return SavedLayer(layer_id=f"Lnew{len(saved)}", style_id="S1", pmtiles_key="k.pmtiles", geoparquet_key=None,
                          bounds=list(BOX), feature_count=len(kwargs["feature_collection"]["features"]), geometry_type="polygon")

    monkeypatch.setattr(sl, "_already_on_map", already)
    monkeypatch.setattr(sl, "save_analysis_layer", save)
    return there, saved


async def _put(view: sv.View) -> list[sl.OnMap]:
    return await sl.put_on_map(view, photo_layer_id="Lphoto", photo_name="Cyampirita", user_uuid="U1", map_id="M1", project_id="P1")


@pytest.mark.anyio
async def test_two_layers_are_saved_each_with_its_own_name_kind_and_stamp(saving):
    _, saved = saving
    layers = await _put(_view())
    assert [(layer.name, layer.kind, layer.reused) for layer in layers] == [
        ("Soil moisture - Cyampirita", sl.SOIL_KIND, False), ("Plants (NDVI) - Cyampirita", sl.PLANTS_KIND, False)]
    soil, plants = saved
    assert soil["metadata"] == {"source": "satellite_view", "analysis_kind": sl.SOIL_KIND, "source_layer_id": "Lphoto",
                                "data_stamp": "2026-11-D1:first", "square_m": 100, "period": "2026-11-D1"}
    assert plants["metadata"]["analysis_kind"] == sl.PLANTS_KIND and plants["metadata"]["data_stamp"] == "2026-10-23,2026-11-19"
    assert len(plants["feature_collection"]["features"]) == 16 and len(soil["feature_collection"]["features"]) == 2
    assert plants["replaces"] == () and soil["replaces"] == ()
    assert plants["style_for"]("Lx")[0]["id"] == "satellite-plants-Lx" and len(soil["style_for"]("Ly")) == 3


@pytest.mark.anyio
async def test_asked_again_with_nothing_newer_the_layers_already_there_are_used(saving):
    there, saved = saving
    there[sl.PLANTS_KIND] = ("Lold1", "Plants (NDVI) - Cyampirita", "2026-10-23,2026-11-19", list(BOX))
    there[sl.SOIL_KIND] = ("Lold2", "Soil moisture - Cyampirita", "2026-11-D1:first", list(BOX))
    layers = await _put(_view())
    assert saved == [] and [(layer.layer_id, layer.reused) for layer in layers] == [("Lold2", True), ("Lold1", True)]


@pytest.mark.anyio
async def test_a_newer_picture_takes_the_old_layers_place_on_the_map(saving):
    there, saved = saving
    there[sl.PLANTS_KIND] = ("Lold1", "Plants (NDVI) - Cyampirita", "2026-10-23", list(BOX))
    there[sl.SOIL_KIND] = ("Lold2", "Soil moisture - Cyampirita", "2026-11-D1:first", list(BOX))
    layers = await _put(_view())
    assert len(saved) == 1 and saved[0]["replaces"] == ("Lold1",)
    assert [(layer.kind, layer.reused) for layer in layers] == [(sl.SOIL_KIND, True), (sl.PLANTS_KIND, False)]


@pytest.mark.anyio
async def test_a_part_the_view_lacks_gets_no_layer_and_a_layer_that_cannot_be_saved_is_left_out(saving, monkeypatch):
    _, saved = saving
    layers = await _put(_view(soil=None))
    assert [layer.kind for layer in layers] == [sl.PLANTS_KIND]

    async def broken(**kwargs):
        raise RuntimeError("tippecanoe failed")

    monkeypatch.setattr(sl, "save_analysis_layer", broken)
    assert await _put(_view()) == []
