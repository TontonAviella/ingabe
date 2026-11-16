"""Band roles are read from the file, never guessed from position; a multispectral photo gives NDVI and NDRE."""

import asyncio

import numpy as np
import pytest
import rasterio
from rasterio.enums import ColorInterp
from rasterio.transform import from_origin

from src.services import drone_cards, drone_first_look, photo_bands


def _photo(path, names, values, interps=None, dtype="uint16"):
    """A small GeoTIFF: 100 m across, one flat value per band, descriptions as given (None = unnamed)."""
    with rasterio.open(path, "w", driver="GTiff", width=200, height=200, count=len(values), dtype=dtype,
                       crs="EPSG:32736", transform=from_origin(500000, 9800000, 0.5, 0.5)) as ds:
        for i, value in enumerate(values, start=1):
            ds.write(np.full((200, 200), value, dtype=dtype), i)
            if names and names[i - 1]:
                ds.set_band_description(i, names[i - 1])
        if interps:
            ds.colorinterp = interps
    return str(path)


def test_colour_photo_is_red_green_blue_by_position():
    assert photo_bands.roles([None] * 3, [], 3) == photo_bands.Bands()
    assert photo_bands.roles([None] * 4, [], 4).rgb == [1, 2, 3]  # the fourth band is transparency


@pytest.mark.parametrize("names, expected", [
    (["Blue", "Green", "Red", "Red Edge", "NIR"], (3, 2, 1, 4, 5)),
    (["green", "red", "rededge", "nir"], (2, 1, None, 3, 4)),  # no blue band on this camera
    (["Band 1 (475 nm)", "560nm", "668 nm", "717 nm", "842 nm"], (3, 2, 1, 4, 5)),
    (["Near-Infrared", "Red", "Green", "Blue", "RE"], (2, 3, 4, 5, 1)),
])
def test_named_bands_decide_the_roles(names, expected):
    bands = photo_bands.roles(names, [], len(names))
    assert bands.kind == photo_bands.MULTISPECTRAL
    assert (bands.red, bands.green, bands.blue, bands.red_edge, bands.nir) == expected


def test_near_infrared_is_never_read_as_red():
    assert photo_bands.roles(["Near Infrared"], [], 1).red is None


def test_five_unnamed_bands_are_unknown_not_guessed():
    bands = photo_bands.roles([None] * 5, [], 5)
    assert bands.kind == photo_bands.UNKNOWN
    with pytest.raises(photo_bands.BandsUnknown):
        _ = bands.red_green
    with pytest.raises(photo_bands.BandsUnknown):
        _ = bands.rgb


def test_two_bands_claiming_one_role_are_unknown():
    assert photo_bands.roles(["Red", "Red", "Green", "NIR", "Blue"], [], 5).kind == photo_bands.UNKNOWN


def test_a_wavelength_with_no_name_is_not_guessed_from_its_words():
    assert photo_bands.roles(["red 1000 nm", "Green", "Red", "Blue", "NIR"], [], 5).red == 3


def test_colour_interpretation_names_a_band_without_a_description():
    bands = photo_bands.roles([None, None, None], [ColorInterp.blue, ColorInterp.green, ColorInterp.red], 3)
    assert bands.kind == photo_bands.COLOUR and bands.rgb == [3, 2, 1]


@pytest.mark.parametrize("names", [["B1", "B2", "B3"], ["R1", "R2", "R3"], ["Band 1", "Band 2", "Band 3"],
                                   ["Red", "Red", "Blue"]])
def test_colour_photo_with_odd_names_stays_by_position(names):
    assert photo_bands.roles(names, [], 3) == photo_bands.Bands()


def test_numbered_extra_bands_name_nothing():
    assert photo_bands.roles(["Blue", "Green", "Red", "NIR1", "NIR2"], [], 5).kind == photo_bands.UNKNOWN


def test_a_longer_wavelength_is_not_read_as_a_shorter_one():
    bands = photo_bands.roles(["Green", "Red", "NIR", "1550nm", "1650 nm"], [], 5)
    assert (bands.green, bands.red, bands.nir) == (1, 2, 3)


def test_colour_photo_with_one_named_extra_band():
    # Its red band is not described, so it stays the colour photo it was before, and the extra band is not used.
    assert photo_bands.roles([None, None, None, "NIR"], [], 4) == photo_bands.Bands()


def test_a_transparency_band_never_gets_a_role():
    interps = [ColorInterp.red, ColorInterp.green, ColorInterp.blue, ColorInterp.alpha]
    assert photo_bands.roles(["Red", "Green", "Blue", "NIR"], interps, 4) == photo_bands.Bands()


