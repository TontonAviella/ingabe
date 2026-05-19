"""Deterministic typed-edge inference for brain_links.

Port of GBrain's `src/core/link-extraction.ts:inferLinkType` (NousResearch/
garrytan/gbrain) adapted for mundi.ai's GIS+agriculture domain. Pure
function, no DB, no LLM. Same input → same output forever.

Why this module exists
----------------------
Before this lands, `BrainService.put_page` writes every auto-extracted
link as `link_type='auto', context=''` — a meaningless string. The
column is there. `traverse_graph` returns it. The graph traversal
payoff GBrain documents (~+31 P@5 on relational queries vs flat RAG)
is paid for on every write and never collected.

This module is the inference layer that turns `'auto'` into one of the
typed edges defined in EDGE_TABLE. `BrainService.put_page` calls
`infer_link_type(source_type, target_type, link_context, page_content)`
once per extracted link and stores the result.

Two-pass design
---------------
Pass 1 (synchronous, no DB): structural inference based on (source_type,
target_type) plus a regex over the link context window. Most edges land
here.

Pass 2 (after the page is committed, single batch SQL): geometric
refinement via PostGIS `ST_Contains`. A `field` linked to a `district`
is `field_in_district` iff the field's geometry is actually contained
by the district's geometry. This is the 150% over GBrain — they don't
have geometry, so they infer containment via text. We can check it.
See `geometric_refinement_sql()`.

Determinism
-----------
Same (source_type, target_type, link_context, page_content) always
returns the same edge type. Regexes are compiled once at module load.
No clock-dependent behavior. Backfill is safe to re-run.

Vocabulary
----------
EDGE_TABLE encodes the (source_type, target_type) → edge_type matrix
that's structurally fixed regardless of text. Anything not in the table
falls through to regex inference, then to a `mentions_{target_type}`
fallback. The full table is documented at the top of the file so
reviewers can audit it without grepping.
"""
from __future__ import annotations

import re
from typing import Optional

# ---------------------------------------------------------------------------
# Edge vocabulary
# ---------------------------------------------------------------------------

# Structural edges that are FIXED by the (source_type, target_type) pair.
# No regex needed. These get applied first and short-circuit the heuristic
# layer. The order of entries does not matter; lookup is O(1).
EDGE_TABLE: dict[tuple[str, str], str] = {
    # Field relationships
    ("field", "district"): "field_in_district",
    ("field", "farmer"): "field_owned_by",
    ("field", "policy"): "field_under_policy",
    ("field", "weather_station"): "field_near_station",
    # Farmer relationships
    ("farmer", "district"): "resides_in_district",
    ("farmer", "company"): "farmer_at_company",
    # Insurance domain
    ("claim", "field"): "claim_on_field",
    ("claim", "policy"): "claim_under_policy",
    ("claim", "farmer"): "claim_by",
    ("claim", "insurance_worker"): "claim_handled_by",
    ("policy", "farmer"): "policyholder",
    ("policy", "field"): "policy_covers_field",
    ("policy", "company"): "policy_issued_by",
    ("policy", "crop"): "policy_covers_crop",
    # Infrastructure
    ("weather_station", "district"): "station_in_district",
    ("equipment", "farmer"): "equipment_owned_by",
    ("equipment", "field"): "equipment_used_on",
    # Insurance intelligence reports
    ("insurance_intelligence", "field"): "intelligence_for_field",
    ("insurance_intelligence", "farmer"): "intelligence_for_farmer",
    ("insurance_intelligence", "policy"): "intelligence_for_policy",
}

# Context-dependent inference. When (source_type, target_type) is NOT in
# EDGE_TABLE, fall through to these regex checks in precedence order. The
# regex matches the ~200-char window around the link in compiled_truth.
# Mirrors GBrain's precedence: explicit verb > role inference > fallback.

