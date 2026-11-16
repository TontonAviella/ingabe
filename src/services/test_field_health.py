"""Each field's health index: measured from the photo's named bands, said with its place and no verdict."""

import asyncio

import numpy as np
import pytest
import rasterio
from pyproj import Transformer
from rasterio.transform import from_origin
from shapely.geometry import box, mapping
from shapely.ops import transform as reproject

from src.services import field_facts, field_health

WEST, NORTH, PIXEL = 500000.0, 9800000.0, 0.5
TO_WGS84 = Transformer.from_crs("EPSG:32736", "EPSG:4326", always_xy=True).transform


def _field(col: int) -> dict:
    """A 40 m square in column `col` of the test photo (four columns of 50 m), as WGS84 GeoJSON."""
    left = WEST + col * 50 + 5
    return mapping(reproject(TO_WGS84, box(left, NORTH - 45, left + 40, NORTH - 5)))


def _photo(path, nir_by_column, dtype="float32", names=("Green", "Red", "RedEdge", "NIR"), scale=1.0):
    """200 m x 100 m: green 0.08, red 0.05, red edge 0.20, and near-infrared as given per 50 m column."""
    nir = np.repeat(np.array(nir_by_column, dtype="float64"), 100)[None, :].repeat(200, axis=0)
    layers = [np.full((200, 400), 0.08), np.full((200, 400), 0.05), np.full((200, 400), 0.20), nir]
    with rasterio.open(path, "w", driver="GTiff", width=400, height=200, count=len(layers), dtype=dtype,
                       crs="EPSG:32736", transform=from_origin(WEST, NORTH, PIXEL, PIXEL)) as ds:
        for i, layer in enumerate(layers, start=1):
            ds.write((layer * scale).astype(dtype), i)
            if names:
                ds.set_band_description(i, names[i - 1])
    return str(path)


def test_each_field_is_measured_from_the_named_bands(tmp_path):
    path = _photo(tmp_path / "ms.tif", [0.20, 0.30, 0.45, 0.45])
    outlines = {f"f{c}": _field(c) for c in range(3)}
    result = field_health.measure(path, outlines)
    assert result["nir"] and result["reflectance"]
    assert result["fields"]["f0"]["ndvi"] == pytest.approx((0.20 - 0.05) / 0.25, abs=1e-3)
    assert result["fields"]["f2"]["ndvi"] == pytest.approx((0.45 - 0.05) / 0.50, abs=1e-3)
    assert result["fields"]["f1"]["ndre"] == pytest.approx((0.30 - 0.20) / 0.50, abs=1e-3)
    assert result["fields"]["f1"]["gndvi"] == pytest.approx((0.30 - 0.08) / 0.38, abs=1e-3)


def test_colour_photo_has_no_health(tmp_path):
    path = _photo(tmp_path / "rgb.tif", [60, 60, 60, 60], dtype="uint8", names=None, scale=1.0)
    assert field_health.measure(path, {"f0": _field(0)}) == {"nir": False, "reflectance": False, "fields": {}}


def test_field_off_the_photo_is_not_measured(tmp_path):
    path = _photo(tmp_path / "ms.tif", [0.2, 0.3, 0.4, 0.5])
    away = mapping(reproject(TO_WGS84, box(WEST + 900, NORTH - 45, WEST + 940, NORTH - 5)))
    half = mapping(reproject(TO_WGS84, box(WEST - 60, NORTH - 45, WEST + 20, NORTH - 5)))  # a quarter on the photo
    fields = field_health.measure(path, {"away": away, "half": half, "in": _field(1)})["fields"]
    assert fields["away"]["ndvi"] is None and fields["half"]["ndvi"] is None and fields["in"]["ndvi"] is not None


def test_said_with_place_and_number_on_reflectance():
    fields = {"a": {"ndvi": 0.60}, "b": {"ndvi": 0.71}, "c": {"ndvi": 0.80}, "d": {"ndvi": None}}
    out = field_health.said(fields, reflectance=True)
    assert out["a"].said == "health index (NDVI) 0.60, lowest of 3 fields"
    assert out["b"].said == "health index (NDVI) 0.71, 2nd lowest of 3 fields"
    assert out["c"].said == "health index (NDVI) 0.80, highest of 3 fields"
    assert "d" not in out and out["b"].ndvi == 0.71


def test_raw_camera_values_get_a_place_and_no_number():
    fields = {"a": {"ndvi": 0.60}, "b": {"ndvi": 0.71}, "c": {"ndvi": 0.80}}
    out = field_health.said(fields, reflectance=False)
    assert out["a"].said == "health index (NDVI): lowest of 3 fields" and out["a"].ndvi is None
    assert "0.6" not in out["a"].said


def test_two_fields_are_too_few_for_a_place():
    out = field_health.said({"a": {"ndvi": 0.60}, "b": {"ndvi": 0.71}}, reflectance=True)
    assert key_is_stable()
    assert out["a"].said == "health index (NDVI) 0.60"
    assert field_health.said({"a": {"ndvi": 0.60}, "b": {"ndvi": 0.71}}, reflectance=False)["a"].said is None