def test_stamped_colours_do_not_name_a_multispectral_stack():
    # Blue, green, red, red edge, near-infrared with red, green, blue stamped on the first three bands.
    interps = [ColorInterp.red, ColorInterp.green, ColorInterp.blue, ColorInterp.undefined, ColorInterp.undefined]
    assert photo_bands.roles([None, None, None, "RedEdge", "NIR"], interps, 5).kind == photo_bands.UNKNOWN


def test_description_against_its_own_stamp_is_a_contradiction():
    interps = [ColorInterp.red, ColorInterp.green, ColorInterp.blue, ColorInterp.undefined, ColorInterp.undefined]
    assert photo_bands.roles(["Blue", "Green", "Red", "RedEdge", "NIR"], interps, 5).kind == photo_bands.UNKNOWN


def test_unnamed_bands_that_are_not_a_picture_are_unknown():
    assert photo_bands.roles([None] * 4, [], 4, ["uint16"] * 4).kind == photo_bands.UNKNOWN
    assert photo_bands.roles([None] * 3, [], 3, ["float32"] * 3).kind == photo_bands.UNKNOWN


def test_no_picture_from_bands_that_are_not_eight_bit():
    bands = photo_bands.roles(["Blue", "Green", "Red", "RedEdge", "NIR"], [], 5, ["uint16"] * 5)
    assert bands.red_green == [3, 2] and not bands.picture
    with pytest.raises(photo_bands.BandsUnknown):
        _ = bands.rgb


def test_reflectance_is_decimals_from_zero_to_about_one():
    assert photo_bands.looks_like_reflectance(np.array([0.05, 0.4], dtype="float32"))
    assert not photo_bands.looks_like_reflectance(np.array([300, 3000], dtype="uint16"))
    assert not photo_bands.looks_like_reflectance(np.array([300.0, 3000.0], dtype="float32"))
    assert np.isnan(photo_bands.ndvi(np.array([-0.2]), np.array([0.1]))[0])


def test_indices():
    assert photo_bands.ndvi(np.array([80.0]), np.array([20.0]))[0] == pytest.approx(0.6)
    assert photo_bands.ndre(np.array([80.0]), np.array([40.0]))[0] == pytest.approx(1 / 3)
    assert np.isnan(photo_bands.ndvi(np.array([0.0]), np.array([0.0]))[0])
    masked = np.ma.masked_array([80.0], mask=[True])
    assert np.isnan(photo_bands.ndvi(masked, np.array([20.0]))[0])


def _look(path):
    layer = {"layer_id": "Lbands", "name": "Bands", "bounds": None,
             "metadata": {"band_count": rasterio.open(path).count}}
    return asyncio.run(drone_first_look.look_at_layer(None, layer, path))


def test_multispectral_photo_is_measured_from_its_named_bands(tmp_path):
    # Blue first: by position, "red" and "green" would be blue 300 and green 900.
    path = _photo(tmp_path / "ms.tif", ["Blue", "Green", "Red", "RedEdge", "NIR"], [300, 900, 600, 2000, 3000])
    look = _look(path)
    assert look.bands.kind == photo_bands.MULTISPECTRAL
    assert look.green.mean == pytest.approx((900 - 600) / (900 + 600), abs=1e-4)  # green and red, not blue and green
    assert look.health.ndvi_mean == pytest.approx((3000 - 600) / 3600, abs=1e-4)
    assert look.health.ndre_mean == pytest.approx((3000 - 2000) / 5000, abs=1e-4)
    assert look.health.lowest is None  # a flat photo: no zone stands out
    assert not look.health.reflectance and not look.bands.picture  # raw 16-bit counts
    for audience in ("farmer", "insurance", "agronomist", "scientist"):
        message = drone_first_look.compose(look, audience)
        assert "NDVI" in message and "no infrared" not in message
        assert "no index number" in message and "0.67" not in message  # raw counts: no number is said
    assert "bands 2 and 3" in drone_first_look.compose(look, "scientist")


def test_colour_photo_has_no_health_index(tmp_path):
    look = _look(_photo(tmp_path / "rgb.tif", None, [60, 90, 30], dtype="uint8"))
    assert look.bands == photo_bands.Bands() and look.health is None
    assert "NDVI" not in drone_first_look.compose(look, "farmer")


def test_unnamed_multispectral_photo_is_not_measured(tmp_path):
    assert _look(_photo(tmp_path / "unnamed.tif", None, [300, 900, 600, 2000, 3000])) is None


