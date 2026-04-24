"""Tests for insurance_engine.py — pure function tests, no DB/API required.

Tests the functions that make payout decisions:
  _evaluate_triggers, _compute_confidence, _compute_spi,
  _compute_phase_rainfall, _current_growth_phase, _centroid_from_geojson,
  _flatten_coords, _default_triggers, _generate_recommendation,
  format_for_audience, InsuranceReport.to_dict, TriggerResult.to_dict
"""

import json
from datetime import date, timedelta

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
    _flatten_coords,
    _generate_recommendation,
    _RAINFALL_NORMALS,
    _RWANDA_CENTER,
    _ET_LONG_TERM_MEAN,
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
