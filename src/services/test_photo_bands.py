"""Band roles are read from the file, never guessed from position; a multispectral photo gives NDVI and NDRE."""

import asyncio

import numpy as np
import pytest
import rasterio
from rasterio.enums import ColorInterp
from rasterio.transform import from_origin

from src.services import drone_cards, drone_first_look, photo_bands


def _photo(path, names, values, interps=None):
    """A small GeoTIFF: 100 m across, one flat value per band, descriptions as given (None = unnamed)."""
    with rasterio.open(path, "w", driver="GTiff", width=200, height=200, count=len(values), dtype="uint16",
                       crs="EPSG:32736", transform=from_origin(500000, 9800000, 0.5, 0.5)) as ds:
        for i, value in enumerate(values, start=1):
            ds.write(np.full((200, 200), value, dtype="uint16"), i)
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
    for audience in ("farmer", "insurance", "agronomist", "scientist"):
        message = drone_first_look.compose(look, audience)
        assert "NDVI" in message and "no infrared" not in message
    assert "bands 2 and 3" in drone_first_look.compose(look, "scientist")


def test_colour_photo_has_no_health_index(tmp_path):
    look = _look(_photo(tmp_path / "rgb.tif", None, [600, 900, 300]))
    assert look.bands == photo_bands.Bands() and look.health is None
    assert "NDVI" not in drone_first_look.compose(look, "farmer")


def test_unnamed_multispectral_photo_is_not_measured(tmp_path):
    assert _look(_photo(tmp_path / "unnamed.tif", None, [300, 900, 600, 2000, 3000])) is None


def test_lowest_zone_needs_a_real_gap():
    health = drone_first_look.Health(0.5, 0.3, 0.7, None, (("north", 0.62), ("south", 0.55)))
    assert health.lowest is None
    health = drone_first_look.Health(0.5, 0.3, 0.7, None, (("north", 0.62), ("south", 0.41)))
    assert health.lowest == ("south", 0.41)
    assert "south is lowest, at 0.41" in drone_first_look.health_said(health)


def test_camera_card_states_the_measured_index(tmp_path):
    path = _photo(tmp_path / "ms.tif", ["Green", "Red", "RedEdge", "NIR"], [900, 600, 2000, 3000])
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
