"""WaPOR point reads: published dekads are read once, a dekad not published yet is not asked for on every report."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import numpy as np
import pytest
import rasterio
from cachetools import LRUCache, TTLCache

from src.services import wapor_service as ws

URL = ws.raster_url("L2-AETI-D", "2026-09-D1")


@pytest.fixture
def cog(monkeypatch):
    """rasterio.open replaced by a one-pixel dataset whose value the test sets; opens are counted.

    The read runs here, not in a raster worker process, so the replacement applies to it."""
    monkeypatch.setattr(ws.raster_process, "run", lambda fn, *args: fn(*args))
    monkeypatch.setattr(ws, "_points", LRUCache(maxsize=64))
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
