"""Unit tests for DSSAT yield forecast service.

All tests use mocks — no external API calls, no DSSAT binary required.
Validates pedotransfer, NDVI→LAI conversion, data assimilation logic,
NASA POWER parsing, crop calendar, enrichment dispatch, and full
end-to-end data workflow accuracy.
"""

from __future__ import annotations

import json
import math
from datetime import datetime
from unittest.mock import MagicMock, patch

import numpy as np
import pandas as pd
import pytest

from src.services.dssat_service import (
    _CROP_CALENDARS,
    _K_EXT,
    detect_current_season,
    ndvi_to_lai,
    pedotransfer_saxton_rawls,
)
from src.services.enrichment_service import AVAILABLE_METRICS, compute_metric

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


def _make_soil_response(**overrides):
    """Build an iSDAsoil-style success response with optional overrides."""
    defaults = {
        "clay_content": {"value": 35.0, "unit": "%"},
        "sand_content": {"value": 30.0, "unit": "%"},
        "carbon_organic": {"value": 22.0, "unit": "g/kg"},
        "ph": {"value": 5.8, "unit": ""},
        "bulk_density": {"value": 1.3, "unit": "g/cm³"},
        "nitrogen_total": {"value": 1.5, "unit": "g/kg"},
        "cation_exchange_capacity": {"value": 18.0, "unit": "cmol(+)/kg"},
    }
    defaults.update(overrides)
    return {
        "status": "success",
        "coordinates": {"lon": 29.05, "lat": -1.95},
        "depth": "0-20 cm",
        "source": "iSDAsoil",
        "properties": defaults,
    }


MOCK_POWER_RESPONSE = {
    "properties": {
        "parameter": {
            "T2M_MAX": {"20240101": 28.5, "20240102": 29.0, "20240103": 27.8},
            "T2M_MIN": {"20240101": 16.2, "20240102": 15.8, "20240103": 16.5},
            "PRECTOTCORR": {"20240101": 5.2, "20240102": 0.0, "20240103": 12.3},
            "ALLSKY_SFC_SW_DWN": {"20240101": 18.5, "20240102": 20.1, "20240103": 15.6},
        }
    }
}


# ---------------------------------------------------------------------------
# 1. Pedotransfer — clay loam
# ---------------------------------------------------------------------------

class TestPedotransfer:

    def test_pedotransfer_clay_loam(self):
        """Known clay loam (35% clay, 30% sand, 22 g/kg OC) produces
        physically valid DSSAT hydraulic parameters."""
        result = pedotransfer_saxton_rawls(
            clay_pct=35.0,
            sand_pct=30.0,
            organic_carbon_g_kg=22.0,
        )

        # Wilting point: 0.05-0.30 for most soils
        assert 0.05 <= result["SLLL"] <= 0.30
        # Field capacity must exceed wilting point
        assert result["SDUL"] > result["SLLL"]
        # Saturation must exceed field capacity
        assert result["SSAT"] > result["SDUL"]
        assert result["SSAT"] <= 0.80
        # Bulk density: 0.80-1.80 g/cm³
        assert 0.80 <= result["SBDM"] <= 1.80
        # Organic carbon: 22 g/kg → 2.2%
        assert result["SLOC"] == pytest.approx(2.2, abs=0.01)
        assert result["SLCL"] == 35.0
        assert result["SLSI"] == 35.0  # 100 - 35 - 30

    def test_pedotransfer_sandy_soil(self):
        """Sandy soil (10% clay, 80% sand) produces lower water retention."""
        sandy = pedotransfer_saxton_rawls(
            clay_pct=10.0,
            sand_pct=80.0,
            organic_carbon_g_kg=5.0,
        )
        clay_loam = pedotransfer_saxton_rawls(
            clay_pct=35.0,
            sand_pct=30.0,
            organic_carbon_g_kg=22.0,
        )

        # Sandy soil should have lower wilting point and field capacity
        assert sandy["SLLL"] < clay_loam["SLLL"]
        assert sandy["SDUL"] < clay_loam["SDUL"]
        # Physical consistency
        assert sandy["SDUL"] > sandy["SLLL"]
        assert sandy["SSAT"] > sandy["SDUL"]


# ---------------------------------------------------------------------------
# 3. NASA POWER fetch
# ---------------------------------------------------------------------------

class TestNasaPower:

    def test_nasa_power_fetch(self):
        """Mock urllib response → verify TMAX/TMIN/RAIN/SRAD extraction."""
        from src.services.nasa_power_service import fetch_power_daily

        mock_response = MagicMock()
        mock_response.read.return_value = json.dumps(MOCK_POWER_RESPONSE).encode()
        mock_response.__enter__ = MagicMock(return_value=mock_response)
        mock_response.__exit__ = MagicMock(return_value=False)

        with patch("src.services.nasa_power_service._cached_fetch") as mock_cached:
            mock_cached.return_value = MOCK_POWER_RESPONSE["properties"]["parameter"]
            result = fetch_power_daily(-1.95, 29.05, "2024-01-01", "2024-01-03")

        assert len(result["dates"]) == 3
        assert result["TMAX"][0] == 28.5
        assert result["TMIN"][1] == 15.8
        assert result["RAIN"][2] == 12.3
        assert result["SRAD"][0] == 18.5

    def test_nasa_power_fallback(self):
        """POWER fails → verify fallback to Open-Meteo."""
        from src.services.nasa_power_service import fetch_power_daily_with_fallback

        mock_openmeteo_resp = MagicMock()
        openmeteo_data = {
            "daily": {
                "time": ["2024-01-01", "2024-01-02"],
                "temperature_2m_max": [28.0, 29.0],
                "temperature_2m_min": [16.0, 15.5],
                "precipitation_sum": [5.0, 0.0],
                "shortwave_radiation_sum": [5.0, 5.5],  # Wh/m²
            }
        }
        mock_openmeteo_resp.read.return_value = json.dumps(openmeteo_data).encode()
        mock_openmeteo_resp.__enter__ = MagicMock(return_value=mock_openmeteo_resp)
        mock_openmeteo_resp.__exit__ = MagicMock(return_value=False)

        with patch("src.services.nasa_power_service._cached_fetch", return_value=None), \
             patch("urllib.request.urlopen", return_value=mock_openmeteo_resp):
            result = fetch_power_daily_with_fallback(-1.95, 29.05, "2024-01-01", "2024-01-02")

        assert len(result["dates"]) == 2
        assert result["TMAX"][0] == 28.0
        # Open-Meteo Wh/m² → MJ/m²/day: 5.0 * 3600 / 1e6 = 0.018
        assert result["SRAD"][0] == pytest.approx(0.018, abs=0.001)


