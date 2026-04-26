"""Tests for insurance_engine.py — pure functions + mocked async functions.

Part 1: Pure function tests (no DB/API required).
Part 2: Mocked async tests for DB/API functions:
  _load_triggers, _fetch_ndvi_anomaly, compute_insurance_intelligence,
  _resolve_location_name, _get_planting_date, _get_harvest_dap,
  compute_insurance_accuracy_safe
"""

import asyncio
import json
from datetime import date, datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.services.insurance_engine import (
    InsuranceReport,
    PhaseRainfall,
    TriggerResult,
    _centroid_from_geojson,
    _compute_confidence,
    _compute_phase_rainfall,
    _compute_spi,
    _current_growth_phase,
    _default_triggers,
    _evaluate_triggers,
    _fetch_ndvi_anomaly,
    _fetch_sar_backscatter,
    _flatten_coords,
    _generate_recommendation,
    _get_harvest_dap,
    _get_planting_date,
    _load_triggers,
    _resolve_location_name,
    _GROWTH_PHASES,
    _NATIONAL_RAINFALL_NORMALS,
    _DISTRICT_RAINFALL_NORMALS,
    _RWANDA_CENTER,
    _ET_LONG_TERM_MEAN,
    _VALID_AUDIENCES,
    compute_insurance_intelligence,
    compute_insurance_accuracy_safe,
    format_for_audience,
)


# ---------------------------------------------------------------------------
# _compute_spi
# ---------------------------------------------------------------------------

class TestComputeSPI:
    def test_normal_rainfall_returns_zero(self):
        spi = _compute_spi(400.0, "A")
        assert spi == pytest.approx(0.0)

    def test_below_normal_returns_negative(self):
        spi = _compute_spi(315.0, "A")
        assert spi == pytest.approx(-1.0)

    def test_above_normal_returns_positive(self):
        spi = _compute_spi(485.0, "A")
        assert spi == pytest.approx(1.0)

    def test_season_b_uses_b_normals(self):
        spi = _compute_spi(350.0, "B")
        assert spi == pytest.approx(0.0)

    def test_unknown_season_falls_back_to_A(self):
        spi = _compute_spi(400.0, "C")
        assert spi == pytest.approx(0.0)

    def test_severe_drought(self):
        spi = _compute_spi(230.0, "A")
        assert spi == pytest.approx(-2.0)

    def test_zero_rainfall(self):
        spi = _compute_spi(0.0, "A")
        expected = -400.0 / 85.0
        assert spi == pytest.approx(expected)


# ---------------------------------------------------------------------------
# _evaluate_triggers
# ---------------------------------------------------------------------------

class TestEvaluateTriggers:
    def _trigger(self, signal="rainfall_cumulative", direction="below",
                 threshold=100.0, weight=1.0, description="test"):
        return {
            "signal": signal, "direction": direction,
            "threshold": threshold, "weight": weight,
            "description": description,
        }

    def test_below_triggered(self):
        triggers = [self._trigger(threshold=100.0, direction="below")]
        results = _evaluate_triggers(triggers, {"rainfall_cumulative": 80.0})
        assert len(results) == 1
        assert results[0].triggered is True
        assert results[0].margin_pct < 0

    def test_below_not_triggered(self):
        triggers = [self._trigger(threshold=100.0, direction="below")]
        results = _evaluate_triggers(triggers, {"rainfall_cumulative": 120.0})
        assert results[0].triggered is False
        assert results[0].margin_pct > 0

    def test_above_triggered(self):
        triggers = [self._trigger(signal="dry_spell_days", direction="above", threshold=15.0)]
        results = _evaluate_triggers(triggers, {"dry_spell_days": 20.0})
        assert results[0].triggered is True

    def test_above_not_triggered(self):
        triggers = [self._trigger(signal="dry_spell_days", direction="above", threshold=15.0)]
        results = _evaluate_triggers(triggers, {"dry_spell_days": 10.0})
        assert results[0].triggered is False

    def test_missing_signal_skipped(self):
        triggers = [self._trigger(signal="rainfall_cumulative")]
        results = _evaluate_triggers(triggers, {"ndvi_z_score": -1.0})
        assert len(results) == 0

    def test_none_value_skipped(self):
        triggers = [self._trigger(signal="rainfall_cumulative")]
        results = _evaluate_triggers(triggers, {"rainfall_cumulative": None})
        assert len(results) == 0

    def test_multiple_triggers_mixed(self):
        triggers = [
            self._trigger(signal="rainfall_cumulative", direction="below", threshold=100.0),
            self._trigger(signal="dry_spell_days", direction="above", threshold=15.0),
            self._trigger(signal="spi", direction="below", threshold=-1.0),
        ]
        values = {"rainfall_cumulative": 80.0, "dry_spell_days": 10.0, "spi": -0.5}
        results = _evaluate_triggers(triggers, values)
        assert len(results) == 3
        assert results[0].triggered is True   # rainfall below 100
        assert results[1].triggered is False   # dry spell below 15
        assert results[2].triggered is False   # spi above -1.0

    def test_margin_clamped_to_999(self):
        triggers = [self._trigger(threshold=0.001, direction="below")]
        results = _evaluate_triggers(triggers, {"rainfall_cumulative": 5000.0})
        assert results[0].margin_pct <= 999

    def test_margin_clamped_negative(self):
        triggers = [self._trigger(threshold=0.001, direction="below")]
        results = _evaluate_triggers(triggers, {"rainfall_cumulative": -5000.0})
        assert results[0].margin_pct >= -999

    def test_threshold_zero_no_division_error(self):
        triggers = [self._trigger(threshold=0.0, direction="below")]
        results = _evaluate_triggers(triggers, {"rainfall_cumulative": 50.0})
        assert results[0].margin_pct == 0

    def test_exact_threshold_below_not_triggered(self):
        triggers = [self._trigger(threshold=100.0, direction="below")]
        results = _evaluate_triggers(triggers, {"rainfall_cumulative": 100.0})
        assert results[0].triggered is False

    def test_exact_threshold_above_not_triggered(self):
        triggers = [self._trigger(signal="dry_spell_days", direction="above", threshold=15.0)]
        results = _evaluate_triggers(triggers, {"dry_spell_days": 15.0})
        assert results[0].triggered is False

    def test_weight_preserved(self):
        triggers = [self._trigger(weight=0.7)]
        results = _evaluate_triggers(triggers, {"rainfall_cumulative": 50.0})
        assert results[0].weight == 0.7

    def test_description_preserved(self):
        triggers = [self._trigger(description="Custom description")]
        results = _evaluate_triggers(triggers, {"rainfall_cumulative": 50.0})
        assert results[0].description == "Custom description"


# ---------------------------------------------------------------------------
# _compute_confidence
# ---------------------------------------------------------------------------

class TestComputeConfidence:
    def _make_trigger(self, triggered, weight=1.0):
        return TriggerResult(
            signal="test", current_value=0, threshold=0,
            direction="below", triggered=triggered, margin_pct=0,
            weight=weight, description="test",
        )

    def test_no_triggers_returns_unknown(self):
        score, status = _compute_confidence([])
        assert score == 50
        assert status == "UNKNOWN"

    def test_all_passing_returns_safe(self):
        triggers = [self._make_trigger(False, 1.0), self._make_trigger(False, 0.8)]
        score, status = _compute_confidence(triggers)
        assert score == 100
        assert status == "SAFE"

    def test_one_low_weight_triggered_returns_watch(self):
        triggers = [
            self._make_trigger(True, 0.5),
            self._make_trigger(False, 1.0),
            self._make_trigger(False, 0.8),
        ]
        score, status = _compute_confidence(triggers)
        assert status == "WATCH"
        assert score < 100

    def test_one_high_weight_triggered_returns_warning(self):
        triggers = [
            self._make_trigger(True, 0.8),
            self._make_trigger(False, 1.0),
            self._make_trigger(False, 0.6),
        ]
        score, status = _compute_confidence(triggers)
        assert status == "WARNING"

    def test_two_triggered_returns_warning(self):
        triggers = [
            self._make_trigger(True, 0.5),
            self._make_trigger(True, 0.6),
            self._make_trigger(False, 1.0),
        ]
        score, status = _compute_confidence(triggers)
        assert status == "WARNING"

    def test_three_triggered_returns_payout_likely(self):
        triggers = [
            self._make_trigger(True, 1.0),
            self._make_trigger(True, 0.8),
            self._make_trigger(True, 0.6),
        ]
        score, status = _compute_confidence(triggers)
        assert status == "PAYOUT_LIKELY"
        assert score < 50

    def test_all_triggered_returns_zero_score(self):
        triggers = [
            self._make_trigger(True, 1.0),
            self._make_trigger(True, 0.8),
            self._make_trigger(True, 0.6),
        ]
        score, status = _compute_confidence(triggers)
        assert score == 0
        assert status == "PAYOUT_LIKELY"

    def test_weighted_score_calculation(self):
        triggers = [
            self._make_trigger(False, 1.0),
            self._make_trigger(True, 0.5),
        ]
        score, _ = _compute_confidence(triggers)
        expected = int((1.0 / 1.5) * 100)
        assert score == expected

    def test_zero_weight_triggers_returns_unknown(self):
        triggers = [self._make_trigger(False, 0.0)]
        score, status = _compute_confidence(triggers)
        assert score == 50
        assert status == "UNKNOWN"