def key_is_stable() -> bool:
    """An outline that went through coordinates and back (a difference far below a millimetre) keeps its key."""
    a = {"type": "Polygon", "coordinates": [[[30.4269721234, -1.6967691234], [30.4279, -1.6967], [30.4279, -1.6977],
                                             [30.4269721234, -1.6967691234]]]}
    b = {"type": "Polygon", "coordinates": [[[30.4269721234 + 1e-10, -1.6967691234 - 1e-10], [30.4279, -1.6967],
                                             [30.4279, -1.6977], [30.4269721234 + 1e-10, -1.6967691234 - 1e-10]]]}
    moved = {"type": "Polygon", "coordinates": [[[30.42698, -1.6967691234], [30.4279, -1.6967], [30.4279, -1.6977],
                                                 [30.42698, -1.6967691234]]]}
    return field_health.key_of(a) == field_health.key_of(b) != field_health.key_of(moved)


def test_fields_that_show_the_same_index_share_a_place():
    fields = {"a": {"ndvi": 0.601}, "b": {"ndvi": 0.604}, "c": {"ndvi": 0.71}, "d": {"ndvi": 0.80}}
    out = field_health.said(fields, reflectance=True)
    assert out["a"].said == out["b"].said == "health index (NDVI) 0.60, lowest of 4 fields, level with another"
    assert out["c"].said == "health index (NDVI) 0.71, 3rd lowest of 4 fields"
    same = field_health.said({k: {"ndvi": 0.5} for k in "abc"}, reflectance=False)
    assert same["a"].said is None  # every field level: no place to say, and no number on raw values


@pytest.mark.parametrize("below, above, total, words", [(0, 14, 15, "lowest of 15 fields"), (2, 12, 15, "3rd lowest of 15 fields"),
                                                        (10, 4, 15, "11th lowest of 15 fields"), (11, 3, 15, "12th lowest of 15 fields"),
                                                        (21, 8, 30, "22nd lowest of 30 fields"), (14, 0, 15, "highest of 15 fields")])
def test_place_words(below, above, total, words):
    assert field_health._place(below, above, False, total) == words


class _Store:
    """Object storage in memory, with the two calls the module makes."""

    class exceptions:
        class NoSuchKey(Exception):
            pass

    def __init__(self):
        self.objects, self.puts = {}, 0

    async def get_object(self, Bucket, Key):
        if Key not in self.objects:
            raise self.exceptions.NoSuchKey()
        data = self.objects[Key]

        class Body:
            async def __aenter__(self):
                return self

            async def __aexit__(self, *_):
                return False

            async def read(self):
                return data

        return {"Body": Body()}

    async def put_object(self, Bucket, Key, Body, ContentType):
        self.objects[Key], self.puts = Body, self.puts + 1


def _ask(store, key, path, features):
    """One ask, then wait for the measurement it started beside it."""
    async def run():
        out = await field_health.of_features(store, "b", key, path, features)
        await field_health.settled(key)
        return out
    return asyncio.run(run())


def test_first_ask_starts_the_measurement_and_the_next_reads_it(tmp_path, monkeypatch):
    path = _photo(tmp_path / "ms.tif", [0.20, 0.30, 0.45, 0.45])
    store = _Store()
    features = [{"geometry": _field(c)} for c in range(3)]
    assert _ask(store, "photo-1", path, features) == {}  # nobody waits for the photo to be read
    monkeypatch.setattr(field_health, "measure", lambda *a: pytest.fail("measured twice"))
    again = _ask(store, "photo-1", "unused", features)
    assert again[field_health.key_of(features[0]["geometry"])].said == "health index (NDVI) 0.60, lowest of 3 fields"
    assert store.puts == 1


def test_only_new_outlines_are_measured(tmp_path, monkeypatch):
    path = _photo(tmp_path / "ms.tif", [0.20, 0.30, 0.45, 0.45])
    store = _Store()
    features = [{"geometry": _field(c)} for c in range(4)]
    _ask(store, "photo-2", path, features[:3])
    asked = []
    real = field_health.measure
    monkeypatch.setattr(field_health, "measure", lambda url, outlines: asked.append(len(outlines)) or real(url, outlines))
    assert len(_ask(store, "photo-2", path, features)) == 3  # the three kept; the fourth is being measured
    assert asked == [1] and len(_ask(store, "photo-2", path, features)) == 4


def test_colour_photo_is_opened_once(tmp_path, monkeypatch):
    path = _photo(tmp_path / "rgb.tif", [60, 60, 60, 60], dtype="uint8", names=None)
    store = _Store()
    features = [{"geometry": _field(0)}]
    assert _ask(store, "photo-3", path, features) == {}
    monkeypatch.setattr(field_health, "measure", lambda *a: pytest.fail("a colour photo was opened again"))
    assert _ask(store, "photo-3", path, features) == {}


def test_a_photo_that_cannot_be_read_is_not_tried_on_every_ask(tmp_path, monkeypatch):
    store, calls = _Store(), []
    features = [{"geometry": _field(0)}]

    def broken(*_):
        calls.append(1)
        raise OSError("the photo could not be opened")

    monkeypatch.setattr(field_health, "measure", broken)

    async def run():
        for _ in range(3):
            assert await field_health.of_features(store, "b", "photo-4", "missing.tif", features) == {}
            await field_health.settled("photo-4")

    asyncio.run(run())
    assert calls == [1] and store.puts == 0  # nothing wrong is kept, and it waits before trying again
    field_health._failed_at.pop("photo-4", None)


def test_a_field_says_its_health_in_its_one_line():
    facts = field_facts._facts(7, "Field 7", 0.18, None, None, None, None, None,
                               field_health.FieldHealth(said="health index (NDVI) 0.71, lowest of 3 fields", ndvi=0.71))
    assert field_facts.said(facts) == "Field 7 · 0.18 ha · health index (NDVI) 0.71, lowest of 3 fields"
    assert field_facts._facts(7, "Field 7", 0.18, None, None, None, None).health_said is None