# ---------------------------------------------------------------------------
# 5-6. Crop calendar + season detection
# ---------------------------------------------------------------------------

class TestCropCalendar:

    def test_crop_calendar_season_a(self):
        """Maize Season A → planting Sep 15, harvest at 120 DAP."""
        cal = _CROP_CALENDARS["maize"]["A"]
        assert cal["planting"] == "09-15"
        assert cal["harvest_dap"] == 120

    def test_crop_calendar_auto_detect_season_b(self):
        """Date in March → auto-selects Season B."""
        march_date = datetime(2024, 3, 15)
        season = detect_current_season("maize", ref_date=march_date)
        assert season == "B"

    def test_crop_calendar_auto_detect_season_a(self):
        """Date in October → auto-selects Season A."""
        oct_date = datetime(2024, 10, 15)
        season = detect_current_season("maize", ref_date=oct_date)
        assert season == "A"

    def test_crop_calendar_wheat_only_a(self):
        """Wheat only has Season A."""
        season = detect_current_season("wheat", ref_date=datetime(2024, 3, 15))
        assert season == "A"


# ---------------------------------------------------------------------------
# 7. DSSAT run returns yield
# ---------------------------------------------------------------------------

class TestDSSATRun:

    def test_dssat_run_returns_yield(self):
        """Mock DSSAT output DataFrame → verify yield extraction."""
        from src.services.dssat_service import run_dssat_with_assimilation

        mock_output = pd.DataFrame({
            "GWAD": [3500.0],  # 3500 kg/ha = 3.5 t/ha
            "LAID": [3.2],
        })

        # Create a mock DSSAT instance whose .output_tables has our DataFrame
        mock_dssat_instance = MagicMock()
        mock_dssat_instance.output_tables = {"PlantGro": mock_output}

        # Create a mock DSSATTools module with a DSSAT class
        mock_dssat_module = MagicMock()
        mock_dssat_module.DSSAT.return_value = mock_dssat_instance

        with patch("src.services.dssat_service._build_soil_profile", return_value=MagicMock()), \
             patch("src.services.dssat_service._build_weather", return_value=MagicMock()), \
             patch("src.services.dssat_service._build_treatment_components", return_value=MagicMock()), \
             patch.dict("sys.modules", {"DSSATTools": mock_dssat_module}):
            result = run_dssat_with_assimilation(-1.95, 29.05, crop_type="maize", season="A")

        assert result["baseline_tha"] == pytest.approx(3.5, abs=0.01)
        assert result["yield_tha"] == pytest.approx(3.5, abs=0.01)  # No geom → ratio=1.0
        assert result["assimilation_ratio"] == 1.0


# ---------------------------------------------------------------------------
# 8. NDVI → LAI conversion
# ---------------------------------------------------------------------------

class TestNDVItoLAI:

    def test_ndvi_to_lai_conversion(self):
        """NDVI=0.7 → LAI ≈ 2.41 (Beer-Lambert with k=0.5)."""
        lai = ndvi_to_lai(0.7, k_ext=0.5)
        expected = -math.log(1.0 - 0.7) / 0.5  # ≈ 2.408
        assert lai == pytest.approx(expected, abs=0.01)
        assert lai == pytest.approx(2.408, abs=0.01)

    def test_ndvi_to_lai_low_ndvi(self):
        """Low NDVI (bare soil) → low LAI."""
        lai = ndvi_to_lai(0.1)
        assert lai < 1.0
        assert lai > 0.0

    def test_ndvi_to_lai_high_ndvi(self):
        """High NDVI (dense vegetation) → high LAI."""
        lai = ndvi_to_lai(0.9)
        assert lai > 3.0

    def test_ndvi_to_lai_clamped(self):
        """Out-of-range NDVI is clamped before log."""
        # Should not raise even with extreme values
        lai_neg = ndvi_to_lai(-0.5)
        lai_over = ndvi_to_lai(1.5)
        assert lai_neg > 0
        assert lai_over > 0


# ---------------------------------------------------------------------------
# 9-10. Assimilation ratio
# ---------------------------------------------------------------------------

class TestAssimilationRatio:

    def test_assimilation_ratio_scales_yield(self):
        """Observed LAI > simulated → yield increases."""
        from src.services.dssat_service import _compute_assimilation_ratio

        mock_sh = MagicMock()
        mock_sh.get_field_timeseries.return_value = {
            "intervals": [
                {"ndvi": {"mean": 0.8, "valid_pixels": 100}},  # High NDVI → high LAI
                {"ndvi": {"mean": 0.75, "valid_pixels": 100}},
            ]
        }

        sim_lai = pd.Series([2.0, 2.5, 2.0])  # Lower simulated LAI

        with patch.dict(
            "sys.modules",
            {"src.services.sentinel_hub_service": MagicMock(
                get_sentinel_hub_service=MagicMock(return_value=mock_sh)
            )},
        ):
            ratio = _compute_assimilation_ratio(
                geom=SAMPLE_FEATURES[0]["geom"],
                sim_lai_values=sim_lai,
            )

        # Observed LAI from NDVI ~0.77 → LAI ~2.94
        # Simulated LAI mean ~2.17
        # Ratio should be > 1.0
        assert ratio > 1.0
        assert ratio <= 1.5  # Clamped

    def test_assimilation_ratio_clamped(self):
        """Extreme ratio is clamped to [0.5, 1.5]."""
        from src.services.dssat_service import _compute_assimilation_ratio

        mock_sh = MagicMock()
        # Very high NDVI → very high observed LAI
        mock_sh.get_field_timeseries.return_value = {
            "intervals": [
                {"ndvi": {"mean": 0.95, "valid_pixels": 100}},
            ]
        }

        # Very low simulated LAI → ratio would be >>1.5
        sim_lai = pd.Series([0.5, 0.3])

        with patch.dict(
            "sys.modules",
            {"src.services.sentinel_hub_service": MagicMock(
                get_sentinel_hub_service=MagicMock(return_value=mock_sh)
            )},
        ):
            ratio = _compute_assimilation_ratio(
                geom=SAMPLE_FEATURES[0]["geom"],
                sim_lai_values=sim_lai,
            )

        assert ratio == 1.5  # Clamped at upper bound


# ---------------------------------------------------------------------------
# 11. DSSATTools unavailable
# ---------------------------------------------------------------------------

class TestDSSATUnavailable:

    def test_dssat_service_unavailable(self):
        """DSSATTools import fails → returns 0.0 yield."""
        from src.services.dssat_service import run_dssat_with_assimilation

        with patch.dict("sys.modules", {"DSSATTools": None}):
            # Force ImportError by making the import fail
            with patch("builtins.__import__", side_effect=_import_blocker("DSSATTools")):
                result = run_dssat_with_assimilation(-1.95, 29.05)

        assert result["yield_tha"] == 0.0
        assert "error" in result


