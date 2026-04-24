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
    _flatten_coords,
    _generate_recommendation,
    _get_harvest_dap,
    _get_planting_date,
    _load_triggers,
    _resolve_location_name,
    _RAINFALL_NORMALS,
    _RWANDA_CENTER,
    _ET_LONG_TERM_MEAN,
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
    def test_returns_five_triggers(self):
        triggers = _default_triggers("full_season")
        assert len(triggers) == 5

    def test_signals_present(self):
        triggers = _default_triggers("full_season")
        signals = {t["signal"] for t in triggers}
        assert "rainfall_cumulative" in signals
        assert "spi" in signals
        assert "dry_spell_days" in signals
        assert "ndvi_z_score" in signals
        assert "et_anomaly" in signals

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


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

class TestConstants:
    def test_rwanda_center_is_tuple(self):
        assert isinstance(_RWANDA_CENTER, tuple)
        assert len(_RWANDA_CENTER) == 2

    def test_et_long_term_mean_is_positive(self):
        assert _ET_LONG_TERM_MEAN > 0

    def test_rainfall_normals_has_both_seasons(self):
        assert "A" in _RAINFALL_NORMALS
        assert "B" in _RAINFALL_NORMALS
        for season in ("A", "B"):
            assert "mean" in _RAINFALL_NORMALS[season]
            assert "std" in _RAINFALL_NORMALS[season]
            assert _RAINFALL_NORMALS[season]["std"] > 0


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
        assert len(result) == 5
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
        expected = {"status": "ok", "confidence_rating": 85}
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

    def _patches(self, *, geom=None, season="A", acc=None, dry=None, conc=None, chirps=None, et=None, soil=None):
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

    def test_unknown_crop_defaults_to_maize(self):
        conn = self._mock_conn()
        with self._patches():
            result = _run(compute_insurance_intelligence(
                conn, crop="quinoa", district="Musanze", ref_date=date(2025, 11, 15),
            ))
        assert result["status"] == "ok"
        assert result["data"]["crop"] == "maize"

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
        dry_result = {"status": "ok", "longest_spell_days": 12, "dry_spells": [{"duration_days": 12, "ongoing": False}]}
        with self._patches(dry=dry_result):
            result = _run(compute_insurance_intelligence(
                conn, crop="maize", district="Musanze", ref_date=date(2025, 11, 15),
            ))
        assert result["data"]["max_dry_spell_days"] == 12

    def test_et_anomaly_computed_from_wapor(self):
        conn = self._mock_conn()
        et_result = {"status": "ok", "time_series": [{"value": 3.0}, {"value": 4.0}, {"value": 3.5}]}
        with self._patches(et=et_result):
            result = _run(compute_insurance_intelligence(
                conn, crop="maize", district="Musanze", ref_date=date(2025, 11, 15),
            ))
        assert result["data"]["et_anomaly_pct"] is not None

    def test_soil_moisture_latest_value_used(self):
        conn = self._mock_conn()
        soil_result = {"status": "ok", "time_series": [{"value": 40.0}, {"value": 35.0}, {"value": 28.0}]}
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
        acc_result = {"status": "ok", "confidence_rating": 85, "recommendation": "Safe"}
        with self._patches(acc=acc_result):
            result = _run(compute_insurance_intelligence(
                conn, crop="maize", district="Musanze", ref_date=date(2025, 11, 15),
            ))
        assert result["status"] == "ok"
        assert "report" in result


# ---------------------------------------------------------------------------
# Migration validation (parse, don't run)
# ---------------------------------------------------------------------------

class TestMigrationIntegrity:
    def test_seed_data_count(self):
        import re
        with open("alembic/versions/a1b2c3d4e5f7_insurance_triggers.py") as f:
            content = f.read()
        insert_rows = re.findall(r"\('(maize|beans|rice)',", content)
        assert len(insert_rows) == 34

    def test_all_crops_have_both_seasons(self):
        import re
        with open("alembic/versions/a1b2c3d4e5f7_insurance_triggers.py") as f:
            content = f.read()
        for crop in ("maize", "beans", "rice"):
            assert f"('{crop}', 'A'" in content
            assert f"('{crop}', 'B'" in content

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
