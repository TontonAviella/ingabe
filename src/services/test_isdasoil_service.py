"""iSDAsoil back-transforms on fixed raw band means (no network).

RAW holds the band means `_read_point` returned at Cyampirita (lon 30.4245,
lat -1.6969) on 2026-10-06: [mean 0-20, mean 20-50, stdev 0-20, stdev 20-50],
plus a few edge cases.
"""
from __future__ import annotations

import numpy as np
import pytest

from src.services import isdasoil_service

CYAMPIRITA = (30.4245, -1.6969)
RAW = {
    "nitrogen_total": [89.05, 62.3, 12.49, 7.25],
    "phosphorous_extractable": [24.5, 23.53, 1.21, 1.19],
    "potassium_extractable": [51.22, 47.55, 1.9, 1.98],
    "carbon_organic": [28.17, 23.0, 1.09, 1.1],
    "ph": [58.14, 56.8, 1.01, 1.17],
    "clay_content": [35.68, 39.94, 4.13, 5.2],
    "texture_class": [4.22, 4.4],
    "zinc_extractable": [3.0, 0.0, 5.0, 0.0],  # stdev larger than the mean; nodata at 20-50
    "iron_extractable": [40.0, 41.0, 0.0, 0.0],  # no stdev
}


@pytest.fixture
def soil(monkeypatch):
    def read_point(url, lon, lat, buffer_m=150.0):
        return np.array(RAW[url.rsplit("/", 1)[-1].removesuffix(".tif")], dtype=np.float64)

    monkeypatch.setattr(isdasoil_service, "_read_point", read_point)

    def query(*properties: str, depth: str = "0-20") -> dict:
        result = isdasoil_service.query_soil_point(*CYAMPIRITA, properties=list(properties), depth=depth)
        assert result["status"] == "success"
        return result["properties"]

    return query


def test_linear_property_keeps_a_plus_minus_spread(soil):
    ph = soil("ph")["ph"]
    assert (ph["value"], ph["uncertainty"], ph["likely_range"]) == (5.81, 0.1, [5.71, 5.92])


def test_log_scaled_values(soil):
    props = soil("nitrogen_total", "phosphorous_extractable", "potassium_extractable", "carbon_organic")
    assert {name: entry["value"] for name, entry in props.items()} == {
        "nitrogen_total": 1.44, "phosphorous_extractable": 10.59,
        "potassium_extractable": 166.67, "carbon_organic": 15.73,
    }


def test_log_scaled_spread_is_a_range_not_plus_minus(soil):
    # Before: expm1 of the log-space stdev alone, "10.59 +/- 0.13 ppm" for phosphorus.
    # After: the 1-sd interval taken in log space, then back-transformed.
    props = soil("nitrogen_total", "phosphorous_extractable", "potassium_extractable", "carbon_organic")
    assert {name: entry["likely_range"] for name, entry in props.items()} == {
        "nitrogen_total": [1.15, 1.76], "phosphorous_extractable": [9.27, 12.08],
        "potassium_extractable": [137.66, 201.76], "carbon_organic": [14.0, 17.65],
    }
    assert not any("uncertainty" in entry for entry in props.values())


def test_range_never_goes_below_zero(soil):
    zinc = soil("zinc_extractable")["zinc_extractable"]
    assert (zinc["value"], zinc["likely_range"]) == (0.35, [0.0, 1.23])


def test_result_says_what_the_spread_fields_mean(soil):
    result = isdasoil_service.query_soil_point(*CYAMPIRITA, properties=["ph"])
    assert "68%" in result["spread_note"]


def test_deeper_layer_reads_its_own_bands(soil):
    ph = soil("ph", depth="20-50")["ph"]
    assert (ph["value"], ph["uncertainty"], ph["likely_range"], ph["depth"]) == (5.68, 0.12, [5.56, 5.8], "20-50 cm")


def test_nodata_is_missing_not_zero(soil):
    zinc = soil("zinc_extractable", depth="20-50")["zinc_extractable"]
    assert zinc["value"] is None
    assert zinc["note"] == "No data at this location"


def test_no_spread_without_a_stdev_band(soil):
    iron = soil("iron_extractable")["iron_extractable"]
    assert iron["value"] == 53.6
    assert "uncertainty" not in iron and "likely_range" not in iron


def test_texture_class_is_named(soil):
    texture = soil("texture_class")["texture_class"]
    assert texture["texture_name"] == "Clay loam"
    assert "uncertainty" not in texture and "likely_range" not in texture
