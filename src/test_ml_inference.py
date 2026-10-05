# Copyright (C) 2025 Ingabe Ltd.
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU Affero General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU Affero General Public License for more details.
#
# You should have received a copy of the GNU Affero General Public License
# along with this program.  If not, see <http://www.gnu.org/licenses/>.

"""Unit tests for the Rwanda ML inference service."""

import pytest

from src.services.ml_inference import (
    CropClassifier,
    MLInferenceService,
    get_ml_service,
)


# ============================================================================
# Unit Tests: CropClassifier.classify_from_ndvi
# ============================================================================


def test_classify_ndvi_dense_vegetation():
    """Test CropClassifier with NDVI values indicating dense vegetation (>0.6)."""
    classifier = CropClassifier()
    ndvi_values = [0.65, 0.7, 0.8, 0.9]

    result = classifier.classify_from_ndvi(ndvi_values)

    assert result["method"] == "spectral_threshold"
    assert result["total_pixels"] == 4
    assert result["mean_ndvi"] == pytest.approx(0.7625, abs=0.001)
    assert result["classification"]["dense_vegetation"]["count"] == 4
    assert result["classification"]["dense_vegetation"]["percentage"] == 100.0


def test_classify_ndvi_bare_soil():
    """Test CropClassifier with NDVI values indicating bare soil (<0.15)."""
    classifier = CropClassifier()
    ndvi_values = [0.0, 0.05, 0.08, 0.1]

    result = classifier.classify_from_ndvi(ndvi_values)

    assert result["method"] == "spectral_threshold"
    assert result["total_pixels"] == 4
    # All values fall in bare_soil range [-0.1, 0.15)
    assert result["classification"]["bare_soil"]["count"] == 4


def test_classify_ndvi_mixed_classes():
    """Test CropClassifier with NDVI values across multiple land cover classes."""
    classifier = CropClassifier()
    # Mixed: water (-0.5), bare_soil (0.0), sparse (0.2), moderate (0.4), dense (0.7)
    ndvi_values = [-0.5, 0.0, 0.2, 0.4, 0.7]

    result = classifier.classify_from_ndvi(ndvi_values)

    assert result["method"] == "spectral_threshold"
    assert result["total_pixels"] == 5
    assert result["classification"]["water"]["count"] == 1
    assert result["classification"]["bare_soil"]["count"] == 1
    assert result["classification"]["sparse_vegetation"]["count"] == 1
    assert result["classification"]["moderate_vegetation"]["count"] == 1
    assert result["classification"]["dense_vegetation"]["count"] == 1


def test_classify_ndvi_edge_cases():
    """Test CropClassifier with NDVI values exactly at thresholds."""
    classifier = CropClassifier()
    # Thresholds: water [-1, -0.1), bare [-0.1, 0.15), sparse [0.15, 0.3), moderate [0.3, 0.6), dense [0.6, 1.0]
    # Test: -1.0, -0.1, 0.0, 0.15, 0.3, 0.6, 1.0
    ndvi_values = [-1.0, -0.1, 0.0, 0.15, 0.3, 0.6, 1.0]

    result = classifier.classify_from_ndvi(ndvi_values)

    assert result["method"] == "spectral_threshold"
    assert result["total_pixels"] == 7
    # -1.0 → water, -0.1 → bare_soil, 0.0 → bare_soil, 0.15 → sparse, 0.3 → moderate, 0.6 → dense, 1.0 → excluded (>= 1.0)
    assert result["classification"]["water"]["count"] == 1  # -1.0
    assert result["classification"]["bare_soil"]["count"] == 2  # -0.1, 0.0
    assert result["classification"]["sparse_vegetation"]["count"] == 1  # 0.15
    assert result["classification"]["moderate_vegetation"]["count"] == 1  # 0.3
    assert result["classification"]["dense_vegetation"]["count"] == 1  # 0.6
    # 1.0 is excluded because upper bound is exclusive


