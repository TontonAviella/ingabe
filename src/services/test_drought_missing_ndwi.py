"""A missing NDWI is unknown, never 0 (CODING_STANDARDS: missing is not zero, and zero is not missing).

Until 2026-10-24 detect_drought filled a week without NDWI with 0.0: a missing latest NDWI was reported (and
stored in drought_cache.latest_ndwi, which Sage's drought tool reads) as 0.0, drought periods listed ndwi 0.0 for
weeks nobody measured, and a district whose NDWI really was 0.0 every week was reported as having no NDWI.
"""

from __future__ import annotations

from datetime import date

from src.services.ml_inference import CropClassifier
from src.services.test_drought_seasonal_vci import _seasonal, _weekly

DROUGHT_2026 = {2023: 0.03, 2024: 0.0, 2025: 0.02, 2026: -0.12}  # 2026 far below earlier years: severe
START, END = date(2023, 10, 2), date(2026, 9, 28)
LAST_WEEKS = date(2026, 8, 1)


def _drought(ndwi):
    return CropClassifier().detect_drought(_weekly(START, END, lambda d: _seasonal(d, DROUGHT_2026), ndwi))


def test_a_missing_latest_ndwi_is_reported_unknown_not_zero():
    result = _drought(lambda d: None if d >= LAST_WEEKS else -0.05)
    assert result["drought_status"] == "severe_drought"
    assert result["latest_ndwi"] is None


def test_drought_periods_leave_unmeasured_weeks_unknown():
    result = _drought(lambda d: None if d >= LAST_WEEKS else -0.05)
    unmeasured = [p for p in result["drought_periods"] if date.fromisoformat(p["date"]) >= LAST_WEEKS]
    measured = [p for p in result["drought_periods"] if date.fromisoformat(p["date"]) < LAST_WEEKS]
    assert unmeasured and measured
    assert all(p["ndwi"] is None for p in unmeasured)
    assert all(p["ndwi"] == -0.05 for p in measured)


def test_an_ndwi_of_exactly_zero_is_a_value():
    result = _drought(lambda d: 0.0)
    assert result["latest_ndwi"] == 0.0
    assert all(p["ndwi"] == 0.0 for p in result["drought_periods"])


def test_water_stress_needs_a_measured_negative_ndwi():
    """Moderate (VCI 20-35) needs water stress; an unmeasured week can only be mild, a dry measured one moderate."""
    mild_offsets = {2023: 0.03, 2024: 0.0, 2025: 0.02, 2026: -0.03}  # 8 weeks with VCI 20-35
    rows = lambda ndwi: _weekly(START, END, lambda d: _seasonal(d, mild_offsets), ndwi)  # noqa: E731
    unknown = CropClassifier().detect_drought(rows(lambda d: None))
    dry = CropClassifier().detect_drought(rows(lambda d: -0.05))
    between = lambda r: [p for p in r["drought_periods"] if 20 <= p["vci"] < 35]  # noqa: E731
    assert between(unknown) and all(p["severity"] == "mild" for p in between(unknown))
    assert between(dry) and all(p["severity"] == "moderate" for p in between(dry))
