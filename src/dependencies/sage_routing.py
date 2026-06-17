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
   request to the local Gemma 4 brain model instead of the transatlantic
   31B. This is the bulk of the win.

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

from src.llm_defaults import DEFAULT_SMALL_TALK_MODEL

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
SPATIAL_INSIGHT = "spatial_insight"


@dataclass(frozen=True)
class FastToolCall:
    """Deterministic tool route that can bypass the LLM safely."""

    tool_name: str
    arguments: dict[str, object]
    reason: str

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
    # Keep this set intentionally small. Satellite tools are not always-on:
    # plain "show me Nyamagabe" should prefer admin boundaries/locations, not
    # drift into imagery unless the user asks for imagery or an index.
    "zoom_to_bounds": ALWAYS_ON,
    "create_point_layer": ALWAYS_ON,
    "search_location": ALWAYS_ON,
    "reverse_geocode_coordinates": ALWAYS_ON,
    "add_layer_to_map": ALWAYS_ON,
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
    # --- Satellite imagery ---
    "search_satellite_imagery": SATELLITE,
    "display_satellite_layer": SATELLITE,
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
    # --- H3/city/environment insight layers ---
    "create_h3_spatial_insight_layer": SPATIAL_INSIGHT,
    "analyze_open_buildings_exposure": SPATIAL_INSIGHT,
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
    r"map|layer|field|farm|province|district|sector|cell|village|parcel|"
    r"admin|administrative|boundary|boundaries|location|"
    r"ndvi|ndwi|nbr|sar|ndre|raster|drone|satellite|cog|"
    r"insurance|harvest|yield|crop|soil|weather|forecast|drought|flood|"
    r"rainfall|temperature|moisture|"
    r"rwanda|kigali|musanze|huye|kayonza|gicumbi|nyagatare|nyabihu|"
    r"nyamagabe|rulindo|ruhanga|gasabo|rusizi|"
    r"show|display|plot|render|zoom|find|search|analyze|analyse|compute|"
    r"upload|download|export|"
    r"yesterday|today|tomorrow|week|month|season|year|january|february|"
    r"march|april|may|june|july|august|september|october|november|december"
    r")\b",
    re.IGNORECASE,
)


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
    return any(p.match(stripped) for p in _SMALL_TALK_PATTERNS)