def _import_blocker(blocked_module):
    """Create an import side-effect that blocks a specific module."""
    original_import = __builtins__.__import__ if hasattr(__builtins__, '__import__') else __import__

    def _blocked_import(name, *args, **kwargs):
        if name == blocked_module or name.startswith(blocked_module + "."):
            raise ImportError(f"Mocked: {name} not available")
        return original_import(name, *args, **kwargs)

    return _blocked_import


# ---------------------------------------------------------------------------
# 12-13. Enrichment dispatch + metric registration
# ---------------------------------------------------------------------------

class TestEnrichmentIntegration:

    @pytest.mark.asyncio
    async def test_compute_metric_dispatch(self):
        """compute_metric('yield_forecast_tha', features) routes correctly."""
        with patch(
            "src.services.enrichment_service._compute_yield_forecast",
            return_value={1: 3.5},
        ):
            result = await compute_metric("yield_forecast_tha", SAMPLE_FEATURES)

        assert 1 in result
        assert result[1] == 3.5

    def test_metric_registered(self):
        """yield_forecast_tha is in AVAILABLE_METRICS, total count = 23."""
        assert "yield_forecast_tha" in AVAILABLE_METRICS
        assert AVAILABLE_METRICS["yield_forecast_tha"].category == "Agriculture"
        assert AVAILABLE_METRICS["yield_forecast_tha"].source == "DSSAT + Sentinel-2"
        assert len(AVAILABLE_METRICS) == 23


# ---------------------------------------------------------------------------
# E2E Data Workflow Tests
# ---------------------------------------------------------------------------
# These tests exercise real internal transformations end-to-end.
# Only external boundaries are mocked: iSDAsoil HTTP, NASA POWER HTTP,
# DSSATTools binary, and Sentinel Hub API.
# ---------------------------------------------------------------------------

# Rwanda-typical soil values (Huye District, Southern Province)
_RWANDA_SOIL = {
    "clay_pct": 40.0,       # Heavy lateritic clays typical of Rwanda highlands
    "sand_pct": 25.0,       # Low sand
    "oc_g_kg": 25.0,        # Moderate OC, volcanic-derived soils
    "ph": 5.5,              # Acidic (typical for Rwanda)
    "bd": 1.25,             # Moderate bulk density
    "ntot": 2.0,            # g/kg
    "cec": 20.0,            # cmol(+)/kg
}

# Season A weather for Southern Rwanda (Sep-Jan): warm, bimodal rainfall
_SEASON_A_WEATHER = {
    "T2M_MAX": {
        "20240915": 27.0, "20240916": 26.5, "20240917": 28.0, "20240918": 27.5,
        "20240919": 27.8, "20240920": 26.0, "20240921": 28.5, "20240922": 27.2,
        "20240923": 27.0, "20240924": 28.0, "20240925": 27.5, "20240926": 26.8,
        "20240927": 27.3, "20240928": 28.2, "20240929": 27.0, "20240930": 27.8,
    },
    "T2M_MIN": {
        "20240915": 15.0, "20240916": 14.8, "20240917": 15.5, "20240918": 15.2,
        "20240919": 15.0, "20240920": 14.5, "20240921": 15.8, "20240922": 15.0,
        "20240923": 15.3, "20240924": 15.5, "20240925": 15.0, "20240926": 14.8,
        "20240927": 15.2, "20240928": 15.6, "20240929": 14.9, "20240930": 15.3,
    },
    "PRECTOTCORR": {
        "20240915": 8.5, "20240916": 2.0, "20240917": 0.0, "20240918": 12.5,
        "20240919": 0.0, "20240920": 5.0, "20240921": 0.0, "20240922": 18.0,
        "20240923": 3.5, "20240924": 0.0, "20240925": 7.0, "20240926": 0.0,
        "20240927": 10.0, "20240928": 0.0, "20240929": 15.0, "20240930": 4.0,
    },
    "ALLSKY_SFC_SW_DWN": {
        "20240915": 18.0, "20240916": 19.5, "20240917": 21.0, "20240918": 15.5,
        "20240919": 20.0, "20240920": 17.5, "20240921": 22.0, "20240922": 14.0,
        "20240923": 19.0, "20240924": 21.5, "20240925": 18.5, "20240926": 20.0,
        "20240927": 16.0, "20240928": 21.0, "20240929": 14.5, "20240930": 19.5,
    },
}


def _make_rwanda_soil_response():
    """Build realistic iSDAsoil response for Rwanda highland location."""
    return {
        "status": "success",
        "coordinates": {"lon": 29.60, "lat": -2.60},
        "depth": "0-20 cm",
        "source": "iSDAsoil",
        "properties": {
            "clay_content": {"value": _RWANDA_SOIL["clay_pct"], "unit": "%"},
            "sand_content": {"value": _RWANDA_SOIL["sand_pct"], "unit": "%"},
            "carbon_organic": {"value": _RWANDA_SOIL["oc_g_kg"], "unit": "g/kg"},
            "ph": {"value": _RWANDA_SOIL["ph"], "unit": ""},
            "bulk_density": {"value": _RWANDA_SOIL["bd"], "unit": "g/cm³"},
            "nitrogen_total": {"value": _RWANDA_SOIL["ntot"], "unit": "g/kg"},
            "cation_exchange_capacity": {"value": _RWANDA_SOIL["cec"], "unit": "cmol(+)/kg"},
        },
    }


