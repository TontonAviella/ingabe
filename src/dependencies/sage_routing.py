"""Sage routing: pre-classify the user turn so we can skip the heavy tool
schema and send the request to a faster, cheaper model when nothing is at
stake.

Why this exists
---------------
Every Sage turn ships ~6.7K tokens of system prompt and ~13.4K tokens of
tool schemas (60 tools) to the LLM. On a 31B model hosted in another
continent, the prefill alone dominates time-to-first-token even for "hi".

Two levers here:

1. **Small-talk fast-path** — if the user said something trivial (greeting,
   thanks, ack), there is no possible tool call. We bypass the tool list
   entirely, replace the system prompt with a one-liner, and route the
   request to the local Ollama qwen2.5:7b-64k container instead of the
   transatlantic 31B. This is the bulk of the win.

2. **Tool subsetting** — if the user is clearly in one domain (map edit,
   agriculture, user-raster analysis, brain), trim the tool list to that
   domain plus a small always-on set. The LLM still sees 1-15 tools, not
   60. We only do this when classification is high-confidence; otherwise
   we send the full list (current behavior).

The router is intentionally regex-based and dependency-free. A misroute
costs the user one extra "what?" round-trip; a subtle ML model costs us
more latency than the problem we're trying to solve.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from typing import Iterable

# ---------------------------------------------------------------------------
# Tool category map
# ---------------------------------------------------------------------------
# Categories used by the router. ALWAYS_ON tools are included in every
# non-small-talk turn regardless of classification.
ALWAYS_ON = "always_on"
MAP_EDIT = "map_edit"
SATELLITE = "satellite"
AGRICULTURE = "agriculture"
USER_RASTER = "user_raster"
BRAIN = "brain"

# Map tool name -> category. Tools not in this dict are treated as
# "uncategorized" and included whenever we cannot rule them out (i.e.
# whenever we fall back to the full list). Category labels reflect what
# the tool is *for*, not which file it lives in.
#
# Fail-open contract: when a new tool is added to `tools.json` or the
# Pydantic registry without a matching entry here, the router will keep
# it in the filtered set. That means new tools work immediately at the
# cost of slightly looser filtering until they are categorized. This
# trades some latency for correctness — the alternative (silently
# dropping uncategorized tools) is the failure mode we never want.
_TOOL_CATEGORIES: dict[str, str] = {
    # --- Always available: trivial display + geocoding ---
    # `add_layer_to_map`, `display_satellite_layer`, `search_satellite_imagery`
    # live here because any domain turn can end with "show me the result on
    # the map". Excluding them from a filtered turn would mean the LLM has
    # the data but no way to surface it.
    "zoom_to_bounds": ALWAYS_ON,
    "create_point_layer": ALWAYS_ON,
    "search_location": ALWAYS_ON,
    "reverse_geocode_coordinates": ALWAYS_ON,
    "add_layer_to_map": ALWAYS_ON,
    "search_satellite_imagery": ALWAYS_ON,
    "display_satellite_layer": ALWAYS_ON,
    # --- Map editing / postgis / generic geoprocessing ---
    "new_layer_from_postgis": MAP_EDIT,
    "set_layer_style": MAP_EDIT,
    "query_duckdb_sql": MAP_EDIT,
    "query_postgis_database": MAP_EDIT,
    "zonal_statistics": MAP_EDIT,
    "query_rwanda_zonal_stats": MAP_EDIT,
    "add_land_cover_layer": MAP_EDIT,
    "gdal_warpreproject": MAP_EDIT,
    "native_aggregate": MAP_EDIT,
    "native_buffer": MAP_EDIT,
    "native_dissolve": MAP_EDIT,
    "native_fieldcalculator": MAP_EDIT,
    "native_fixgeometries": MAP_EDIT,
    "native_geometrybyexpression": MAP_EDIT,
    "native_joinattributesbylocation": MAP_EDIT,
    "native_mergevectorlayers": MAP_EDIT,
    "native_reprojectlayer": MAP_EDIT,
    "native_creategrid": MAP_EDIT,
    "native_zonalstatisticsfb": MAP_EDIT,
    "qgis_clip": MAP_EDIT,
    "qgis_intersection": MAP_EDIT,
    "qgis_joinbylocationsummary": MAP_EDIT,
    "qgis_statisticsbycategories": MAP_EDIT,
    # --- Satellite imagery (compute side; display is ALWAYS_ON) ---
    "compute_spectral_index": SATELLITE,
    # --- Agricultural data products ---
    "get_field_health": AGRICULTURE,
    "get_ndvi_stats": AGRICULTURE,
    "get_cell_ndvi_stats": AGRICULTURE,
    "get_soil_properties": AGRICULTURE,
    "get_parcel_ndvi_stats": AGRICULTURE,
    "get_agri_indices": AGRICULTURE,
    "query_worldcover_stats": AGRICULTURE,
    "get_crop_classifications": AGRICULTURE,
    "get_anomaly_alerts": AGRICULTURE,
    "get_yield_risk": AGRICULTURE,
    "get_drought_status": AGRICULTURE,
    "get_crop_growth_stage": AGRICULTURE,
    "get_weather_stats": AGRICULTURE,
    "get_forecast": AGRICULTURE,
    "get_forecast_accuracy": AGRICULTURE,
    "get_emissions_stats": AGRICULTURE,
    "create_management_zones": AGRICULTURE,
    "create_prescription_map": AGRICULTURE,
    "create_soil_sampling_plan": AGRICULTURE,
    "identify_parcel_crop": AGRICULTURE,
    "confirm_crop_prediction": AGRICULTURE,
    "get_soil_moisture": AGRICULTURE,
    "get_evapotranspiration": AGRICULTURE,
    "get_food_security_alerts": AGRICULTURE,
    "detect_dry_spells": AGRICULTURE,
    "get_insurance_accuracy": AGRICULTURE,
    "get_insurance_intelligence": AGRICULTURE,
    "predict_ndvi_from_sar": AGRICULTURE,
    "detect_water_bodies": AGRICULTURE,
    "detect_flood_extent": AGRICULTURE,
    "get_alos_l_band_stats": AGRICULTURE,
    "get_alos_temporal_variation": AGRICULTURE,
    "check_cygnss_availability": AGRICULTURE,
    "get_cygnss_soil_moisture": AGRICULTURE,
    "get_cygnss_watermask": AGRICULTURE,
    # --- User-uploaded raster (drone, COG) analysis ---
    "describe_user_raster": USER_RASTER,
    "compute_zonal_stats": USER_RASTER,
    "interpret_raster_health": USER_RASTER,
    "analyze_rgb_field": USER_RASTER,
    "read_pixel_at": USER_RASTER,
    "get_value_distribution": USER_RASTER,
    "find_stress_zones": USER_RASTER,
    "compare_rasters": USER_RASTER,
    "evaluate_insurance_trigger": USER_RASTER,
    "find_similar_tiles": USER_RASTER,
    # --- Knowledge graph / Brain ---
    "search_brain": BRAIN,
    "get_entity": BRAIN,
    "add_observation": BRAIN,
}


# ---------------------------------------------------------------------------
# Small-talk detection
# ---------------------------------------------------------------------------
# Tight allowlist of patterns. We only trigger on clear, short, tool-free
# turns. Anything that mentions a place, a layer, a number, or a verb that
# could imply data work falls through to the normal path.
_SMALL_TALK_MAX_LEN = 80

_SMALL_TALK_PATTERNS = [
    re.compile(r"^(hi+|hey+|hello+|yo|sup|howdy)\b[\s!.,?]*$", re.IGNORECASE),
    re.compile(r"^(good\s+(morning|afternoon|evening|day))[\s!.,?]*$", re.IGNORECASE),
    re.compile(r"^(thanks?|thank\s+you|thx|ty|cheers|merci|murakoze)[\s!.,?]*$", re.IGNORECASE),
    re.compile(r"^(ok|okay|cool|nice|great|awesome|got\s+it|sounds\s+good)[\s!.,?]*$", re.IGNORECASE),
    re.compile(r"^(yes|no|yep|nope|sure|maybe)[\s!.,?]*$", re.IGNORECASE),
    re.compile(
        r"^(how\s+(are\s+you|r\s+u|is\s+it\s+going)|what's\s+up|whats\s+up)[\s!.,?]*$",
        re.IGNORECASE,
    ),
    re.compile(r"^(bye|goodbye|see\s+you|see\s+ya|later|cya)[\s!.,?]*$", re.IGNORECASE),
    re.compile(r"^(who\s+are\s+you|what\s+are\s+you|what\s+can\s+you\s+do)\??$", re.IGNORECASE),
]

# Words that, if present, override the small-talk match. The user might
# *open* with "hi" but follow with a real ask; we only fire on pure
# small-talk turns.
_DOMAIN_BLOCKERS = re.compile(
    r"\b("
    r"map|layer|field|farm|district|sector|cell|parcel|"
    r"ndvi|ndwi|nbr|sar|ndre|raster|drone|satellite|cog|"
    r"insurance|harvest|yield|crop|soil|weather|forecast|drought|flood|"
    r"rainfall|temperature|moisture|"
    r"rwanda|kigali|musanze|huye|kayonza|gicumbi|nyagatare|nyabihu|"
    r"show|display|plot|render|zoom|find|search|analyze|analyse|compute|"
    r"upload|download|export|"
    r"yesterday|today|tomorrow|week|month|season|year|january|february|"
    r"march|april|may|june|july|august|september|october|november|december"
    r")\b",
    re.IGNORECASE,
)


# Trailing chars to strip before matching small-talk patterns. Users hit
# any of these as typos or stylistic flourishes after a greeting:
#   "hi'"  "hi!"  "hi."  "hi..."  "hi ?"  "hi !!"  "hi 😀"
# Stripping them widens the regex's catch radius without rewriting it.
# Emoji + miscellaneous Unicode punctuation get the same treatment via
# the str.isalnum() filter in _normalize_for_smalltalk_match.
_SMALL_TALK_TRAILING_STRIP = r"""!.,?'"`~*_-…—:;)]}>/\\"""


