"""The reader says which band is which: checked, written into the photo's copy, and read by everything after."""

import numpy as np
import pytest
import rasterio
from rasterio.transform import from_origin

from src.services import band_names, field_health, photo_bands
from src.services.test_field_health import _field


def _unnamed(path, values, dtype="float32"):
    """A 200 m x 100 m photo of flat bands with no names, as Agisoft Metashape writes them."""
    with rasterio.open(path, "w", driver="GTiff", width=400, height=200, count=len(values), dtype=dtype,
                       crs="EPSG:32736", transform=from_origin(500000.0, 9800000.0, 0.5, 0.5)) as ds:
        for i, value in enumerate(values, start=1):
            ds.write(np.full((200, 400), value, dtype=dtype), i)
    return str(path)


@pytest.mark.parametrize("order, count, unit, words", [
    (["blue", "green", "red"], 5, None, "This photo has 5 bands"),
    (["blue", "green", "red", "red", "nir"], 5, None, "Red is given to two bands"),
    (["blue", "green", "red", "red_edge", "swir"], 5, None, "is not a band the app knows"),
    (["blue", "green", "other", "other", "other"], 5, None, "Name at least the red and the near-infrared"),
    (["blue", "green", "red", "red_edge", "nir"], 5, "celsius", "The unit is for a thermal band"),
    (["thermal"], 1, "fahrenheit", "The unit is for a thermal band")])
def test_what_the_reader_says_is_checked(order, count, unit, words):
    with pytest.raises(ValueError, match=words):
        band_names.check(order, count, unit)


def test_orders_that_can_be_measured_pass():
    band_names.check(["blue", "green", "red", "red_edge", "nir"], 5)
    band_names.check(["other", "red", "other", "nir"], 4)  # two bands the reader does not know are both "other"
    band_names.check(["thermal"], 1, "kelvin")
    for count, presets in band_names.PRESETS.items():
        for preset in presets:
            band_names.check(preset["order"], count)


def test_the_names_are_written_into_the_copy_and_the_pixels_are_not_touched(tmp_path):
    source = _unnamed(tmp_path / "metashape.tif", [0.03, 0.06, 0.05, 0.20, 0.45])
    with rasterio.open(source) as ds:
        before = band_names.state(ds)
        pixels = ds.read()
    assert before["needs_names"] and before["kind"] == photo_bands.UNKNOWN and not before["said_by_reader"]
    assert [p["order"] for p in before["presets"]][0] == ["blue", "green", "red", "red_edge", "nir"]
    target = str(tmp_path / "named.tif")
    band_names.rewrite(source, target, ["blue", "green", "red", "red_edge", "nir"], None, str(tmp_path))
    with rasterio.open(target) as ds:
        after = band_names.state(ds)
        assert np.array_equal(ds.read(), pixels)
    assert not after["needs_names"] and after["said_by_reader"] and after["presets"] == []
    assert after["roles"] == {"blue": 1, "green": 2, "red": 3, "red_edge": 4, "nir": 5}
    result = field_health.measure(target, {"f0": _field(0)})
    assert result["reflectance"] and result["fields"]["f0"]["ndvi"] == pytest.approx((0.45 - 0.05) / 0.50, abs=1e-3)
    assert field_health.measure(source, {"f0": _field(0)})["nir"] is False  # before: nothing to measure


def test_another_order_gives_another_reading(tmp_path):
    source = _unnamed(tmp_path / "ms.tif", [0.03, 0.06, 0.05, 0.45, 0.20])  # near-infrared fourth, red edge fifth
    target = str(tmp_path / "named.tif")
    band_names.rewrite(source, target, ["blue", "green", "red", "nir", "red_edge"], None, str(tmp_path))
    with rasterio.open(target) as ds:
        assert photo_bands.of(ds).nir == 4 and photo_bands.of(ds).red_edge == 5


def test_a_thermal_photo_gets_its_unit_from_the_reader(tmp_path):
    source = _unnamed(tmp_path / "thermal.tif", [24.5])
    assert field_health.measure(source, {"f0": _field(0)})["nir"] is False
    target = str(tmp_path / "named.tif")
    band_names.rewrite(source, target, ["thermal"], "celsius", str(tmp_path))
    result = field_health.measure(target, {"f0": _field(0)})
    assert result["thermal_unit"] == "celsius" and result["fields"]["f0"]["temp"] == pytest.approx(24.5, abs=0.01)
    with rasterio.open(target) as ds:
        state = band_names.state(ds)
    assert state["roles"] == {"thermal": 1} and not state["needs_names"]
    no_unit = str(tmp_path / "no_unit.tif")
    band_names.rewrite(source, no_unit, ["thermal"], None, str(tmp_path))
    result = field_health.measure(no_unit, {"f0": _field(0)})
    assert result["nir"] is True and result["thermal_unit"] is None  # thermal as the reader said, with no degrees
    with rasterio.open(source) as ds:
        assert band_names.state(ds)["needs_names"]  # one unnamed band: a height map or a thermal photo, the reader says


def test_a_file_that_names_its_bands_does_not_need_the_reader(tmp_path):
    path = _unnamed(tmp_path / "named.tif", [0.08, 0.05, 0.2, 0.4])
    with rasterio.open(path, "r+") as ds:
        for i, name in enumerate(("Green", "Red", "RedEdge", "NIR"), start=1):
            ds.set_band_description(i, name)
    with rasterio.open(path) as ds:
        state = band_names.state(ds)
    assert not state["needs_names"] and state["presets"] == [] and not state["said_by_reader"]


def test_a_failed_rewrite_says_so_without_the_command(tmp_path):
    with pytest.raises(OSError) as error:
        band_names.rewrite(str(tmp_path / "missing.tif"), str(tmp_path / "out.tif"), ["thermal"], None, str(tmp_path))
    assert "missing.tif" not in str(error.value)