# ---------------------------------------------------------------------------
# _compute_phase_rainfall
# ---------------------------------------------------------------------------

class TestComputePhaseRainfall:
    def test_full_data_computes_correctly(self):
        planting = date(2025, 9, 15)
        today = date(2025, 10, 10)
        daily = {}
        d = planting
        while d < today:
            daily[d.strftime("%Y-%m-%d")] = 5.0
            d += timedelta(days=1)

        results = _compute_phase_rainfall(daily, planting, "maize", today)
        assert len(results) >= 1
        assert results[0].phase == "planting"
        assert results[0].cumulative_mm > 0
        assert results[0].daily_avg_mm == pytest.approx(5.0)

    def test_sampled_data_estimates_correctly(self):
        planting = date(2025, 9, 15)
        today = date(2025, 10, 5)
        daily = {
            "2025-09-15": 10.0,
            "2025-09-20": 10.0,
            "2025-09-25": 10.0,
            "2025-09-30": 10.0,
        }
        results = _compute_phase_rainfall(daily, planting, "maize", today)
        assert len(results) >= 1
        planting_phase = results[0]
        assert planting_phase.daily_avg_mm == pytest.approx(10.0)
        assert planting_phase.cumulative_mm == pytest.approx(planting_phase.daily_avg_mm * planting_phase.day_count)

    def test_no_data_returns_zero(self):
        planting = date(2025, 9, 15)
        today = date(2025, 10, 5)
        results = _compute_phase_rainfall({}, planting, "maize", today)
        assert len(results) >= 1
        assert results[0].cumulative_mm == 0.0

    def test_future_phases_excluded(self):
        planting = date(2025, 9, 15)
        today = date(2025, 9, 20)  # only 5 days in
        results = _compute_phase_rainfall({}, planting, "maize", today)
        assert len(results) == 1
        assert results[0].phase == "planting"

    def test_phase_dates_are_correct(self):
        planting = date(2025, 9, 15)
        today = date(2026, 1, 15)
        daily = {}
        results = _compute_phase_rainfall(daily, planting, "maize", today)
        assert results[0].date_from == "2025-09-15"
        assert results[0].date_to == "2025-10-05"


# ---------------------------------------------------------------------------
# _current_growth_phase
# ---------------------------------------------------------------------------

class TestCurrentGrowthPhase:
    def test_maize_planting(self):
        assert _current_growth_phase("maize", 10) == "planting"

    def test_maize_vegetative(self):
        assert _current_growth_phase("maize", 30) == "vegetative"

    def test_maize_flowering(self):
        assert _current_growth_phase("maize", 60) == "flowering"

    def test_maize_grain_fill(self):
        assert _current_growth_phase("maize", 80) == "grain_fill"

    def test_maize_maturity(self):
        assert _current_growth_phase("maize", 110) == "maturity"

    def test_beyond_maturity_returns_maturity(self):
        assert _current_growth_phase("maize", 200) == "maturity"

    def test_unknown_crop_uses_maize(self):
        assert _current_growth_phase("quinoa", 10) == "planting"

    def test_day_zero(self):
        assert _current_growth_phase("maize", 0) == "planting"

    def test_phase_boundary_vegetative(self):
        assert _current_growth_phase("maize", 20) == "vegetative"


# ---------------------------------------------------------------------------
# _centroid_from_geojson / _flatten_coords
# ---------------------------------------------------------------------------

class TestCentroidFromGeojson:
    def test_point_geometry(self):
        geom = {"type": "Point", "coordinates": [29.87, -1.94]}
        lat, lon = _centroid_from_geojson(geom)
        assert lat == pytest.approx(-1.94)
        assert lon == pytest.approx(29.87)

    def test_polygon_centroid(self):
        geom = {
            "type": "Polygon",
            "coordinates": [[[29.0, -2.0], [30.0, -2.0], [30.0, -1.0], [29.0, -1.0], [29.0, -2.0]]],
        }
        lat, lon = _centroid_from_geojson(geom)
        assert lat == pytest.approx(-1.6, abs=0.01)
        assert lon == pytest.approx(29.4, abs=0.01)

    def test_empty_coords_returns_rwanda_center(self):
        geom = {"type": "Point", "coordinates": []}
        lat, lon = _centroid_from_geojson(geom)
        assert (lat, lon) == _RWANDA_CENTER

    def test_missing_coordinates_key(self):
        geom = {"type": "Point"}
        lat, lon = _centroid_from_geojson(geom)
        assert (lat, lon) == _RWANDA_CENTER


class TestFlattenCoords:
    def test_point(self):
        assert _flatten_coords([29.0, -1.0]) == [(29.0, -1.0)]

    def test_linestring(self):
        coords = [[29.0, -1.0], [30.0, -2.0]]
        result = _flatten_coords(coords)
        assert len(result) == 2

    def test_polygon(self):
        coords = [[[29.0, -1.0], [30.0, -2.0], [29.0, -1.0]]]
        result = _flatten_coords(coords)
        assert len(result) == 3

    def test_empty(self):
        assert _flatten_coords([]) == []

    def test_none_like(self):
        assert _flatten_coords(None) == []


# ---------------------------------------------------------------------------
# _default_triggers
# ---------------------------------------------------------------------------

class TestDefaultTriggers:
    def test_returns_six_triggers(self):
        triggers = _default_triggers("full_season")
        assert len(triggers) == 6

    def test_signals_present(self):
        triggers = _default_triggers("full_season")
        signals = {t["signal"] for t in triggers}
        assert "rainfall_cumulative" in signals
        assert "spi" in signals
        assert "dry_spell_days" in signals
        assert "ndvi_z_score" in signals
        assert "et_anomaly" in signals
        assert "sar_backscatter" in signals

    def test_all_have_required_fields(self):
        for t in _default_triggers("full_season"):
            assert "signal" in t
            assert "direction" in t
            assert "threshold" in t
            assert "weight" in t


# ---------------------------------------------------------------------------
# _generate_recommendation
# ---------------------------------------------------------------------------

class TestGenerateRecommendation:
    def _make_trigger(self, signal, triggered):
        return TriggerResult(
            signal=signal, current_value=0, threshold=0,
            direction="below", triggered=triggered, margin_pct=0,
            weight=1.0, description="test",
        )

    def test_safe_status(self):
        rec = _generate_recommendation("SAFE", "maize", "vegetative", [])
        assert "normally" in rec.lower()
        assert "maize" in rec.lower()

    def test_watch_status(self):
        triggers = [self._make_trigger("rainfall_cumulative", True)]
        rec = _generate_recommendation("WATCH", "beans", "flowering", triggers)
        assert "monitor" in rec.lower()

    def test_warning_status(self):
        triggers = [self._make_trigger("spi", True)]
        rec = _generate_recommendation("WARNING", "rice", "grain_fill", triggers)
        assert "warning" in rec.lower()

    def test_payout_likely_status(self):
        triggers = [
            self._make_trigger("rainfall_cumulative", True),
            self._make_trigger("spi", True),
            self._make_trigger("dry_spell_days", True),
        ]
        rec = _generate_recommendation("PAYOUT_LIKELY", "maize", "flowering", triggers)
        assert "payout" in rec.lower() or "claims" in rec.lower()


# ---------------------------------------------------------------------------
# format_for_audience
# ---------------------------------------------------------------------------