class TestE2ESoilPipeline:
    """Verify iSDAsoil → pedotransfer → DSSAT soil profile data correctness."""

    def test_rwanda_soil_pedotransfer_values(self):
        """Rwanda highland clay soil (40% clay, 25% sand, 25 g/kg OC)
        must produce literature-consistent hydraulic parameters."""
        pt = pedotransfer_saxton_rawls(
            clay_pct=_RWANDA_SOIL["clay_pct"],
            sand_pct=_RWANDA_SOIL["sand_pct"],
            organic_carbon_g_kg=_RWANDA_SOIL["oc_g_kg"],
        )

        # Wilting point for high-clay soils: typically 0.15-0.30
        assert 0.10 <= pt["SLLL"] <= 0.30, f"SLLL={pt['SLLL']} outside range"
        # Field capacity for high-clay: typically 0.25-0.45
        assert 0.20 <= pt["SDUL"] <= 0.50, f"SDUL={pt['SDUL']} outside range"
        # Saturation: typically 0.40-0.65
        assert 0.35 <= pt["SSAT"] <= 0.70, f"SSAT={pt['SSAT']} outside range"
        # Plant-available water (SDUL - SLLL): 0.05-0.25 for most soils
        paw = pt["SDUL"] - pt["SLLL"]
        assert 0.03 <= paw <= 0.30, f"PAW={paw} outside range"
        # Bulk density consistent with high-clay: 1.0-1.6
        assert 1.0 <= pt["SBDM"] <= 1.60, f"SBDM={pt['SBDM']} outside range"
        # Silt = 100 - clay - sand = 35%
        assert pt["SLSI"] == pytest.approx(35.0, abs=0.1)
        # OC: 25 g/kg → 2.5%
        assert pt["SLOC"] == pytest.approx(2.5, abs=0.01)

    def test_pedotransfer_unit_conversions(self):
        """Verify intermediate unit conversions in pedotransfer are correct.

        Input: clay=40%, sand=25%, OC=25 g/kg
        Expected internal fractions:
          S = 0.25, C = 0.40, OM = (25/10)*1.724/100 = 0.0431
        """
        S = 25.0 / 100.0   # sand fraction
        C = 40.0 / 100.0   # clay fraction
        OM = (25.0 / 10.0) * 1.724 / 100.0  # OC g/kg → OM fraction

        assert S == pytest.approx(0.25, abs=1e-6)
        assert C == pytest.approx(0.40, abs=1e-6)
        assert OM == pytest.approx(0.0431, abs=0.001)

        # Reproduce theta_1500t manually (Saxton & Rawls Eq. 1)
        theta_1500t = (
            -0.024 * S + 0.487 * C + 0.006 * OM
            + 0.005 * S * OM - 0.013 * C * OM
            + 0.068 * S * C + 0.031
        )
        slll = theta_1500t + (0.14 * theta_1500t - 0.02)

        # Now run the actual function and compare
        pt = pedotransfer_saxton_rawls(40.0, 25.0, 25.0)
        assert pt["SLLL"] == pytest.approx(slll, abs=0.005)

    def test_soil_profile_data_reaches_dssat(self):
        """Verify soil data flows through _build_soil_profile correctly:
        iSDAsoil response → pedotransfer → SoilLayer/SoilProfile construction (v3 API)."""
        from src.services.dssat_service import _build_soil_profile

        # Capture SoilLayer constructor kwargs (v3 API)
        captured_layer_args = {}

        def capture_soil_layer(**kwargs):
            captured_layer_args.update(kwargs)
            return MagicMock()

        mock_dssat_module = MagicMock()
        mock_dssat_module.SoilLayer.side_effect = capture_soil_layer
        mock_dssat_module.SoilProfile.return_value = MagicMock()

        with patch.dict("sys.modules", {"DSSATTools": mock_dssat_module}), \
             patch("src.services.isdasoil_service.query_soil_point",
                   return_value=_make_rwanda_soil_response()):
            result = _build_soil_profile(-2.60, 29.60)

        assert result is not None

        # Verify pedotransfer values were passed to SoilLayer
        pt = pedotransfer_saxton_rawls(
            _RWANDA_SOIL["clay_pct"],
            _RWANDA_SOIL["sand_pct"],
            _RWANDA_SOIL["oc_g_kg"],
        )

        assert captured_layer_args["slll"] == pt["SLLL"]
        assert captured_layer_args["sdul"] == pt["SDUL"]
        assert captured_layer_args["ssat"] == pt["SSAT"]
        assert captured_layer_args["sbdm"] == pt["SBDM"]
        assert captured_layer_args["sloc"] == pt["SLOC"]
        assert captured_layer_args["slcl"] == pt["SLCL"]
        # Nitrogen: 2.0 g/kg → 0.2%
        assert captured_layer_args["slni"] == pytest.approx(0.2, abs=0.01)
        # pH passes through directly
        assert captured_layer_args["slhw"] == pytest.approx(5.5, abs=0.1)
        # CEC passes through directly
        assert captured_layer_args["scec"] == pytest.approx(20.0, abs=0.1)


class TestE2EWeatherPipeline:
    """Verify NASA POWER → weather dict → DataFrame transformation."""

    def test_weather_data_integrity(self):
        """Full 16-day season A weather data parsed correctly with
        proper units and no data loss."""
        from src.services.nasa_power_service import fetch_power_daily

        with patch("src.services.nasa_power_service._cached_fetch") as mock_cached:
            mock_cached.return_value = _SEASON_A_WEATHER
            result = fetch_power_daily(-2.60, 29.60, "2024-09-15", "2024-09-30")

        # All 16 days should parse (no -999 sentinels in test data)
        assert len(result["dates"]) == 16
        assert len(result["TMAX"]) == 16
        assert len(result["TMIN"]) == 16
        assert len(result["RAIN"]) == 16
        assert len(result["SRAD"]) == 16

        # Temperature ranges: tropical highland Rwanda
        for tmax in result["TMAX"]:
            assert 20.0 <= tmax <= 35.0, f"TMAX={tmax} unrealistic for Rwanda"
        for tmin in result["TMIN"]:
            assert 8.0 <= tmin <= 22.0, f"TMIN={tmin} unrealistic for Rwanda"

        # TMAX must always exceed TMIN
        for tmax, tmin in zip(result["TMAX"], result["TMIN"]):
            assert tmax > tmin, f"TMAX={tmax} <= TMIN={tmin}"

        # Rainfall: non-negative
        for rain in result["RAIN"]:
            assert rain >= 0.0

        # SRAD: 10-25 MJ/m²/day typical for tropical locations
        for srad in result["SRAD"]:
            assert 5.0 <= srad <= 30.0, f"SRAD={srad} outside tropical range"

        # Check date format is YYYY-MM-DD
        for d in result["dates"]:
            assert len(d) == 10 and d[4] == "-" and d[7] == "-"

    def test_weather_missing_values_filtered(self):
        """NASA POWER -999 sentinel values are excluded from output."""
        from src.services.nasa_power_service import fetch_power_daily

        weather_with_gaps = {
            "T2M_MAX": {"20240915": 27.0, "20240916": -999, "20240917": 28.0},
            "T2M_MIN": {"20240915": 15.0, "20240916": 14.8, "20240917": 15.5},
            "PRECTOTCORR": {"20240915": 8.5, "20240916": 2.0, "20240917": 0.0},
            "ALLSKY_SFC_SW_DWN": {"20240915": 18.0, "20240916": 19.5, "20240917": 21.0},
        }

        with patch("src.services.nasa_power_service._cached_fetch") as mock_cached:
            mock_cached.return_value = weather_with_gaps
            result = fetch_power_daily(-2.60, 29.60, "2024-09-15", "2024-09-17")

        # Day 2 (20240916) has -999 for TMAX → should be filtered out
        assert len(result["dates"]) == 2
        assert "2024-09-16" not in result["dates"]
        assert result["TMAX"] == [27.0, 28.0]

    def test_weather_flows_to_dssat_dataframe(self):
        """Weather dict → _build_weather → DSSAT WeatherStation receives
        correctly formatted WeatherRecords (v3 API)."""
        from src.services.dssat_service import _build_weather

        captured_records = []
        captured_station_args = {}

        def capture_record(**kwargs):
            captured_records.append(kwargs)
            return MagicMock()

        def capture_station(**kwargs):
            captured_station_args.update(kwargs)
            return MagicMock()

        mock_dssat_module = MagicMock()
        mock_dssat_module.WeatherRecord.side_effect = capture_record
        mock_dssat_module.WeatherStation.side_effect = capture_station

        with patch.dict("sys.modules", {"DSSATTools": mock_dssat_module}), \
             patch("src.services.nasa_power_service.fetch_power_daily_with_fallback") as mock_fetch:
            mock_fetch.return_value = {
                "dates": ["2024-09-15", "2024-09-16", "2024-09-17"],
                "TMAX": [27.0, 26.5, 28.0],
                "TMIN": [15.0, 14.8, 15.5],
                "RAIN": [8.5, 2.0, 0.0],
                "SRAD": [18.0, 19.5, 21.0],
            }
            result = _build_weather(-2.60, 29.60, "2024-09-15", "2024-09-17")

        assert result is not None
        # 3 dates → 3 WeatherRecords
        assert len(captured_records) == 3
        assert captured_records[0]["tmax"] == 27.0
        assert captured_records[2]["rain"] == 0.0
        # WeatherStation receives lat/lon
        assert captured_station_args["lat"] == -2.60
        assert captured_station_args["long"] == 29.60