def _normalize_for_smalltalk_match(text: str) -> str:
    """Strip whitespace + trailing non-word punctuation/emoji before matching.

    The small-talk regex needs an exact-end anchor (`$`) so it doesn't
    accidentally match phrases that *start* with a greeting. But that
    same anchor makes `hi'`, `hi!`, `hi 😀` slip through to the slow
    path. Normalize-then-match keeps the regex strict while accepting
    the common typo / decoration patterns users actually send.
    """
    s = text.strip()
    # Drop trailing whitespace + any chars in our explicit strip set.
    s = s.rstrip(_SMALL_TALK_TRAILING_STRIP + " \t")
    # Drop trailing non-alphanumeric chars (catches emoji, smart quotes,
    # zero-width joiners, etc.). Loop is bounded by str length.
    while s and not s[-1].isalnum():
        s = s[:-1]
    return s


def detect_small_talk(text: str) -> bool:
    """Return True if `text` is pure small-talk that needs no tools.

    Conservative by design: the cost of a false negative (treating a real
    ask as small-talk) is a useless reply, and the cost of a false
    positive (treating small-talk as a real ask) is one slow turn. We
    skew toward false positives.
    """
    if not text:
        return False
    stripped = text.strip()
    if len(stripped) > _SMALL_TALK_MAX_LEN:
        return False
    if _DOMAIN_BLOCKERS.search(stripped):
        return False
    normalized = _normalize_for_smalltalk_match(stripped)
    if not normalized:
        return False
    return any(p.match(normalized) for p in _SMALL_TALK_PATTERNS)


