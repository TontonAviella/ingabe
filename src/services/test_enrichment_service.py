"""Unit tests for choropleth enrichment metrics (vegetation, emissions, soil).

Verifies accuracy of the 14 new metrics by mocking external services and
asserting computed values match expected outputs within scientifically valid
ranges.  All 26 tests run without any external service calls.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import numpy as np
import pytest

from src.services.enrichment_service import (
    AVAILABLE_METRICS,
    _AGRI_INDEX_MAP,
    _EMISSIONS_MAP,
    _SOIL_PROPERTY_MAP,
    _compute_agri_index_metric,
    _compute_emissions_metric,
    _compute_soil_metric,
    compute_metric,
)

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

SAMPLE_FEATURES = [
    {
        "id": 1,
        "geom": {
            "type": "Polygon",
            "coordinates": [
                [
                    [29.0, -2.0],
                    [29.1, -2.0],
                    [29.1, -1.9],
                    [29.0, -1.9],
                    [29.0, -2.0],
                ]
            ],
        },
    }
]

# Known mean values for each vegetation index
_AGRI_MEANS = {
    "ndvi": 0.65,
    "evi": 0.45,
    "ndwi": -0.15,
    "savi": 0.55,
    "ndre": 0.35,
    "ndbi": -0.25,
}

MOCK_AGRI_STATS = {
    "service": "sentinel_hub_cdse",
    "collection": "SENTINEL2_L2A",
    "indices": list(_AGRI_MEANS.keys()),
    "date_from": "2025-01-01",
    "date_to": "2025-01-31",
    "intervals": [
        {
            "date_from": "2025-01-01",
            "date_to": "2025-01-15",
            **{
                idx: {"mean": val, "std": 0.1, "min": val - 0.2, "max": val + 0.2, "valid_pixels": 500, "no_data_pixels": 10}
                for idx, val in _AGRI_MEANS.items()
            },
        },
        {
            "date_from": "2025-01-15",
            "date_to": "2025-01-31",
            **{
                idx: {"mean": val, "std": 0.1, "min": val - 0.2, "max": val + 0.2, "valid_pixels": 480, "no_data_pixels": 15}
                for idx, val in _AGRI_MEANS.items()
            },
        },
    ],
    "interval_count": 2,
}

# 2x2 grid centred around the test polygon centroid (≈29.05, -1.95)
_GRID_VALUE = 0.7
MOCK_EDGAR_GRID = {
    "values": np.array([[_GRID_VALUE, _GRID_VALUE], [_GRID_VALUE, _GRID_VALUE]]),
    "lats": np.array([-2.0, -1.9]),
    "lons": np.array([29.0, 29.1]),
    "unit": "tonnes/cell/year",
    "emission_type": "CH4",
    "sector": "AGS",
    "year": 2022,
}


def _make_soil_response(prop: str, value: float) -> dict:
    """Build an iSDAsoil-style success response for *prop*."""
    return {
        "status": "success",
        "coordinates": {"lon": 29.05, "lat": -1.95},
        "depth": "0-20 cm",
        "source": "iSDAsoil (30m resolution, machine learning predictions)",
        "properties": {
            prop: {
                "value": value,
                "unit": "",
                "label": prop,
                "description": "",
                "depth": "0-20 cm",
            }
        },
    }


# ---------------------------------------------------------------------------
# Vegetation index tests
# ---------------------------------------------------------------------------

class TestAgriIndexMetrics:
    """Test _compute_agri_index_metric for all 6 indices + edge cases."""

    @pytest.mark.parametrize(
        "metric_key, index_name, expected, lo, hi",
        [
            ("ndvi_mean", "ndvi", 0.65, -1, 1),
            ("evi_mean", "evi", 0.45, -1, 1),
            ("ndwi_mean", "ndwi", -0.15, -1, 1),
            ("savi_mean", "savi", 0.55, -1.5, 1.5),
            ("ndre_mean", "ndre", 0.35, -1, 1),
            ("ndbi_mean", "ndbi", -0.25, -1, 1),
        ],
    )
    @pytest.mark.asyncio
    async def test_agri_index_returns_expected_value(
        self, metric_key, index_name, expected, lo, hi
    ):
        mock_sh = MagicMock()
        mock_sh.get_agri_stats.return_value = MOCK_AGRI_STATS

        with patch.dict(
            "sys.modules",
            {
                "src.services.sentinel_hub_service": MagicMock(
                    get_sentinel_hub_service=MagicMock(return_value=mock_sh)
                )
            },
        ):
            result = await _compute_agri_index_metric(SAMPLE_FEATURES, index_name)

        assert 1 in result
        assert result[1] == pytest.approx(expected, abs=1e-4)
        assert lo <= result[1] <= hi

    @pytest.mark.asyncio
    async def test_agri_index_service_unavailable(self):
        with patch.dict(
            "sys.modules",
            {
                "src.services.sentinel_hub_service": MagicMock(
                    get_sentinel_hub_service=MagicMock(return_value=None)
                )
            },
        ):
            result = await _compute_agri_index_metric(SAMPLE_FEATURES, "ndvi")

        assert result == {}

    @pytest.mark.asyncio
    async def test_agri_index_no_valid_intervals(self):
        stats_no_valid = {
            "intervals": [
                {
                    "ndvi": {"mean": 0.5, "std": 0.1, "valid_pixels": 0, "no_data_pixels": 100},
                }
            ],
        }
        mock_sh = MagicMock()
        mock_sh.get_agri_stats.return_value = stats_no_valid

        with patch.dict(
            "sys.modules",
            {
                "src.services.sentinel_hub_service": MagicMock(
                    get_sentinel_hub_service=MagicMock(return_value=mock_sh)
                )
            },
        ):
            result = await _compute_agri_index_metric(SAMPLE_FEATURES, "ndvi")

        assert result[1] == 0.0


# ---------------------------------------------------------------------------
# Emissions tests
# ---------------------------------------------------------------------------

class TestEmissionsMetrics:
    """Test _compute_emissions_metric for CH4, N2O, CO2 + edge cases."""

    @pytest.mark.parametrize(
        "metric_key, emission_type, num_sectors, expected",
        [
            ("ch4_emissions", "CH4", 4, round(4 * _GRID_VALUE, 2)),
            ("n2o_emissions", "N2O", 3, round(3 * _GRID_VALUE, 2)),
            ("co2_emissions", "CO2", 1, round(1 * _GRID_VALUE, 2)),
        ],
    )
    def test_emissions_returns_expected_value(
        self, metric_key, emission_type, num_sectors, expected
    ):
        mock_svc = MagicMock()
        # Every sector call returns the same grid
        mock_svc.download_edgar_gridmap.return_value = {
            **MOCK_EDGAR_GRID,
            "emission_type": emission_type,
        }

        mock_module = MagicMock()
        mock_module.get_emissions_service.return_value = mock_svc
        mock_module.VALID_COMBOS = {
            "CH4": ["AGS", "ENF", "MNM", "AWB"],
            "N2O": ["AGS", "MNM", "AWB"],
            "CO2": ["AGS"],
        }

        with patch.dict("sys.modules", {"src.services.emissions_service": mock_module}):
            result = _compute_emissions_metric(SAMPLE_FEATURES, emission_type)

        assert 1 in result
        assert result[1] == pytest.approx(expected, abs=0.01)
        assert result[1] >= 0

    def test_emissions_service_unavailable(self):
        mock_module = MagicMock()
        mock_module.get_emissions_service.return_value = None
        mock_module.VALID_COMBOS = {"CH4": ["AGS"]}

        with patch.dict("sys.modules", {"src.services.emissions_service": mock_module}):
            result = _compute_emissions_metric(SAMPLE_FEATURES, "CH4")

        assert result == {}

    def test_emissions_all_grids_error(self):
        mock_svc = MagicMock()
        mock_svc.download_edgar_gridmap.return_value = {"error": "download failed"}

        mock_module = MagicMock()
        mock_module.get_emissions_service.return_value = mock_svc
        mock_module.VALID_COMBOS = {"CH4": ["AGS", "ENF", "MNM", "AWB"]}

        with patch.dict("sys.modules", {"src.services.emissions_service": mock_module}):
            result = _compute_emissions_metric(SAMPLE_FEATURES, "CH4")

        assert result[1] == 0.0


# ---------------------------------------------------------------------------
# Soil tests
# ---------------------------------------------------------------------------

class TestSoilMetrics:
    """Test _compute_soil_metric with mocked rasterio COG access."""

    @staticmethod
    def _raw_value(soil_prop: str, expected: float) -> float:
        """Compute the raw COG pixel value that produces `expected` after transform."""
        if soil_prop in ("ph", "clay_content"):  # transform: x / 10.0
            return expected * 10.0
        elif soil_prop == "nitrogen_total":  # transform: expm1(x / 100.0)
            return float(np.log1p(expected) * 100.0)
        else:  # transform: expm1(x / 10.0)
            return float(np.log1p(expected) * 10.0)

    @staticmethod
    def _make_mock_src(raw_value: float) -> MagicMock:
        """Create a mock rasterio dataset returning `raw_value` for all reads."""
        from affine import Affine

        mock_src = MagicMock()
        mock_src.__enter__ = MagicMock(return_value=mock_src)
        mock_src.__exit__ = MagicMock(return_value=False)
        mock_src.transform = Affine(30, 0, 3_000_000, 0, -30, 0)
        mock_src.read.return_value = np.array(
            [[[raw_value, raw_value], [raw_value, raw_value]]]
        )
        return mock_src

    @pytest.mark.parametrize(
        "metric_key, soil_prop, value, lo, hi",
        [
            ("soil_ph", "ph", 5.8, 3, 10),
            ("soil_nitrogen", "nitrogen_total", 1.2, 0, 10),
            ("soil_phosphorus", "phosphorous_extractable", 15.5, 0, 100),
            ("soil_potassium", "potassium_extractable", 120.0, 0, 500),
            ("soil_organic_carbon", "carbon_organic", 22.3, 0, 100),
            ("soil_clay", "clay_content", 35.0, 0, 100),
        ],
    )
    def test_soil_returns_expected_value(self, metric_key, soil_prop, value, lo, hi):
        raw = self._raw_value(soil_prop, value)
        mock_src = self._make_mock_src(raw)

        with patch("rasterio.open", return_value=mock_src):
            result = _compute_soil_metric(SAMPLE_FEATURES, soil_prop)

        assert 1 in result
        assert result[1] == pytest.approx(value, abs=0.05)
        assert lo <= result[1] <= hi

    def test_soil_cog_open_failure_returns_zero(self):
        with patch("rasterio.open", side_effect=Exception("COG unavailable")):
            result = _compute_soil_metric(SAMPLE_FEATURES, "ph")

        assert result[1] == 0.0

    def test_soil_no_valid_pixels_returns_zero(self):
        mock_src = self._make_mock_src(0.0)

        with patch("rasterio.open", return_value=mock_src):
            result = _compute_soil_metric(SAMPLE_FEATURES, "ph")

        assert result[1] == 0.0


# ---------------------------------------------------------------------------
# Dispatch / registration tests
# ---------------------------------------------------------------------------

class TestComputeMetricDispatch:
    """Test the top-level compute_metric router."""

    @pytest.mark.asyncio
    async def test_empty_features_returns_empty(self):
        result = await compute_metric("ndvi_mean", [])
        assert result == {}

    @pytest.mark.asyncio
    async def test_unknown_metric_raises(self):
        with pytest.raises(ValueError, match="Unknown metric"):
            await compute_metric("fake_metric_xyz", SAMPLE_FEATURES)

    def test_all_new_metrics_registered(self):
        expected_keys = (
            set(_AGRI_INDEX_MAP.keys())
            | set(_EMISSIONS_MAP.keys())
            | set(_SOIL_PROPERTY_MAP.keys())
        )
        # 6 vegetation + 3 emissions + 6 soil = 15 new metric keys
        assert len(expected_keys) == 15
        for key in expected_keys:
            assert key in AVAILABLE_METRICS, f"{key} not in AVAILABLE_METRICS"

        # Also verify total count is 23 (4 LULC + 3 weather + 15 veg/emis/soil + 1 yield)
        assert len(AVAILABLE_METRICS) == 23


# ---------------------------------------------------------------------------
# Edge-case / exception handling
# ---------------------------------------------------------------------------

class TestEdgeCases:
    """Test graceful error handling when services raise exceptions."""

    @pytest.mark.asyncio
    async def test_agri_stats_exception_handled(self):
        mock_sh = MagicMock()
        mock_sh.get_agri_stats.side_effect = RuntimeError("API timeout")

        with patch.dict(
            "sys.modules",
            {
                "src.services.sentinel_hub_service": MagicMock(
                    get_sentinel_hub_service=MagicMock(return_value=mock_sh)
                )
            },
        ):
            result = await _compute_agri_index_metric(SAMPLE_FEATURES, "ndvi")

        assert result[1] == 0.0

    def test_soil_query_exception_handled(self):
        with patch("rasterio.open", side_effect=ConnectionError("network down")):
            result = _compute_soil_metric(SAMPLE_FEATURES, "ph")

        assert result[1] == 0.0