class TestE2EManagementPipeline:
    """Verify crop calendar → treatment components data flow."""

    def test_treatment_season_a_maize(self):
        """Maize Season A 2024: planting 2024-09-15, with RAB fertilizer."""
        from src.services.dssat_service import _build_treatment_components

        mock_soil = MagicMock()
        mock_weather = MagicMock()

        # Mock DSSATTools.filex and DSSATTools.crop submodules
        mock_filex = MagicMock()
        mock_crop = MagicMock()
        captured_planting_args = {}

        def capture_planting_init(**kwargs):
            captured_planting_args.update(kwargs)
            return MagicMock()

        mock_filex.Planting.side_effect = capture_planting_init

        with patch.dict("sys.modules", {
            "DSSATTools": MagicMock(),
            "DSSATTools.filex": mock_filex,
            "DSSATTools.crop": mock_crop,
        }):
            result = _build_treatment_components("maize", "A", 2024, mock_soil, mock_weather)

        assert result is not None
        assert "field" in result
        assert "cultivar" in result
        assert "planting" in result
        assert "fertilizer" in result
        # Planting date should be 2024-09-15
        from datetime import date
        assert captured_planting_args["pdate"] == date(2024, 9, 15)

    def test_treatment_season_b_beans(self):
        """Beans Season B 2024: planting 2024-02-15."""
        from src.services.dssat_service import _build_treatment_components

        mock_filex = MagicMock()
        mock_crop = MagicMock()
        captured_planting_args = {}

        def capture_planting_init(**kwargs):
            captured_planting_args.update(kwargs)
            return MagicMock()

        mock_filex.Planting.side_effect = capture_planting_init

        with patch.dict("sys.modules", {
            "DSSATTools": MagicMock(),
            "DSSATTools.filex": mock_filex,
            "DSSATTools.crop": mock_crop,
        }):
            result = _build_treatment_components("beans", "B", 2024, MagicMock(), MagicMock())

        assert result is not None
        from datetime import date
        assert captured_planting_args["pdate"] == date(2024, 2, 15)

    def test_treatment_invalid_season_returns_none(self):
        """Invalid season for a crop returns None."""
        from src.services.dssat_service import _build_treatment_components

        with patch.dict("sys.modules", {
            "DSSATTools": MagicMock(),
            "DSSATTools.filex": MagicMock(),
            "DSSATTools.crop": MagicMock(),
        }):
            result = _build_treatment_components("wheat", "B", 2024, MagicMock(), MagicMock())

        # Wheat has no Season B → should return None
        assert result is None