# ---------------------------------------------------------------------------
# Intent classification
# ---------------------------------------------------------------------------
# Map keyword regex -> categories to enable. Multi-match is fine; we union.
_INTENT_KEYWORDS: list[tuple[re.Pattern[str], frozenset[str]]] = [
    # Map / layer editing
    (
        re.compile(
            r"\b(layer|style|symbology|postgis|sql|geojson|flatgeobuf|"
            r"reproject|buffer|dissolve|merge|clip|intersect|join|grid|"
            r"zonal|aggregate)\b",
            re.IGNORECASE,
        ),
        frozenset({MAP_EDIT}),
    ),
    # Satellite imagery (Earth Search, Sentinel-2)
    (
        re.compile(
            r"\b(sentinel|sentinel-?2|landsat|tci|true\s*color|"
            r"satellite\s+(image|imagery|scene)|cog\s+tile|"
            r"ndvi|ndwi|nbr|spectral\s+index|natural\s+color|s2)\b",
            re.IGNORECASE,
        ),
        frozenset({SATELLITE}),
    ),
    # Agriculture / weather / insurance
    (
        re.compile(
            r"\b(field|farm|crop|harvest|yield|drought|flood|water|"
            r"rainfall|precip|weather|forecast|temperature|"
            r"soil|moisture|evapo|ndre|emission|"
            r"insurance|trigger|payout|"
            r"sar|alos|cygnss|wapor|chirps|food\s+security|fewsnet|"
            r"maize|beans|rice|cassava|coffee|tea|sorghum|wheat|"
            r"season\s*[abc]|growing\s+season|dry\s+spell)\b",
            re.IGNORECASE,
        ),
        frozenset({AGRICULTURE}),
    ),
    # User-uploaded raster (drone ortho, custom COG)
    (
        re.compile(
            r"\b(my\s+(field|raster|cog|drone|ortho|image)|"
            r"this\s+(raster|drone|ortho|image|cog)|"
            r"uploaded|drone|ortho(photo|mosaic)?|tiff|geotiff|"
            r"stress\s+zone|pixel|histogram|distribution|"
            r"compare\s+(raster|image)|similar\s+tile|find\s+similar)\b",
            re.IGNORECASE,
        ),
        frozenset({USER_RASTER}),
    ),
    # Knowledge graph / Brain
    (
        re.compile(
            r"\b(brain|entity|observation|knowledge|"
            r"who\s+is|what\s+is\s+the\s+(rab|minagri|bk|bnr|naeb)|"
            r"institution|cooperative|government|ministry|partner)\b",
            re.IGNORECASE,
        ),
        frozenset({BRAIN}),
    ),
]


