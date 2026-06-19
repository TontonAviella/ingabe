"""Tests for src.dependencies.sage_routing.

These cover the three observable contracts:
  1. Small-talk detection is conservative (no false positives on real
     domain asks) and triggers on the obvious cases.
  2. classify_intent picks the right category for clear domain language
     and returns empty when the message is ambiguous.
  3. filter_tools_by_categories preserves ALWAYS_ON tools, drops
     out-of-category tools, and keeps tools the router has never seen.
"""

from __future__ import annotations

import pytest

from src.dependencies.sage_routing import (
    AGRICULTURE,
    BRAIN,
    MAP_EDIT,
    SATELLITE,
    SPATIAL_INSIGHT,
    USER_RASTER,
    build_admin_boundary_tool_args,
    build_fast_tool_call,
    classify_intent,
    detect_admin_boundary_display,
    detect_raster_area_question,
    detect_small_talk,
    extract_last_user_text,
    filter_tools_by_categories,
    raster_layer_match_score,
    route_chat,
    routing_alignment_for_tool,
    tool_category_for_name,
)


# ---------------------------------------------------------------------------
# Small-talk detection
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    "msg",
    [
        "hi",
        "Hi",
        "hello",
        "hey there"[:3],  # "hey"
        "yo",
        "thanks",
        "thank you",
        "thx",
        "ok",
        "cool",
        "got it",
        "sounds good",
        "good morning",
        "good afternoon",
        "yes",
        "no",
        "bye",
        "see ya",
        "what can you do?",
        "who are you",
        "Hello!",
        "thanks.",
    ],
)
def test_detect_small_talk_positive(msg: str) -> None:
    assert detect_small_talk(msg) is True


@pytest.mark.parametrize(
    "msg",
    [
        "",
        "hi, can you show me the NDVI for Musanze?",
        "thanks for that — now show me the soil moisture",
        "what is the rainfall in Kigali this week",
        "show me my drone ortho",
        "hello — what's the yield risk in Huye?",
        # Long enough that it can't be small-talk regardless of words.
        "hi " * 30,
        "ok let me know how the crops are doing in Nyagatare",
        "hey can you analyze this raster",
        # Domain-blocker overrides even if it starts with a greeting word.
        "good morning, what's the weather forecast",
    ],
)
def test_detect_small_talk_negative(msg: str) -> None:
    assert detect_small_talk(msg) is False


# ---------------------------------------------------------------------------
# Intent classification
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    "msg, expected",
    [
        ("show me the NDVI for Musanze", {SATELLITE}),
        ("compute the spectral index for January 2025", {SATELLITE}),
        ("what is the soil moisture in Kigali", {AGRICULTURE}),
        ("dry spell in Nyagatare last month", {AGRICULTURE}),
        ("analyze my drone ortho", {USER_RASTER}),
        ("what is happening in this drone image", {USER_RASTER, SPATIAL_INSIGHT}),
        ("what damage should we expect around this orthophoto", {USER_RASTER, SPATIAL_INSIGHT}),
        ("is this agriculture or housing infrastructure on the map", {SPATIAL_INSIGHT}),
        ("compare this raster to last week's", {USER_RASTER}),
        ("find similar tiles to this one", {USER_RASTER}),
        ("who is RAB", {BRAIN}),
        ("what is the cooperative in Gabiro", {BRAIN}),
        ("buffer the layer by 100m", {MAP_EDIT}),
        ("reproject the layer to EPSG:32735", {MAP_EDIT}),
        ("show Open Buildings exposure for this village", {SPATIAL_INSIGHT}),
        ("how many houses are exposed to flood risk here", {SPATIAL_INSIGHT}),
    ],
)
def test_classify_intent_known_domains(
    msg: str, expected: set[str]
) -> None:
    cats = classify_intent(msg)
    # We only assert the expected categories are present; classify_intent
    # may include others (e.g. "raster" matches both USER_RASTER and
    # MAP_EDIT keywords), and that's a safe over-approximation.
    assert expected.issubset(cats), f"{msg!r} -> {cats}"


@pytest.mark.parametrize(
    "msg",
    [
        "",
        "tell me a joke",
        "explain how this works",
        "i have a question",
    ],
)
def test_classify_intent_uncertain_returns_empty(msg: str) -> None:
    assert classify_intent(msg) == frozenset()


# ---------------------------------------------------------------------------
# Tool filtering
# ---------------------------------------------------------------------------
def _tool(name: str) -> dict:
    return {"type": "function", "function": {"name": name, "parameters": {}}}


def test_filter_keeps_always_on() -> None:
    tools = [
        _tool("zoom_to_bounds"),
        _tool("add_layer_to_map"),
        _tool("search_location"),
        _tool("display_satellite_layer"),
        _tool("get_field_health"),  # AGRICULTURE
    ]
    out = filter_tools_by_categories(tools, {MAP_EDIT})
    names = {t["function"]["name"] for t in out}
    # ALWAYS_ON survives any filter
    assert "zoom_to_bounds" in names
    assert "add_layer_to_map" in names
    assert "search_location" in names
    # Satellite display is only available when satellite intent is selected.
    assert "display_satellite_layer" not in names
    # AGRICULTURE-only tool must be dropped under MAP_EDIT filter
    assert "get_field_health" not in names


