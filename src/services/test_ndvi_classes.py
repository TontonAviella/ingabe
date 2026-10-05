"""ndvi_classes: one NDVI scale for map colours, legends and Sage's wording."""

from __future__ import annotations

import pytest

from src.services.ndvi_classes import legend, ndvi_class, scale_text


@pytest.mark.parametrize("value,key", [
    (-0.3, "bare"), (0.0, "bare"), (0.099, "bare"), (0.1, "sparse"), (0.29, "sparse"),
    (0.3, "moderate"), (0.59, "moderate"), (0.6, "dense"), (0.95, "dense"),
])
def test_class_boundaries_are_inclusive_lower_bounds(value, key):
    assert ndvi_class(value).key == key


def test_no_value_has_no_class():
    assert ndvi_class(None) is None


def test_zero_is_a_value_not_missing():
    assert ndvi_class(0.0) is not None


def test_legend_and_sage_text_describe_the_same_scale():
    ranges = [item["range"] for item in legend()["items"]]
    assert ranges == ["below 0.1", "0.1-0.3", "0.3-0.6", "0.6 and above"]
    text = scale_text()
    for item in legend()["items"]:
        assert f"{item['range']} = {item['label'].lower()}" in text
