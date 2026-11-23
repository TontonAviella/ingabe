"""WaPOR point reads: published dekads are read once, a dekad not published yet is not asked for on every report."""

from __future__ import annotations

from datetime import date, timedelta
from unittest.mock import MagicMock, patch

import numpy as np
import pytest
import rasterio
from cachetools import LRUCache, TTLCache

from src.services import wapor_service as ws

URL = ws.raster_url("L2-AETI-D", "2025-09-D1")  # long settled: FAO's replaced, final file


@pytest.fixture
def cog(monkeypatch):
    """rasterio.open replaced by a one-pixel dataset whose value the test sets; opens are counted.

    The read runs here, not in a raster worker process, so the replacement applies to it."""
    monkeypatch.setattr(ws.raster_process, "run", lambda fn, *args: fn(*args))
    monkeypatch.setattr(ws, "_points", LRUCache(maxsize=64))
    monkeypatch.setattr(ws, "_first_estimates", TTLCache(maxsize=64, ttl=24 * 3600))
    now = [0.0]
    monkeypatch.setattr(ws, "_unpublished", TTLCache(maxsize=64, ttl=3 * 3600, timer=lambda: now[0]))
    state = {"value": 37, "error": None, "opens": 0}

    def open_(url):
        state["opens"] += 1
        if state["error"]:
            raise state["error"]
        ds = MagicMock()
        ds.index.return_value = (0, 0)
        ds.read.return_value = np.array([[state["value"]]], dtype=np.int16)
        ds.__enter__.return_value = ds
        return ds

    with patch.object(ws.rasterio, "open", side_effect=open_):
        yield state, now


def test_a_published_value_is_read_once(cog):
    state, _ = cog
    assert ws._read_point(URL, -1.9, 30.1, 0.1, 0.0) == pytest.approx(3.7)
    assert ws._read_point(URL, -1.9, 30.1, 0.1, 0.0) == pytest.approx(3.7)
    assert state["opens"] == 1


def test_zero_is_a_value_and_nodata_is_missing(cog):
    state, _ = cog
    state["value"] = 0
    assert ws._read_point(URL, -1.9, 30.1, 0.1, 0.0) == 0.0
    state["value"] = ws.NODATA
    assert ws._read_point(URL, -1.8, 30.2, 0.1, 0.0) is None
    assert ws._read_point(URL, -1.8, 30.2, 0.1, 0.0) is None
    assert state["opens"] == 2


def test_a_dekad_not_published_is_asked_for_again_only_after_a_while(cog):
    state, now = cog
    state["error"] = rasterio.errors.RasterioIOError("HTTP response code: 404")
    assert ws._read_point(URL, -1.9, 30.1, 0.1, 0.0) is None
    assert ws._read_point(URL, -2.0, 30.0, 0.1, 0.0) is None  # any place: the whole dekad is missing
    assert state["opens"] == 1
    now[0] = 3 * 3600 + 1
    state["error"] = None
    assert ws._read_point(URL, -1.9, 30.1, 0.1, 0.0) == pytest.approx(3.7)
    assert state["opens"] == 2


def test_other_failures_are_not_kept(cog):
    state, _ = cog
    state["error"] = rasterio.errors.RasterioIOError("HTTP response code: 503")
    assert ws._read_point(URL, -1.9, 30.1, 0.1, 0.0) is None
    state["error"] = None
    assert ws._read_point(URL, -1.9, 30.1, 0.1, 0.0) == pytest.approx(3.7)
    assert state["opens"] == 2


def test_a_restart_reads_kept_values_from_disk_not_the_file(cog, monkeypatch):
    # Roger, 2026-10-26: the first insurance report after a deploy took ~10 s, re-reading dekads read before.
    state, _ = cog
    assert ws._read_point(URL, -1.9, 30.1, 0.1, 0.0) == pytest.approx(3.7)
    state["value"] = ws.NODATA
    assert ws._read_point(URL, -1.8, 30.2, 0.1, 0.0) is None
    monkeypatch.setattr(ws, "_points", LRUCache(maxsize=64))  # the restart: memory is empty, the disk is not
    state["value"], state["error"] = 99, rasterio.errors.RasterioIOError("HTTP response code: 503")
    assert ws._read_point(URL, -1.9, 30.1, 0.1, 0.0) == pytest.approx(3.7)
    assert ws._read_point(URL, -1.8, 30.2, 0.1, 0.0) is None  # NODATA kept as missing, not asked again
    assert state["opens"] == 2


def test_failures_are_not_kept_on_disk(cog, monkeypatch):
    state, _ = cog
    state["error"] = rasterio.errors.RasterioIOError("HTTP response code: 503")
    assert ws._read_point(URL, -1.9, 30.1, 0.1, 0.0) is None
    monkeypatch.setattr(ws, "_points", LRUCache(maxsize=64))
    state["error"] = None
    assert ws._read_point(URL, -1.9, 30.1, 0.1, 0.0) == pytest.approx(3.7)
    assert state["opens"] == 2


