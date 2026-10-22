"""The weekly cell NDVI job: per-cell aggregation and its schedule."""

from __future__ import annotations

import pytest

pytest.importorskip("dagster")

from src.pipelines import rwanda_assets  # noqa: E402

CELL = {"type": "Polygon", "coordinates": [[[30, -1], [30.01, -1], [30.01, -1.01], [30, -1]]]}


class FakeDEA:
    def __init__(self, result):
        self.result = result
        self.calls = []

    def get_field_stats(self, **kwargs):
        self.calls.append(kwargs)
        return self.result


def _scene(mean, pixels, inside=10_000):
    """A clipped scene: the cell is 10,000 pixels on the grid read, so a week needs 2,000 clear."""
    return {"ndvi": {"mean": mean, "valid_pixels": pixels, "inside_pixels": inside}}


def test_cell_row_clips_to_the_cell_and_drops_water():
    dea = FakeDEA({"intervals": [_scene(0.5, 5000)]})
    rwanda_assets._cell_ndvi_row(dea, CELL, "2026-10-15", "2026-10-22")
    assert dea.calls[0]["clip_to_geometry"] is True
    assert dea.calls[0]["drop_water"] is True


def test_cell_row_pools_the_weeks_clear_pixels_weighting_each_scene():
    # Two halves of a cell on a tile edge: neither alone is 20% of it, together they are.
    dea = FakeDEA({"intervals": [_scene(0.6, 1500), _scene(0.3, 500), _scene(0.0, 0)]})
    row = rwanda_assets._cell_ndvi_row(dea, CELL, "2026-10-15", "2026-10-22")
    assert row is not None
    mean, std, low, high, pixels = row
    assert mean == pytest.approx(0.525)  # (0.6*1500 + 0.3*500) / 2000
    assert std == pytest.approx(0.15)
    assert (low, high) == (pytest.approx(0.3), pytest.approx(0.6))
    assert pixels == 2000


def test_cell_row_is_none_when_the_week_saw_too_little():
    dea = FakeDEA({"intervals": [_scene(0.3, 1200), _scene(0.4, 700)]})  # 1,900 < 2,000
    assert rwanda_assets._cell_ndvi_row(dea, CELL, "2026-10-15", "2026-10-22") is None
    dea = FakeDEA({"intervals": []})
    assert rwanda_assets._cell_ndvi_row(dea, CELL, "2026-10-15", "2026-10-22") is None


def test_cell_row_bar_follows_the_pixels_read_not_the_area():
    # A capped window (large cell) reads fewer, larger pixels: 1,000 clear of 4,000 inside is 25%.
    dea = FakeDEA({"intervals": [_scene(0.4, 1000, inside=4000)]})
    assert rwanda_assets._cell_ndvi_row(dea, CELL, "2026-10-15", "2026-10-22") is not None


def test_cell_row_raises_on_a_reader_error():
    dea = FakeDEA({"error": "DE Africa search failed"})
    with pytest.raises(RuntimeError, match="search failed"):
        rwanda_assets._cell_ndvi_row(dea, CELL, "2026-10-15", "2026-10-22")


def test_weekly_cell_ndvi_is_scheduled_sunday_evening_and_running():
    from dagster import DefaultScheduleStatus

    from src.pipelines.schedules import weekly_cell_ndvi_schedule

    assert weekly_cell_ndvi_schedule.cron_schedule == "0 20 * * 0"
    assert weekly_cell_ndvi_schedule.job_name == "weekly_cell_ndvi_job"
    assert weekly_cell_ndvi_schedule.default_status == DefaultScheduleStatus.RUNNING


def test_weekly_cell_ndvi_job_is_registered():
    from src.pipelines import defs

    assert defs.get_job_def("weekly_cell_ndvi_job") is not None
