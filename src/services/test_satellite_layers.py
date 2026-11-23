"""The satellite view as two layers of hexagons on the map: their sizes by zoom, colours, legends and dates, and one
layer of each kind a photo."""

from __future__ import annotations

from datetime import date, timedelta
from typing import Any

import numpy as np
import pytest
from rasterio.warp import transform_bounds

from src.services import ndvi_classes, satellite_layers as sl, satellite_view as sv
from src.services.h3_layer_persistence import SavedLayer

TODAY = date(2026, 11, 23)
BOX = (30.41749, -1.7009, 30.43148, -1.69289)


def _plants(old_part: bool = True) -> sv.Plants:
    ndvi = np.full((4, 4), 0.55, dtype=np.float32)
    seen = np.full((4, 4), (TODAY - timedelta(days=4)).toordinal(), dtype=np.int32)
    if old_part:
        seen[:, 3] = (TODAY - timedelta(days=31)).toordinal()
    west, _, _, north = transform_bounds("EPSG:4326", "EPSG:32736", *BOX)
    return sv.Plants(sv.Grid("EPSG:32736", (10.0, 0.0, west, 0.0, -10.0, north), (4, 4)), ndvi, seen,
                     np.zeros((4, 4), bool), None, 3)


def _soil(first: bool = True) -> sv.Soil:
    return sv.Soil(period="2026-11-D1", percent=np.array([[62.0, 68.3]], dtype=np.float32), west=30.4169922,
                   north=-1.6923828, step=0.0009765625, first_estimate=first, centre=69.0, year_low=40.0, year_high=75.0)


def _view(plants: Any = "yes", soil: Any = "yes") -> sv.View:
    return sv.View(bbox=BOX, today=TODAY, plants=_plants() if plants == "yes" else plants, plants_missing=None,
                   soil=_soil() if soil == "yes" else soil, soil_missing=None)


PLANTS_BANDS = [{"h3_resolution": 10, "minzoom": 0.0, "maxzoom": 15.0}, {"h3_resolution": 11, "minzoom": 15.0, "maxzoom": 16.5},
                {"h3_resolution": 12, "minzoom": 16.5, "maxzoom": 24.0}]
SOIL_BANDS = [{"h3_resolution": 9, "minzoom": 0.0, "maxzoom": 16.5}, {"h3_resolution": 10, "minzoom": 16.5, "maxzoom": 24.0}]


def test_a_smaller_hexagon_takes_over_at_the_zoom_where_it_is_wide_enough_on_the_screen():
    # at 1.7 degrees south a 50 m hexagon is 20 pixels wide at zoom 14.9 and a 19 m one at zoom 16.4
    assert sl.zoom_bands([10, 11, 12], -1.7, sl.PLANTS_PIXELS_ACROSS) == PLANTS_BANDS
    # soil moisture switches later, so its hexagons are always the wider ones
    assert sl.zoom_bands([9, 10], -1.7, sl.SOIL_PIXELS_ACROSS) == SOIL_BANDS
    assert sl.zoom_bands([12], -1.7, 20) == [{"h3_resolution": 12, "minzoom": 0.0, "maxzoom": 24.0}]
    assert sl.zoom_bands([], -1.7, 20) == []
    # sizes that would switch at the same half zoom still get a band each
    starts = [band["minzoom"] for band in sl.zoom_bands([10, 11, 12], -1.7, 2000)]
    assert starts == sorted(set(starts))