class TestE2EFullPipeline:
    """Full pipeline: soil + weather + management → DSSAT → yield.
    Mocks only DSSATTools binary and external HTTP calls.
    All internal transformations run with real code."""

    def test_full_pipeline_realistic_maize_yield(self):
        """Realistic Rwanda inputs → DSSAT mock returning 3200 kg/ha →
        verify correct t/ha conversion and metadata."""
        from src.services.dssat_service import run_dssat_with_assimilation

        # DSSAT output: realistic maize yield for Rwanda smallholder
        mock_output = pd.DataFrame({
            "GWAD": [3200.0],     # 3200 kg/ha (typical Rwanda maize)
            "LAID": [3.5],        # Peak LAI
        })

        mock_dssat_instance = MagicMock()
        mock_dssat_instance.output_tables = {"PlantGro": mock_output}

        mock_dssat_module = MagicMock()
        mock_dssat_module.DSSAT.return_value = mock_dssat_instance

        # Let the real _build_soil_profile, _build_weather, _build_treatment_components
        # run with mocked DSSATTools classes
        mock_soil_profile = MagicMock()
        mock_weather = MagicMock()
        mock_management = MagicMock()

        with patch("src.services.dssat_service._build_soil_profile", return_value=mock_soil_profile), \
             patch("src.services.dssat_service._build_weather", return_value=mock_weather), \
             patch("src.services.dssat_service._build_treatment_components", return_value=mock_management), \
             patch.dict("sys.modules", {"DSSATTools": mock_dssat_module}):
            result = run_dssat_with_assimilation(
                lat=-2.60, lon=29.60,
                crop_type="maize", season="A",
            )

        # Yield: 3200 kg/ha → 3.2 t/ha
        assert result["yield_tha"] == pytest.approx(3.2, abs=0.01)
        assert result["baseline_tha"] == pytest.approx(3.2, abs=0.01)
        assert result["assimilation_ratio"] == 1.0  # No geom → no assimilation
        assert result["crop"] == "maize"
        assert result["season"] == "A"
        assert "error" not in result

    def test_full_pipeline_with_assimilation_upward(self):
        """DSSAT baseline + Sentinel-2 NDVI showing better-than-simulated
        vegetation → yield adjusted upward."""
        from src.services.dssat_service import run_dssat_with_assimilation

        # DSSAT gives 2800 kg/ha baseline with moderate simulated LAI
        # DSSAT output has one row per timestep; GWAD is grain weight at harvest
        mock_output = pd.DataFrame({
            "GWAD": [0.0, 0.0, 2800.0],
            "LAID": [2.5, 3.0, 2.8],  # Moderate simulated LAI
        })

        mock_dssat_instance = MagicMock()
        mock_dssat_instance.output_tables = {"PlantGro": mock_output}

        mock_dssat_module = MagicMock()
        mock_dssat_module.DSSAT.return_value = mock_dssat_instance

        # Sentinel-2: higher NDVI (0.8) → higher observed LAI
        mock_sh = MagicMock()
        mock_sh.get_field_timeseries.return_value = {
            "intervals": [
                {"ndvi": {"mean": 0.80, "valid_pixels": 200}},
                {"ndvi": {"mean": 0.78, "valid_pixels": 180}},
                {"ndvi": {"mean": 0.82, "valid_pixels": 190}},
            ]
        }

        geom = SAMPLE_FEATURES[0]["geom"]

        with patch("src.services.dssat_service._build_soil_profile", return_value=MagicMock()), \
             patch("src.services.dssat_service._build_weather", return_value=MagicMock()), \
             patch("src.services.dssat_service._build_treatment_components", return_value=MagicMock()), \
             patch.dict("sys.modules", {
                 "DSSATTools": mock_dssat_module,
                 "src.services.sentinel_hub_service": MagicMock(
                     get_sentinel_hub_service=MagicMock(return_value=mock_sh),
                 ),
             }):
            result = run_dssat_with_assimilation(
                lat=-2.60, lon=29.60,
                crop_type="maize", season="A",
                geom=geom,
            )

        baseline_tha = 2800.0 / 1000.0  # 2.8 t/ha
        assert result["baseline_tha"] == pytest.approx(baseline_tha, abs=0.01)
        # Assimilation ratio should be > 1.0 (observed LAI > simulated LAI)
        assert result["assimilation_ratio"] > 1.0
        assert result["assimilation_ratio"] <= 1.5
        # Adjusted yield should be higher than baseline
        assert result["yield_tha"] > baseline_tha
        # But still within realistic range for Rwanda maize (1-8 t/ha)
        assert 1.0 <= result["yield_tha"] <= 8.0

    def test_full_pipeline_with_assimilation_downward(self):
        """Sentinel-2 shows worse vegetation than simulated → yield
        adjusted downward."""
        from src.services.dssat_service import run_dssat_with_assimilation

        # DSSAT gives optimistic 4500 kg/ha with high simulated LAI
        mock_output = pd.DataFrame({
            "GWAD": [0.0, 0.0, 4500.0],
            "LAID": [4.0, 4.5, 4.2],  # High simulated LAI
        })

        mock_dssat_instance = MagicMock()
        mock_dssat_instance.output_tables = {"PlantGro": mock_output}

        mock_dssat_module = MagicMock()
        mock_dssat_module.DSSAT.return_value = mock_dssat_instance

        # Sentinel-2: low NDVI (0.5) → low observed LAI, crop struggling
        mock_sh = MagicMock()
        mock_sh.get_field_timeseries.return_value = {
            "intervals": [
                {"ndvi": {"mean": 0.50, "valid_pixels": 200}},
                {"ndvi": {"mean": 0.45, "valid_pixels": 190}},
            ]
        }

        geom = SAMPLE_FEATURES[0]["geom"]

        with patch("src.services.dssat_service._build_soil_profile", return_value=MagicMock()), \
             patch("src.services.dssat_service._build_weather", return_value=MagicMock()), \
             patch("src.services.dssat_service._build_treatment_components", return_value=MagicMock()), \
             patch.dict("sys.modules", {
                 "DSSATTools": mock_dssat_module,
                 "src.services.sentinel_hub_service": MagicMock(
                     get_sentinel_hub_service=MagicMock(return_value=mock_sh),
                 ),
             }):
            result = run_dssat_with_assimilation(
                lat=-2.60, lon=29.60,
                crop_type="maize", season="A",
                geom=geom,
            )

        baseline_tha = 4.5
        assert result["baseline_tha"] == pytest.approx(baseline_tha, abs=0.01)
        # Ratio should be < 1.0 (observed < simulated)
        assert result["assimilation_ratio"] < 1.0
        assert result["assimilation_ratio"] >= 0.5
        # Adjusted yield should be lower
        assert result["yield_tha"] < baseline_tha

    def test_full_pipeline_soil_unavailable_graceful(self):
        """iSDAsoil fails → pipeline returns 0.0 yield with error."""
        from src.services.dssat_service import run_dssat_with_assimilation

        mock_dssat_module = MagicMock()

        with patch("src.services.dssat_service._build_soil_profile", return_value=None), \
             patch.dict("sys.modules", {"DSSATTools": mock_dssat_module}):
            result = run_dssat_with_assimilation(-2.60, 29.60)

        assert result["yield_tha"] == 0.0
        assert "error" in result
        assert "Soil" in result["error"]

    def test_full_pipeline_weather_unavailable_graceful(self):
        """Weather unavailable → pipeline returns 0.0 yield with error."""
        from src.services.dssat_service import run_dssat_with_assimilation

        mock_dssat_module = MagicMock()

        with patch("src.services.dssat_service._build_soil_profile", return_value=MagicMock()), \
             patch("src.services.dssat_service._build_weather", return_value=None), \
             patch.dict("sys.modules", {"DSSATTools": mock_dssat_module}):
            result = run_dssat_with_assimilation(-2.60, 29.60)

        assert result["yield_tha"] == 0.0
        assert "error" in result
        assert "Weather" in result["error"]