def test_classify_ndvi_below_negative_threshold():
    """Test CropClassifier with NDVI value below -0.1 (edge case for water)."""
    classifier = CropClassifier()
    ndvi_values = [-0.5, -0.2, -0.15]

    result = classifier.classify_from_ndvi(ndvi_values)

    assert result["method"] == "spectral_threshold"
    assert result["classification"]["water"]["count"] == 3
    assert result["classification"]["water"]["percentage"] == 100.0


def test_classify_ndvi_empty_array():
    """Test CropClassifier with empty NDVI array."""
    classifier = CropClassifier()
    ndvi_values = []

    result = classifier.classify_from_ndvi(ndvi_values)

    assert result["method"] == "spectral_threshold"
    assert result["total_pixels"] == 0
    assert result["mean_ndvi"] is None
    assert result["std_ndvi"] is None
    assert result["median_ndvi"] is None
    assert result["mode_class"] is None
    assert "histogram" in result
    for class_name in classifier.CROP_THRESHOLDS.keys():
        assert result["classification"][class_name]["count"] == 0
        assert result["classification"][class_name]["percentage"] == 0


# ============================================================================
# Unit Tests: CropClassifier.predict_yield_risk
# ============================================================================


def test_predict_yield_risk_improving_trend():
    """Test yield risk prediction with improving NDVI trend (increasing values)."""
    classifier = CropClassifier()
    timeseries = [
        {"date": "2024-01-01", "mean_ndvi": 0.3},
        {"date": "2024-01-08", "mean_ndvi": 0.4},
        {"date": "2024-01-15", "mean_ndvi": 0.5},
        {"date": "2024-01-22", "mean_ndvi": 0.6},
    ]

    result = classifier.predict_yield_risk(timeseries)

    assert result["method"] == "mann_kendall_trend"
    assert result["observations"] == 4
    assert result["latest_ndvi"] == 0.6
    assert result["trend_slope"] > 0.02  # Strong positive trend
    assert result["risk_level"] == "low"
    assert "increasing" in result["risk_description"].lower()


def test_predict_yield_risk_declining_trend():
    """Test yield risk prediction with declining NDVI trend (decreasing values)."""
    classifier = CropClassifier()
    timeseries = [
        {"date": "2024-01-01", "mean_ndvi": 0.6},
        {"date": "2024-01-08", "mean_ndvi": 0.5},
        {"date": "2024-01-15", "mean_ndvi": 0.4},
        {"date": "2024-01-22", "mean_ndvi": 0.3},
    ]

    result = classifier.predict_yield_risk(timeseries)

    assert result["method"] == "mann_kendall_trend"
    assert result["observations"] == 4
    assert result["latest_ndvi"] == 0.3
    assert result["trend_slope"] < -0.02  # Strong negative trend
    assert result["risk_level"] == "high"
    assert "declining" in result["risk_description"].lower()


def test_predict_yield_risk_critical_low_ndvi():
    """Test yield risk prediction with critically low latest NDVI (<0.2)."""
    classifier = CropClassifier()
    timeseries = [
        {"date": "2024-01-01", "mean_ndvi": 0.5},
        {"date": "2024-01-08", "mean_ndvi": 0.3},
        {"date": "2024-01-15", "mean_ndvi": 0.15},
    ]

    result = classifier.predict_yield_risk(timeseries)

    assert result["method"] == "mann_kendall_trend"
    assert result["latest_ndvi"] == 0.15
    assert result["risk_level"] == "critical"
    assert "very low" in result["risk_description"].lower()