_CROP_PLANTED_RE = re.compile(
    r"\b(planted|growing|cultivated|sown|cropped with|crop is|sowed)\b",
    re.IGNORECASE,
)
_SEASON_ACTIVE_RE = re.compile(
    r"\b(season|Season\s*[ABC]|S[ABC]\d{4}|kicker|harvest|planting)\b",
    re.IGNORECASE,
)
_FARMER_GROWS_RE = re.compile(
    r"\b(grows|plants|cultivates|specia(?:l|li)?ses in|harvests)\b",
    re.IGNORECASE,
)
_VERDICT_RE = re.compile(
    r"\b(verdict|trigger(?:ed)?|stress|drought|flood|anomaly|risk|alert)\b",
    re.IGNORECASE,
)
_OWNED_BY_RE = re.compile(
    r"\b(owned by|belongs to|tilled by|operated by|tenant)\b",
    re.IGNORECASE,
)
_ISSUED_BY_RE = re.compile(
    r"\b(insurer|underwriter|issued by|underwritten by|policy from)\b",
    re.IGNORECASE,
)

# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def infer_link_type(
    source_type: str,
    target_type: str,
    link_context: str = "",
    page_content: str = "",
) -> str:
    """Infer link_type from a brain page write.

    Pure function. Deterministic. No DB access. Same inputs always yield
    the same output.

    Args:
        source_type: page.type of the source (e.g. "field", "claim").
        target_type: page.type of the target (e.g. "district", "policy").
            Caller must look this up before invoking; the inference is
            type-aware. If unknown, pass "" — function falls through to
            a generic `mentions` edge.
        link_context: ~200-char window around the link in compiled_truth.
            Used by the regex layer when EDGE_TABLE doesn't decide.
            Empty string is safe.
        page_content: the full compiled_truth of the source page. Used as
            global-context fallback for the regex layer when per-edge
            context is too narrow (mirrors GBrain's globalContext arg).

    Returns:
        One of the typed edges in EDGE_TABLE, a typed edge from the
        regex precedence layer, or `mentions_{target_type}` as the
        catch-all fallback. Never returns the legacy `'auto'`.

    Examples:
        >>> infer_link_type("field", "district", "", "")
        'field_in_district'
        >>> infer_link_type("field", "crop", "planted with maize this season", "")
        'crop_planted_in'
        >>> infer_link_type("concept", "concept", "", "")
        'mentions_concept'
    """
    # Pass 1: structural EDGE_TABLE lookup. O(1), no text scanning.
    if (source_type, target_type) in EDGE_TABLE:
        return EDGE_TABLE[(source_type, target_type)]

    # Pass 2: context regex inference for the type-pairs that need it.
    # Precedence within each pair: most specific verb wins.
    context = link_context or ""
    full = page_content or ""

    if source_type == "field" and target_type == "crop":
        if _CROP_PLANTED_RE.search(context) or _CROP_PLANTED_RE.search(full):
            return "crop_planted_in"
        return "mentions_crop"

    if source_type == "field" and target_type == "season":
        if _SEASON_ACTIVE_RE.search(context) or _SEASON_ACTIVE_RE.search(full):
            return "active_in_season"
        return "mentions_season"

    if source_type == "farmer" and target_type == "crop":
        if _FARMER_GROWS_RE.search(context) or _FARMER_GROWS_RE.search(full):
            return "farmer_grows"
        return "mentions_crop"

    if source_type == "insurance_intelligence" and target_type == "crop":
        if _VERDICT_RE.search(context) or _VERDICT_RE.search(full):
            return "verdict_about_crop"
        return "mentions_crop"

    if source_type == "insurance_intelligence" and target_type == "season":
        return "intelligence_in_season"

    # Field ownership when EDGE_TABLE didn't catch it (e.g. an `entity` type)
    if target_type == "farmer" and _OWNED_BY_RE.search(context):
        return "owned_by_farmer"

    # Policy/company issuer fallback when target type wasn't classified
    if source_type == "policy" and _ISSUED_BY_RE.search(context):
        return "policy_issued_by"

    # Source citations are always 'cites_source'
    if target_type == "source":
        return "cites_source"

    # Person + concept fallbacks — informational, not structural
    if target_type == "person":
        return "mentions_person"
    if target_type == "concept":
        return "mentions_concept"

    # Final catch-all: name the kind of mention so downstream filters
    # can include or exclude it. Empty target_type is rare but possible
    # if the caller hits a deleted target.
    return f"mentions_{target_type}" if target_type else "mentions"


