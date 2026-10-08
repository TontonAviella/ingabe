"""Field checks: the crop people found in a plot replaces the model's and scores it."""

from __future__ import annotations

import asyncio

import pytest

from src.services import drone_vision, field_checks
from src.services.test_farm_records import _FakeS3


def _survey(crops):
    looks = {n: drone_vision._look(n, {"main_crop": c, "other_crops": [], "stage": "young", "crop_cover_percent": 50,
                                       "weeds": "few", "problems": [], "note": "", "confidence": "medium"})
             for n, c in crops.items()}
    return drone_vision.Survey(looks=looks, plots=len(looks), model="m", done_at="t", cost_usd=0.0)


def test_checks_are_kept_per_photo_and_a_later_check_replaces_an_earlier_one():
    s3 = _FakeS3()
    asyncio.run(field_checks.add_check(s3, "b", "cog/a.tif", 3, "maize"))
    checks = asyncio.run(field_checks.add_check(s3, "b", "cog/a.tif", 3, "cassava"))
    assert {n: c.crop for n, c in checks.items()} == {3: "cassava"}
    assert asyncio.run(field_checks.load_checks(s3, "b", "cog/other.tif")) == {}
    with pytest.raises(ValueError):
        asyncio.run(field_checks.add_check(s3, "b", "cog/a.tif", 4, "unsure"))


def test_the_model_is_scored_only_where_it_named_a_crop():
    survey = _survey({1: "maize", 2: "cassava", 3: "unsure"})
    checks = {n: field_checks.Check(n, "maize", "t") for n in (1, 2, 3)}
    record = field_checks.model_record(survey, checks)
    assert (record.right, record.named, record.unsure) == (1, 2, 1) and record.share == 0.5
    applied = field_checks.apply(survey, checks)
    assert {n: look.main_crop for n, look in applied.looks.items()} == {1: "maize", 2: "maize", 3: "maize"}