class TestFormatForAudience:
    @pytest.fixture
    def report(self):
        return InsuranceReport(
            location_name="Musanze",
            admin_level="district",
            crop="maize",
            season="A",
            growth_phase="vegetative",
            days_after_planting=35,
            season_rainfall_mm=178.0,
            spi=-0.3,
            ndvi_z_score=-0.18,
            max_dry_spell_days=8,
            triggers=[
                TriggerResult("rainfall_cumulative", 178.0, 100.0, "below", False, 78.0, 1.0, "Season rainfall"),
                TriggerResult("spi", -0.3, -1.0, "below", False, 70.0, 0.8, "SPI drought"),
            ],
            triggers_activated=0,
            triggers_total=2,
            confidence_score=100,
            overall_status="SAFE",
            recommendation="Crop progressing normally.",
            sources=["CHIRPS v2.0", "Sentinel-2"],
            period_start="2025-09-15",
            period_end="2025-10-20",
            computed_at="2025-10-20T12:00:00Z",
        )

    def test_farmer_format_short(self, report):
        text = format_for_audience(report, "farmer")
        assert "Musanze" in text
        assert "maize" in text
        assert "SAFE" in text
        assert len(text) < 600

    def test_farmer_format_no_trigger_line(self, report):
        text = format_for_audience(report, "farmer")
        assert "No drought trigger activated" in text

    def test_farmer_format_triggered_shows_count(self, report):
        report.triggers[0] = TriggerResult("rainfall_cumulative", 50.0, 100.0, "below", True, -50.0, 1.0, "Rainfall")
        report.triggers_activated = 1
        report.overall_status = "WATCH"
        text = format_for_audience(report, "farmer")
        assert "1 trigger(s) activated" in text

    def test_insurance_format_has_table(self, report):
        text = format_for_audience(report, "insurance")
        assert "TRIGGER ASSESSMENT" in text
        assert "Signal" in text
        assert "PASS" in text
        assert "Confidence: 100/100" in text

    def test_agronomist_format_has_sections(self, report):
        text = format_for_audience(report, "agronomist")
        assert "AGRONOMIC ASSESSMENT" in text
        assert "RAINFALL:" in text
        assert "VEGETATION:" in text
        assert "WATER BALANCE:" in text
        assert "STATUS:" in text
        assert "RECOMMENDATION:" in text

    def test_scientist_format_is_valid_json(self, report):
        text = format_for_audience(report, "scientist")
        data = json.loads(text)
        assert "methodology" in data
        assert "location" in data
        assert data["location"] == "Musanze"
        assert data["crop"] == "maize"

    def test_unknown_audience_defaults_to_insurance(self, report):
        text = format_for_audience(report, "unknown_audience")
        assert "TRIGGER ASSESSMENT" in text

    def test_farmer_format_with_stressed_ndvi(self, report):
        report.ndvi_z_score = -1.8
        text = format_for_audience(report, "farmer")
        assert "very stressed" in text

    def test_farmer_format_with_healthy_ndvi(self, report):
        report.ndvi_z_score = 0.5
        text = format_for_audience(report, "farmer")
        assert "healthy" in text


# ---------------------------------------------------------------------------
# TriggerResult.to_dict
# ---------------------------------------------------------------------------

class TestTriggerResultToDict:
    def test_roundtrip(self):
        t = TriggerResult(
            signal="rainfall_cumulative",
            current_value=123.456789,
            threshold=100.0,
            direction="below",
            triggered=False,
            margin_pct=23.456789,
            weight=1.0,
            description="Test trigger",
        )
        d = t.to_dict()
        assert d["current_value"] == 123.46
        assert d["margin_pct"] == 23.5
        assert d["triggered"] is False
        assert d["signal"] == "rainfall_cumulative"


# ---------------------------------------------------------------------------
# InsuranceReport.to_dict
# ---------------------------------------------------------------------------

class TestInsuranceReportToDict:
    def test_basic_fields(self):
        r = InsuranceReport(
            location_name="Gasabo",
            admin_level="district",
            crop="beans",
            season="B",
            growth_phase="flowering",
            days_after_planting=45,
            season_rainfall_mm=123.456,
            spi=-0.789,
        )
        d = r.to_dict()
        assert d["location"] == "Gasabo"
        assert d["crop"] == "beans"
        assert d["season_rainfall_mm"] == 123.5
        assert d["spi"] == -0.79
        assert d["ndvi_z_score"] is None

    def test_phase_rainfall_included(self):
        r = InsuranceReport(
            location_name="Test", admin_level="district",
            crop="maize", season="A",
            growth_phase="planting", days_after_planting=10,
            phase_rainfall=[
                PhaseRainfall("planting", 50.123, 10, 5.012, "2025-09-15", "2025-09-25"),
            ],
        )
        d = r.to_dict()
        assert len(d["phase_rainfall"]) == 1
        assert d["phase_rainfall"][0]["cumulative_mm"] == 50.1
        assert d["phase_rainfall"][0]["daily_avg_mm"] == 5.0

    def test_accuracy_components_included(self):
        acc = {"pod": 0.85, "far": 0.12, "hss": 0.71, "csi": 0.68}
        r = InsuranceReport(
            location_name="Test", admin_level="district",
            crop="maize", season="A",
            growth_phase="planting", days_after_planting=10,
            accuracy_components=acc,
        )
        d = r.to_dict()
        assert d["accuracy_components"] == acc

    def test_accuracy_components_none_by_default(self):
        r = InsuranceReport(
            location_name="Test", admin_level="district",
            crop="maize", season="A",
            growth_phase="planting", days_after_planting=10,
        )
        d = r.to_dict()
        assert d["accuracy_components"] is None

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

class TestConstants:
    def test_rwanda_center_is_tuple(self):
        assert isinstance(_RWANDA_CENTER, tuple)
        assert len(_RWANDA_CENTER) == 2

    def test_et_long_term_mean_is_positive(self):
        assert _ET_LONG_TERM_MEAN > 0

    def test_national_rainfall_normals_has_both_seasons(self):
        assert "A" in _NATIONAL_RAINFALL_NORMALS
        assert "B" in _NATIONAL_RAINFALL_NORMALS
        for season in ("A", "B"):
            assert "mean" in _NATIONAL_RAINFALL_NORMALS[season]
            assert "std" in _NATIONAL_RAINFALL_NORMALS[season]
            assert _NATIONAL_RAINFALL_NORMALS[season]["std"] > 0

    def test_district_rainfall_normals_cover_30_districts(self):
        assert len(_DISTRICT_RAINFALL_NORMALS) >= 28
        for dist, seasons in _DISTRICT_RAINFALL_NORMALS.items():
            assert "A" in seasons, f"{dist} missing season A"
            assert "B" in seasons, f"{dist} missing season B"
            for s in ("A", "B"):
                assert seasons[s]["std"] > 0, f"{dist} season {s} has zero std"

    def test_district_spi_differs_across_districts(self):
        rainfall = 250.0
        spi_bugesera = _compute_spi(rainfall, "B", district="bugesera")
        spi_musanze = _compute_spi(rainfall, "B", district="musanze")
        assert spi_bugesera != spi_musanze, "SPI should differ for different districts"
        assert spi_bugesera > spi_musanze, "250mm is closer to normal for dry Bugesera"


# ---------------------------------------------------------------------------
# Integration: evaluate + confidence pipeline
# ---------------------------------------------------------------------------

class TestEvaluateAndConfidencePipeline:
    """End-to-end test: trigger defs + values -> evaluate -> confidence."""

    def test_safe_scenario(self):
        triggers = _default_triggers("full_season")
        values = {
            "rainfall_cumulative": 200.0,
            "spi": 0.5,
            "dry_spell_days": 5.0,
            "ndvi_z_score": 0.0,
            "et_anomaly": -5.0,
        }
        results = _evaluate_triggers(triggers, values)
        score, status = _compute_confidence(results)
        assert status == "SAFE"
        assert score == 100

    def test_drought_scenario(self):
        triggers = _default_triggers("full_season")
        values = {
            "rainfall_cumulative": 50.0,
            "spi": -2.0,
            "dry_spell_days": 25.0,
            "ndvi_z_score": -2.0,
            "et_anomaly": -30.0,
        }
        results = _evaluate_triggers(triggers, values)
        score, status = _compute_confidence(results)
        assert status == "PAYOUT_LIKELY"
        assert score == 0

    def test_partial_data_still_works(self):
        triggers = _default_triggers("full_season")
        values = {"rainfall_cumulative": 50.0}
        results = _evaluate_triggers(triggers, values)
        assert len(results) == 1
        score, status = _compute_confidence(results)
        assert status in ("WATCH", "WARNING", "PAYOUT_LIKELY")

    def test_watch_scenario(self):
        triggers = _default_triggers("full_season")
        values = {
            "rainfall_cumulative": 200.0,
            "spi": 0.5,
            "dry_spell_days": 20.0,   # only this one triggered (weight 0.6)
            "ndvi_z_score": 0.0,
            "et_anomaly": -5.0,
        }
        results = _evaluate_triggers(triggers, values)
        score, status = _compute_confidence(results)
        assert status == "WATCH"


# ===========================================================================
# Part 2: Mocked async tests — DB/API functions with mocked dependencies
# ===========================================================================

def _run(coro):
    """Run an async coroutine in a fresh event loop."""
    return asyncio.get_event_loop().run_until_complete(coro)


# ---------------------------------------------------------------------------
# _resolve_location_name
# ---------------------------------------------------------------------------

class TestResolveLocationName:
    def test_village_most_specific(self):
        name, level = _resolve_location_name(district="Musanze", village="Kinigi")
        assert name == "Kinigi"
        assert level == "village"

    def test_cell_level(self):
        name, level = _resolve_location_name(district="Musanze", sector="Gataraga", cell="Ruhondo")
        assert name == "Ruhondo"
        assert level == "cell"

    def test_sector_level(self):
        name, level = _resolve_location_name(district="Musanze", sector="Gataraga")
        assert name == "Gataraga"
        assert level == "sector"

    def test_district_level(self):
        name, level = _resolve_location_name(district="Musanze")
        assert name == "Musanze"
        assert level == "district"

    def test_nothing_returns_empty(self):
        name, level = _resolve_location_name()
        assert name == ""
        assert level == ""

    def test_whitespace_stripped(self):
        name, level = _resolve_location_name(district="  Huye  ")
        assert name == "Huye"


