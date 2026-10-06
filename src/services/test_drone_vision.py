"""The vision model's look at each plot, with the model replaced by fixed answers (no paid calls)."""

from __future__ import annotations

import asyncio
import io

import numpy as np
import pytest
import rasterio
from PIL import Image
from pyproj import Transformer
from rasterio.transform import from_origin
from shapely.geometry import box
from shapely.ops import transform as reproject

from src.services import drone_plots, drone_vision

X0, Y0, PIXEL_M = 830000, 9810000, 0.5


@pytest.fixture
def photo(tmp_path):
    path = tmp_path / "field.tif"
    with rasterio.open(path, "w", driver="GTiff", width=400, height=400, count=3, dtype="uint8",
                       crs="EPSG:32735", transform=from_origin(X0, Y0, PIXEL_M, PIXEL_M)) as ds:
        ds.write(np.stack([np.full((400, 400), v, dtype="uint8") for v in (90, 140, 60)]))
    return path


@pytest.fixture
def plots(photo):
    to_wgs84 = Transformer.from_crs("EPSG:32735", "EPSG:4326", always_xy=True).transform
    own = [drone_plots.MapPlot(reproject(to_wgs84, box(X0 + 10 + 50 * i, Y0 - 60, X0 + 50 + 50 * i, Y0 - 20)), f"P{i}")
           for i in range(3)]
    return drone_plots.measure_own_plots(str(photo), own, "Test plots")


def test_a_plot_picture_is_a_jpeg_of_the_plot_and_its_margin(photo):
    with rasterio.open(photo) as ds:
        picture, width_m, cm = drone_vision.plot_picture(ds, box(X0 + 10, Y0 - 60, X0 + 50, Y0 - 20))
    image = Image.open(io.BytesIO(picture))
    assert image.format == "JPEG"
    assert width_m == pytest.approx(40 + 2 * drone_vision.CONTEXT_M)
    assert cm == pytest.approx(PIXEL_M * 100, rel=0.05)  # small plot: shown at the photo's own detail


def test_answers_outside_the_lists_are_dropped():
    look = drone_vision._look(4, {"main_crop": "tomatoes", "other_crops": ["beans", "weeds", "beans"],
                                  "stage": "ripe", "crop_cover_percent": 140, "weeds": "lots",
                                  "problems": ["gaps", "aliens"], "note": "x" * 500, "confidence": "certain"})
    assert (look.main_crop, look.stage, look.weeds, look.confidence) == ("unsure", "unsure", "unsure", "low")
    assert look.other_crops == ["beans", "beans"] and look.problems == ["gaps"]
    assert look.crop_cover_percent == 100 and len(look.note) == 300


def _answer(crop):
    return {"main_crop": crop, "other_crops": [], "stage": "young", "crop_cover_percent": 40, "weeds": "few",
            "problems": [], "note": "Young plants in rows.", "confidence": "medium"}


def test_survey_looks_at_every_measured_plot(photo, plots, monkeypatch):
    calls = []

    async def fake_ask(client, model, picture, width_m, cm, place):
        calls.append(place)
        return _answer("maize"), 0.0002

    monkeypatch.setattr(drone_vision, "_ask", fake_ask)
    monkeypatch.setattr(drone_vision, "_client", lambda: (None, "openai/gpt-6-luna"))
    seen = []
    result = asyncio.run(drone_vision.survey(str(photo), plots, "Test cell", lambda d, n: seen.append((d, n))))
    assert sorted(result.looks) == [1, 2, 3] and result.plots == 3
    assert {look.main_crop for look in result.looks.values()} == {"maize"}
    assert result.cost_usd == pytest.approx(0.0006)
    assert calls == ["Test cell"] * 3 and seen[-1] == (3, 3)


def test_a_survey_with_too_many_failed_looks_fails(photo, plots, monkeypatch):
    async def failing_ask(*args):
        raise RuntimeError("provider down")

    monkeypatch.setattr(drone_vision, "_ask", failing_ask)
    monkeypatch.setattr(drone_vision, "_client", lambda: (None, "openai/gpt-6-luna"))
    with pytest.raises(RuntimeError, match="could not look at 3 of 3"):
        asyncio.run(drone_vision.survey(str(photo), plots, None, lambda d, n: None))


def test_a_kept_survey_round_trips():
    survey = drone_vision.Survey(looks={1: drone_vision._look(1, _answer("beans"))}, plots=1,
                                 model="openai/gpt-6-luna", done_at="2026-10-06T17:00:00+00:00", cost_usd=0.0002)
    assert drone_vision._from_json(drone_vision._to_json(survey)) == survey