def test_the_plants_layer_is_solid_hexagons_in_the_shared_shades_with_older_looks_in_grey_and_a_dated_legend():
    legend = sl.plants_legend(_plants())
    assert legend["title"] == "Plants from the satellite (NDVI), seen 19 Nov 2026"
    assert [item["label"] for item in legend["items"]] == [
        "Below 0.1: bare soil", "0.1-0.2: sparse vegetation", "0.2-0.3", "0.3-0.4: cropland or moderate vegetation",
        "0.4-0.5", "0.5-0.6", "0.6-0.7: dense vegetation", "0.7 and above",
        "Grey: an older look, darker where greener (tap it for its day)"]
    assert [item["color"] for item in legend["items"][:8]] == [shade for _, shade in ndvi_classes.NDVI_SHADES]
    assert len(sl.plants_legend(_plants(old_part=False))["items"]) == 8  # no older look: no such line
    layers = sl.plants_style("L1", legend, PLANTS_BANDS)
    assert [layer["id"] for layer in layers] == [
        "satellite-plants-L1-r10", "satellite-plants-edge-L1-r10", "satellite-plants-L1-r11", "satellite-plants-edge-L1-r11",
        "satellite-plants-L1-r12", "satellite-plants-edge-L1-r12"]
    wide, _, middle, _, small, _ = layers
    # one size is drawn at a time
    assert "minzoom" not in wide and wide["maxzoom"] == 15.0
    assert (middle["minzoom"], middle["maxzoom"]) == (15.0, 16.5)
    assert small["minzoom"] == 16.5 and "maxzoom" not in small
    assert small["filter"] == ["any", ["==", ["get", "h3_resolution"], 12], ["==", ["get", "h3_resolution"], "12"]]
    assert wide["source"] == "L1" and wide["metadata"]["mundi:legend"] == legend
    case, is_older, grey, fresh = wide["paint"]["fill-color"]
    assert case == "case" and is_older == ["==", ["get", "older"], True]
    assert grey[0] == "interpolate" and grey[-4:] == [0, "#f3f4f6", 0.8, "#4b5563"]
    assert fresh == ["step", ["coalesce", ["get", "ndvi"], -1], "#d73027", 0.1, "#f46d43", 0.2, "#fdae61", 0.3, "#d9ef8b",
                     0.4, "#a6d96a", 0.5, "#66bd63", 0.6, "#1a9850", 0.7, "#006837"]
    # nearly solid from afar, so a colour on the map is a colour in the legend
    assert wide["paint"]["fill-opacity"] == ["interpolate", ["linear"], ["zoom"], 15, 0.88, 18, 0.62]


def test_every_shade_sits_inside_one_class_so_the_finer_colours_never_contradict_the_words():
    lows = [low for low, _ in ndvi_classes.NDVI_SHADES]
    assert lows == sorted(lows) and {cls.min_ndvi for cls in ndvi_classes.NDVI_CLASSES} <= set(lows)
    for cls in ndvi_classes.NDVI_CLASSES:
        assert dict(ndvi_classes.NDVI_SHADES)[cls.min_ndvi]  # a class begins on a shade


def test_the_soil_layer_is_a_rim_and_a_number_with_no_fill_so_a_tap_inside_reaches_the_plants():
    legend = sl.soil_legend(_soil())
    assert legend["title"] == "Soil moisture, 1-10 Nov 2026 (FAO estimate from 100 m readings), first estimate"
    assert [item["label"] for item in legend["items"]] == ["Below 45%", "45-55%", "55-65%", "65-75%", "75% and above"]
    assert sl.soil_legend(_soil(first=False))["title"] == "Soil moisture, 1-10 Nov 2026 (FAO estimate from 100 m readings)"
    layers = sl.soil_style("L2", legend, SOIL_BANDS)
    # nothing is filled. The one fill matches no hexagon: it is there so the map does not add an unseen fill of its
    # own to a layer of lines, which would catch every tap meant for the plants underneath.
    assert [layer["type"] for layer in layers] == ["fill"] + ["line", "line", "symbol"] * 2
    assert layers[0]["filter"] == ["==", ["get", "h3_resolution"], -1] and layers[0]["paint"] == {"fill-opacity": 0}
    layers = layers[1:]
    under, rim, reading = layers[:3]
    assert under["paint"]["line-width"] > rim["paint"]["line-width"]
    assert rim["paint"]["line-color"][:3] == ["step", ["coalesce", ["get", "moisture"], 0], "#eff3ff"]
    assert rim["metadata"]["mundi:legend"] == legend
    assert reading["layout"]["text-field"] == ["get", "reading"]
    assert reading["filter"] == ["all", sl.h3_resolution_filter(9), ["has", "reading"]]  # the points, not the outlines
    assert rim["maxzoom"] == 16.5 and layers[4]["minzoom"] == 16.5
    assert {layer["source"] for layer in layers} == {"L2"}


