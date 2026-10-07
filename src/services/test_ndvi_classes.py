"""ndvi_classes: one NDVI scale for map colours, legends and Sage's wording."""

from __future__ import annotations

import pytest

from src.services.ndvi_classes import (
    NDVI_ANOMALY_ALERTS,
    anomaly_alert_scale_text,
    legend,
    ndvi_anomaly_alert,
    ndvi_class,
    scale_text,
)


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


@pytest.mark.parametrize("z,key", [
    (-2.4, "high"), (-1.5, "high"), (-1.49, "moderate"), (-1.0, "moderate"), (-0.99, None), (0.0, None), (1.3, None),
])
def test_anomaly_alert_breaks_are_inclusive_upper_bounds(z, key):
    alert = ndvi_anomaly_alert(z)
    assert (alert.key if alert else None) == key


def test_no_anomaly_value_is_no_alert():
    assert ndvi_anomaly_alert(None) is None


def test_anomaly_scale_text_names_every_class_at_its_break():
    text = anomaly_alert_scale_text()
    for alert in NDVI_ANOMALY_ALERTS:
        assert alert.key in text and f"{alert.max_z:g}" in text