# ---------------------------------------------------------------------------
# Intent classification
# ---------------------------------------------------------------------------
# Map keyword regex -> categories to enable. Multi-match is fine; we union.
_INTENT_KEYWORDS: list[tuple[re.Pattern[str], frozenset[str]]] = [
    # Rwanda administrative boundaries / plain place display.
    (
        re.compile(
            r"\b("
            r"admin(?:istrative)?|boundar(?:y|ies)|"
            r"province|district|sector|cell|village|"
            r"intara|akarere|umurenge|akagari|umudugudu|"
            r"show\s+me|show|display|locate|where\s+is|find|zoom\s+to"
            r")\b",
            re.IGNORECASE,
        ),
        frozenset({MAP_EDIT}),
    ),
    # Map / layer editing
    (
        re.compile(
            r"\b(layer|style|symbology|postgis|sql|geojson|flatgeobuf|"
            r"reproject|buffer|dissolve|merge|clip|intersect|join|grid|"
            r"zonal|aggregate|h3|hex|hexagon|whitebox|terrain|runoff|"
            r"housing|house|houses|building|buildings|open\s+buildings|"
            r"settlement|city|urban|"
            r"infrastructure|road|roads|bridge|drainage|culvert|environment|"
            r"pollution|erosion)\b",
            re.IGNORECASE,
        ),
        frozenset({MAP_EDIT, SPATIAL_INSIGHT}),
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
        frozenset({USER_RASTER, SPATIAL_INSIGHT}),
    ),
    # H3-first spatial intelligence for housing, infrastructure, environment,
    # city, and drone/basemap analysis.
    (
        re.compile(
            r"\b(h3|hex|hexagon|spatial\s+insight|insight\s+layer|"
            r"housing|house|houses|building|buildings|open\s+buildings|"
            r"settlement|city|urban|"
            r"infrastructure|road|roads|bridge|bridges|drainage|culvert|"
            r"environment|pollution|erosion|runoff|whitebox|terrain|"
            r"drone\s+analysis|basemap|satellite\s+basemap)\b",
            re.IGNORECASE,
        ),
        frozenset({SPATIAL_INSIGHT, MAP_EDIT, USER_RASTER}),
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


_ADMIN_ANALYSIS_BLOCKERS = re.compile(
    r"\b("
    r"ndvi|ndwi|nbr|evi|savi|ndre|ndbi|index|indices|satellite|sentinel|"
    r"weather|forecast|rain|rainfall|temperature|drought|flood|soil|crop|"
    r"yield|harvest|insurance|risk|analy[sz]e|analysis|statistics?|stats|"
    r"zonal|land\s*cover|worldcover|emissions?|food\s+security"
    r")\b",
    re.IGNORECASE,
)


def detect_admin_boundary_display(text: str) -> bool:
    """True for pure Rwanda admin boundary/location display requests."""
    stripped = " ".join(str(text or "").strip().split())
    if not stripped or _ADMIN_ANALYSIS_BLOCKERS.search(stripped):
        return False

    if re.search(
        r"\b(province|district|sector|cell|village|admin(?:istrative)?|"
        r"boundar(?:y|ies)|intara|akarere|umurenge|akagari|umudugudu)\b",
        stripped,
        re.IGNORECASE,
    ):
        return bool(
            re.search(
                r"\b(show|display|locate|find|draw|outline|map|put|zoom|go\s+to|where\s+is)\b",
                stripped,
                re.IGNORECASE,
            )
        )

    return bool(
        re.match(
            r"(?i)^(?:please\s+)?(?:again\s+)?"
            r"(?:show|display|locate|find|draw|outline|map|put|zoom(?:\s+to)?|go\s+to)"
            r"\s+(?:me|us\s+)?(?:the\s+)?[a-z][a-z\s.'-]{1,60}"
            r"(?:\s+on\s+the\s+map)?[?.!]*$",
            stripped,
        )
    )


def _clean_admin_boundary_candidate(value: str) -> str:
    cleaned = " ".join(str(value or "").strip().split())
    cleaned = re.sub(r"(?i)\b(on|onto)\s+the\s+map\b.*$", "", cleaned)
    cleaned = re.sub(r"(?i)\b(boundary|boundaries|outline)\b", "", cleaned)
    cleaned = re.sub(
        r"(?i)^\s*(?:please\s+)?(?:again\s+)?"
        r"(?:show|display|locate|find|draw|outline|map|put|zoom(?:\s+to)?|go\s+to|where\s+is)\s+",
        "",
        cleaned,
    )
    cleaned = re.sub(r"(?i)^\s*(?:me|us)\s+", "", cleaned)
    cleaned = re.sub(r"(?i)^\s*(?:the|a|an|all|every)\s+", "", cleaned)
    cleaned = re.sub(r"(?i)^\s*(?:around|for|of|in|at)\s+", "", cleaned)
    return cleaned.strip(" \t\r\n,.;:!?\"'`")


def build_admin_boundary_tool_args(text: str) -> dict[str, object] | None:
    """Build deterministic args for a pure admin-boundary display prompt."""
    if not detect_admin_boundary_display(text):
        return None
    prompt = " ".join(str(text or "").strip().split())

    child_match = re.search(
        r"(?i)\b(?:show|display|map|draw|outline|locate|find|zoom(?:\s+to)?|go\s+to)?"
        r"\s*(?:me|us)?\s*(?:all|the|every)?\s*"
        r"(villages|cells|sectors)\s+"
        r"(?:in|of|within|under|inside)\s+(.+?)\s+"
        r"(district|sector|cell)\b",
        prompt,
    )
    if child_match:
        child_level = child_match.group(1).lower().rstrip("s")
        parent_name = _clean_admin_boundary_candidate(child_match.group(2))
        parent_level = child_match.group(3).lower()
        if parent_name:
            args: dict[str, object] = {"admin_level": child_level, "name": "*"}
            args[parent_level] = parent_name
            return args

    for level in ("village", "cell", "sector", "district", "province"):
        explicit = re.search(rf"(?i)(.+?)\b{level}s?\b", prompt)
        if explicit:
            name = _clean_admin_boundary_candidate(explicit.group(1))
            if name:
                if level == "province" and name.lower() not in {"kigali", "kigali city"}:
                    if not name.lower().endswith("province"):
                        name = f"{name} Province"
                return {"admin_level": level, "name": name}

    simple = re.match(
        r"(?i)^(?:please\s+)?(?:again\s+)?"
        r"(?:show|display|locate|find|draw|outline|map|put|zoom(?:\s+to)?|go\s+to)"
        r"\s+(?:me|us\s+)?(?:the\s+)?(.+?)(?:\s+on\s+the\s+map)?[?.!]*$",
        prompt,
    )
    if simple:
        name = _clean_admin_boundary_candidate(simple.group(1))
        if name:
            return {"admin_level": "auto", "name": name}

    where = re.match(r"(?i)^where\s+is\s+(.+?)[?.!]*$", prompt)
    if where:
        name = _clean_admin_boundary_candidate(where.group(1))
        if name:
            return {"admin_level": "auto", "name": name}

    return None


_RASTER_AREA_KEYWORDS = re.compile(
    r"\b(hectares?|ha|area|acreage|size|coverage|covers?|covering|"
    r"footprint|extent|how\s+(?:big|large|many\s+hectares))\b",
    re.IGNORECASE,
)

_RASTER_OBJECT_KEYWORDS = re.compile(
    r"\b(raster|drone|ortho(?:photo|mosaic)?|orthophoto|image|cog|"
    r"tiff|geotiff|layer|file|upload(?:ed)?|field)\b",
    re.IGNORECASE,
)

_RASTER_NAME_STOPWORDS = {
    "a",
    "an",
    "area",
    "cog",
    "current",
    "drone",
    "field",
    "file",
    "geotiff",
    "ha",
    "hectare",
    "hectares",
    "image",
    "layer",
    "map",
    "my",
    "of",
    "ortho",
    "orthomosaic",
    "orthophoto",
    "raster",
    "size",
    "the",
    "this",
    "tiff",
    "uploaded",
}


def _normalize_raster_name(value: str) -> str:
    text = re.sub(r"[_/.-]+", " ", str(value or "").lower())
    text = re.sub(r"[^a-z0-9]+", " ", text)
    return " ".join(text.split())


def detect_raster_area_question(text: str) -> bool:
    """True for simple area/hectare questions about a user raster layer."""
    prompt = " ".join(str(text or "").strip().split())
    if not prompt:
        return False
    normalized_prompt = _normalize_raster_name(prompt)
    if not _RASTER_AREA_KEYWORDS.search(normalized_prompt):
        return False
    return bool(_RASTER_OBJECT_KEYWORDS.search(normalized_prompt))


def raster_layer_match_score(question: str, layer_name: str) -> float:
    """Score how clearly `question` refers to `layer_name`.

    The caller still owns ambiguity handling. This helper deliberately gives
    no credit for generic words like "orthophoto" or "raster" unless the full
    normalized layer name appears in the question.
    """
    q_norm = _normalize_raster_name(question)
    layer_norm = _normalize_raster_name(layer_name)
    if not q_norm or not layer_norm:
        return 0.0
    if layer_norm in q_norm:
        return 1.0

    q_tokens = set(q_norm.split())
    name_tokens = [
        token
        for token in layer_norm.split()
        if len(token) > 2 and token not in _RASTER_NAME_STOPWORDS
    ]
    if not name_tokens:
        return 0.0

    hits = sum(1 for token in name_tokens if token in q_tokens)
    if hits == 0:
        return 0.0
    return hits / len(name_tokens)


def build_fast_tool_call(text: str) -> FastToolCall | None:
    args = build_admin_boundary_tool_args(text)
    if args:
        return FastToolCall("show_admin_boundary", args, "fast:admin_boundary")
    return None


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
# RTT. Override via env if the deployment has something better.
def _small_talk_model() -> str:
    return os.environ.get("SAGE_SMALL_TALK_MODEL", DEFAULT_SMALL_TALK_MODEL)


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