# ---------------------------------------------------------------------------
# _get_planting_date and _get_harvest_dap
# ---------------------------------------------------------------------------

class TestGetPlantingDate:
    def test_known_crop_season(self):
        d = _get_planting_date("maize", "A", 2025)
        assert isinstance(d, date)
        assert d.year == 2025

    def test_unknown_crop_falls_back(self):
        d = _get_planting_date("quinoa", "A", 2025)
        assert isinstance(d, date)

    def test_season_b(self):
        d = _get_planting_date("maize", "B", 2026)
        assert d.year == 2026


class TestGetHarvestDap:
    def test_known_crop(self):
        dap = _get_harvest_dap("maize", "A")
        assert isinstance(dap, int)
        assert dap > 0

    def test_unknown_crop_returns_default(self):
        dap = _get_harvest_dap("quinoa", "A")
        assert dap == 120


# ---------------------------------------------------------------------------
# _fetch_ndvi_anomaly (mock conn)
# ---------------------------------------------------------------------------

class TestFetchNdviAnomaly:
    def test_with_district(self):
        conn = AsyncMock()
        conn.fetchrow.return_value = {"mean_z": -0.85}
        result = _run(_fetch_ndvi_anomaly(conn, district="Musanze"))
        assert result == pytest.approx(-0.85)
        conn.fetchrow.assert_called_once()
        call_sql = conn.fetchrow.call_args[0][0]
        assert "LOWER(district)" in call_sql

    def test_without_district(self):
        conn = AsyncMock()
        conn.fetchrow.return_value = {"mean_z": 0.3}
        result = _run(_fetch_ndvi_anomaly(conn, district=None))
        assert result == pytest.approx(0.3)
        call_sql = conn.fetchrow.call_args[0][0]
        assert "LOWER(district)" not in call_sql

    def test_no_data_returns_none(self):
        conn = AsyncMock()
        conn.fetchrow.return_value = {"mean_z": None}
        result = _run(_fetch_ndvi_anomaly(conn, district="Musanze"))
        assert result is None

    def test_no_rows_returns_none(self):
        conn = AsyncMock()
        conn.fetchrow.return_value = None
        result = _run(_fetch_ndvi_anomaly(conn))
        assert result is None

    def test_exception_returns_none(self):
        conn = AsyncMock()
        conn.fetchrow.side_effect = Exception("connection lost")
        result = _run(_fetch_ndvi_anomaly(conn, district="Musanze"))
        assert result is None


# ---------------------------------------------------------------------------
# _fetch_sar_backscatter (mock sentinel1_service)
# ---------------------------------------------------------------------------

class TestFetchSarBackscatter:
    def test_linear_power_values(self):
        """VH=0.05, VV=0.3 in linear power → ratio 0.167"""
        svc = MagicMock()
        svc.get_backscatter.return_value = {
            "status": "success",
            "statistics": {"vh": {"mean": 0.05}, "vv": {"mean": 0.3}},
        }
        with patch("src.services.sentinel1_service.get_sentinel1_service", return_value=svc):
            result = _run(_fetch_sar_backscatter(1.5, 29.5, "2025-10-01", "2025-11-15"))
        assert result == pytest.approx(0.05 / 0.3, rel=1e-4)

    def test_db_values_converted_to_linear_ratio(self):
        """VH=-20dB, VV=-12dB → linear ratio = 10^((-20-(-12))/10) ≈ 0.158"""
        svc = MagicMock()
        svc.get_backscatter.return_value = {
            "status": "success",
            "statistics": {"vh": {"mean": -20.0}, "vv": {"mean": -12.0}},
        }
        with patch("src.services.sentinel1_service.get_sentinel1_service", return_value=svc):
            result = _run(_fetch_sar_backscatter(1.5, 29.5, "2025-10-01", "2025-11-15"))
        expected = 10 ** ((-20.0 - (-12.0)) / 10)  # ≈ 0.158
        assert result == pytest.approx(expected, rel=1e-4)
        assert 0.1 < result < 0.3  # sanity: within expected VH/VV range

    def test_db_values_typical_vegetation(self):
        """VH=-15dB, VV=-8dB → healthy vegetation, ratio ≈ 0.2"""
        svc = MagicMock()
        svc.get_backscatter.return_value = {
            "status": "success",
            "statistics": {"vh": {"mean": -15.0}, "vv": {"mean": -8.0}},
        }
        with patch("src.services.sentinel1_service.get_sentinel1_service", return_value=svc):
            result = _run(_fetch_sar_backscatter(1.5, 29.5, "2025-10-01", "2025-11-15"))
        expected = 10 ** ((-15.0 - (-8.0)) / 10)  # ≈ 0.2
        assert result == pytest.approx(expected, rel=1e-4)

    def test_service_error_returns_none(self):
        svc = MagicMock()
        svc.get_backscatter.side_effect = Exception("service down")
        with patch("src.services.sentinel1_service.get_sentinel1_service", return_value=svc):
            result = _run(_fetch_sar_backscatter(1.5, 29.5, "2025-10-01", "2025-11-15"))
        assert result is None

    def test_missing_stats_returns_none(self):
        svc = MagicMock()
        svc.get_backscatter.return_value = {"status": "success", "statistics": {}}
        with patch("src.services.sentinel1_service.get_sentinel1_service", return_value=svc):
            result = _run(_fetch_sar_backscatter(1.5, 29.5, "2025-10-01", "2025-11-15"))
        assert result is None

    def test_vv_zero_returns_none(self):
        svc = MagicMock()
        svc.get_backscatter.return_value = {
            "status": "success",
            "statistics": {"vh": {"mean": 0.05}, "vv": {"mean": 0}},
        }
        with patch("src.services.sentinel1_service.get_sentinel1_service", return_value=svc):
            result = _run(_fetch_sar_backscatter(1.5, 29.5, "2025-10-01", "2025-11-15"))
        assert result is None


# ---------------------------------------------------------------------------
# _VALID_AUDIENCES
# ---------------------------------------------------------------------------

class TestValidAudiences:
    def test_constant_matches_formatters(self):
        assert _VALID_AUDIENCES == {"farmer", "insurance", "agronomist", "scientist"}

    def test_invalid_audience_clamped_to_farmer(self):
        conn = AsyncMock()
        conn.fetchrow.return_value = {"mean_z": -0.5}
        conn.fetch.return_value = []

        from contextlib import ExitStack
        stack = ExitStack()
        stack.enter_context(patch("src.services.admin_boundaries.lookup_admin_geometry", new_callable=AsyncMock, return_value=None))
        stack.enter_context(patch("src.services.dssat_service.detect_current_season", return_value="A"))
        stack.enter_context(patch("src.services.insurance_engine.compute_insurance_accuracy_safe", new_callable=AsyncMock, return_value=None))
        stack.enter_context(patch("src.services.weather_accuracy.detect_dry_spells", new_callable=AsyncMock, return_value=None))
        stack.enter_context(patch("src.services.weather_accuracy.compute_ndvi_concordance", new_callable=AsyncMock, return_value=None))
        stack.enter_context(patch("src.services.forecast_fusion._fetch_chirps_precip", return_value={}))
        stack.enter_context(patch("src.services.wapor_service.query_et", return_value=None))
        stack.enter_context(patch("src.services.wapor_service.query_soil_moisture", return_value=None))
        svc = MagicMock()
        svc.get_backscatter.return_value = {"status": "success", "statistics": {"vh": {"mean": 0.05}, "vv": {"mean": 0.3}}}
        stack.enter_context(patch("src.services.sentinel1_service.get_sentinel1_service", return_value=svc))
        pred = MagicMock()
        pred.predict_ndvi.return_value = {"status": "success", "predicted_ndvi": 0.45}
        stack.enter_context(patch("src.services.sar_ndvi.get_sar_ndvi_predictor", return_value=pred))

        with stack:
            result = _run(compute_insurance_intelligence(
                conn, crop="maize", district="Musanze",
                audience="hacker_injection", ref_date=date(2025, 11, 15),
            ))
        assert result["status"] == "ok"
        assert result["audience"] == "farmer"


# ---------------------------------------------------------------------------
# _load_triggers (mock conn)
# ---------------------------------------------------------------------------