def test_filter_keeps_selected_categories() -> None:
    tools = [
        _tool("get_field_health"),  # AGRICULTURE
        _tool("get_ndvi_stats"),  # AGRICULTURE
        _tool("describe_user_raster"),  # USER_RASTER
        _tool("set_layer_style"),  # MAP_EDIT
    ]
    out = filter_tools_by_categories(tools, {AGRICULTURE})
    names = {t["function"]["name"] for t in out}
    assert names == {"get_field_health", "get_ndvi_stats"}


def test_filter_keeps_uncategorized_tools() -> None:
    """Tools the router has never been taught about must not be silently
    dropped — the router should fail open."""
    tools = [
        _tool("brand_new_tool_we_havent_categorized_yet"),
        _tool("get_field_health"),  # AGRICULTURE
    ]
    out = filter_tools_by_categories(tools, {MAP_EDIT})
    names = {t["function"]["name"] for t in out}
    assert "brand_new_tool_we_havent_categorized_yet" in names
    assert "get_field_health" not in names


def test_tool_category_helpers_support_observability() -> None:
    assert tool_category_for_name("create_raster_h3_context_layer") == SPATIAL_INSIGHT
    assert tool_category_for_name("unknown_new_tool") == "uncategorized"
    assert (
        routing_alignment_for_tool(
            "create_raster_h3_context_layer",
            {SPATIAL_INSIGHT, USER_RASTER},
        )
        == "matched"
    )
    assert routing_alignment_for_tool("zoom_to_bounds", {USER_RASTER}) == "always_on"
    assert routing_alignment_for_tool("get_soil_moisture", {USER_RASTER}) == "mismatch"
    assert routing_alignment_for_tool("unknown_new_tool", {USER_RASTER}) == "uncategorized_kept"
    assert routing_alignment_for_tool("get_soil_moisture", set()) == "full_toolset"


# ---------------------------------------------------------------------------
# route_chat top-level decisions
# ---------------------------------------------------------------------------
def test_route_chat_small_talk() -> None:
    decision = route_chat("hi", history=[])
    assert decision.is_small_talk is True
    assert decision.primary_model_override is not None
    assert decision.reason == "small_talk"


def test_route_chat_real_ask_filters_intent() -> None:
    decision = route_chat(
        "what's the NDVI in Musanze", history=[]
    )
    assert decision.is_small_talk is False
    assert SATELLITE in decision.selected_categories
    assert decision.reason.startswith("intent:")


def test_route_chat_plain_admin_place_prefers_boundary_tools() -> None:
    decision = route_chat("show me nyamagabe ?", history=[])
    assert decision.is_small_talk is False
    assert MAP_EDIT in decision.selected_categories
    assert SATELLITE not in decision.selected_categories


def test_admin_boundary_filter_excludes_satellite_tools() -> None:
    tools = [
        _tool("search_location"),
        _tool("new_layer_from_postgis"),
        _tool("set_layer_style"),
        _tool("search_satellite_imagery"),
        _tool("display_satellite_layer"),
    ]
    out = filter_tools_by_categories(tools, {MAP_EDIT})
    names = {t["function"]["name"] for t in out}
    assert "search_location" in names
    assert "new_layer_from_postgis" in names
    assert "set_layer_style" in names
    assert "search_satellite_imagery" not in names
    assert "display_satellite_layer" not in names


def test_explicit_satellite_place_keeps_satellite_tools() -> None:
    decision = route_chat("show Sentinel satellite imagery for Nyamagabe", history=[])
    assert MAP_EDIT in decision.selected_categories
    assert SATELLITE in decision.selected_categories

    tools = [
        _tool("new_layer_from_postgis"),
        _tool("display_satellite_layer"),
    ]
    out = filter_tools_by_categories(tools, decision.selected_categories)
    names = {t["function"]["name"] for t in out}
    assert "new_layer_from_postgis" in names
    assert "display_satellite_layer" in names


@pytest.mark.parametrize(
    "msg, expected",
    [
        ("show me Nyamagabe", {"admin_level": "auto", "name": "Nyamagabe"}),
        ("show me Musanze district", {"admin_level": "district", "name": "Musanze"}),
        ("i wanna see the Busasamana sector", {"admin_level": "sector", "name": "Busasamana"}),
        ("view Busasamana sector", {"admin_level": "sector", "name": "Busasamana"}),
        ("locate Gasharu village", {"admin_level": "village", "name": "Gasharu"}),
        ("show me Southern Province", {"admin_level": "province", "name": "Southern Province"}),
        ("I want to see all villages in Gasabo district", {"admin_level": "village", "name": "*", "district": "Gasabo"}),
        ("show all villages in Gasabo district", {"admin_level": "village", "name": "*", "district": "Gasabo"}),
    ],
)
def test_build_admin_boundary_tool_args(msg: str, expected: dict[str, object]) -> None:
    assert build_admin_boundary_tool_args(msg) == expected