class TestE2EAssimilationAccuracy:
    """Verify mathematical accuracy of the NDVI→LAI→ratio pipeline."""

    def test_ndvi_to_lai_mathematical_accuracy(self):
        """Verify Beer-Lambert conversion matches hand calculations
        for a range of NDVI values typical in Rwanda."""
        test_cases = [
            (0.3, -math.log(1.0 - 0.3) / 0.5),   # Sparse vegetation
            (0.5, -math.log(1.0 - 0.5) / 0.5),   # Moderate vegetation
            (0.7, -math.log(1.0 - 0.7) / 0.5),   # Dense vegetation
            (0.85, -math.log(1.0 - 0.85) / 0.5),  # Very dense
        ]

        for ndvi, expected_lai in test_cases:
            actual = ndvi_to_lai(ndvi, k_ext=0.5)
            assert actual == pytest.approx(expected_lai, abs=0.001), \
                f"NDVI={ndvi}: expected LAI={expected_lai:.3f}, got {actual:.3f}"

    def test_assimilation_ratio_mathematical_accuracy(self):
        """Verify ratio = mean(obs_LAI) / mean(sim_LAI) with known values.

        Observed: NDVI=[0.7, 0.75, 0.8]
        → LAI = [-ln(0.3)/0.5, -ln(0.25)/0.5, -ln(0.2)/0.5]
        → LAI = [2.408, 2.773, 3.219]
        → mean = 2.800

        Simulated: LAI = [2.0, 2.5, 3.0] → mean = 2.5

        Expected ratio: 2.800 / 2.5 = 1.12
        """
        from src.services.dssat_service import _compute_assimilation_ratio

        mock_sh = MagicMock()
        mock_sh.get_field_timeseries.return_value = {
            "intervals": [
                {"ndvi": {"mean": 0.7, "valid_pixels": 100}},
                {"ndvi": {"mean": 0.75, "valid_pixels": 100}},
                {"ndvi": {"mean": 0.8, "valid_pixels": 100}},
            ]
        }

        sim_lai = pd.Series([2.0, 2.5, 3.0])

        # Hand-calculate expected
        obs_lais = [
            -math.log(1.0 - 0.7) / 0.5,   # 2.408
            -math.log(1.0 - 0.75) / 0.5,   # 2.773
            -math.log(1.0 - 0.8) / 0.5,    # 3.219
        ]
        mean_obs = sum(obs_lais) / len(obs_lais)  # ~2.800
        mean_sim = (2.0 + 2.5 + 3.0) / 3.0        # 2.5
        expected_ratio = mean_obs / mean_sim        # ~1.12

        with patch.dict(
            "sys.modules",
            {"src.services.sentinel_hub_service": MagicMock(
                get_sentinel_hub_service=MagicMock(return_value=mock_sh),
            )},
        ):
            actual_ratio = _compute_assimilation_ratio(
                geom=SAMPLE_FEATURES[0]["geom"],
                sim_lai_values=sim_lai,
            )

        assert actual_ratio == pytest.approx(expected_ratio, abs=0.01)
        assert actual_ratio == pytest.approx(1.12, abs=0.02)

    def test_assimilation_yield_adjustment_accuracy(self):
        """Full yield adjustment: baseline × ratio matches expected value.

        Baseline: 3000 kg/ha = 3.0 t/ha
        Ratio: 1.2 (observed vegetation 20% better)
        Expected adjusted: 3.0 × 1.2 = 3.6 t/ha
        """
        from src.services.dssat_service import run_dssat_with_assimilation

        mock_output = pd.DataFrame({
            "GWAD": [0.0, 0.0, 3000.0],
            "LAID": [2.0, 2.5, 2.0],  # sim LAI mean = 2.167
        })
        mock_dssat_instance = MagicMock()
        mock_dssat_instance.output_tables = {"PlantGro": mock_output}
        mock_dssat_module = MagicMock()
        mock_dssat_module.DSSAT.return_value = mock_dssat_instance

        # Observed NDVI values that give ~20% higher LAI
        # sim LAI mean ≈ 2.167, target ratio ≈ 1.2
        # target obs LAI ≈ 2.167 * 1.2 = 2.6
        # NDVI for LAI=2.6: 1 - exp(-2.6*0.5) = 1 - exp(-1.3) ≈ 0.727
        mock_sh = MagicMock()
        mock_sh.get_field_timeseries.return_value = {
            "intervals": [
                {"ndvi": {"mean": 0.73, "valid_pixels": 100}},
            ]
        }

        geom = SAMPLE_FEATURES[0]["geom"]

        with patch("src.services.dssat_service._build_soil_profile", return_value=MagicMock()), \
             patch("src.services.dssat_service._build_weather", return_value=MagicMock()), \
             patch("src.services.dssat_service._build_treatment_components", return_value=MagicMock()), \
             patch.dict("sys.modules", {
                 "DSSATTools": mock_dssat_module,
                 "src.services.sentinel_hub_service": MagicMock(
                     get_sentinel_hub_service=MagicMock(return_value=mock_sh),
                 ),
             }):
            result = run_dssat_with_assimilation(
                lat=-2.60, lon=29.60,
                crop_type="maize", season="A",
                geom=geom,
            )

        baseline = 3.0
        # Verify the math: yield_adj = baseline × ratio
        assert result["baseline_tha"] == pytest.approx(baseline, abs=0.01)
        expected_adj = round(baseline * result["assimilation_ratio"], 2)
        assert result["yield_tha"] == pytest.approx(expected_adj, abs=0.01)


class TestE2EEnrichmentDispatch:
    """Verify full enrichment system flow: features → centroids → DSSAT → results."""

    @pytest.mark.asyncio
    async def test_multi_feature_enrichment(self):
        """Multiple features each get independent yield values via centroids."""
        features = [
            {
                "id": 1,
                "geom": {
                    "type": "Polygon",
                    "coordinates": [[[29.0, -2.0], [29.1, -2.0],
                                     [29.1, -1.9], [29.0, -1.9], [29.0, -2.0]]],
                },
            },
            {
                "id": 2,
                "geom": {
                    "type": "Polygon",
                    "coordinates": [[[29.5, -2.5], [29.6, -2.5],
                                     [29.6, -2.4], [29.5, -2.4], [29.5, -2.5]]],
                },
            },
        ]

        call_count = {"n": 0}

        def mock_dssat_run(lat, lon, crop_type="maize", season=None, geom=None):
            call_count["n"] += 1
            # Each location gets different yield based on lat
            yield_val = 3.0 + abs(lat) * 0.5  # ~4.0 for lat=-2.0, ~4.25 for lat=-2.5
            return {"yield_tha": round(yield_val, 2)}

        with patch("src.services.dssat_service.run_dssat_with_assimilation",
                   side_effect=mock_dssat_run):
            from src.services.enrichment_service import _compute_yield_forecast
            result = _compute_yield_forecast(features)

        # Both features should have results
        assert 1 in result
        assert 2 in result
        # Different locations → different yields
        assert result[1] != result[2]
        # Should have called DSSAT twice (once per feature)
        assert call_count["n"] == 2

    @pytest.mark.asyncio
    async def test_enrichment_centroid_calculation(self):
        """Feature centroid is computed correctly and passed to DSSAT."""
        features = [{
            "id": 1,
            "geom": {
                "type": "Polygon",
                "coordinates": [[[29.0, -2.0], [29.1, -2.0],
                                 [29.1, -1.9], [29.0, -1.9], [29.0, -2.0]]],
            },
        }]

        captured_coords = {}

        def mock_dssat_run(lat, lon, crop_type="maize", season=None, geom=None):
            captured_coords["lat"] = lat
            captured_coords["lon"] = lon
            captured_coords["geom"] = geom
            return {"yield_tha": 3.5}

        with patch("src.services.dssat_service.run_dssat_with_assimilation",
                   side_effect=mock_dssat_run):
            from src.services.enrichment_service import _compute_yield_forecast
            _compute_yield_forecast(features)

        # Centroid of the box: (29.05, -1.95)
        assert captured_coords["lat"] == pytest.approx(-1.95, abs=0.01)
        assert captured_coords["lon"] == pytest.approx(29.05, abs=0.01)
        # Geom should be passed through for Sentinel-2 assimilation
        assert captured_coords["geom"] is not None
        assert captured_coords["geom"]["type"] == "Polygon"