def test_predict_yield_risk_stable_trend():
    """Test yield risk prediction with stable NDVI (small slope)."""
    classifier = CropClassifier()
    timeseries = [
        {"date": "2024-01-01", "mean_ndvi": 0.45},
        {"date": "2024-01-08", "mean_ndvi": 0.46},
        {"date": "2024-01-15", "mean_ndvi": 0.47},
        {"date": "2024-01-22", "mean_ndvi": 0.48},
    ]

    result = classifier.predict_yield_risk(timeseries)

    assert result["method"] == "mann_kendall_trend"
    assert result["observations"] == 4
    assert abs(result["trend_slope"]) <= 0.02  # Stable trend
    assert result["risk_level"] in ["normal", "low"]


def test_predict_yield_risk_moderate_decline():
    """Test yield risk prediction with moderate NDVI decline."""
    classifier = CropClassifier()
    timeseries = [
        {"date": "2024-01-01", "mean_ndvi": 0.5},
        {"date": "2024-01-08", "mean_ndvi": 0.48},
        {"date": "2024-01-15", "mean_ndvi": 0.46},
    ]

    result = classifier.predict_yield_risk(timeseries)

    assert result["method"] == "mann_kendall_trend"
    assert result["observations"] == 3
    # Slope should be between -0.02 (inclusive) and -0.005
    assert -0.025 <= result["trend_slope"] <= -0.005
    assert result["risk_level"] in ["moderate", "high"]


def test_predict_yield_risk_insufficient_data():
    """Test yield risk prediction with insufficient data (< 2 observations)."""
    classifier = CropClassifier()
    timeseries = [{"date": "2024-01-01", "mean_ndvi": 0.5}]

    result = classifier.predict_yield_risk(timeseries)

    assert "error" in result
    assert "at least 2" in result["error"].lower()


def test_predict_yield_risk_empty_timeseries():
    """Test yield risk prediction with empty timeseries."""
    classifier = CropClassifier()
    timeseries = []

    result = classifier.predict_yield_risk(timeseries)

    assert "error" in result
    assert "no ndvi data" in result["error"].lower()


def test_predict_yield_risk_missing_mean_ndvi():
    """Test yield risk prediction with missing mean_ndvi fields."""
    classifier = CropClassifier()
    timeseries = [
        {"date": "2024-01-01"},  # Missing mean_ndvi
        {"date": "2024-01-08", "mean_ndvi": 0.5},
    ]

    result = classifier.predict_yield_risk(timeseries)

    # Should filter out the entry without mean_ndvi
    assert "error" in result
    assert "at least 2" in result["error"].lower()


# ============================================================================
# Unit Tests: MLInferenceService
# ============================================================================


def test_ml_service_get_status():
    """Test MLInferenceService.get_status returns expected keys."""
    service = MLInferenceService()
    status = service.get_status()

    assert "sklearn_available" in status
    assert "ml_ready" in status
    assert "available_methods" in status
    assert "method_descriptions" in status
    assert isinstance(status["available_methods"], list)
    assert "spectral_threshold" in status["available_methods"]
    assert "mann_kendall_trend" in status["available_methods"]
    assert "z_score_anomaly" in status["available_methods"]


def test_ml_service_classify_ndvi_delegates_to_classifier():
    """Test MLInferenceService.classify_ndvi delegates to CropClassifier."""
    service = MLInferenceService()
    ndvi_values = [0.5, 0.6, 0.7]

    result = service.classify_ndvi(ndvi_values)

    assert result["method"] == "spectral_threshold"
    assert result["total_pixels"] == 3


def test_ml_service_predict_yield_risk_delegates_to_classifier():
    """Test MLInferenceService.predict_yield_risk delegates to CropClassifier."""
    service = MLInferenceService()
    timeseries = [
        {"date": "2024-01-01", "mean_ndvi": 0.4},
        {"date": "2024-01-08", "mean_ndvi": 0.5},
    ]

    result = service.predict_yield_risk(timeseries)

    assert result["method"] == "mann_kendall_trend"
    assert result["observations"] == 2


def test_ml_service_singleton():
    """Test get_ml_service returns the same instance (singleton pattern)."""
    service1 = get_ml_service()
    service2 = get_ml_service()

    assert service1 is service2
