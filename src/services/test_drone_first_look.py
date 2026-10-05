"""Drone first look: where the field is weak, and how each reader hears it."""

from __future__ import annotations

import numpy as np
import pytest

from src.services import drone_first_look as fl


def _field(weak_rows=slice(0, 30), weak_cols=slice(60, 90), size=90):
    """Green everywhere (G > R), red-dominant (low green) in one block; north is row 0."""
    red = np.full((size, size), 60.0)
    green = np.full((size, size), 110.0)
    red[weak_rows, weak_cols] = 120.0
    green[weak_rows, weak_cols] = 100.0
    return np.ma.masked_array(red), np.ma.masked_array(green)


def test_a_weak_north_east_corner_is_found():
    g = fl.measure_greenness(*_field())
    assert g is not None
    assert [z.name for z in g.hotspots] == ["north-east"]
    assert g.low_share == pytest.approx(1 / 9, abs=0.01)


def test_an_even_field_has_no_weak_part():
    red, green = _field(weak_rows=slice(0, 0))
    g = fl.measure_greenness(red, green)
    assert g.hotspots == [] and g.uniform and g.low_share == 0


def test_nodata_is_not_counted_as_bare():
    red, green = _field(weak_rows=slice(0, 0))
    mask = np.zeros(red.shape, bool)
    mask[:, :45] = True  # left half outside the photo
    g = fl.measure_greenness(np.ma.masked_array(red, mask), np.ma.masked_array(green, mask))
    assert g.low_share == 0 and g.valid_pct == pytest.approx(50, abs=1)
    assert g.hotspots == []


def test_an_empty_image_gives_nothing():
    red = np.ma.masked_all((10, 10))
    assert fl.measure_greenness(red, red) is None


def test_place_names_take_the_largest_unit_per_level():
    units = {"cell": [{"name": "Kabarama"}], "sector": [{"name": "Ruhango"}, {"name": "Kinazi"}],
             "district": [{"name": "Ruhango"}], "village": [{"name": "X"}]}
    assert fl.place_name(units) == "Kabarama cell, Ruhango sector, Ruhango district"
    assert fl.place_name({}) is None


def _look():
    return fl.FirstLook(layer_name="Cyampirita", place="Kabarama cell, Ruhango sector, Ruhango district",
                        area_ha=4.2, resolution_cm=3.0, green=fl.measure_greenness(*_field()))


def test_the_farmer_hears_where_to_walk_in_plain_words():
    text = fl.compose(_look(), "farmer")
    assert "Kabarama cell" in text and "4.2 ha" in text and "**north-east** part looks less green" in text
    assert "Most of the field" in text  # 4.2 ha is a field
    assert "GRVI" not in text and "p10" not in text


def test_the_insurer_hears_a_share_and_that_it_is_not_a_loss_assessment():
    text = fl.compose(_look(), "insurance")
    assert "**11%**" in text and "north-east" in text and "Not a loss assessment" in text and "3 cm per pixel" in text


def test_agronomist_and_scientist_get_the_numbers_and_the_scientist_the_method():
    agro, sci = fl.compose(_look(), "agronomist"), fl.compose(_look(), "scientist")
    assert "GRVI" in agro and "north-east 100%" in agro and "Method:" not in agro
    assert "Method:" in sci and "3 x 3 grid" in sci


class _Conn:
    def __init__(self, owned=None, latest=None):
        self.owned, self.latest, self.created = owned, latest, []

    async def fetchval(self, sql, *args):
        if sql.startswith("SELECT id FROM conversations WHERE id"):
            return self.owned
        if sql.startswith("SELECT id FROM conversations"):
            return self.latest
        self.created.append(args)
        return 99


@pytest.mark.anyio
async def test_the_first_look_goes_to_the_open_chat_else_the_latest_else_a_new_one():
    assert await fl.conversation_for_upload(_Conn(owned=5, latest=7), "P1", "u", 5, "t") == 5
    assert await fl.conversation_for_upload(_Conn(owned=None, latest=7), "P1", "u", 5, "t") == 7  # not theirs
    conn = _Conn()
    assert await fl.conversation_for_upload(conn, "P1", "u", None, "Drone image: Cyampirita") == 99
    assert conn.created == [("P1", "u", "Drone image: Cyampirita")]


def test_a_big_patchy_photo_is_an_area_with_mixed_patches_and_a_greenest_part():
    size = 90
    rng = np.random.default_rng(0)
    red = np.full((size, size), 60.0)
    green = np.full((size, size), 110.0)
    speckle = rng.random((size, size)) < 0.45
    speckle[30:60, 60:90] = rng.random((30, 30)) < 0.1  # east: far fewer low-green pixels
    red[speckle], green[speckle] = 120.0, 100.0
    look = fl.FirstLook(layer_name="x", place=None, area_ha=124.0, resolution_cm=3.0,
                        green=fl.measure_greenness(np.ma.masked_array(red), np.ma.masked_array(green)))
    text = fl.compose(look, "farmer")
    assert "Most of the area" in text and "mixed all over the area" in text and "**east** part is the greenest" in text
    assert "greenest: east" in fl.compose(look, "agronomist")
