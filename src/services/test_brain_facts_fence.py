"""Unit tests for brain_facts_fence.

Pure-function tests, no DB. Covers:
  - Fence detection (present, absent, wrong heading level)
  - Multi-key per line, single key per line
  - validFrom / validUntil / supersededBy modifier extraction
  - Strikethrough → forgotten status
  - Numeric value sniff + unit suffix
  - Skipped-line counter for malformed input
  - REGRESSION_THRESHOLDS table + flag_regressions correctness
"""
from __future__ import annotations

from datetime import datetime

from src.services.brain_facts_fence import (
    REGRESSION_THRESHOLDS,
    flag_regressions,
    parse_facts_fence,
)


# ---------------------------------------------------------------------------
# Fence detection
# ---------------------------------------------------------------------------


def test_no_fence_returns_empty():
    facts, skipped = parse_facts_fence("just narrative text, no fence")
    assert facts == []
    assert skipped == 0


def test_empty_text():
    assert parse_facts_fence("") == ([], 0)


def test_single_key_value():
    text = "## Facts\nndvi=0.65\n"
    facts, skipped = parse_facts_fence(text)
    assert len(facts) == 1
    assert facts[0].key == "ndvi"
    assert facts[0].value == "0.65"
    assert facts[0].value_numeric == 0.65
    assert facts[0].status == "active"
    assert skipped == 0


def test_multi_key_on_one_line():
    text = "## Facts\nndvi=0.65 soil_moisture=0.32 crop=maize\n"
    facts, _ = parse_facts_fence(text)
    assert len(facts) == 3
    keys = {f.key for f in facts}
    assert keys == {"ndvi", "soil_moisture", "crop"}


def test_fence_terminates_at_next_heading():
    text = "## Facts\nndvi=0.65\n## Timeline\nsome other section with key=value here\n"
    facts, _ = parse_facts_fence(text)
    assert len(facts) == 1
    assert facts[0].key == "ndvi"


def test_fence_case_insensitive():
    text = "## facts\nndvi=0.5\n"
    assert len(parse_facts_fence(text)[0]) == 1


# ---------------------------------------------------------------------------
# Modifier tokens
# ---------------------------------------------------------------------------


def test_valid_from_token():
    text = "## Facts\nndvi=0.65 validFrom:2026-05-15\n"
    facts, _ = parse_facts_fence(text)
    assert facts[0].valid_from == datetime(2026, 5, 15)


def test_valid_until_token():
    text = "## Facts\ncrop=maize validFrom:2025-09-01 validUntil:2026-02-01\n"
    facts, _ = parse_facts_fence(text)
    assert facts[0].valid_from == datetime(2025, 9, 1)
    assert facts[0].valid_until == datetime(2026, 2, 1)


def test_superseded_by():
    text = "## Facts\nndvi=0.41 supersededBy:next_observation\n"
    facts, _ = parse_facts_fence(text)
    assert facts[0].status == "superseded"
    assert facts[0].superseded_by_key == "next_observation"


def test_default_valid_from_when_no_explicit():
    fallback = datetime(2026, 4, 28)
    text = "## Facts\nndvi=0.5\n"
    facts, _ = parse_facts_fence(text, default_valid_from=fallback)
    assert facts[0].valid_from == fallback


def test_explicit_valid_from_beats_default():
    fallback = datetime(2026, 4, 28)
    text = "## Facts\nndvi=0.5 validFrom:2026-05-15\n"
    facts, _ = parse_facts_fence(text, default_valid_from=fallback)
    assert facts[0].valid_from == datetime(2026, 5, 15)


def test_malformed_date_falls_through():
    """Malformed validFrom string is ignored — default_valid_from wins."""
    text = "## Facts\nndvi=0.5 validFrom:not-a-date\n"
    facts, _ = parse_facts_fence(text, default_valid_from=datetime(2026, 1, 1))
    assert facts[0].valid_from == datetime(2026, 1, 1)


# ---------------------------------------------------------------------------
# Strikethrough → forgotten
# ---------------------------------------------------------------------------


def test_strikethrough_marks_forgotten():
    text = "## Facts\n~~crop=cassava~~ forgotten\n"
    facts, _ = parse_facts_fence(text)
    assert facts[0].key == "crop"
    assert facts[0].value == "cassava"
    assert facts[0].status == "forgotten"


def test_strikethrough_without_explicit_flag():
    text = "## Facts\n~~ndvi=0.41~~\n"
    facts, _ = parse_facts_fence(text)
    assert facts[0].status == "forgotten"


def test_forgotten_keyword_only():
    """`forgotten` keyword alone (no strike) also marks status."""
    text = "## Facts\nndvi=0.41 forgotten\n"
    facts, _ = parse_facts_fence(text)
    assert facts[0].status == "forgotten"


# ---------------------------------------------------------------------------
# Numeric value sniff + unit suffix
# ---------------------------------------------------------------------------


def test_integer_value():
    text = "## Facts\nyield_kg_per_ha=2400\n"
    facts, _ = parse_facts_fence(text)
    assert facts[0].value_numeric == 2400.0
    assert facts[0].unit is None


def test_float_value():
    text = "## Facts\nndvi=0.6532\n"
    facts, _ = parse_facts_fence(text)
    assert facts[0].value_numeric == 0.6532


def test_negative_value():
    text = "## Facts\ntemp_c=-3.5\n"
    facts, _ = parse_facts_fence(text)
    assert facts[0].value_numeric == -3.5


def test_scientific_notation():
    text = "## Facts\narea_m2=1.2e4\n"
    facts, _ = parse_facts_fence(text)
    assert facts[0].value_numeric == 12000.0