def classify_intent(text: str) -> frozenset[str]:
    """Return the set of tool categories likely needed for this turn.

    Returns an empty frozenset when classification is uncertain — caller
    should treat that as "send the full tool list" (current behavior).
    Always-on tools are added by `filter_tools_by_categories`, not here.
    """
    if not text:
        return frozenset()
    cats: set[str] = set()
    for pattern, categories in _INTENT_KEYWORDS:
        if pattern.search(text):
            cats.update(categories)
    return frozenset(cats)


def filter_tools_by_categories(
    tools: list[dict], categories: Iterable[str]
) -> list[dict]:
    """Return the subset of `tools` whose names map to one of `categories`,
    plus all ALWAYS_ON tools and any uncategorized tools.

    Uncategorized tools (names not in `_TOOL_CATEGORIES`) are kept by
    default so we don't accidentally drop newly-added tools the router
    hasn't been taught about yet.
    """
    cat_set = set(categories)
    cat_set.add(ALWAYS_ON)
    out: list[dict] = []
    for tool in tools:
        name = tool.get("function", {}).get("name", "")
        if not name:
            out.append(tool)
            continue
        cat = _TOOL_CATEGORIES.get(name)
        if cat is None or cat in cat_set:
            out.append(tool)
    return out


# ---------------------------------------------------------------------------
# RoutingDecision
# ---------------------------------------------------------------------------
# One-liner system prompt for small-talk turns. The big prompt has 380
# lines explaining tool routing, identifier hierarchy, Rwanda admin
# boundaries, etc. — none of which matter for "hi".
SMALL_TALK_SYSTEM_PROMPT = (
    "You are Sage, a friendly AI GIS assistant for Ingabe (mundi.ai), "
    "a precision agriculture platform for Rwanda. Reply in 1-2 short "
    "sentences. If the user has a real question about maps, fields, "
    "satellite data, or agriculture, ask them to clarify."
)

