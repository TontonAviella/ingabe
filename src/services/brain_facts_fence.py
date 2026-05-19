"""Parser for the `## Facts` fence convention.

Adapted from GBrain's `src/core/facts/extract-from-fence.ts`. Pure
function, no DB, no LLM. Same input → same output.

The convention: brain pages can carry typed claims in a fenced section
named exactly `## Facts`. One claim per line. Two shapes accepted:

    key=value                            (the common case)
    key=value validFrom:YYYY-MM-DD       (timestamped explicitly)
    key=value validUntil:YYYY-MM-DD      (closed-interval claim)
    ~~key=value~~ forgotten              (strikethrough = retracted)
    key=value supersededBy:other_key     (chain reference)

Multiple `key=value` on the same line are allowed, space-separated.
Lines that don't parse are skipped (with a count surfaced to the
caller so they can warn — never raised).

For mundi.ai's GIS+agriculture context, the canonical keys are:

    ndvi, ndwi, nbr           — spectral indices
    soil_moisture, et         — water + evapotranspiration
    anomaly_score, yield      — derived metrics
    crop, season              — categorical identity
    area_ha, perimeter_m      — geometric facts (also from PostGIS)

The parser is deliberately schema-loose: any key=value pair is accepted.
The vocabulary above is what trajectory tooling and regression flags
care about; everything else just sits in the table for ad-hoc queries.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Optional


# ---------------------------------------------------------------------------
# Public dataclass
# ---------------------------------------------------------------------------


@dataclass
class ParsedFact:
    key: str
    value: str
    value_numeric: Optional[float] = None
    unit: Optional[str] = None
    valid_from: Optional[datetime] = None
    valid_until: Optional[datetime] = None
    status: str = "active"  # 'active' | 'superseded' | 'forgotten'
    superseded_by_key: Optional[str] = None
    source: str = "fence:reconcile"
    context: str = ""


# ---------------------------------------------------------------------------
# Fence detection + line parsing
# ---------------------------------------------------------------------------

# Match the `## Facts` heading, capture everything until the next `## `
# heading or end-of-string. Multiline mode so `^` anchors to line starts.
_FENCE_RE = re.compile(
    r"^##\s+Facts\s*$([\s\S]*?)(?=^##\s+|\Z)",
    re.MULTILINE | re.IGNORECASE,
)

# `key=value` token. Value runs until whitespace or strikethrough close,
# with optional unit suffix (e.g. `area_ha=12.4ha` — the unit is sniffed
# from a trailing non-numeric segment after the number).
_KV_RE = re.compile(r"([A-Za-z_][A-Za-z0-9_]*)=([^\s~]+)")

# Modifier tokens that affect the whole line.
_VALID_FROM_RE = re.compile(
    r"validFrom:(\d{4}-\d{2}-\d{2}(?:[T ]\d{2}:\d{2}(?::\d{2})?)?)"
)
_VALID_UNTIL_RE = re.compile(
    r"validUntil:(\d{4}-\d{2}-\d{2}(?:[T ]\d{2}:\d{2}(?::\d{2})?)?)"
)
_SUPERSEDED_RE = re.compile(r"supersededBy:([A-Za-z0-9_-]+)")
_FORGOTTEN_FLAG = "forgotten"

# Strikethrough: `~~key=value~~` marks a forgotten claim. We strip the
# tildes for the key=value extraction but remember the line was struck.
_STRIKE_RE = re.compile(r"~~([^~]+?)~~")

# Numeric value sniff — leading optional sign, digits, optional decimal,
# optional exponent. Used to populate value_numeric for trajectory tools.
_NUMERIC_RE = re.compile(r"^([+-]?\d+(?:\.\d+)?(?:[eE][+-]?\d+)?)([A-Za-z%]+)?$")


def _parse_iso_date(s: str) -> Optional[datetime]:
    """Parse 'YYYY-MM-DD' or full ISO datetime. None on malformed input.

    Lenient on shape — accepts date-only or full datetime. Used for
    validFrom/validUntil modifier tokens. Caller defaults to "now" when
    this returns None.
    """
    s = s.strip()
    if not s:
        return None
    try:
        if "T" in s or " " in s:
            return datetime.fromisoformat(s.replace(" ", "T"))
        d = date.fromisoformat(s)
        return datetime(d.year, d.month, d.day)
    except (ValueError, TypeError):
        return None


def parse_facts_fence(
    text: str,
    *,
    default_valid_from: Optional[datetime] = None,
) -> tuple[list[ParsedFact], int]:
    """Extract typed claims from a brain page's compiled_truth.

    Args:
        text: full compiled_truth of the page (or any markdown text).
            If no `## Facts` section is present, returns ([], 0).
        default_valid_from: fallback for claims that don't carry an
            explicit `validFrom:` modifier. Production callers thread
            the page's `updated_at` or `effective_date` so a meeting
            page dated 2026-04-28 stamps its facts as claimed-on that
            date instead of "import timestamp". None means leave the
            field null and let the DB column default kick in.

    Returns:
        (facts, skipped_count) where facts is the parsed claims and
        skipped_count is the number of lines that looked like claims
        but failed to parse (caller can warn).

    Determinism:
        Same input + same default_valid_from always yields the same
        output. No clock-dependent behavior.
    """
    if not text:
        return [], 0

    m = _FENCE_RE.search(text)
    if not m:
        return [], 0

    body = m.group(1)
    facts: list[ParsedFact] = []
    skipped = 0

    for raw_line in body.splitlines():
        line = raw_line.strip()
        if not line or line.startswith(("#", "<!--", "//")):
            continue

        # Strikethrough detection BEFORE we strip the tildes.
        struck_keys: set[str] = set()
        for sm in _STRIKE_RE.finditer(line):
            for km in _KV_RE.finditer(sm.group(1)):
                struck_keys.add(km.group(1))

        forgotten = _FORGOTTEN_FLAG in line.lower() or bool(struck_keys)

        # Strip the strikethrough wrappers so the KV extraction works.
        line_clean = _STRIKE_RE.sub(r"\1", line)

        # Pull modifier tokens out (they're not key=value pairs).
        valid_from_m = _VALID_FROM_RE.search(line_clean)
        valid_until_m = _VALID_UNTIL_RE.search(line_clean)
        superseded_m = _SUPERSEDED_RE.search(line_clean)

        valid_from = (
            _parse_iso_date(valid_from_m.group(1)) if valid_from_m else default_valid_from
        )
        valid_until = (
            _parse_iso_date(valid_until_m.group(1)) if valid_until_m else None
        )
        superseded_by_key = superseded_m.group(1) if superseded_m else None

        # Find every key=value pair on the line.
        line_no_modifiers = _VALID_FROM_RE.sub(
            "", _VALID_UNTIL_RE.sub("", _SUPERSEDED_RE.sub("", line_clean))
        )
        kv_matches = list(_KV_RE.finditer(line_no_modifiers))
        if not kv_matches:
            # Looked like a content line but no key=value found — count
            # for the caller's warning surface.
            if line and not line.startswith(("validFrom", "validUntil", _FORGOTTEN_FLAG)):
                skipped += 1
            continue

        for km in kv_matches:
            key = km.group(1)
            value = km.group(2)
            value_numeric: Optional[float] = None
            unit: Optional[str] = None
            nm = _NUMERIC_RE.match(value)
            if nm:
                try:
                    value_numeric = float(nm.group(1))
                    unit = nm.group(2) or None
                except (ValueError, TypeError):
                    value_numeric = None

            status = "active"
            if forgotten or key in struck_keys:
                status = "forgotten"
            elif superseded_by_key:
                status = "superseded"

            facts.append(
                ParsedFact(
                    key=key,
                    value=value,
                    value_numeric=value_numeric,
                    unit=unit,
                    valid_from=valid_from,
                    valid_until=valid_until,
                    status=status,
                    superseded_by_key=superseded_by_key,
                    context=line,
                )
            )

    return facts, skipped


# ---------------------------------------------------------------------------
# Regression flagging for trajectory queries
# ---------------------------------------------------------------------------

# Per-key thresholds. A trajectory entry is flagged as a regression when
# `prev.value_numeric - curr.value_numeric >= threshold`. None means the
# key has no automated regression definition (caller can still walk the
# series; just no auto-flag).
REGRESSION_THRESHOLDS: dict[str, float] = {
    "ndvi": 0.10,            # drop > 0.10 = significant vegetation loss
    "ndwi": 0.08,
    "nbr": 0.10,
    "soil_moisture": 0.05,    # drop > 0.05 m³/m³ = significant drying
    "et": 1.0,                # mm/day drop > 1
    "yield_kg_per_ha": 200,   # rough threshold; tune per crop
    # anomaly_score INCREASES are bad — handled as a separate path
    "anomaly_score": -1.0,
}


def flag_regressions(
    trajectory: list[dict],
    *,
    key: str,
) -> list[dict]:
    """Annotate a trajectory list with `regression_flag` per entry.

    Walks chronologically (oldest → newest) and stamps `True` when the
    current entry's value_numeric is materially worse than the previous
    one per `REGRESSION_THRESHOLDS[key]`. For keys not in the threshold
    table, every entry gets `regression_flag = False`.

    Mutates a copy — input list is unchanged. Returns the annotated copy
    in the same order.
    """
    if not trajectory:
        return []

    threshold = REGRESSION_THRESHOLDS.get(key)
    annotated = [dict(e) for e in trajectory]
    if threshold is None:
        for e in annotated:
            e["regression_flag"] = False
        return annotated

    # Anomaly score is the inverted case: increase = bad.
    increase_is_bad = threshold < 0
    abs_threshold = abs(threshold)

    prev_numeric: Optional[float] = None
    for e in annotated:
        v = e.get("value_numeric")
        if v is None or prev_numeric is None:
            e["regression_flag"] = False
        else:
            delta = prev_numeric - v if not increase_is_bad else v - prev_numeric
            e["regression_flag"] = delta >= abs_threshold
        if v is not None:
            prev_numeric = v
    return annotated
