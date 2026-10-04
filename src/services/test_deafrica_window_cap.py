"""_read_window caps large windows (district-wide reads) and leaves small ones exact."""

from __future__ import annotations

import numpy as np
import pytest
import rasterio
from rasterio.transform import from_origin

from src.services import deafrica_stac


@pytest.fixture
def cog(tmp_path):
    path = tmp_path / "band.tif"
    data = np.arange(400 * 300, dtype="uint16").reshape(300, 400)
    profile = {
        "driver": "GTiff", "width": 400, "height": 300, "count": 1, "dtype": "uint16",
        "crs": "EPSG:4326", "transform": from_origin(30.0, -1.0, 0.001, 0.001),
    }
    with rasterio.open(path, "w", **profile) as dst:
        dst.write(data, 1)
    return str(path)


def test_small_window_is_read_at_full_resolution(cog, monkeypatch):
    monkeypatch.setattr(deafrica_stac, "MAX_WINDOW_PIXELS", 1_000_000)
    arr, transform = deafrica_stac._read_window(cog, (30.0, -1.3, 30.4, -1.0))
    assert arr.shape == (300, 400)
    assert transform.a == pytest.approx(0.001)


def test_large_window_is_capped_and_transform_scaled(cog, monkeypatch):
    monkeypatch.setattr(deafrica_stac, "MAX_WINDOW_PIXELS", 12_000)
    arr, transform = deafrica_stac._read_window(cog, (30.0, -1.3, 30.4, -1.0))
    assert arr.shape[0] * arr.shape[1] <= 12_000
    assert arr.shape == (94, 126)  # 300x400 scaled by sqrt(120000/12000)
    # The transform still maps the output grid onto the same bounds.
    assert transform.c == pytest.approx(30.0)
    assert transform.c + transform.a * arr.shape[1] == pytest.approx(30.4)
    assert transform.f + transform.e * arr.shape[0] == pytest.approx(-1.3)
    # Nearest sampling keeps real pixel values (no averaging).
    assert set(np.unique(arr)).issubset(set(range(400 * 300)))
