import io

import numpy as np
import pytest
import rasterio
from PIL import Image
from rasterio.transform import from_bounds
from rio_tiler.io import Reader

from src.services import raster_display

# A small field near Cyampirita, in Web Mercator like the stored COGs.
MERCATOR_BOUNDS = (3386000.0, -189300.0, 3386400.0, -189100.0)


def _write_tif(path, bands: np.ndarray) -> str:
    count, height, width = bands.shape
    with rasterio.open(
        path, "w", driver="GTiff", width=width, height=height, count=count,
        dtype=bands.dtype, crs="EPSG:3857",
        transform=from_bounds(*MERCATOR_BOUNDS, width, height),
    ) as dst:
        dst.write(bands)
    return str(path)


@pytest.mark.parametrize(
    "metadata, expected",
    [
        ({"band_count": 4}, (1, 2, 3)),
        ({"band_count": 3}, None),
        ({"band_count": 1}, None),
        ({}, None),
        (None, None),
    ],
)
def test_display_indexes(metadata, expected):
    assert raster_display.display_indexes(metadata) == expected


def test_four_band_ortho_renders_as_rgba_png(tmp_path):
    rgba = np.zeros((4, 64, 128), dtype=np.uint8)
    rgba[0], rgba[1], rgba[2], rgba[3] = 30, 160, 40, 255
    metadata = {"band_count": 4}
    with Reader(_write_tif(tmp_path / "ortho.tif", rgba)) as src:
        img = src.preview(indexes=raster_display.display_indexes(metadata))
    png = Image.open(io.BytesIO(raster_display.render_png(img, metadata)))
    assert png.mode == "RGBA"
    assert png.getpixel((10, 10)) == (30, 160, 40, 255)


def test_single_band_with_stats_is_coloured(tmp_path):
    band = np.linspace(0, 1, 64 * 128, dtype=np.float32).reshape(1, 64, 128)
    metadata = {"band_count": 1, "raster_value_stats_b1": {"min": 0.0, "max": 1.0}}
    with Reader(_write_tif(tmp_path / "ndvi.tif", band)) as src:
        img = src.preview(indexes=raster_display.display_indexes(metadata))
    png = Image.open(io.BytesIO(raster_display.render_png(img, metadata))).convert("RGB")
    low, high = png.getpixel((0, 0)), png.getpixel((127, 63))
    assert low != high
    assert len(set(low)) > 1, "a coloured ramp, not greyscale"