# ---------------------------------------------------------------------------
# Geometric refinement (Pass 2) — the 150% over GBrain
# ---------------------------------------------------------------------------

# Type pairs where a geometric containment check is meaningful. The query
# below promotes regex-inferred or auto edges to geometry-verified edges
# when both pages have non-null `geom`. Pairs not listed are skipped at
# query time via the source.type / target.type predicate.
GEOMETRIC_PAIRS: dict[tuple[str, str], str] = {
    ("field", "district"): "field_in_district",
    ("farmer", "district"): "resides_in_district",
    ("weather_station", "district"): "station_in_district",
}


def geometric_refinement_sql() -> str:
    """SQL for Pass 2: promote regex-inferred edges to geometry-verified
    edges via PostGIS `ST_Contains`. Idempotent — re-running produces the
    same result. Caller binds `$1 = from_page_id` after a page write.

    Why this is separated from `infer_link_type`: the regex pass runs
    inside `put_page` with no DB access (pure function, easy to test).
    The geometric pass needs PostGIS, runs once per page write as a
    single batch SQL, and is reversible (revert the UPDATE if needed).

    Strategy:
        For each link where (source.type, target.type) is in
        GEOMETRIC_PAIRS AND both pages have a non-null geom column AND
        ST_Contains(target.geom, source.geom) is true, set link_type to
        the geometry-verified value. Otherwise leave the regex-inferred
        type alone.

    Returns the SQL as a string. Caller is responsible for execute()
    and bind variables. Keeping it as a function instead of a constant
    so test_brain_edge_inference.py can assert the SQL shape stays
    in sync with GEOMETRIC_PAIRS.
    """
    # Build a VALUES clause from GEOMETRIC_PAIRS so the SQL is explicit
    # about which (source.type, target.type) pairs to consider.
    pairs = ", ".join(
        f"('{src}', '{tgt}', '{edge}')"
        for (src, tgt), edge in sorted(GEOMETRIC_PAIRS.items())
    )
    return f"""
        UPDATE brain_links bl
        SET link_type = gp.edge
        FROM brain_pages s, brain_pages t,
             (VALUES {pairs}) AS gp(src_type, tgt_type, edge)
        WHERE bl.from_page_id = $1
          AND s.id = bl.from_page_id
          AND t.id = bl.to_page_id
          AND s.type = gp.src_type
          AND t.type = gp.tgt_type
          AND s.geom IS NOT NULL
          AND t.geom IS NOT NULL
          AND ST_Contains(t.geom, s.geom)
          AND bl.link_type IS DISTINCT FROM gp.edge
    """


# ---------------------------------------------------------------------------
# Context-window extraction helper (Pass 1 supports this)
# ---------------------------------------------------------------------------


def context_window(
    text: str, match_start: int, match_end: int, window: int = 100
) -> str:
    """Extract a context window around a regex match.

    Used by `_extract_link_targets` after matching a wikilink/markdown
    link to give `infer_link_type` enough surrounding text to fire the
    regex layer.

    Args:
        text: full source text (compiled_truth).
        match_start: regex match start offset.
        match_end: regex match end offset.
        window: chars to take on each side of the match (default 100).

    Returns:
        A substring of `text` with up to `window` chars on each side of
        the match. Total length is at most `2*window + (match_end -
        match_start)`.
    """
    if not text:
        return ""
    start = max(0, match_start - window)
    end = min(len(text), match_end + window)
    return text[start:end]
