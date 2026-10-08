"""A SAR->NDVI model answers only for the area it was trained on (2026-10-07).

The predictor kept one model, trained on the first area anyone asked about, and used it for every
other area; when training failed it trained again on every request (~70 scenes each time).
Training data is stubbed in the shape _generate_training_data returns: (X, y) with the 120 lag
features _extract_features builds, or TrainingDataUnavailable.
"""

from __future__ import annotations

import threading
import time
from typing import Any, Dict, List

import numpy as np
import pytest

from src.services import sar_ndvi, sentinel1_service
from src.services.sar_ndvi import SARNDVIPredictor, TrainingDataUnavailable

KIGALI = (30.05, -1.97, 30.10, -1.92)
NYAGATARE = (30.30, -1.35, 30.35, -1.30)


@pytest.fixture(autouse=True)
def no_cropland_lookup(monkeypatch) -> None:
    """The cropland enrichment asks Digital Earth Africa; it is not what these tests are about."""
    monkeypatch.setattr(sar_ndvi, "_enrich_ndvi_with_cropland", lambda result, bbox: result)


@pytest.fixture
def sentinel1(monkeypatch) -> None:
    """Five Sentinel-1 scenes, in the shape Sentinel1Service.get_time_series returns."""
    series = {
        "status": "success",
        "dates": [f"2026-10-0{day}T16:13:37.123456Z" for day in (1, 2, 3, 4, 5)],
        "vv_means": [-10.5, -10.2, -10.8, -10.1, -10.4],
        "vv_stds": [1.1, 1.0, 1.2, 1.1, 1.0],
        "vh_means": [-17.2, -17.0, -17.5, -16.9, -17.1],
        "vh_stds": [1.5, 1.4, 1.6, 1.5, 1.4],
        "scene_count": 5,
    }

    class Sentinel1:
        def get_time_series(self, bbox, date_range, limit=20):
            return series

    monkeypatch.setattr(sentinel1_service, "get_sentinel1_service", Sentinel1)


@pytest.fixture
def training(monkeypatch) -> List[tuple]:
    """Training pairs whose NDVI is 0.2 around Kigali and 0.8 elsewhere; records each call."""
    calls: List[tuple] = []

    def generate(bbox, days_back=180):
        calls.append(tuple(bbox))
        rng = np.random.default_rng(0)
        ndvi = 0.2 if bbox[0] < 30.2 else 0.8
        return rng.normal(-12.0, 3.0, size=(12, 120)), np.full(12, ndvi)

    monkeypatch.setattr(sar_ndvi, "_generate_training_data", generate)
    return calls


def test_each_area_gets_its_own_model(sentinel1, training):
    pred = SARNDVIPredictor()
    kigali = pred.predict_ndvi(KIGALI)
    nyagatare = pred.predict_ndvi(NYAGATARE)
    assert kigali["predicted_ndvi"] == pytest.approx(0.2, abs=0.01)
    assert nyagatare["predicted_ndvi"] == pytest.approx(0.8, abs=0.01)
    assert training == [KIGALI, NYAGATARE]


def test_an_area_within_a_kilometre_reuses_the_model(sentinel1, training):
    pred = SARNDVIPredictor()
    pred.predict_ndvi(KIGALI)
    pred.predict_ndvi(tuple(v + 0.001 for v in KIGALI))
    assert len(training) == 1


def test_a_model_older_than_a_week_is_retrained(sentinel1, training, monkeypatch):
    pred = SARNDVIPredictor()
    pred.predict_ndvi(KIGALI)
    now = time.monotonic()
    monkeypatch.setattr(sar_ndvi.time, "monotonic", lambda: now + sar_ndvi._MODEL_MAX_AGE_S + 1)
    pred.predict_ndvi(KIGALI)
    assert len(training) == 2


def test_a_failed_training_is_not_retried_for_six_hours(sentinel1, monkeypatch):
    calls: List[Any] = []

    def generate(bbox, days_back=180):
        calls.append(bbox)
        raise TrainingDataUnavailable("3 Sentinel-2 NDVI observations in the last 180 days, need at least 5")

    monkeypatch.setattr(sar_ndvi, "_generate_training_data", generate)
    pred = SARNDVIPredictor()
    first: Dict[str, Any] = pred.predict_ndvi(KIGALI)
    second = pred.predict_ndvi(KIGALI)
    assert len(calls) == 1
    assert first["method"] == second["method"] == "empirical_cross_pol_ratio"
    assert second["model_unavailable_reason"] == first["model_unavailable_reason"]
    assert "3 Sentinel-2 NDVI observations" in second["model_unavailable_reason"]

    now = time.monotonic()
    monkeypatch.setattr(sar_ndvi.time, "monotonic", lambda: now + sar_ndvi._FAILED_RETRY_S + 1)
    pred.predict_ndvi(KIGALI)
    assert len(calls) == 2


def test_callers_waiting_on_one_area_share_one_training(sentinel1, training, monkeypatch):
    real = sar_ndvi._generate_training_data

    def slow(bbox, days_back=180):
        time.sleep(0.3)
        return real(bbox, days_back)

    monkeypatch.setattr(sar_ndvi, "_generate_training_data", slow)
    pred = SARNDVIPredictor()
    threads = [threading.Thread(target=pred.predict_ndvi, args=(KIGALI,)) for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert training == [KIGALI]
