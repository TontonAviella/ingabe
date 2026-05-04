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
    USER_RASTER,
    classify_intent,
    detect_small_talk,
    extract_last_user_text,
    filter_tools_by_categories,
    route_chat,
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
        ("compare this raster to last week's", {USER_RASTER}),
        ("find similar tiles to this one", {USER_RASTER}),
        ("who is RAB", {BRAIN}),
        ("what is the cooperative in Gabiro", {BRAIN}),
        ("buffer the layer by 100m", {MAP_EDIT}),
        ("reproject the layer to EPSG:32735", {MAP_EDIT}),
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
        _tool("get_field_health"),  # AGRICULTURE
    ]
    out = filter_tools_by_categories(tools, {MAP_EDIT})
    names = {t["function"]["name"] for t in out}
    # ALWAYS_ON survives any filter
    assert "zoom_to_bounds" in names
    assert "add_layer_to_map" in names
    assert "search_location" in names
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


def test_route_chat_uncertain_falls_through() -> None:
    decision = route_chat("hmm let me think", history=[])
    assert decision.is_small_talk is False
    assert decision.selected_categories == frozenset()
    assert decision.reason == "default"


def test_route_chat_blocks_small_talk_when_tools_in_flight() -> None:
    """Once a tool round has started, an "ok" might mean "yes proceed" —
    we must not strip tools from the request."""
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
