"""crop_stages: one growth-stage model; stage windows agree with stage_from_dap."""

from datetime import date, timedelta

import pytest

from src.services.crop_stages import stage_from_dap, stage_labels, stage_window


@pytest.mark.parametrize("crop, total", [("maize", 135), ("beans", 90), ("maize", 120), ("sorghum", 113)])
def test_every_day_in_a_window_has_that_stage(crop, total):
    planting = date(2026, 9, 15)
    for stage in stage_labels(crop):
        start, end = stage_window(stage, crop, planting, total)
        days = range((start - planting).days, (end - planting).days + 1)
        assert all(stage_from_dap(d, total, crop) == stage for d in days)
        assert stage_from_dap((start - planting).days - 1, total, crop) != stage or stage == "planting"
        assert stage_from_dap((end - planting).days + 1, total, crop) != stage or stage == "maturity"


def test_maize_season_a_flowering_window():
    start, end = stage_window("flowering", "maize", date(2026, 9, 15), 135)
    assert (start - date(2026, 9, 15)).days == 51 and (end - date(2026, 9, 15)).days == 84


def test_unknown_stage_or_cycle():
    assert stage_window("tasseling", "maize", date(2026, 9, 15), 135) is None
    assert stage_window("flowering", "maize", date(2026, 9, 15), 0) is None
    assert stage_from_dap(-1, 135, "maize") == "_any"
    assert stage_from_dap(10, 90, "beans") == "planting"
    assert stage_from_dap(70, 90, "beans") == "pod_fill"
