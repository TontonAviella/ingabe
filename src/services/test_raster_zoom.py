"""How deep the map asks for a raster's tiles: deep enough to show its own pixels (Roger, 2026-10-26: the 3.2 cm
Cyampirita photo looked blurred because tiles stopped at zoom 20, about 15 cm per screen pixel)."""

import pytest

from src.services import raster_zoom

CYAMPIRITA = {"width": 48008, "height": 27272}  # the real photo: 3.2 cm per pixel
CYAMPIRITA_BOUNDS = [30.4174921, -1.7009046, 30.4314816, -1.6928948]


@pytest.fixture(autouse=True)
def _default_env(monkeypatch):
    monkeypatch.delenv("RASTER_SOURCE_MAXZOOM", raising=False)


def test_a_three_centimetre_drone_photo_is_asked_for_down_to_zoom_22():
    assert raster_zoom.raster_source_maxzoom(CYAMPIRITA, CYAMPIRITA_BOUNDS) == 22


def test_a_ten_centimetre_photo_needs_zoom_21():
    tenth = {"width": 15_570}  # the same 1.56 km across at 10 cm per pixel
    assert raster_zoom.raster_source_maxzoom(tenth, CYAMPIRITA_BOUNDS) == 21


def test_coarse_rasters_and_missing_details_keep_the_default():
    satellite = {"width": 156}  # 10 m per pixel over the same area
    assert raster_zoom.raster_source_maxzoom(satellite, CYAMPIRITA_BOUNDS) == 20
    assert raster_zoom.raster_source_maxzoom({}, CYAMPIRITA_BOUNDS) == 20
    assert raster_zoom.raster_source_maxzoom(CYAMPIRITA, None) == 20
    assert raster_zoom.raster_source_maxzoom({"width": "?"}, CYAMPIRITA_BOUNDS) == 20


def test_the_default_still_comes_from_the_environment(monkeypatch):
    monkeypatch.setenv("RASTER_SOURCE_MAXZOOM", "18")
    assert raster_zoom.default_raster_maxzoom() == 18
    assert raster_zoom.raster_source_maxzoom({}, None) == 18
    assert raster_zoom.raster_source_maxzoom(CYAMPIRITA, CYAMPIRITA_BOUNDS) == 22  # its own pixels still win