@pytest.mark.parametrize(
    "msg",
    [
        "show me NDVI in Nyamagabe",
        "weather forecast for Kigali tomorrow",
        "predict floods in Western Province",
        "show satellite imagery for Musanze",
        "analyze crop risk in Ruhango",
    ],
)
def test_admin_boundary_fast_path_blocks_analysis(msg: str) -> None:
    assert detect_admin_boundary_display(msg) is False
    assert build_fast_tool_call(msg) is None


def test_build_fast_tool_call_admin_boundary() -> None:
    fast = build_fast_tool_call("show me Rulindo district")
    assert fast is not None
    assert fast.tool_name == "show_admin_boundary"
    assert fast.arguments == {"admin_level": "district", "name": "Rulindo"}


@pytest.mark.parametrize(
    "msg",
    [
        "tell me the hectares of Cyampirita_Orthophoto?",
        "how big is this drone orthophoto?",
        "what area does my uploaded raster cover?",
    ],
)
def test_detect_raster_area_question(msg: str) -> None:
    assert detect_raster_area_question(msg) is True


@pytest.mark.parametrize(
    "msg",
    [
        "tell me the hectares of Nyagatare Cells",
        "show me Rulindo district",
        "what happened in this conversation?",
    ],
)
def test_detect_raster_area_question_blocks_non_rasters(msg: str) -> None:
    assert detect_raster_area_question(msg) is False


def test_build_fast_tool_call_raster_area_defers_until_layer_id_is_known() -> None:
    assert build_fast_tool_call("tell me the hectares of Cyampirita_Orthophoto?") is None


def test_raster_layer_match_score_uses_distinctive_name_tokens() -> None:
    assert (
        raster_layer_match_score(
            "tell me the hectares of Cyampirita_Orthophoto?",
            "Cyampirita_Orthophoto",
        )
        == 1.0
    )
    assert (
        raster_layer_match_score(
            "tell me the hectares of this orthophoto?",
            "Cyampirita_Orthophoto",
        )
        == 0.0
    )


def test_route_chat_uncertain_falls_through() -> None:
    decision = route_chat("hmm let me think", history=[])
    assert decision.is_small_talk is False
    assert decision.selected_categories == frozenset()
    assert decision.reason == "default"


def test_route_chat_blocks_small_talk_when_tools_in_flight() -> None:
    """While the LLM is mid-tool-round (tool_calls issued, no follow-up
    assistant text yet), an "ok" might mean "yes proceed" — we must not
    strip tools from the request."""
    history = [
        {"role": "user", "content": "show me the NDVI in Huye"},
        {
            "role": "assistant",
            "content": None,
            "tool_calls": [
                {
                    "id": "call_1",
                    "type": "function",
                    "function": {
                        "name": "compute_spectral_index",
                        "arguments": "{}",
                    },
                }
            ],
        },
        {"role": "tool", "tool_call_id": "call_1", "content": "ok"},
    ]
    decision = route_chat("ok", history=history)
    assert decision.is_small_talk is False


def test_route_chat_allows_small_talk_after_completed_tool_round() -> None:
    """A finished tool round (assistant tool_calls -> tool responses ->
    assistant text) is closed. A "thanks" reply afterward is genuine
    small-talk and should take the fast-path."""
    history = [
        {"role": "user", "content": "show me the NDVI in Huye"},
        {
            "role": "assistant",
            "content": None,
            "tool_calls": [
                {
                    "id": "call_1",
                    "type": "function",
                    "function": {
                        "name": "compute_spectral_index",
                        "arguments": "{}",
                    },
                }
            ],
        },
        {"role": "tool", "tool_call_id": "call_1", "content": "{\"mean\": 0.62}"},
        {
            "role": "assistant",
            "content": "NDVI in Huye averages 0.62 — healthy vegetation.",
        },
    ]
    decision = route_chat("thanks", history=history)
    assert decision.is_small_talk is True
    assert decision.reason == "small_talk"


# ---------------------------------------------------------------------------
# extract_last_user_text
# ---------------------------------------------------------------------------
def test_extract_last_user_text_string_content() -> None:
    msgs = [
        {"role": "user", "content": "first"},
        {"role": "assistant", "content": "reply"},
        {"role": "user", "content": "second"},
    ]
    assert extract_last_user_text(msgs) == "second"


def test_extract_last_user_text_multipart() -> None:
    msgs = [
        {
            "role": "user",
            "content": [
                {"type": "text", "text": "part one"},
                {"type": "text", "text": "part two"},
            ],
        },
    ]
    assert "part one" in extract_last_user_text(msgs)
    assert "part two" in extract_last_user_text(msgs)


def test_extract_last_user_text_no_user_msg() -> None:
    msgs = [{"role": "system", "content": "you are sage"}]
    assert extract_last_user_text(msgs) == ""