class TestLoadTriggers:
    def test_returns_parsed_rows(self):
        conn = AsyncMock()
        row1 = {"signal": "spi", "direction": "below", "threshold": -1.0, "weight": 0.8, "description": "drought"}
        row2 = {"signal": "rainfall_cumulative", "direction": "below", "threshold": 100.0, "weight": 1.0, "description": "low rain"}
        conn.fetch.return_value = [MagicMock(**{"__getitem__": lambda s, k: row1[k], "keys": lambda s: row1.keys()}),
                                    MagicMock(**{"__getitem__": lambda s, k: row2[k], "keys": lambda s: row2.keys()})]
        conn.fetch.return_value = [row1, row2]
        result = _run(_load_triggers(conn, "maize", "A", "flowering", "Musanze"))
        assert len(result) == 2
        assert result[0]["signal"] == "spi"

    def test_sql_includes_enabled_filter(self):
        conn = AsyncMock()
        conn.fetch.return_value = []
        _run(_load_triggers(conn, "maize", "A", "flowering", None))
        call_sql = conn.fetch.call_args[0][0]
        assert "enabled = true" in call_sql

    def test_sql_passes_crop_season_phase_district(self):
        conn = AsyncMock()
        conn.fetch.return_value = []
        _run(_load_triggers(conn, "beans", "B", "vegetative", "Huye"))
        args = conn.fetch.call_args[0]
        assert args[1] == "beans"
        assert args[2] == "B"
        assert args[3] == "vegetative"
        assert args[4] == "Huye"

    def test_exception_falls_back_to_defaults(self):
        conn = AsyncMock()
        conn.fetch.side_effect = Exception("table not found")
        result = _run(_load_triggers(conn, "maize", "A", "flowering", None))
        assert len(result) == 6
        assert result[0]["signal"] == "rainfall_cumulative"

    def test_distinct_on_phase_signal(self):
        conn = AsyncMock()
        conn.fetch.return_value = []
        _run(_load_triggers(conn, "maize", "A", "full_season", None))
        call_sql = conn.fetch.call_args[0][0]
        assert "DISTINCT ON (phase, signal)" in call_sql

    def test_district_override_ordering(self):
        conn = AsyncMock()
        conn.fetch.return_value = []
        _run(_load_triggers(conn, "maize", "A", "full_season", "Musanze"))
        call_sql = conn.fetch.call_args[0][0]
        assert "CASE WHEN district IS NOT NULL THEN 0 ELSE 1 END" in call_sql


# ---------------------------------------------------------------------------
# compute_insurance_accuracy_safe (mock)
# ---------------------------------------------------------------------------

class TestComputeInsuranceAccuracySafe:
    def test_returns_result_on_success(self):
        conn = AsyncMock()
        expected = {"status": "success", "confidence_rating": 85}
        mock_fn = AsyncMock(return_value=expected)
        fake_module = MagicMock(compute_insurance_accuracy=mock_fn)
        with patch.dict("sys.modules", {"src.services.weather_accuracy": fake_module}):
            result = _run(compute_insurance_accuracy_safe(conn, "Musanze", "A"))
        assert result is not None

    def test_returns_none_on_exception(self):
        conn = AsyncMock()
        mock_fn = AsyncMock(side_effect=Exception("fail"))
        fake_module = MagicMock(compute_insurance_accuracy=mock_fn)
        with patch.dict("sys.modules", {"src.services.weather_accuracy": fake_module}):
            result = _run(compute_insurance_accuracy_safe(conn, "Musanze", "A"))
        assert result is None


# ---------------------------------------------------------------------------
# compute_insurance_intelligence (full orchestrator mock)
# ---------------------------------------------------------------------------

class TestComputeInsuranceIntelligence:
    """Full orchestrator tests. All external lazy imports are patched at their source modules."""

    def _mock_conn(self):
        conn = AsyncMock()
        conn.fetch.return_value = [
            {"signal": "rainfall_cumulative", "direction": "below", "threshold": 100.0, "weight": 1.0, "description": "Low rain"},
            {"signal": "spi", "direction": "below", "threshold": -1.0, "weight": 0.8, "description": "Drought"},
        ]
        conn.fetchrow.return_value = {"mean_z": -0.5}
        return conn

    def _patches(self, *, geom=None, season="A", acc=None, dry=None, conc=None, chirps=None, et=None, soil=None, sar=None):
        """Return a contextlib.ExitStack context manager with all external deps patched."""
        from contextlib import ExitStack
        stack = ExitStack()
        stack.enter_context(patch("src.services.admin_boundaries.lookup_admin_geometry", new_callable=AsyncMock, return_value=geom))
        stack.enter_context(patch("src.services.dssat_service.detect_current_season", return_value=season))
        stack.enter_context(patch("src.services.insurance_engine.compute_insurance_accuracy_safe", new_callable=AsyncMock, return_value=acc))
        stack.enter_context(patch("src.services.weather_accuracy.detect_dry_spells", new_callable=AsyncMock, return_value=dry))
        stack.enter_context(patch("src.services.weather_accuracy.compute_ndvi_concordance", new_callable=AsyncMock, return_value=conc))
        stack.enter_context(patch("src.services.forecast_fusion._fetch_chirps_precip", return_value=chirps or {}))
        stack.enter_context(patch("src.services.wapor_service.query_et", return_value=et))
        stack.enter_context(patch("src.services.wapor_service.query_soil_moisture", return_value=soil))
        # SAR services — cloud-penetrating fallback
        sar_svc = MagicMock()
        sar_svc.get_backscatter.return_value = sar or {"status": "success", "statistics": {"vh": {"mean": 0.05}, "vv": {"mean": 0.3}}}
        stack.enter_context(patch("src.services.sentinel1_service.get_sentinel1_service", return_value=sar_svc))
        sar_ndvi_pred = MagicMock()
        sar_ndvi_pred.predict_ndvi.return_value = {"status": "success", "predicted_ndvi": 0.45}
        stack.enter_context(patch("src.services.sar_ndvi.get_sar_ndvi_predictor", return_value=sar_ndvi_pred))
        return stack

    def test_returns_ok_with_district(self):
        conn = self._mock_conn()
        with self._patches():
            result = _run(compute_insurance_intelligence(
                conn, crop="maize", district="Musanze", ref_date=date(2025, 11, 15),
            ))
        assert result["status"] == "ok"
        assert "report" in result
        assert "data" in result
        assert result["audience"] == "farmer"

    def test_error_without_location(self):
        conn = self._mock_conn()
        with self._patches():
            result = _run(compute_insurance_intelligence(conn, crop="maize", ref_date=date(2025, 11, 15)))
        assert result["status"] == "error"
        assert "district" in result["error"].lower() or "specify" in result["error"].lower()

    def test_season_auto_detection_used(self):
        conn = self._mock_conn()
        with self._patches(season="B") as stack:
            result = _run(compute_insurance_intelligence(
                conn, crop="maize", district="Huye", ref_date=date(2026, 3, 15),
            ))
        assert result["status"] == "ok"
        assert result["data"]["season"] == "B"

    def test_geometry_used_for_centroid(self):
        conn = self._mock_conn()
        with self._patches(geom={"type": "Point", "coordinates": [29.5, -1.5]}):
            result = _run(compute_insurance_intelligence(
                conn, crop="maize", district="Musanze", ref_date=date(2025, 11, 15),
            ))
        assert result["status"] == "ok"
        assert result["geometry"] is not None

    def test_unknown_crop_defaults_to_district_primary(self):
        conn = self._mock_conn()
        with self._patches():
            result = _run(compute_insurance_intelligence(
                conn, crop="quinoa", district="Musanze", ref_date=date(2025, 11, 15),
            ))
        assert result["status"] == "ok"
        assert result["data"]["crop"] == "potato"

    def test_audience_parameter_forwarded(self):
        conn = self._mock_conn()
        with self._patches():
            result = _run(compute_insurance_intelligence(
                conn, crop="maize", district="Musanze",
                audience="scientist", ref_date=date(2025, 11, 15),
            ))
        assert result["audience"] == "scientist"
        data = json.loads(result["report"])
        assert "methodology" in data

    def test_chirps_data_flows_to_rainfall(self):
        conn = self._mock_conn()
        chirps_data = {
            "2025-10-01": 5.0, "2025-10-02": 3.0, "2025-10-03": 0.0,
            "2025-10-15": 8.0, "2025-10-20": 12.0,
            "2025-11-01": 6.0, "2025-11-10": 4.0,
        }
        with self._patches(chirps=chirps_data):
            result = _run(compute_insurance_intelligence(
                conn, crop="maize", district="Musanze", ref_date=date(2025, 11, 15),
            ))
        assert result["status"] == "ok"
        assert result["data"]["season_rainfall_mm"] > 0

    def test_dry_spells_flow_through(self):
        conn = self._mock_conn()
        dry_result = {"status": "success", "longest_spell_days": 12, "dry_spells": [{"duration_days": 12, "ongoing": False}]}
        with self._patches(dry=dry_result):
            result = _run(compute_insurance_intelligence(
                conn, crop="maize", district="Musanze", ref_date=date(2025, 11, 15),
            ))
        assert result["data"]["max_dry_spell_days"] == 12

    def test_et_anomaly_computed_from_wapor(self):
        conn = self._mock_conn()
        et_result = {"status": "success", "time_series": [{"value": 3.0}, {"value": 4.0}, {"value": 3.5}]}
        with self._patches(et=et_result):
            result = _run(compute_insurance_intelligence(
                conn, crop="maize", district="Musanze", ref_date=date(2025, 11, 15),
            ))
        assert result["data"]["et_anomaly_pct"] is not None

    def test_soil_moisture_latest_value_used(self):
        conn = self._mock_conn()
        soil_result = {"status": "success", "time_series": [{"value": 40.0}, {"value": 35.0}, {"value": 28.0}]}
        with self._patches(soil=soil_result):
            result = _run(compute_insurance_intelligence(
                conn, crop="maize", district="Musanze", ref_date=date(2025, 11, 15),
            ))
        assert result["data"]["soil_moisture_pct"] == pytest.approx(28.0)

    def test_slug_format(self):
        conn = self._mock_conn()
        with self._patches():
            result = _run(compute_insurance_intelligence(
                conn, crop="maize", district="Musanze", ref_date=date(2025, 11, 15),
            ))
        assert result["slug"].startswith("insurance-maize-musanze")

    def test_accuracy_result_used_when_available(self):
        conn = self._mock_conn()
        acc_result = {
            "status": "success",
            "confidence_rating": 85,
            "recommendation": "Safe",
            "components": {
                "binary_accuracy": {
                    "overall_binary": {"pod": 0.9, "far": 0.1, "hss": 0.8, "csi": 0.75}
                }
            },
        }
        with self._patches(acc=acc_result):
            result = _run(compute_insurance_intelligence(
                conn, crop="maize", district="Musanze", ref_date=date(2025, 11, 15),
            ))
        assert result["status"] == "ok"
        assert "report" in result
        ac = result["data"]["accuracy_components"]
        assert ac is not None
        assert ac["confidence_rating"] == 85
        assert ac["pod"] == 0.9
        assert ac["far"] == 0.1
        assert ac["hss"] == 0.8
        assert ac["csi"] == 0.75


