"""get_field_stats can clip to the polygon and leave water out (used by the weekly cell NDVI job)."""

from __future__ import annotations

import numpy as np
import pytest
import rasterio
from rasterio.transform import from_origin

from src.services import deafrica_stac

# A 10 x 10 window over lon 30.00-30.10, lat -1.00 to -1.10 (0.01 degree pixels).
TRANSFORM = from_origin(30.0, -1.0, 0.01, 0.01)
BBOX_POLYGON = {
    "type": "Polygon",
    "coordinates": [[[30.0, -1.0], [30.1, -1.0], [30.1, -1.1], [30.0, -1.1], [30.0, -1.0]]],
}
# The polygon covers only the left half (columns 0-4).
LEFT_HALF = {
    "type": "Polygon",
    "coordinates": [[[30.0, -1.0], [30.05, -1.0], [30.05, -1.1], [30.0, -1.1], [30.0, -1.0]]],
}


def _tif(path, data):
    profile = {
        "driver": "GTiff", "width": 10, "height": 10, "count": 1, "dtype": "uint16",
        "crs": "EPSG:4326", "transform": TRANSFORM,
    }
    with rasterio.open(path, "w", **profile) as dst:
        dst.write(data.astype("uint16"), 1)
    return str(path)


@pytest.fixture
def scene(tmp_path, monkeypatch):
    """Left half NDVI 0.6 (land), right half NDVI -0.5 (water, SCL 6)."""
    red = np.full((10, 10), 1000)
    nir = np.full((10, 10), 4000)  # (4000-1000)/(4000+1000) = 0.6
    red[:, 5:] = 3000
    nir[:, 5:] = 1000  # (1000-3000)/(1000+3000) = -0.5
    scl = np.full((10, 10), 4)  # vegetation
    scl[:, 5:] = deafrica_stac.SCL_WATER
    hrefs = {
        "B04": _tif(tmp_path / "b04.tif", red),
        "B08": _tif(tmp_path / "b08.tif", nir),
        "SCL": _tif(tmp_path / "scl.tif", scl),
    }
    item = {
        "id": "S2_TEST",
        "properties": {"datetime": "2026-10-20T08:00:00Z", "eo:cloud_cover": 5},
        "assets": {band: {"href": href} for band, href in hrefs.items()},
    }
    monkeypatch.setattr(deafrica_stac, "_search_s2_items", lambda *a, **k: [item])
    return item


def _ndvi(**kwargs):
    stats = deafrica_stac.DEAfricaSTACService().get_field_stats(
        kwargs.pop("geometry", BBOX_POLYGON), "2026-10-15", "2026-10-22", index="ndvi", **kwargs,
    )
    return stats["intervals"][0]["ndvi"]


def test_default_covers_the_whole_box_including_water(scene):
    ndvi = _ndvi()
    assert ndvi["valid_pixels"] == 100
    assert ndvi["mean"] == pytest.approx(0.05)  # half 0.6, half -0.5


def test_drop_water_leaves_out_water_pixels(scene):
    ndvi = _ndvi(drop_water=True)
    assert ndvi["valid_pixels"] == 50
    assert ndvi["mean"] == pytest.approx(0.6)


def test_clip_to_geometry_keeps_only_pixels_inside_the_polygon(scene):
    ndvi = _ndvi(geometry=LEFT_HALF, clip_to_geometry=True)
    assert ndvi["valid_pixels"] == 50
    assert ndvi["inside_pixels"] == 50
    assert ndvi["mean"] == pytest.approx(0.6)


def test_clip_counts_pixels_inside_even_when_they_are_water(scene):
    ndvi = _ndvi(clip_to_geometry=True, drop_water=True)
    assert ndvi["valid_pixels"] == 50
    assert ndvi["inside_pixels"] == 100


def test_a_scene_whose_clip_fails_is_skipped_not_widened(scene, monkeypatch):
    monkeypatch.setattr(deafrica_stac, "_inside_geometry", lambda *a, **k: None)
    stats = deafrica_stac.DEAfricaSTACService().get_field_stats(
        LEFT_HALF, "2026-10-15", "2026-10-22", index="ndvi", clip_to_geometry=True,
    )
    assert stats["intervals"] == []
