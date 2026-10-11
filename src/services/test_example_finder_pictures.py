"""Pictures for the example finder (src/services/example_finder_pictures.py): a window of an orthophoto read through
the raster worker processes (real GeoTIFFs, real rasterio), boxes between lon/lat, the layer's CRS and the window's
pixels, and photos given as bytes."""

from __future__ import annotations

import io

import numpy as np
import pytest
import rasterio
from PIL import Image
from rasterio.transform import from_origin
from rasterio.warp import transform_bounds

from src.services import example_finder_pictures as pictures
from src.services.example_finder_pictures import FinderInputError, PictureReadError

CRS = "EPSG:32735"  # UTM 35S, as Rwandan drone photos
WEST, NORTH, PX_M = 800_000.0, 9_790_000.0, 0.05  # 5 cm a pixel


def _ortho(path, width=600, height=400, bands=3, dtype="uint8"):
    data = np.zeros((bands, height, width), dtype=dtype)
    data[0] = (np.arange(width)[None, :] % 251).astype(dtype)  # each column its own red
    data[1 % bands] += (np.arange(height)[:, None] % 241).astype(dtype)  # each row its own green
    with rasterio.open(path, "w", driver="GTiff", width=width, height=height, count=bands, dtype=dtype, crs=CRS,
                       transform=from_origin(WEST, NORTH, PX_M, PX_M), tiled=True, blockxsize=256,
                       blockysize=256) as dst:
        dst.write(data)
    return str(path), data


def _layer_box(col0, row0, col1, row1):
    """A box in the layer's CRS (west, south, east, north) covering these pixel columns and rows."""
    return (WEST + col0 * PX_M, NORTH - row1 * PX_M, WEST + col1 * PX_M, NORTH - row0 * PX_M)


@pytest.mark.anyio
async def test_a_window_in_the_layers_crs_is_read_at_the_photos_own_pixels(tmp_path):
    path, data = _ortho(tmp_path / "ortho.tif")
    picture = await pictures.OrthophotoWindow(path, _layer_box(100, 50, 300, 250), pictures.LAYER).read()
    assert picture.pixels.shape == (200, 200, 3) and picture.pixels.dtype == np.uint8
    assert np.array_equal(picture.pixels, data[:, 50:250, 100:300].transpose(1, 2, 0))
    assert picture.to_pixels(_layer_box(110, 60, 130, 90)) == pytest.approx((10, 10, 30, 40))
    assert picture.from_pixels((10, 10, 30, 40)) == pytest.approx(_layer_box(110, 60, 130, 90))


@pytest.mark.anyio
async def test_a_lonlat_window_and_its_boxes_map_to_pixels_and_back(tmp_path):
    path, _ = _ortho(tmp_path / "ortho.tif")
    lonlat = transform_bounds(CRS, "EPSG:4326", *_layer_box(100, 50, 300, 250))
    picture = await pictures.OrthophotoWindow(path, lonlat, pictures.LONLAT).read()
    assert abs(picture.width - 200) <= 2 and abs(picture.height - 200) <= 2  # reprojected bounds grow a little
    mark = transform_bounds(CRS, "EPSG:4326", *_layer_box(150, 100, 170, 130))
    px = picture.to_pixels(mark)
    assert px[2] - px[0] == pytest.approx(20, abs=0.5) and px[3] - px[1] == pytest.approx(30, abs=0.5)
    back = picture.from_pixels(px)
    assert back == pytest.approx(mark, abs=1e-6)  # about 0.1 m


@pytest.mark.anyio
async def test_the_same_pixels_in_another_frame_are_another_picture(tmp_path):
    path, _ = _ortho(tmp_path / "ortho.tif")
    layer_box = _layer_box(0, 0, 100, 100)
    in_layer = await pictures.OrthophotoWindow(path, layer_box, pictures.LAYER).read()
    again = await pictures.OrthophotoWindow(path, layer_box, pictures.LAYER).read()
    assert in_layer.sha256 == again.sha256
    lonlat = await pictures.OrthophotoWindow(path, transform_bounds(CRS, "EPSG:4326", *layer_box),
                                             pictures.LONLAT).read()
    assert lonlat.sha256 != in_layer.sha256


@pytest.mark.anyio
async def test_windows_that_cannot_be_read_as_asked_say_why(tmp_path, monkeypatch):
    path, _ = _ortho(tmp_path / "ortho.tif")
    with pytest.raises(FinderInputError, match="does not overlap"):
        await pictures.OrthophotoWindow(path, _layer_box(700, 500, 800, 600), pictures.LAYER).read()
    monkeypatch.setattr(pictures, "MAX_WINDOW_PX", 150)
    with pytest.raises(FinderInputError, match="200 x 200 px of the photo; at most 150"):
        await pictures.OrthophotoWindow(path, _layer_box(100, 50, 300, 250), pictures.LAYER).read()
    grey, _ = _ortho(tmp_path / "grey.tif", bands=1)
    with pytest.raises(FinderInputError, match="colour photo"):
        await pictures.OrthophotoWindow(grey, _layer_box(0, 0, 50, 50), pictures.LAYER).read()
    deep, _ = _ortho(tmp_path / "deep.tif", dtype="uint16")
    with pytest.raises(FinderInputError, match="8 bits"):
        await pictures.OrthophotoWindow(deep, _layer_box(0, 0, 50, 50), pictures.LAYER).read()
    with pytest.raises(PictureReadError):
        await pictures.OrthophotoWindow(str(tmp_path / "gone.tif"), _layer_box(0, 0, 50, 50), pictures.LAYER).read()


@pytest.mark.parametrize("bbox, crs, says", [
    ((0, 0, 1), pictures.LAYER, "four numbers"),
    ((0, 0, 1, float("inf")), pictures.LAYER, "four numbers"),
    ((5, 0, 1, 1), pictures.LAYER, "west must be less than its east"),
    ((29, -2, 200, -1), pictures.LONLAT, "within -180..180"),
    ((0, 0, 1, 1), "EPSG:3857", "must be one of"),
])
def test_window_boxes_are_checked(bbox, crs, says):
    with pytest.raises(FinderInputError) as raised:
        pictures.check_window(bbox, crs)
    assert says in str(raised.value)


def _jpeg(width, height, orientation=None) -> bytes:
    image = Image.new("RGB", (width, height), (200, 30, 30))
    out = io.BytesIO()
    if orientation is None:
        image.save(out, format="JPEG")
    else:
        exif = Image.Exif()
        exif[0x0112] = orientation
        image.save(out, format="JPEG", exif=exif)
    return out.getvalue()


@pytest.mark.anyio
async def test_a_photo_is_read_upright_as_its_exif_says():
    picture = await pictures.Photo(_jpeg(40, 20, orientation=6)).read()  # 6: shown turned a quarter right
    assert picture.pixels.shape == (40, 20, 3) and picture.frame is None
    assert picture.to_pixels((1, 2, 3, 4)) == (1, 2, 3, 4)
    plain = await pictures.Photo(_jpeg(40, 20)).read()
    assert plain.pixels.shape == (20, 40, 3) and plain.sha256 != picture.sha256


def test_photos_that_are_not_pictures_or_too_large_are_refused(monkeypatch):
    with pytest.raises(FinderInputError, match="could not be read as a picture"):
        pictures.decode_photo(b"%PDF-1.7 not a picture")
    monkeypatch.setattr(pictures, "MAX_PHOTO_PIXELS", 500)
    with pytest.raises(FinderInputError, match="at most 500 pixels are read"):
        pictures.decode_photo(_jpeg(40, 20))
