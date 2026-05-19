"""Unit tests for brain_edge_inference.

Pure-function tests, no DB, no async. Cover:
  - Every (source_type, target_type) pair in EDGE_TABLE returns the
    declared edge.
  - The 6 regex layers fire when their verbs appear in context, and
    fall back cleanly when they don't.
  - mentions_{target_type} catch-all and empty-target-type behavior.
  - Determinism: same inputs always yield same output.
  - context_window helper trims correctly.
  - geometric_refinement_sql() stays in sync with GEOMETRIC_PAIRS.
"""
from __future__ import annotations

import pytest

from src.services.brain_edge_inference import (
    EDGE_TABLE,
    GEOMETRIC_PAIRS,
    context_window,
    geometric_refinement_sql,
    infer_link_type,
)


# ---------------------------------------------------------------------------
# Structural EDGE_TABLE — every pair must return the exact declared edge
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "src_type,tgt_type,expected",
    [(src, tgt, edge) for (src, tgt), edge in sorted(EDGE_TABLE.items())],
)
def test_edge_table_pair_returns_declared_type(src_type, tgt_type, expected):
    """Every declared structural pair returns the table value, no context needed."""
    # EDGE_TABLE entries fire BEFORE the regex layer; empty context proves it.
    result = infer_link_type(src_type, tgt_type)
    assert result == expected, (
        f"({src_type}, {tgt_type}) should be {expected!r} but got {result!r}"
    )


def test_edge_table_pair_ignores_misleading_context():
    """When the structural pair is in EDGE_TABLE, context regexes do not fire."""
    # "claim → field" is structurally `claim_on_field`. The "planted" verb
    # would otherwise hit _CROP_PLANTED_RE if we naively fell through.
    result = infer_link_type(
        "claim", "field",
        link_context="planted with maize in season B2025",
        page_content="this is a damage claim covering planted area",
    )
    assert result == "claim_on_field"


# ---------------------------------------------------------------------------
# Regex layer — context-dependent inference
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "context,expected",
    [
        ("planted with maize this season", "crop_planted_in"),
        ("growing maize on 12 hectares", "crop_planted_in"),
        ("the crop is mostly cassava", "crop_planted_in"),
        ("recently sown with beans", "crop_planted_in"),
        ("cultivated under irrigation", "crop_planted_in"),
        # Falls through when no verb
        ("see also: maize varieties", "mentions_crop"),
        ("notes about crop disease", "mentions_crop"),
    ],
)
def test_field_to_crop_regex(context, expected):
    assert infer_link_type("field", "crop", link_context=context) == expected


@pytest.mark.parametrize(
    "context,expected",
    [
        ("active during Season B 2025", "active_in_season"),
        ("harvest expected late SA2026", "active_in_season"),
        ("planting begins in Season A", "active_in_season"),
        # No verb → fallback
        ("see also: seasonal patterns", "mentions_season"),
    ],
)
def test_field_to_season_regex(context, expected):
    assert infer_link_type("field", "season", link_context=context) == expected


def test_field_to_crop_uses_page_content_when_link_context_empty():
    """Fallback to globalContext (page_content) when link context is short."""
    result = infer_link_type(
        "field", "crop",
        link_context="",
        page_content="This field has been planted with maize for the past 3 seasons.",
    )
    assert result == "crop_planted_in"


def test_farmer_grows_regex():
    assert infer_link_type(
        "farmer", "crop", link_context="specialises in coffee"
    ) == "farmer_grows"
    assert infer_link_type(
        "farmer", "crop", link_context="see also crops list"
    ) == "mentions_crop"


def test_insurance_intelligence_verdict_regex():
    assert infer_link_type(
        "insurance_intelligence", "crop",
        link_context="drought trigger activated for maize",
    ) == "verdict_about_crop"
    assert infer_link_type(
        "insurance_intelligence", "crop",
        link_context="see crop list for context",
    ) == "mentions_crop"


def test_insurance_intelligence_to_season_always_structural():
    """No verb needed; intelligence reports are always tied to a season."""
    assert infer_link_type(
        "insurance_intelligence", "season", link_context=""
    ) == "intelligence_in_season"


# ---------------------------------------------------------------------------
# Fallbacks for unclassified target types
# ---------------------------------------------------------------------------


def test_mentions_person_fallback():
    assert infer_link_type("field", "person") == "mentions_person"
    assert infer_link_type("concept", "person") == "mentions_person"


def test_mentions_concept_fallback():
    assert infer_link_type("field", "concept") == "mentions_concept"


def test_cites_source_always_fires():
    """Any → source is structural, no context required."""
    assert infer_link_type("field", "source") == "cites_source"
    assert infer_link_type("claim", "source") == "cites_source"
    assert infer_link_type("", "source") == "cites_source"


def test_unknown_target_type_falls_to_named_mentions():
    """Unknown target type produces `mentions_<type>` so filters work."""
    assert infer_link_type("field", "mystery_type") == "mentions_mystery_type"