def test_the_stamps_change_when_the_satellites_have_something_newer_or_the_drawing_changes():
    assert sl.plants_stamp(_plants()) == "hexagons:2026-10-23,2026-11-19"
    assert sl.plants_stamp(_plants(old_part=False)) == "hexagons:2026-11-19"
    assert sl.soil_stamp(_soil()) == "hexagons:2026-11-D1:first" and sl.soil_stamp(_soil(first=False)) == "hexagons:2026-11-D1:final"


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
async def test_two_layers_are_saved_plants_first_so_soil_moisture_is_drawn_over_them(saving):
    _, saved = saving
    layers = await _put(_view())
    assert [(layer.name, layer.kind, layer.reused) for layer in layers] == [
        ("Plants (NDVI) - Cyampirita", sl.PLANTS_KIND, False), ("Soil moisture - Cyampirita", sl.SOIL_KIND, False)]
    plants, soil = saved
    assert soil["metadata"] == {
        "source": "satellite_view", "analysis_kind": sl.SOIL_KIND, "source_layer_id": "Lphoto",
        "data_stamp": "hexagons:2026-11-D1:first", "h3_resolutions": [10],
        "zoom_resolution_map": [{"h3_resolution": 10, "minzoom": 0.0, "maxzoom": 24.0}], "square_m": 100, "period": "2026-11-D1"}
    assert plants["metadata"]["analysis_kind"] == sl.PLANTS_KIND
    assert plants["metadata"]["data_stamp"] == "hexagons:2026-10-23,2026-11-19" and plants["metadata"]["h3_resolutions"] == [12]
    assert plants["maxzoom"] == sl.PLANTS_MAXZOOM and soil["maxzoom"] == sl.SOIL_MAXZOOM
    assert {f["geometry"]["type"] for f in plants["feature_collection"]["features"]} == {"Polygon"}
    assert {f["geometry"]["type"] for f in soil["feature_collection"]["features"]} == {"Polygon", "Point"}
    assert plants["replaces"] == () and soil["replaces"] == ()
    assert plants["style_for"]("Lx")[0]["id"] == "satellite-plants-Lx-r12" and len(soil["style_for"]("Ly")) == 4


@pytest.mark.anyio
async def test_asked_again_with_nothing_newer_the_layers_already_there_are_used(saving):
    there, saved = saving
    there[sl.PLANTS_KIND] = ("Lold1", "Plants (NDVI) - Cyampirita", "hexagons:2026-10-23,2026-11-19", list(BOX))
    there[sl.SOIL_KIND] = ("Lold2", "Soil moisture - Cyampirita", "hexagons:2026-11-D1:first", list(BOX))
    layers = await _put(_view())
    assert saved == [] and [(layer.layer_id, layer.reused) for layer in layers] == [("Lold1", True), ("Lold2", True)]


@pytest.mark.anyio
async def test_a_newer_picture_or_one_drawn_the_old_way_gives_its_place_on_the_map(saving):
    there, saved = saving
    there[sl.PLANTS_KIND] = ("Lold1", "Plants (NDVI) - Cyampirita", "hexagons:2026-10-23", list(BOX))
    there[sl.SOIL_KIND] = ("Lold2", "Soil moisture - Cyampirita", "2026-11-D1:first", list(BOX))  # squares, before hexagons
    layers = await _put(_view())
    assert [layer["replaces"] for layer in saved] == [("Lold1",), ("Lold2",)]
    assert [(layer.kind, layer.reused) for layer in layers] == [(sl.PLANTS_KIND, False), (sl.SOIL_KIND, False)]


@pytest.mark.anyio
async def test_a_part_the_view_lacks_gets_no_layer_and_a_layer_that_cannot_be_saved_is_left_out(saving, monkeypatch):
    _, saved = saving
    layers = await _put(_view(soil=None))
    assert [layer.kind for layer in layers] == [sl.PLANTS_KIND]

    async def broken(**kwargs):
        raise RuntimeError("tippecanoe failed")

    monkeypatch.setattr(sl, "save_analysis_layer", broken)
    assert await _put(_view()) == []
