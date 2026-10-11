"""The looks at a photo's plots as the cards and Sage take them (photo_context.plot_looks), on a fake store: a kept
vision survey, kept fingerprints, plots named from the pictures and plots checked on the ground. No model is called."""

from __future__ import annotations

import uuid

import numpy as np
import pytest

from src.services import crop_fingerprints, drone_plots, drone_vision, field_checks, photo_context
from src.services.test_farm_records import _FakeS3

RUN_TAG = uuid.uuid4().hex[:8]  # the modules keep surveys and fingerprints in memory by key: one photo per test
BUCKET = "b"
SCOPE = "org-test"
PROJECT = "Ptest"

MAIZE, BANANA, OTHER = (np.eye(8, dtype=np.float32)[i] for i in range(3))
PRINTS = {1: MAIZE, 2: MAIZE, 3: MAIZE, 4: MAIZE, 5: MAIZE, 6: BANANA, 7: BANANA, 8: BANANA, 9: BANANA, 10: OTHER}
EXAMPLES = {1: "maize", 2: "maize", 3: "maize", 6: "banana", 7: "banana"}  # named from the drone pictures
CHECKS = {4: "maize", 8: "banana"}  # checked on the ground


class _Store(_FakeS3):
    async def generate_presigned_url(self, method, Params, ExpiresIn):
        return f"https://store.test/{Params['Key']}"


def _plots(n: int = 10) -> drone_plots.PlotSet:
    features = [{"type": "Feature",
                 "geometry": {"type": "Polygon", "coordinates": [[[30, -2], [30.001, -2], [30.001, -2.001], [30, -2]]]},
                 "properties": {"number": i, "area_ha": 0.1 * i, "greenness": 0.1, "bare_share": 0.0,
                                "group": drone_plots.BETWEEN, "lon": 30.0005, "lat": -2.0005}}
                for i in range(1, n + 1)]
    return drone_plots.PlotSet(geojson={"type": "FeatureCollection", "features": features},
                               found_at="2026-10-11T00:00:00+00:00", seconds=1.0, source="found", read_m_per_px=0.5)


def _vision_survey(plots: drone_plots.PlotSet) -> drone_vision.Survey:
    looks = {}
    for f in plots.geojson["features"]:
        n = f["properties"]["number"]
        looks[n] = drone_vision._look(n, {"main_crop": "cassava", "other_crops": [], "stage": "young",
                                          "crop_cover_percent": 60, "weeds": "few",
                                          "problems": ["gaps"] if n == 5 else [], "note": f"Plot {n}.",
                                          "confidence": "medium"})
    return drone_vision.Survey(looks=looks, plots=len(looks), model="claude-haiku-5-5",
                               done_at="2026-10-11T00:00:00+00:00", cost_usd=0.0)


@pytest.fixture
def photo(monkeypatch):
    """A photo's store holding fingerprints, picture names and ground checks (and no survey yet)."""
    monkeypatch.setenv("DRONE_VISION_MODEL", "claude-haiku-5-5")
    monkeypatch.setenv("OPENAI_BASE_URL", "https://api.anthropic.com/v1/")
    s3, plots = _Store(), _plots()
    metadata = {"cog_key": f"cog/{RUN_TAG}-{uuid.uuid4().hex[:8]}.tif"}
    key = metadata["cog_key"]
    s3.objects[crop_fingerprints._object_key(crop_fingerprints.store_key(key, plots))] = crop_fingerprints._to_bytes(PRINTS)
    plot_set = field_checks.plot_set_id(key, plots)
    return s3, metadata, plots, plot_set


async def _named(s3, plot_set) -> None:
    await crop_fingerprints.save_examples(s3, BUCKET, PROJECT, plot_set, EXAMPLES, "test, drone pictures")
    for number, crop in CHECKS.items():
        await field_checks.add_check(s3, BUCKET, PROJECT, plot_set, number, crop)


def _keep_survey(s3, metadata, plots) -> drone_vision.Survey:
    survey = _vision_survey(plots)
    key = drone_vision.survey_key(metadata["cog_key"], plots, SCOPE)
    s3.objects[drone_vision._store_key(key)] = drone_vision._to_json(survey)
    return survey


async def _looks(s3, metadata, plots, *, start: bool = False) -> photo_context.PlotLooks:
    return await photo_context.plot_looks(s3, BUCKET, metadata, plots, PROJECT, place=None, scope=SCOPE, start=start,
                                          retry_failed=False)


async def test_crops_come_from_the_fingerprints_and_checks_and_the_survey_keeps_the_rest(photo):
    s3, metadata, plots, plot_set = photo
    await _named(s3, plot_set)
    _keep_survey(s3, metadata, plots)
    looks = await _looks(s3, metadata, plots)
    crops = {n: look.main_crop for n, look in looks.survey.looks.items()}
    assert crops == {1: "maize", 2: "maize", 3: "maize", 4: "maize", 5: "maize", 6: "unsure", 7: "unsure",
                     8: "banana", 9: "banana", 10: "unsure"}  # banana has 2 picture names: 6 and 7 hide their own
    five = looks.survey.look(5)
    assert (five.source, five.stage, five.weeds, five.problems) == ("fingerprints", "young", "few", ["gaps"])
    assert looks.survey.look(8).confidence == "high"  # checked on the ground
    assert looks.checked == frozenset(CHECKS)
    assert looks.record == field_checks.Record(right=1, named=1, unsure=1)  # plot 4 right; plot 8 unsure (2 bananas)
    assert looks.picture_record == field_checks.Record(right=3, named=3, unsure=2)


async def test_sage_names_the_crop_the_cards_show(photo):
    from src.tools import drone_photo_tools

    s3, metadata, plots, plot_set = photo
    await _named(s3, plot_set)
    _keep_survey(s3, metadata, plots)

    async def crop(number):
        return await drone_photo_tools._crop_seen(s3, BUCKET, metadata, plots, number, PROJECT, SCOPE)

    assert [await crop(n) for n in (4, 5, 9, 10)] == ["Maize", "Maize", "Banana", None]