def test_lowest_zone_needs_a_real_gap():
    health = drone_first_look.Health(0.5, 0.3, 0.7, None, (("north", 0.62), ("centre", 0.6), ("south", 0.55)))
    assert health.lowest is None
    health = drone_first_look.Health(0.5, 0.3, 0.7, None, (("north", 0.62), ("south", 0.41)))
    assert health.lowest is None  # two zones are too few to call one the lowest
    health = drone_first_look.Health(0.5, 0.3, 0.7, None, (("north", 0.62), ("centre", 0.6), ("south", 0.41)))
    assert health.lowest == ("south", 0.41)
    assert "south is lowest, at 0.41" in drone_first_look.health_said(health)
    raw = drone_first_look.Health(0.5, 0.3, 0.7, None, health.zones, reflectance=False)
    assert "south has the lowest" in drone_first_look.health_said(raw) and "0.41" not in drone_first_look.health_said(raw)


def test_camera_card_states_the_measured_index(tmp_path):
    path = _photo(tmp_path / "ms.tif", ["Green", "Red", "RedEdge", "NIR"], [0.09, 0.06, 0.2, 0.3], dtype="float32")
    look = _look(path)
    with rasterio.open(path) as ds:
        bare = drone_cards.measure_bare_ground(ds)  # reads red and green by role, not bands 1 and 2
    analysis = drone_cards.PhotoAnalysis(layer_id="Lbands", look=look, band_count=4, bounds=[30.0, -2.0, 30.001, -1.999],
                                         bare=bare, zones={"type": "FeatureCollection", "features": []})
    assert analysis.camera == "multispectral"  # four bands, but the file names a near-infrared one
    answer = drone_cards.answer_card("learn_camera", analysis, "farmer", drone_cards.Here(photos=1))
    assert "NDVI" in answer["what"] and "0.67" in answer["what"]
    assert {"label": "NDRE across the photo", "value": "0.20"} in answer["facts"]
    assert "not built yet" in answer["todo"]


@pytest.mark.parametrize("name", ["LWIR", "Thermal", "Thermal Infrared", "TIR", "Band 6 (11000 nm)", "11 um", "10.5 µm"])
def test_thermal_band_is_named(name):
    bands = photo_bands.roles(["Blue", "Green", "Red", "RedEdge", "NIR", name], [], 6, ["uint16"] * 6)
    assert bands.thermal == 6 and bands.nir == 5 and bands.kind == photo_bands.MULTISPECTRAL


def test_short_wave_infrared_is_not_thermal():
    assert photo_bands.roles(["Blue", "Green", "Red", "RedEdge", "NIR", "2200 nm"], [], 6).thermal is None
    assert photo_bands.roles(["Blue", "Green", "Red", "RedEdge", "NIR", "1.6 um"], [], 6).thermal is None


def test_colour_photo_with_a_described_thermal_band():
    bands = photo_bands.roles([None, None, None, "Thermal"], [], 4)
    assert bands.kind == photo_bands.COLOUR and bands.rgb == [1, 2, 3] and bands.thermal == 4
    assert photo_bands.roles(["Thermal", None, None], [], 3).thermal is None  # one of the picture's own three


@pytest.mark.parametrize("unit, description, expected", [
    ("K", "LWIR", photo_bands.KELVIN), ("kelvin", None, photo_bands.KELVIN), ("C", "Thermal", photo_bands.CELSIUS),
    ("°C", None, photo_bands.CELSIUS), (None, "Temperature (degrees Celsius)", photo_bands.CELSIUS),
    (None, "LWIR", None), ("", "Thermal", None), ("cK", "LWIR", None), ("DN", "Thermal counts", None)])
def test_thermal_unit_is_only_what_the_file_states(unit, description, expected):
    assert photo_bands.thermal_unit(unit, description) == expected


def test_temperature_in_degrees_only_with_a_stated_unit():
    kelvin = photo_bands.temperature(np.array([29715, 30465, 0], dtype="uint16"), photo_bands.KELVIN, scale=0.01,
                                     whole_numbers=True)
    assert kelvin[:2] == pytest.approx([24.0, 31.5], abs=0.01) and np.isnan(kelvin[2])  # a zero is an empty pixel
    assert photo_bands.temperature(np.array([24.0, 31.5]), photo_bands.CELSIUS) == pytest.approx([24.0, 31.5])
    raw = photo_bands.temperature(np.array([300, 315], dtype="uint16"), None, whole_numbers=True)
    assert list(raw) == [300.0, 315.0]  # no unit stated: the stored values, good for their order only
    assert np.isnan(photo_bands.temperature(np.array([400.0]), photo_bands.CELSIUS)[0])  # no land surface is 400 degrees