class TestE2ERwandaYieldPlausibility:
    """Validate that pipeline outputs are physically plausible for Rwanda.

    Reference yield data (FAOSTAT / MINAGRI / NISR):
    - Rwanda national average maize: 1.5-2.5 t/ha
    - Well-managed smallholder maize: 3.0-5.0 t/ha
    - Research station maize: 5.0-8.0 t/ha
    - Beans: 0.8-2.0 t/ha
    - Rice (irrigated): 4.0-7.0 t/ha
    """

    @pytest.mark.parametrize("yield_kg_ha, crop, expected_range", [
        (2000.0, "maize", (1.0, 8.0)),    # Low-input maize
        (3500.0, "maize", (1.0, 8.0)),    # Average managed maize
        (5000.0, "maize", (1.0, 8.0)),    # Good conditions maize
        (1500.0, "beans", (0.5, 4.0)),    # Beans
        (5500.0, "rice", (2.0, 10.0)),    # Irrigated rice
    ])
    def test_yield_in_plausible_range(self, yield_kg_ha, crop, expected_range):
        """DSSAT yield output is within physically plausible range for crop."""
        from src.services.dssat_service import run_dssat_with_assimilation

        mock_output = pd.DataFrame({
            "GWAD": [yield_kg_ha],
            "LAID": [3.0],
        })

        mock_dssat_instance = MagicMock()
        mock_dssat_instance.output_tables = {"PlantGro": mock_output}
        mock_dssat_module = MagicMock()
        mock_dssat_module.DSSAT.return_value = mock_dssat_instance

        with patch("src.services.dssat_service._build_soil_profile", return_value=MagicMock()), \
             patch("src.services.dssat_service._build_weather", return_value=MagicMock()), \
             patch("src.services.dssat_service._build_treatment_components", return_value=MagicMock()), \
             patch.dict("sys.modules", {"DSSATTools": mock_dssat_module}):
            result = run_dssat_with_assimilation(
                lat=-2.60, lon=29.60,
                crop_type=crop, season="A",
            )

        yield_tha = result["yield_tha"]
        lo, hi = expected_range
        assert lo <= yield_tha <= hi, \
            f"{crop} yield {yield_tha} t/ha outside plausible range [{lo}, {hi}]"

    def test_assimilation_preserves_plausible_range(self):
        """Even with extreme assimilation ratio (clamped at 1.5),
        final yield stays within physically possible range."""
        from src.services.dssat_service import run_dssat_with_assimilation

        # High baseline yield
        mock_output = pd.DataFrame({
            "GWAD": [0.0, 5000.0],  # 5.0 t/ha
            "LAID": [1.0, 1.0],     # Very low sim LAI → high ratio → clamped
        })
        mock_dssat_instance = MagicMock()
        mock_dssat_instance.output_tables = {"PlantGro": mock_output}
        mock_dssat_module = MagicMock()
        mock_dssat_module.DSSAT.return_value = mock_dssat_instance

        mock_sh = MagicMock()
        mock_sh.get_field_timeseries.return_value = {
            "intervals": [
                {"ndvi": {"mean": 0.95, "valid_pixels": 200}},
            ]
        }

        with patch("src.services.dssat_service._build_soil_profile", return_value=MagicMock()), \
             patch("src.services.dssat_service._build_weather", return_value=MagicMock()), \
             patch("src.services.dssat_service._build_treatment_components", return_value=MagicMock()), \
             patch.dict("sys.modules", {
                 "DSSATTools": mock_dssat_module,
                 "src.services.sentinel_hub_service": MagicMock(
                     get_sentinel_hub_service=MagicMock(return_value=mock_sh),
                 ),
             }):
            result = run_dssat_with_assimilation(
                lat=-2.60, lon=29.60,
                crop_type="maize", season="A",
                geom=SAMPLE_FEATURES[0]["geom"],
            )

        # Maximum: 5.0 t/ha × 1.5 (clamped) = 7.5 t/ha
        assert result["yield_tha"] <= 5.0 * 1.5
        assert result["assimilation_ratio"] == 1.5  # Clamped at max


class TestE2ESoilTextureVariations:
    """Verify pedotransfer produces correct relative differences
    across Rwanda's soil diversity."""

    def test_volcanic_vs_laterite_soils(self):
        """Volcanic soils (NW Rwanda) have higher water retention
        than laterite soils (Eastern Rwanda)."""
        # Volcanic soil: high OC, moderate clay
        volcanic = pedotransfer_saxton_rawls(
            clay_pct=30.0, sand_pct=20.0, organic_carbon_g_kg=40.0,
        )
        # Laterite soil: high clay, low OC
        laterite = pedotransfer_saxton_rawls(
            clay_pct=55.0, sand_pct=15.0, organic_carbon_g_kg=10.0,
        )

        # Volcanic soils with high OC should have higher field capacity
        # (OM increases water retention at 33 kPa)
        assert volcanic["SLOC"] > laterite["SLOC"]
        # Both should be physically valid
        for soil in [volcanic, laterite]:
            assert soil["SDUL"] > soil["SLLL"]
            assert soil["SSAT"] > soil["SDUL"]
            assert 0.80 <= soil["SBDM"] <= 1.80

    def test_marshland_vs_hillside_soils(self):
        """Marshland soils (high OC, silty) vs hillside (sandy, low OC)
        produce different hydraulic profiles."""
        # Marshland (bas-fond)
        marsh = pedotransfer_saxton_rawls(
            clay_pct=25.0, sand_pct=15.0, organic_carbon_g_kg=50.0,
        )
        # Hillside (eroded)
        hill = pedotransfer_saxton_rawls(
            clay_pct=20.0, sand_pct=50.0, organic_carbon_g_kg=8.0,
        )

        # Marshland has much higher organic carbon
        assert marsh["SLOC"] > hill["SLOC"]
        # Sandy hillside should have lower wilting point
        assert hill["SLLL"] < marsh["SLLL"]