def test_empty_target_type_falls_to_generic_mentions():
    """When the target page is missing/deleted, target_type is empty."""
    assert infer_link_type("field", "") == "mentions"


# ---------------------------------------------------------------------------
# Owned-by verb (catches non-EDGE_TABLE source types)
# ---------------------------------------------------------------------------


def test_owned_by_farmer_when_not_in_edge_table():
    """`equipment → farmer` IS in EDGE_TABLE, so this needs a non-table source."""
    # PAGE_TYPES doesn't include 'parcel' — but if a future type appears,
    # the verb fallback should still classify ownership correctly.
    result = infer_link_type(
        "parcel", "farmer", link_context="owned by Jean Bosco",
    )
    assert result == "owned_by_farmer"


# ---------------------------------------------------------------------------
# Determinism — same inputs always yield same output
# ---------------------------------------------------------------------------


def test_determinism_repeated_calls():
    args = ("field", "crop", "planted with maize", "")
    results = {infer_link_type(*args) for _ in range(50)}
    assert results == {"crop_planted_in"}


def test_no_legacy_auto_returned():
    """The legacy `'auto'` string must never come out of the inference layer.

    Spot-check the common cases. The structural EDGE_TABLE pairs are
    already covered by the parametrized test above; this is the
    catch-all check that no codepath leaks `'auto'`.
    """
    samples = [
        ("field", "district"),
        ("field", "crop"),
        ("field", "season"),
        ("claim", "field"),
        ("policy", "company"),
        ("farmer", "district"),
        ("concept", "concept"),
        ("source", ""),
        ("", "field"),
    ]
    for src, tgt in samples:
        assert infer_link_type(src, tgt) != "auto"


# ---------------------------------------------------------------------------
# context_window helper
# ---------------------------------------------------------------------------


def test_context_window_centered():
    text = "x" * 50 + "[[link]]" + "y" * 50
    # Match the "[[link]]" portion
    start = 50
    end = 58
    win = context_window(text, start, end, window=20)
    # 20 chars of x's + the link + 20 chars of y's
    assert "[[link]]" in win
    assert win.count("x") == 20
    assert win.count("y") == 20


def test_context_window_clamps_at_boundaries():
    text = "abc[[link]]xyz"
    win = context_window(text, 3, 11, window=100)
    # Boundary-clamp means we return the whole string
    assert win == text


def test_context_window_empty_text():
    assert context_window("", 0, 0) == ""


# ---------------------------------------------------------------------------
# Geometric refinement SQL — must stay in sync with GEOMETRIC_PAIRS
# ---------------------------------------------------------------------------


def test_geometric_sql_contains_every_declared_pair():
    sql = geometric_refinement_sql()
    for (src, tgt), edge in GEOMETRIC_PAIRS.items():
        assert f"'{src}'" in sql, f"missing source type {src} in geometric SQL"
        assert f"'{tgt}'" in sql, f"missing target type {tgt} in geometric SQL"
        assert f"'{edge}'" in sql, f"missing edge {edge} in geometric SQL"


def test_geometric_sql_uses_st_contains():
    """PostGIS containment check is what makes this 'verified' instead of guessed."""
    assert "ST_Contains" in geometric_refinement_sql()


def test_geometric_sql_guards_against_null_geom():
    sql = geometric_refinement_sql()
    assert "s.geom IS NOT NULL" in sql
    assert "t.geom IS NOT NULL" in sql


def test_geometric_sql_is_idempotent_via_distinct_from():
    """Re-running should be a no-op when the edge already matches."""
    assert "IS DISTINCT FROM" in geometric_refinement_sql()


# ---------------------------------------------------------------------------
# Coverage assertion: every EDGE_TABLE row has a downstream consumer
# ---------------------------------------------------------------------------


def test_edge_table_no_legacy_auto():
    for edge in EDGE_TABLE.values():
        assert edge != "auto", "EDGE_TABLE leaks legacy 'auto' for an entry"


def test_geometric_pairs_subset_of_edge_table():
    """Geometric refinement promotes edges that EDGE_TABLE already declares.

    Loose coupling: a geometry-verified `field_in_district` must match
    the value EDGE_TABLE returns when the regex layer is skipped, so the
    refinement is idempotent — re-running can't churn the edge label.
    """
    for (src, tgt), edge in GEOMETRIC_PAIRS.items():
        assert (src, tgt) in EDGE_TABLE, (
            f"GEOMETRIC_PAIRS ({src}, {tgt}) → {edge} must also be in EDGE_TABLE "
            f"so the structural and geometric passes agree."
        )
        assert EDGE_TABLE[(src, tgt)] == edge, (
            f"EDGE_TABLE and GEOMETRIC_PAIRS disagree on ({src}, {tgt}): "
            f"{EDGE_TABLE[(src, tgt)]!r} vs {edge!r}"
        )