def test_ten_day_periods_have_their_days_and_their_neighbour_before():
    assert ws.dekad_of(date(2026, 11, 10)) == "2026-11-D1" and ws.dekad_of(date(2026, 11, 11)) == "2026-11-D2"
    assert ws.dekad_days("2026-11-D1") == (date(2026, 11, 1), date(2026, 11, 10))
    assert ws.dekad_days("2026-02-D3") == (date(2026, 2, 21), date(2026, 2, 28))
    assert ws.dekad_days("2025-12-D3") == (date(2025, 12, 21), date(2025, 12, 31))
    assert ws.dekad_before("2026-11-D1") == "2026-10-D3" and ws.dekad_before("2026-01-D1") == "2025-12-D3"
    assert ws.dekad_before("2026-03-D3") == "2026-03-D2"


def test_a_first_estimate_is_not_kept_on_disk_so_a_restart_reads_faos_replacement(cog, monkeypatch):
    # FAO replaces each file 66-70 days after its ten days end (bucket listing, 2026-11-23); a first value kept for
    # good would never be corrected.
    state, _ = cog
    fresh = ws.raster_url("L2-RSM-D", ws.dekad_of(date.today() - timedelta(days=20)))
    assert ws._read_point(fresh, -1.9, 30.1, 0.001, 0.0) == pytest.approx(0.037)
    assert ws._read_point(fresh, -1.9, 30.1, 0.001, 0.0) == pytest.approx(0.037)  # kept in memory meanwhile
    assert state["opens"] == 1
    clock = [0.0]
    monkeypatch.setattr(ws, "_first_estimates", TTLCache(maxsize=64, ttl=24 * 3600, timer=lambda: clock[0]))
    assert ws._read_point(fresh, -1.9, 30.1, 0.001, 0.0) == pytest.approx(0.037)  # the restart: read again, not from disk
    assert state["opens"] == 2
    clock[0] = 24 * 3600 + 1  # and without a restart it is read again after a day
    state["value"] = 41  # FAO's replacement
    assert ws._read_point(fresh, -1.9, 30.1, 0.001, 0.0) == pytest.approx(0.041)
    assert state["opens"] == 3


def test_a_series_reads_only_what_is_not_kept_and_keeps_settled_periods(cog, monkeypatch):
    state, _ = cog
    old = ["2025-09-D1", "2025-08-D3", "2025-08-D2"]
    assert ws._read_point(ws.raster_url("L2-RSM-D", old[0]), -1.9, 30.1, 0.001, 0.0) == pytest.approx(0.037)
    state["value"] = 650
    series = ws.point_series("L2-RSM-D", old, -1.9, 30.1)
    assert series == {old[0]: pytest.approx(0.037), old[1]: pytest.approx(0.65), old[2]: pytest.approx(0.65)}
    assert state["opens"] == 3  # the first period was already kept
    monkeypatch.setattr(ws, "_points", LRUCache(maxsize=64))  # a restart: settled periods come back from disk
    state["error"] = rasterio.errors.RasterioIOError("HTTP response code: 503")
    assert ws.point_series("L2-RSM-D", old, -1.9, 30.1)[old[2]] == pytest.approx(0.65)
    assert state["opens"] == 3


def test_a_series_gives_none_for_a_period_not_published_or_not_read_and_zero_stays_a_value(cog):
    state, _ = cog
    state["value"] = 0
    assert ws.point_series("L2-RSM-D", ["2025-07-D1"], -1.9, 30.1) == {"2025-07-D1": 0.0}
    state["value"] = ws.NODATA
    assert ws.point_series("L2-RSM-D", ["2025-07-D2"], -1.9, 30.1) == {"2025-07-D2": None}
    state["error"] = rasterio.errors.RasterioIOError("HTTP response code: 404")
    assert ws.point_series("L2-RSM-D", ["2025-07-D3"], -1.9, 30.1) == {"2025-07-D3": None}
    opens = state["opens"]
    assert ws.point_series("L2-RSM-D", ["2025-07-D3"], -2.0, 30.0) == {"2025-07-D3": None}  # not asked again for a while
    assert state["opens"] == opens
    state["error"] = rasterio.errors.RasterioIOError("HTTP response code: 503")
    assert ws.point_series("L2-RSM-D", ["2025-06-D1"], -1.9, 30.1) == {"2025-06-D1": None}
    state["error"] = None
    state["value"] = 500
    assert ws.point_series("L2-RSM-D", ["2025-06-D1"], -1.9, 30.1) == {"2025-06-D1": pytest.approx(0.5)}  # a failure is not kept