def test_unit_suffix():
    text = "## Facts\narea_ha=12.4ha rainfall_mm=85.2mm\n"
    facts, _ = parse_facts_fence(text)
    area = next(f for f in facts if f.key == "area_ha")
    rain = next(f for f in facts if f.key == "rainfall_mm")
    assert area.value_numeric == 12.4
    assert area.unit == "ha"
    assert rain.value_numeric == 85.2
    assert rain.unit == "mm"


def test_non_numeric_value():
    """Categorical values (crop=maize) have value_numeric=None."""
    text = "## Facts\ncrop=maize\n"
    facts, _ = parse_facts_fence(text)
    assert facts[0].value == "maize"
    assert facts[0].value_numeric is None


# ---------------------------------------------------------------------------
# Determinism
# ---------------------------------------------------------------------------


def test_determinism():
    text = "## Facts\nndvi=0.65 soil_moisture=0.32\ncrop=maize\n"
    results = []
    for _ in range(20):
        facts, _ = parse_facts_fence(text)
        results.append(tuple((f.key, f.value, f.status) for f in facts))
    assert len(set(results)) == 1


# ---------------------------------------------------------------------------
# Skipped-line counter
# ---------------------------------------------------------------------------


def test_blank_lines_not_counted_as_skipped():
    text = "## Facts\n\nndvi=0.5\n\n"
    _facts, skipped = parse_facts_fence(text)
    assert skipped == 0


def test_comment_lines_not_counted():
    text = "## Facts\n# this is a comment\nndvi=0.5\n"
    facts, skipped = parse_facts_fence(text)
    assert skipped == 0
    assert len(facts) == 1


def test_malformed_line_counted_as_skipped():
    text = "## Facts\nthis line has no kv pair\nndvi=0.5\n"
    facts, skipped = parse_facts_fence(text)
    assert skipped == 1
    assert len(facts) == 1


# ---------------------------------------------------------------------------
# REGRESSION_THRESHOLDS table — flag_regressions
# ---------------------------------------------------------------------------


def test_regression_thresholds_table_keys():
    """The thresholds named match the GIS+agriculture domain claims."""
    for k in ("ndvi", "ndwi", "nbr", "soil_moisture", "et", "yield_kg_per_ha"):
        assert k in REGRESSION_THRESHOLDS, f"missing key {k}"


def test_flag_regressions_drop_flagged():
    """NDVI drop > 0.10 between consecutive entries is flagged."""
    trajectory = [
        {"value_numeric": 0.65, "value": "0.65"},
        {"value_numeric": 0.50, "value": "0.50"},  # drop 0.15 — flagged
        {"value_numeric": 0.48, "value": "0.48"},  # drop 0.02 — no
    ]
    out = flag_regressions(trajectory, key="ndvi")
    assert out[0]["regression_flag"] is False  # first entry never flagged
    assert out[1]["regression_flag"] is True
    assert out[2]["regression_flag"] is False


def test_flag_regressions_no_threshold_returns_false_everywhere():
    """Unknown key — every entry gets regression_flag=False but the trajectory walks."""
    trajectory = [
        {"value_numeric": 10, "value": "10"},
        {"value_numeric": 5, "value": "5"},
    ]
    out = flag_regressions(trajectory, key="unknown_key")
    assert all(e["regression_flag"] is False for e in out)


def test_flag_regressions_anomaly_score_is_inverted():
    """Anomaly score INCREASE = regression (threshold is negative in table)."""
    trajectory = [
        {"value_numeric": 0.1, "value": "0.1"},
        {"value_numeric": 1.5, "value": "1.5"},  # increase 1.4 — flagged
        {"value_numeric": 1.4, "value": "1.4"},  # decrease — no
    ]
    out = flag_regressions(trajectory, key="anomaly_score")
    assert out[1]["regression_flag"] is True
    assert out[2]["regression_flag"] is False


def test_flag_regressions_empty_trajectory():
    assert flag_regressions([], key="ndvi") == []


def test_flag_regressions_does_not_mutate_input():
    original = [{"value_numeric": 0.5, "value": "0.5"}]
    flag_regressions(original, key="ndvi")
    assert "regression_flag" not in original[0]


def test_flag_regressions_handles_missing_numeric():
    """Categorical values (no value_numeric) → no flag, no crash."""
    trajectory = [
        {"value_numeric": None, "value": "maize"},
        {"value_numeric": None, "value": "beans"},
    ]
    out = flag_regressions(trajectory, key="crop")
    assert all(e["regression_flag"] is False for e in out)


# ---------------------------------------------------------------------------
# Integration-shaped: realistic agriculture page
# ---------------------------------------------------------------------------


def test_realistic_field_page():
    text = """\
# Cyampirita Field

A 12-hectare maize plot in Huye District. Owned by [[farmers/jean-bosco]].
Insured under [[policies/bk-2025-q2]].

## Facts
area_ha=12.4 crop=maize season=B2025 validFrom:2025-09-01
ndvi=0.62 validFrom:2025-10-15
soil_moisture=0.34 validFrom:2025-10-15
ndvi=0.58 validFrom:2025-11-01
ndvi=0.41 validFrom:2025-11-29
~~crop=cassava~~ forgotten

## Timeline
2025-09-01: planted
"""
    facts, skipped = parse_facts_fence(text)
    assert skipped == 0
    # Count claims by key
    by_key: dict[str, int] = {}
    for f in facts:
        by_key[f.key] = by_key.get(f.key, 0) + 1
    assert by_key["ndvi"] == 3
    assert by_key["soil_moisture"] == 1
    assert by_key["area_ha"] == 1
    assert by_key["crop"] == 2  # one active maize, one forgotten cassava
    forgotten = [f for f in facts if f.status == "forgotten"]
    assert len(forgotten) == 1
    assert forgotten[0].key == "crop"
    assert forgotten[0].value == "cassava"