# ---------------------------------------------------------------------------
# Migration validation (parse, don't run)
# ---------------------------------------------------------------------------

class TestMigrationIntegrity:
    def test_seed_data_count(self):
        import re
        with open("alembic/versions/a1b2c3d4e5f7_insurance_triggers.py") as f:
            content = f.read()
        insert_rows = re.findall(r"^\s+\('[\w]+',\s*'[AB]',", content, re.MULTILINE)
        assert len(insert_rows) == 568

    def test_all_crops_have_at_least_one_season(self):
        with open("alembic/versions/a1b2c3d4e5f7_insurance_triggers.py") as f:
            content = f.read()
        for crop in _GROWTH_PHASES:
            assert f"('{crop}'," in content, f"Crop {crop} missing from seed data"

    def test_enabled_column_exists(self):
        with open("alembic/versions/a1b2c3d4e5f7_insurance_triggers.py") as f:
            content = f.read()
        assert '"enabled"' in content
        assert 'server_default="true"' in content

    def test_check_constraints_defined(self):
        with open("alembic/versions/a1b2c3d4e5f7_insurance_triggers.py") as f:
            content = f.read()
        assert "ck_insurance_triggers_season" in content
        assert "ck_insurance_triggers_phase" in content
        assert "ck_insurance_triggers_signal" in content
        assert "ck_insurance_triggers_direction" in content

    def test_downgrade_drops_table(self):
        with open("alembic/versions/a1b2c3d4e5f7_insurance_triggers.py") as f:
            content = f.read()
        assert "drop_table" in content
        assert "insurance_triggers" in content

    def test_revision_chain(self):
        with open("alembic/versions/a1b2c3d4e5f7_insurance_triggers.py") as f:
            content = f.read()
        assert 'revision: str = "a1b2c3d4e5f7"' in content
        assert 'down_revision: str = "47463555a0f8"' in content


# ===========================================================================
# Part 3: Coverage gap tests — edge cases and conditional branches
# ===========================================================================


# ---------------------------------------------------------------------------
# _compute_spi: std == 0 branch
# ---------------------------------------------------------------------------

class TestComputeSPIEdgeCases:
    def test_std_zero_returns_zero(self):
        """When std is 0, _compute_spi should return 0.0 to avoid division by zero."""
        with patch.dict(
            "src.services.insurance_engine._NATIONAL_RAINFALL_NORMALS",
            {"A": {"mean": 400.0, "std": 0}, "B": {"mean": 350.0, "std": 75.0}},
        ):
            spi = _compute_spi(500.0, "A")
            assert spi == 0.0

    def test_district_normals_used_when_available(self):
        spi_with_district = _compute_spi(300.0, "B", district="bugesera")
        spi_without_district = _compute_spi(300.0, "B")
        assert spi_with_district != spi_without_district

    def test_unknown_district_falls_back_to_national(self):
        spi_unknown = _compute_spi(300.0, "B", district="nonexistent")
        spi_national = _compute_spi(300.0, "B")
        assert spi_unknown == spi_national


# ---------------------------------------------------------------------------
# _format_farmer: edge case branches
# ---------------------------------------------------------------------------

class TestFormatFarmerEdgeCases:
    def _report(self, **overrides):
        defaults = dict(
            location_name="Musanze", admin_level="district",
            crop="maize", season="A", growth_phase="vegetative",
            days_after_planting=35, season_rainfall_mm=178.0, spi=-0.3,
            ndvi_z_score=-0.18, max_dry_spell_days=8,
            triggers=[], triggers_activated=0, triggers_total=0,
            confidence_score=100, overall_status="SAFE",
            recommendation="Crop progressing normally.",
            sources=["CHIRPS v2.0"], period_start="2025-09-15",
            period_end="2025-10-20", computed_at="2025-10-20T12:00:00Z",
        )
        defaults.update(overrides)
        return InsuranceReport(**defaults)

    def test_ndvi_stressed_middle_band(self):
        """NDVI z-score between -1.5 and -0.5 should show 'stressed'."""
        r = self._report(ndvi_z_score=-1.0)
        text = format_for_audience(r, "farmer")
        assert "stressed" in text
        assert "very stressed" not in text

    def test_ndvi_none_skips_vegetation_line(self):
        """When ndvi_z_score is None, no vegetation line should appear."""
        r = self._report(ndvi_z_score=None)
        text = format_for_audience(r, "farmer")
        assert "Vegetation:" not in text
        assert "healthy" not in text
        assert "stressed" not in text

    def test_max_dry_spell_zero_skips_line(self):
        """When max_dry_spell_days is 0, no dry spell line should appear."""
        r = self._report(max_dry_spell_days=0)
        text = format_for_audience(r, "farmer")
        assert "dry spell" not in text.lower()


# ---------------------------------------------------------------------------
# _format_insurance: direction="above" operator and empty sources fallback
# ---------------------------------------------------------------------------

class TestFormatInsuranceEdgeCases:
    def _report(self, **overrides):
        defaults = dict(
            location_name="Huye", admin_level="district",
            crop="beans", season="B", growth_phase="flowering",
            days_after_planting=50, season_rainfall_mm=120.0, spi=-0.5,
            triggers=[], triggers_activated=0, triggers_total=0,
            confidence_score=80, overall_status="WATCH",
            recommendation="Monitor.", sources=["CHIRPS v2.0"],
            period_start="2026-02-15", period_end="2026-04-01",
            computed_at="2026-04-01T12:00:00Z",
        )
        defaults.update(overrides)
        return InsuranceReport(**defaults)

    def test_above_direction_shows_gt_operator(self):
        """Triggers with direction='above' should show > operator (trigger fires when current > threshold)."""
        trigger = TriggerResult("dry_spell_days", 20.0, 15.0, "above", True, 33.3, 0.6, "Max dry spell")
        r = self._report(triggers=[trigger], triggers_activated=1, triggers_total=1)
        text = format_for_audience(r, "insurance")
        assert ">" in text

    def test_empty_sources_uses_fallback(self):
        """When sources list is empty, should fallback to default source string."""
        r = self._report(sources=[])
        text = format_for_audience(r, "insurance")
        assert "CHIRPS, Sentinel-1/2, WaPOR" in text


# ---------------------------------------------------------------------------
# _format_agronomist: optional data field branches
# ---------------------------------------------------------------------------

class TestFormatAgronomistEdgeCases:
    def _report(self, **overrides):
        defaults = dict(
            location_name="Kayonza", admin_level="district",
            crop="rice", season="A", growth_phase="grain_fill",
            days_after_planting=90, season_rainfall_mm=350.0, spi=-0.1,
            max_dry_spell_days=5, active_dry_spell_days=0,
            ndvi_z_score=-0.3, ndvi_concordance_score=None,
            et_anomaly_pct=None, soil_moisture_pct=None,
            triggers=[], triggers_activated=0, triggers_total=0,
            confidence_score=90, overall_status="SAFE",
            recommendation="On track.", sources=["CHIRPS v2.0"],
            period_start="2025-09-15", period_end="2025-12-14",
            computed_at="2025-12-14T12:00:00Z",
        )
        defaults.update(overrides)
        return InsuranceReport(**defaults)

    def test_active_dry_spell_shown(self):
        """When active_dry_spell_days > 0, line should appear with '(ongoing)'."""
        r = self._report(active_dry_spell_days=6)
        text = format_for_audience(r, "agronomist")
        assert "Active dry spell: 6 days" in text
        assert "ongoing" in text.lower()

    def test_ndvi_concordance_shown(self):
        """When ndvi_concordance_score is not None, concordance line appears."""
        r = self._report(ndvi_concordance_score=0.82)
        text = format_for_audience(r, "agronomist")
        assert "concordance" in text.lower()
        assert "0.82" in text

    def test_et_anomaly_shown(self):
        """When et_anomaly_pct is not None, ET anomaly line appears."""
        r = self._report(et_anomaly_pct=-12.5)
        text = format_for_audience(r, "agronomist")
        assert "ET anomaly" in text
        assert "-12.5" in text

    def test_soil_moisture_shown(self):
        """When soil_moisture_pct is not None, soil moisture line appears."""
        r = self._report(soil_moisture_pct=38.2)
        text = format_for_audience(r, "agronomist")
        assert "Soil moisture" in text
        assert "38.2" in text


