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
    assert out["a"].said == "health index (NDVI) 0.60"
    assert field_health.said({"a": {"ndvi": 0.60}, "b": {"ndvi": 0.71}}, reflectance=False)["a"].said is None


@pytest.mark.parametrize("rank, total, words", [(1, 15, "lowest of 15 fields"), (3, 15, "3rd lowest of 15 fields"),
                                                (11, 15, "11th lowest of 15 fields"), (12, 15, "12th lowest of 15 fields"),
                                                (22, 30, "22nd lowest of 30 fields"), (15, 15, "highest of 15 fields")])
def test_place_words(rank, total, words):
    assert field_health._place(rank, total) == words


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


def test_measured_once_then_read(tmp_path, monkeypatch):
    path = _photo(tmp_path / "ms.tif", [0.20, 0.30, 0.45, 0.45])
    store = _Store()
    features = [{"geometry": _field(c)} for c in range(3)]
    first = asyncio.run(field_health.of_features(store, "b", "photo-1", path, features))
    assert first[field_health.key_of(features[0]["geometry"])].said == "health index (NDVI) 0.60, lowest of 3 fields"
    monkeypatch.setattr(field_health, "measure", lambda *a: pytest.fail("measured twice"))
    again = asyncio.run(field_health.of_features(store, "b", "photo-1", "unused", features))
    assert again == first and store.puts == 1


def test_only_new_outlines_are_measured(tmp_path, monkeypatch):
    path = _photo(tmp_path / "ms.tif", [0.20, 0.30, 0.45, 0.45])
    store = _Store()
    features = [{"geometry": _field(c)} for c in range(4)]
    asyncio.run(field_health.of_features(store, "b", "photo-2", path, features[:3]))
    asked = []
    real = field_health.measure
    monkeypatch.setattr(field_health, "measure", lambda url, outlines: asked.append(len(outlines)) or real(url, outlines))
    out = asyncio.run(field_health.of_features(store, "b", "photo-2", path, features))
    assert asked == [1] and len(out) == 4


def test_colour_photo_is_asked_once(tmp_path, monkeypatch):
    path = _photo(tmp_path / "rgb.tif", [60, 60, 60, 60], dtype="uint8", names=None)
    store = _Store()
    features = [{"geometry": _field(0)}]
    assert asyncio.run(field_health.of_features(store, "b", "photo-3", path, features)) == {}
    monkeypatch.setattr(field_health, "measure", lambda *a: pytest.fail("a colour photo was opened again"))
    assert asyncio.run(field_health.of_features(store, "b", "photo-3", path, features)) == {}


def test_a_field_says_its_health_in_its_one_line():
    facts = field_facts._facts(7, "Field 7", 0.18, None, None, None, None, None,
                               field_health.FieldHealth(said="health index (NDVI) 0.71, lowest of 3 fields", ndvi=0.71))
    assert field_facts.said(facts) == "Field 7 · 0.18 ha · health index (NDVI) 0.71, lowest of 3 fields"
    assert field_facts._facts(7, "Field 7", 0.18, None, None, None, None).health_said is None
