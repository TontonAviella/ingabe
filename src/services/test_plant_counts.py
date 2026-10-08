"""Plant counts on a made-up photo: green crowns of known number on bare soil."""

from __future__ import annotations

import numpy as np
import pytest
import rasterio
from pyproj import Transformer
from rasterio.transform import from_origin
from shapely.geometry import box, mapping
from shapely.ops import transform as reproject

from src.services import plant_counts

X0, Y0 = 830000, 9810000  # UTM 35S, near Kigali


def _photo(tmp_path, pixel_m, rows=6, cols=7, spacing_m=0.9, radius_m=0.22):
    """A 8 m square of soil with rows x cols round green crowns; (path, plot feature in WGS84)."""
    size = int(8 / pixel_m)
    rgb = np.empty((3, size, size), dtype="uint8")
    rgb[0], rgb[1], rgb[2] = 196, 158, 118  # soil
    yy, xx = np.mgrid[0:size, 0:size] * pixel_m
    for i in range(rows):
        for j in range(cols):
            cy, cx = 1.0 + i * spacing_m, 1.0 + j * spacing_m
            disc = (yy - cy) ** 2 + (xx - cx) ** 2 <= radius_m ** 2
            rgb[0][disc], rgb[1][disc], rgb[2][disc] = 70, 150, 55
    path = tmp_path / "plot.tif"
    with rasterio.open(path, "w", driver="GTiff", width=size, height=size, count=3, dtype="uint8",
                       crs="EPSG:32735", transform=from_origin(X0, Y0, pixel_m, pixel_m)) as ds:
        ds.write(rgb)
    to_wgs84 = Transformer.from_crs("EPSG:32735", "EPSG:4326", always_xy=True).transform
    plot = reproject(to_wgs84, box(X0 + 0.6, Y0 - 5.9, X0 + 6.9, Y0 - 0.6))  # the planted part, 6.3 x 5.3 m
    return path, {"type": "Feature", "geometry": mapping(plot), "properties": {"number": 4, "area_ha": 0.005}}


def test_every_crown_is_one_dot(tmp_path):
    path, plot = _photo(tmp_path, 0.03)
    count = plant_counts.count_plants(str(path), plot)
    assert count.plants == 42
    assert (count.low, count.high) == (35, 49)  # ±15%
    assert count.area_m2 == pytest.approx(33.4, rel=0.02) and count.per_m2 == pytest.approx(42 / 33.4, rel=0.03)
    assert len(count.points["features"]) == 42 and count.cm_per_px == 3.0
    assert count.gap_share < 0.05  # crowns 0.9 m apart: nothing is farther than 1 m from a plant


def test_a_coarse_photo_or_a_big_plot_cannot_be_counted(tmp_path):
    path, plot = _photo(tmp_path, 0.08)
    with pytest.raises(plant_counts.CannotCount, match="8 cm per pixel"):
        plant_counts.count_plants(str(path), plot)
    path, plot = _photo(tmp_path, 0.03)
    plot["properties"]["area_ha"] = 5.0
    with pytest.raises(plant_counts.CannotCount, match="up to 2 ha"):
        plant_counts.count_plants(str(path), plot)


def test_a_missing_row_shows_as_gaps(tmp_path):
    full, plot = _photo(tmp_path, 0.03)
    whole = plant_counts.count_plants(str(full), plot)
    sparse, plot = _photo(tmp_path, 0.03, rows=3, spacing_m=0.9)  # rows 4-6 missing: the lower half is bare
    thin = plant_counts.count_plants(str(sparse), plot)
    assert thin.plants == 21 and thin.gap_share > whole.gap_share + 0.2