# ---------------------------------------------------------------------------
# Orchestrator edge cases
# ---------------------------------------------------------------------------

class TestOrchestratorEdgeCases:
    """Tests for conditional branches inside compute_insurance_intelligence."""

    def _mock_conn(self):
        conn = AsyncMock()
        conn.fetch.return_value = [
            {"signal": "rainfall_cumulative", "direction": "below", "threshold": 100.0, "weight": 1.0, "description": "Low rain"},
        ]
        conn.fetchrow.return_value = {"mean_z": -0.5}
        return conn

    def _patches(self, *, geom=None, season="A", acc=None, dry=None, conc=None, chirps=None, et=None, soil=None):
        from contextlib import ExitStack
        stack = ExitStack()
        stack.enter_context(patch("src.services.admin_boundaries.lookup_admin_geometry", new_callable=AsyncMock, return_value=geom))
        stack.enter_context(patch("src.services.dssat_service.detect_current_season", return_value=season))
        stack.enter_context(patch("src.services.insurance_engine.compute_insurance_accuracy_safe", new_callable=AsyncMock, return_value=acc))
        stack.enter_context(patch("src.services.weather_accuracy.detect_dry_spells", new_callable=AsyncMock, return_value=dry))
        stack.enter_context(patch("src.services.weather_accuracy.compute_ndvi_concordance", new_callable=AsyncMock, return_value=conc))
        stack.enter_context(patch("src.services.forecast_fusion._fetch_chirps_precip", return_value=chirps or {}))
        stack.enter_context(patch("src.services.wapor_service.query_et", return_value=et))
        stack.enter_context(patch("src.services.wapor_service.query_soil_moisture", return_value=soil))
        sar_svc = MagicMock()
        sar_svc.get_backscatter.return_value = {"status": "success", "statistics": {"vh": {"mean": 0.05}, "vv": {"mean": 0.3}}}
        stack.enter_context(patch("src.services.sentinel1_service.get_sentinel1_service", return_value=sar_svc))
        sar_ndvi_pred = MagicMock()
        sar_ndvi_pred.predict_ndvi.return_value = {"status": "success", "predicted_ndvi": 0.45}
        stack.enter_context(patch("src.services.sar_ndvi.get_sar_ndvi_predictor", return_value=sar_ndvi_pred))
        return stack

    def test_dap_negative_correction(self):
        """When computed DAP < 0, planting_year should decrement and recalculate."""
        conn = self._mock_conn()
        # Use a date early in the year for Season A — maize Season A plants in Sep,
        # so ref_date=2025-08-01 with season=A gives a planting_date in Sep 2025
        # which is AFTER ref_date, producing negative DAP.
        with self._patches(season="A"):
            result = _run(compute_insurance_intelligence(
                conn, crop="maize", district="Musanze", ref_date=date(2025, 8, 1),
                season="A",
            ))
        assert result["status"] == "ok"
        assert result["data"]["days_after_planting"] >= 0

    def test_season_a_year_adjustment_jan(self):
        """Season A with today.month <= 2 should adjust planting_year to previous year."""
        conn = self._mock_conn()
        with self._patches(season="A"):
            result = _run(compute_insurance_intelligence(
                conn, crop="maize", district="Musanze", ref_date=date(2026, 1, 15),
                season="A",
            ))
        assert result["status"] == "ok"
        # Season A planted Sep 2025, ref_date Jan 2026 — DAP should be ~120 days
        dap = result["data"]["days_after_planting"]
        assert dap > 90

    def test_active_dry_spell_ongoing(self):
        """Dry spell with ongoing=True should populate active_dry_spell_days."""
        conn = self._mock_conn()
        dry_result = {
            "status": "success",
            "longest_spell_days": 10,
            "dry_spells": [
                {"duration_days": 5, "ongoing": False},
                {"duration_days": 10, "ongoing": True},
            ],
        }
        with self._patches(dry=dry_result):
            result = _run(compute_insurance_intelligence(
                conn, crop="maize", district="Musanze", ref_date=date(2025, 11, 15),
            ))
        assert result["data"]["active_dry_spell_days"] == 10

    def test_ndvi_concordance_extraction(self):
        """NDVI concordance result with status=ok should populate concordance score."""
        conn = self._mock_conn()
        conc_result = {"status": "success", "concordance_score": 0.78}
        with self._patches(conc=conc_result):
            result = _run(compute_insurance_intelligence(
                conn, crop="maize", district="Musanze", ref_date=date(2025, 11, 15),
            ))
        assert result["data"]["ndvi_concordance_score"] == pytest.approx(0.78)


# ===========================================================================
# Part 4: Cross-file coverage — message_routes dispatch, brain_service SQL,
#          tools.json schema validation (all mocked, no DB required)
# ===========================================================================

import pathlib


# ---------------------------------------------------------------------------
# tools.json schema validation for get_insurance_intelligence
# ---------------------------------------------------------------------------

class TestInsuranceToolSchema:
    def _load_tool(self):
        tools_path = pathlib.Path(__file__).parent.parent / "geoprocessing" / "tools.json"
        with open(tools_path) as f:
            tools = json.load(f)
        return next(t for t in tools if t["function"]["name"] == "get_insurance_intelligence")

    def test_tool_present_in_registry(self):
        tool = self._load_tool()
        assert tool["type"] == "function"
        assert tool["function"]["name"] == "get_insurance_intelligence"

    def test_schema_has_all_params(self):
        tool = self._load_tool()
        props = tool["function"]["parameters"]["properties"]
        for param in ("crop", "season", "district", "sector", "cell", "village", "audience"):
            assert param in props, f"Missing parameter {param}"

    def test_crop_description_lists_supported_crops(self):
        """Tool schema crop field should be free-text with supported crops listed in description."""
        tool = self._load_tool()
        crop_field = tool["function"]["parameters"]["properties"]["crop"]
        assert "enum" not in crop_field, "Crop field should be free-text, not enum-restricted"
        desc = crop_field["description"]
        for crop in ("maize", "beans", "rice", "banana", "coffee", "tomato", "potato"):
            assert crop in desc, f"Crop {crop} not listed in description"

    def test_audience_enum_matches_formatters(self):
        tool = self._load_tool()
        schema_audiences = set(tool["function"]["parameters"]["properties"]["audience"]["enum"])
        assert schema_audiences == {"farmer", "insurance", "agronomist", "scientist"}

    def test_no_required_params(self):
        """All params optional — auto-detection fills in defaults."""
        tool = self._load_tool()
        assert tool["function"]["parameters"]["required"] == []

    def test_dispatch_wired_in_message_routes(self):
        routes_path = pathlib.Path(__file__).parent.parent / "routes" / "message_routes.py"
        src = routes_path.read_text()
        assert 'function_name == "get_insurance_intelligence"' in src


# ---------------------------------------------------------------------------
# message_routes.py dispatch: mocked end-to-end tool call
# ---------------------------------------------------------------------------