# Default fast model for small-talk turns. Local container, no transatlantic
# RTT, ~7B params. Override via env if the deployment has something better.
def _small_talk_model() -> str:
    return os.environ.get("SAGE_SMALL_TALK_MODEL", "ollama:qwen2.5:7b-64k")


@dataclass(frozen=True)
class RoutingDecision:
    """Result of routing a single user turn.

    Fields:
        is_small_talk: When True, caller should drop tools, swap in
            `SMALL_TALK_SYSTEM_PROMPT`, and use `primary_model_override`.
        selected_categories: When non-empty AND `is_small_talk` is False,
            caller should filter the tools list by these categories. When
            empty, caller should send the full tools list (current path).
        primary_model_override: When set, caller should use this model
            as the head of the fallback chain instead of OPENAI_MODEL.
        reason: Short human-readable label for logs and observability.
    """

    is_small_talk: bool
    selected_categories: frozenset[str]
    primary_model_override: str | None
    reason: str


def _tool_round_in_flight(history: list[dict] | None) -> bool:
    """True if the most recent assistant message issued tool_calls and the
    LLM has not yet produced a follow-up text message. In this state the
    user's "ok" might mean "yes proceed" rather than chitchat, so we must
    not strip tools from the request.

    A completed earlier tool round (assistant tool_calls -> tool responses
    -> assistant text) does NOT count: that round is closed, and a
    "thanks" or "ok" reply afterward is genuine small-talk that should
    take the fast-path. We look at the *most recent* assistant message
    only, because OpenAI's chat protocol guarantees any pending tool_calls
    live there (the LLM cannot start a new turn while older tool_calls
    remain).
    """
    if not history:
        return False
    for msg in reversed(history):
        if not isinstance(msg, dict):
            continue
        if msg.get("role") == "assistant":
            return bool(msg.get("tool_calls"))
    return False


def route_chat(
    user_message: str,
    history: list[dict] | None = None,
) -> RoutingDecision:
    """Decide how to handle one Sage turn.

    Args:
        user_message: The latest user message text (content only, no
            roles or metadata). Empty string is allowed.
        history: Prior messages in OpenAI chat format (list of dicts
            with "role" and "content"/"tool_calls"). Used to suppress
            small-talk routing when a tool round is in progress.

    Returns:
        A RoutingDecision the caller can act on.
    """
    if (
        detect_small_talk(user_message)
        and not _tool_round_in_flight(history)
    ):
        return RoutingDecision(
            is_small_talk=True,
            selected_categories=frozenset(),
            primary_model_override=_small_talk_model(),
            reason="small_talk",
        )

    cats = classify_intent(user_message)
    if cats:
        return RoutingDecision(
            is_small_talk=False,
            selected_categories=cats,
            primary_model_override=None,
            reason=f"intent:{','.join(sorted(cats))}",
        )

    return RoutingDecision(
        is_small_talk=False,
        selected_categories=frozenset(),
        primary_model_override=None,
        reason="default",
    )


def extract_last_user_text(messages: list[dict]) -> str:
    """Pull the text of the last user message out of an OpenAI chat-style
    list. Tool messages and assistant messages are skipped.

    Returns empty string when no user message is present.
    """
    for msg in reversed(messages):
        if not isinstance(msg, dict):
            continue
        if msg.get("role") != "user":
            continue
        content = msg.get("content")
        if isinstance(content, str):
            return content
        if isinstance(content, list):
            # OpenAI multi-part content: list of {type, text} dicts.
            parts: list[str] = []
            for part in content:
                if isinstance(part, dict) and part.get("type") == "text":
                    txt = part.get("text")
                    if isinstance(txt, str):
                        parts.append(txt)
            return "\n".join(parts)
    return ""