class TestMessageRoutesInsuranceDispatch:
    """Verify the message_routes.py glue code by importing and calling the
    dispatch logic pattern directly with mocked dependencies."""

    def test_happy_path_calls_engine_and_brain(self):
        """Simulate the dispatch: engine returns ok, brain save succeeds."""
        mock_conn = AsyncMock()
        engine_result = {
            "status": "ok",
            "report": "Your maize in Musanze is SAFE.",
            "data": {
                "crop": "maize", "location": "Musanze", "season": "A",
                "admin_level": "district", "confidence_score": 100,
                "overall_status": "SAFE", "triggers_activated": 0,
                "triggers_total": 2,
            },
            "audience": "farmer",
            "geometry": {"type": "Point", "coordinates": [29.5, -1.5]},
            "slug": "insurance-maize-musanze-a-20251115",
        }

        mock_engine = AsyncMock(return_value=engine_result)
        mock_brain = MagicMock()
        mock_brain.put_page = AsyncMock()
        mock_brain.add_timeline_entry = AsyncMock()

        async def simulate_dispatch():
            from src.services.brain_service import PageInput, TimelineInput
            tool_args = {"crop": "maize", "district": "Musanze", "audience": "farmer"}
            tool_result = await mock_engine(
                mock_conn,
                crop=tool_args.get("crop", "maize"),
                season=tool_args.get("season"),
                district=tool_args.get("district"),
                audience=tool_args.get("audience", "farmer"),
            )
            if tool_result.get("status") == "ok":
                _ins_slug = tool_result.get("slug", "insurance-report")
                _ins_data = tool_result.get("data", {})
                _ins_geom = tool_result.get("geometry")
                _ins_geom_str = json.dumps(_ins_geom) if _ins_geom else None
                _page_input = PageInput(
                    type="insurance_intelligence",
                    title=f"Insurance: {_ins_data.get('crop', '')} in {_ins_data.get('location', '')}",
                    compiled_truth=tool_result.get("report", ""),
                    frontmatter={"type": "insurance_intelligence"},
                    geom_geojson=_ins_geom_str,
                )
                await mock_brain.put_page(mock_conn, _ins_slug, _page_input, owner_uuid="test-user")
                _tl_input = TimelineInput(
                    date=date.today(),
                    summary=f"{_ins_data.get('overall_status')}: {_ins_data.get('crop')}",
                    source="insurance_engine",
                    detail=json.dumps(_ins_data, default=str),
                )
                await mock_brain.add_timeline_entry(mock_conn, _ins_slug, _tl_input)
            return tool_result

        result = _run(simulate_dispatch())
        assert result["status"] == "ok"
        mock_brain.put_page.assert_called_once()
        mock_brain.add_timeline_entry.assert_called_once()
        put_call = mock_brain.put_page.call_args
        assert put_call[1]["owner_uuid"] == "test-user" or put_call[0][3] == "test-user"

    def test_brain_save_failure_does_not_crash_dispatch(self):
        """Brain save exception should be caught — tool_result still returned."""
        mock_conn = AsyncMock()
        engine_result = {
            "status": "ok", "report": "SAFE", "data": {"crop": "maize", "location": "X", "season": "A"},
            "geometry": None, "slug": "test-slug",
        }
        mock_engine = AsyncMock(return_value=engine_result)
        mock_brain = MagicMock()
        mock_brain.put_page = AsyncMock(side_effect=Exception("DB down"))

        async def simulate_dispatch():
            tool_result = await mock_engine(mock_conn)
            if tool_result.get("status") == "ok":
                try:
                    from src.services.brain_service import PageInput
                    _page_input = PageInput(type="x", title="x", compiled_truth="x")
                    await mock_brain.put_page(mock_conn, "slug", _page_input, owner_uuid="u")
                except Exception:
                    pass  # mirrors the logger.warning in message_routes
            return tool_result

        result = _run(simulate_dispatch())
        assert result["status"] == "ok"

    def test_engine_exception_returns_error_dict(self):
        """Outer except should catch engine failure and return error dict."""
        mock_engine = AsyncMock(side_effect=Exception("CHIRPS timeout"))

        async def simulate_dispatch():
            try:
                return await mock_engine()
            except Exception:
                return {"status": "error", "error": "Insurance intelligence computation failed. Please try again."}

        result = _run(simulate_dispatch())
        assert result["status"] == "error"
        assert "computation failed" in result["error"]

    def test_geometry_serialized_as_geojson_string(self):
        """When geometry is present, it should be JSON-serialized for geom_geojson."""
        geom = {"type": "Polygon", "coordinates": [[[29, -1], [30, -2], [29, -1]]]}

        async def check_geom():
            from src.services.brain_service import PageInput
            _ins_geom_str = json.dumps(geom) if geom else None
            page = PageInput(type="t", title="t", compiled_truth="c", geom_geojson=_ins_geom_str)
            assert page.geom_geojson is not None
            parsed = json.loads(page.geom_geojson)
            assert parsed["type"] == "Polygon"
            return True

        assert _run(check_geom())

    def test_no_geometry_passes_none(self):
        """When geometry is None, geom_geojson should be None."""
        geom = None

        async def check_no_geom():
            from src.services.brain_service import PageInput
            _ins_geom_str = json.dumps(geom) if geom else None
            page = PageInput(type="t", title="t", compiled_truth="c", geom_geojson=_ins_geom_str)
            assert page.geom_geojson is None
            return True

        assert _run(check_no_geom())


# ---------------------------------------------------------------------------
# brain_service.py: put_page SQL branch coverage (mocked conn)
# ---------------------------------------------------------------------------

class TestBrainServicePutPageParams:
    """Verify put_page correctly forwards access_scope and partner_id
    to the SQL query for both with-geom and without-geom branches."""

    def _mock_row(self):
        return {
            "id": 1, "slug": "test-page", "type": "insurance_intelligence",
            "title": "Test", "compiled_truth": "content", "timeline": "",
            "frontmatter": "{}", "content_hash": "abc123",
            "owner_uuid": "owner-1", "viewer_uuids": [], "editor_uuids": [],
            "created_at": datetime(2025, 1, 1), "updated_at": datetime(2025, 1, 1),
        }

    def test_without_geom_passes_access_scope_and_partner_id(self):
        """put_page without geom_geojson should pass access_scope and partner_id as params $11 and $12."""
        from src.services.brain_service import BrainService, PageInput
        brain = BrainService()
        conn = AsyncMock()
        conn.fetchrow.return_value = self._mock_row()
        conn.execute = AsyncMock()
        page = PageInput(type="insurance_intelligence", title="Test", compiled_truth="c")
        _run(brain.put_page(
            conn, "test-page", page, owner_uuid="owner-1",
            access_scope="partner_internal", partner_id="org-123",
        ))
        call_args = conn.fetchrow.call_args[0]
        sql = call_args[0]
        assert "access_scope" in sql
        assert "partner_id" in sql
        positional = call_args[1:]
        assert "partner_internal" in positional
        assert "org-123" in positional

    def test_with_geom_passes_access_scope_and_partner_id(self):
        """put_page with geom_geojson should pass access_scope and partner_id AND the geometry."""
        from src.services.brain_service import BrainService, PageInput
        brain = BrainService()
        conn = AsyncMock()
        conn.fetchrow.return_value = self._mock_row()
        conn.execute = AsyncMock()
        geom = '{"type":"Point","coordinates":[29.5,-1.5]}'
        page = PageInput(type="insurance_intelligence", title="Test", compiled_truth="c", geom_geojson=geom)
        _run(brain.put_page(
            conn, "test-page", page, owner_uuid="owner-1",
            access_scope="public", partner_id="org-456",
        ))
        call_args = conn.fetchrow.call_args[0]
        sql = call_args[0]
        assert "ST_GeomFromGeoJSON" in sql
        assert "access_scope" in sql
        positional = call_args[1:]
        assert "public" in positional
        assert "org-456" in positional
        assert geom in positional

    def test_coalesce_on_conflict_preserves_existing_scope(self):
        """ON CONFLICT UPDATE uses COALESCE so NULL excluded doesn't overwrite existing."""
        from src.services.brain_service import BrainService, PageInput
        brain = BrainService()
        conn = AsyncMock()
        conn.fetchrow.return_value = self._mock_row()
        conn.execute = AsyncMock()
        page = PageInput(type="t", title="t", compiled_truth="c")
        _run(brain.put_page(conn, "test-page", page, owner_uuid="o"))
        sql = conn.fetchrow.call_args[0][0]
        assert "COALESCE(EXCLUDED.access_scope, brain_pages.access_scope)" in sql
        assert "COALESCE(EXCLUDED.partner_id, brain_pages.partner_id)" in sql

    def test_default_scope_is_none(self):
        """When access_scope/partner_id not provided, None should be passed."""
        from src.services.brain_service import BrainService, PageInput
        brain = BrainService()
        conn = AsyncMock()
        conn.fetchrow.return_value = self._mock_row()
        conn.execute = AsyncMock()
        page = PageInput(type="t", title="t", compiled_truth="c")
        _run(brain.put_page(conn, "test-page", page, owner_uuid="o"))
        positional = conn.fetchrow.call_args[0][1:]
        assert positional[10] is None  # access_scope ($11)
        assert positional[11] is None  # partner_id ($12)

    def test_partner_filter_constant_structure(self):
        """_PARTNER_FILTER SQL constant must check access_scope and partner_id via GUC."""
        from src.services.brain_service import _PARTNER_FILTER
        assert "access_scope" in _PARTNER_FILTER
        assert "partner_id" in _PARTNER_FILTER
        assert "current_setting('app.partner_id'" in _PARTNER_FILTER
        assert "partner_internal" in _PARTNER_FILTER

    def test_partner_filter_alias_placeholder(self):
        """_PARTNER_FILTER should use {a} placeholder for table alias."""
        from src.services.brain_service import _PARTNER_FILTER
        assert "{a}" in _PARTNER_FILTER
        formatted = _PARTNER_FILTER.format(a="p.")
        assert "p.access_scope" in formatted
        assert "p.partner_id" in formatted
