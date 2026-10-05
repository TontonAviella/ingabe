"""The tool list and per-turn request plan for Sage's hand-rolled chat loop.

Moved out of ``src/routes/message_routes.py`` (behaviour-preserving) so the
live loop and the routing eval (``scripts/eval_sage_routing.py``) build the
model request with the same code instead of a copy that drifts (H2).
"""
from __future__ import annotations

import logging
import os
import re
import time
from dataclasses import dataclass, replace
from typing import Any, Callable, Mapping

from src.dependencies.sage_routing import (
    SMALL_TALK_SYSTEM_PROMPT,
    RoutingDecision,
    filter_tools_by_categories,
    route_chat,
)
from src.geoprocessing.dispatch import get_tools
from src.services.brain_embeddings import embed_texts
from src.services.life_harness import (
    apply_life_harness_system_prompt,
    apply_life_harness_tool_contracts,
)
from src.services.sage_tool_shortlist import Embedder, ToolEmbeddingCache, hybrid_shortlist
from src.tools.pyd import tool_from as tool_from_pyd

logger = logging.getLogger(__name__)


def build_sage_tools_payload(
    pydantic_tool_calls: Mapping[str, Any],
    layer_enum: Mapping[str, str],
) -> list[dict]:
    """Every tool schema Sage's loop offers, before per-turn routing.

    ``layer_enum`` maps unattached layer ids to labels; it fills the
    ``add_layer_to_map`` enum (dropped when empty).
    """
    tools_payload = [
        {
            "type": "function",
            "function": {
                "name": "new_layer_from_postgis",
                "strict": True,
                "description": "Creates a new layer, given a PostGIS connection and query, and adds it to the map so the user can see it. Layer will automatically pull data from PostGIS. Modify style using the set_layer_style tool.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "postgis_connection_id": {
                            "type": "string",
                            "description": "Unique PostGIS connection ID used as source",
                        },
                        "query": {
                            "type": "string",
                            "description": "SQL query to execute against PostGIS database for this layer, should list fetched columns for attributes that might be used for symbology (+ shape geometry). This query MUST alias the geometry column as 'geom' AND have a unique numeric id aliased as 'id'. Include newlines+spaces at ~55 column wrap",
                        },
                        "layer_name": {
                            "type": "string",
                            "description": "Sets a human-readable name for this layer. This name will appear in the layer list/legend for the user.",
                        },
                    },
                    "required": [
                        "postgis_connection_id",
                        "query",
                        "layer_name",
                    ],
                    "additionalProperties": False,
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "add_layer_to_map",
                "strict": True,
                "description": "Shows a newly created or existing unattached layer on the user's current map and layer list. Use this after a geoprocessing step that creates a layer, or if the user asks to see an existing layer that isn't currently on their map.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "layer_id": {
                            "type": "string",
                            "description": "The ID of the layer to add to the map. Choose from available unattached layers.",
                            "enum": list(layer_enum.keys())
                            if layer_enum
                            else ["NO_UNATTACHED_LAYERS"],
                        },
                        "new_name": {
                            "type": "string",
                            "description": "Sets a new human-readable name for this layer. This name will appear in the layer list/legend for the user.",
                        },
                    },
                    "required": ["layer_id", "new_name"],
                    "additionalProperties": False,
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "set_layer_style",
                "strict": True,
                "description": "Creates a new style for a layer with MapLibre JSON layers and immediately applies it as the active style",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "layer_id": {
                            "type": "string",
                            "description": "The ID of the layer to create and apply a style for",
                        },
                        "maplibre_json_layers_str": {
                            "type": "string",
                            "description": 'JSON string of MapLibre layer objects. Example: [{"id": "LZJ5RmuZr6qN-line", "type": "line", "source": "LZJ5RmuZr6qN", "paint": {"line-color": "#1E90FF"}}]',
                        },
                    },
                    "required": ["layer_id", "maplibre_json_layers_str"],
                    "additionalProperties": False,
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "reverse_geocode_coordinates",
                "strict": True,
                "description": "Given latitude and longitude, returns the Rwanda administrative divisions (province, district, sector, cell, village) that contain that point. Use this whenever the user provides coordinates and asks what location they correspond to.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "lat": {
                            "type": "number",
                            "description": "Latitude (e.g. -1.9403)",
                        },
                        "lon": {
                            "type": "number",
                            "description": "Longitude (e.g. 29.8739)",
                        },
                    },
                    "required": ["lat", "lon"],
                    "additionalProperties": False,
                },
            },
        },
    ]

    all_tools = get_tools()
    geoprocessing_names = {
        tool["function"]["name"] for tool in all_tools
    }
    # Generate schemas from Pydantic models only for tools NOT already
    # defined in tools.json (avoids duplicates and allows tools.json
    # tools to use Optional fields that strict schema generation rejects).
    for name, (fn, arg_model, _mundi_model) in pydantic_tool_calls.items():
        if name not in geoprocessing_names:
            tools_payload.append(tool_from_pyd(fn, arg_model))

    tools_payload.extend(all_tools)

    if not layer_enum:
        add_layer_tool = next(
            tool
            for tool in tools_payload
            if tool["function"]["name"] == "add_layer_to_map"
        )
        add_layer_tool["function"]["parameters"]["properties"][
            "layer_id"
        ].pop("enum", None)

    return apply_life_harness_tool_contracts(tools_payload)


@dataclass(frozen=True)
class SageTurnPlan:
    """What one turn sends to the model: prompt, tools and model override."""

    routing: RoutingDecision
    system_prompt: str
    tools: list[dict]
    model_override: str | None
    # How the tool list was shortlisted ("hybrid" / "bm25"), None if it wasn't.
    shortlist: str | None = None

    @property
    def tool_choice(self) -> str | None:
        return "auto" if self.tools else None


def plan_sage_turn(
    last_user_text: str,
    history: list[dict],
    tools_payload: list[dict],
    full_system_prompt: Callable[[], str],
) -> SageTurnPlan:
    """Apply Sage routing to one turn.

    Small talk drops the tools and swaps in a one-line prompt (optionally a
    smaller model). A confident intent filters the tools to its categories;
    an evidence decision can exclude tools; otherwise the full list is sent.
    ``full_system_prompt`` is only called when the turn is not small talk.
    """
    routing = route_chat(last_user_text, history=history)

    if routing.is_small_talk:
        return SageTurnPlan(
            routing=routing,
            system_prompt=SMALL_TALK_SYSTEM_PROMPT,
            tools=[],
            model_override=routing.primary_model_override or None,
        )

    system_prompt = apply_life_harness_system_prompt(full_system_prompt(), last_user_text)
    tools = tools_payload
    if routing.selected_categories:
        before = len(tools)
        tools = filter_tools_by_categories(
            tools,
            routing.selected_categories,
            excluded_tool_names=routing.excluded_tool_names,
        )
        logger.info(
            "sage_routing: filtered tools by %s (%d -> %d, excluded=%s)",
            routing.reason,
            before,
            len(tools),
            ",".join(sorted(routing.excluded_tool_names)) or "-",
        )
    elif routing.excluded_tool_names:
        before = len(tools)
        excluded = set(routing.excluded_tool_names)
        tools = [
            tool
            for tool in tools
            if tool.get("function", {}).get("name", "") not in excluded
        ]
        logger.info(
            "sage_routing: excluded tools by evidence decision (%d -> %d, excluded=%s)",
            before,
            len(tools),
            ",".join(sorted(routing.excluded_tool_names)),
        )
    else:
        # Always log the default-path decision so we can spot small-talk that's
        # slipping through the regex. Truncate to 60 chars to avoid leaking
        # long user input to logs.
        preview = last_user_text[:60].replace("\n", " ")
        logger.info(
            "sage_routing: default path (reason=%s, msg_len=%d, preview=%r)",
            routing.reason, len(last_user_text), preview,
        )
    return SageTurnPlan(
        routing=routing,
        system_prompt=system_prompt,
        tools=tools,
        model_override=None,
    )


# Tool-description embeddings, shared across turns in this process.
_TOOL_EMBEDDINGS = ToolEmbeddingCache()


def tool_shortlist_k() -> int:
    """SAGE_TOOL_SHORTLIST_K: tools kept per turn; 0 (default) keeps routing as-is."""
    raw = os.environ.get("SAGE_TOOL_SHORTLIST_K", "0").strip() or "0"
    try:
        k = int(raw)
    except ValueError as exc:
        raise ValueError(f"SAGE_TOOL_SHORTLIST_K must be an integer, got {raw!r}") from exc
    if k < 0:
        raise ValueError("SAGE_TOOL_SHORTLIST_K must be >= 0")
    return k


async def warm_tool_shortlist(full_tools: list[dict], *, embed: Embedder = embed_texts) -> None:
    """Embed the tool catalog now and wait for it. Live turns warm it in the
    background instead; batch jobs (the routing eval) call this first so
    their early cases are not scored on BM25 alone."""
    await _TOOL_EMBEDDINGS.vectors(full_tools, embed)


async def apply_tool_shortlist(
    plan: SageTurnPlan,
    last_user_text: str,
    history: list[dict],
    full_tools: list[dict],
    *,
    k: int,
    embed: Embedder | None = embed_texts,
) -> SageTurnPlan:
    """Replace the plan's tools with the ``k`` most relevant from the FULL
    catalog (the category filter can drop the right tool). Small-talk plans,
    which carry no tools, are returned unchanged."""
    if not plan.tools or k <= 0:
        return plan
    shortlist = await hybrid_shortlist(
        last_user_text, history, full_tools, k=k, embed=embed, cache=_TOOL_EMBEDDINGS,
    )
    logger.info(
        "sage_routing: tool shortlist %s (%d -> %d): %s",
        shortlist.method, len(full_tools), len(shortlist.tools),
        ",".join(t["function"]["name"] for t in shortlist.tools),
    )
    return replace(plan, tools=shortlist.tools, shortlist=shortlist.method)


# --- Abdication guard ---------------------------------------------------------
# On the first model call of a turn, a prose-only answer when tools were sent
# is often "abdication": the model describes what it would do instead of
# calling the tool. The guard retries once with a forced tool call over a
# narrowed list. It never fires on small talk, clarifying questions or
# explanation requests, where prose is the right answer.

# Per-minute provider rate limits (OpenRouter free models: 20 requests a
# minute) are worth waiting out; a daily cap is not.
RATE_LIMIT_RETRIES = 2
_RATE_LIMIT_DEFAULT_WAIT_S = 5.0
_RATE_LIMIT_MAX_WAIT_S = 15.0


def _header(headers: Any, name: str) -> str | None:
    if not headers:
        return None
    for key, value in dict(headers).items():
        if str(key).lower() == name:
            return str(value)
    return None


def rate_limit_retry_after(error: Exception) -> float | None:
    """Seconds to wait before retrying a call that hit a per-minute rate
    limit, or None when the error is not one (other errors, daily caps)."""
    if getattr(error, "status_code", None) != 429:
        return None
    text = str(error).lower()
    if "per-day" in text or "per_day" in text or "daily" in text:
        return None
    body = getattr(error, "body", None)
    body_headers = None
    if isinstance(body, Mapping):
        meta = body.get("metadata") or (body.get("error") or {}).get("metadata") or {}
        body_headers = meta.get("headers") if isinstance(meta, Mapping) else None
    response_headers = getattr(getattr(error, "response", None), "headers", None)
    wait: float | None = None
    for headers in (response_headers, body_headers):
        retry_after = _header(headers, "retry-after")
        reset_ms = _header(headers, "x-ratelimit-reset")
        try:
            if retry_after:
                wait = float(retry_after)
            elif reset_ms:
                wait = float(reset_ms) / 1000 - time.time()
        except ValueError:
            wait = None
        if wait is not None:
            break
    if wait is None:
        wait = _RATE_LIMIT_DEFAULT_WAIT_S
    return max(1.0, min(wait, _RATE_LIMIT_MAX_WAIT_S))


def rate_limit_user_message(error: Exception) -> str | None:
    """What to tell the user when a rate limit ends the turn, or None.

    A daily cap does not clear by retrying or by starting a new chat, so
    the generic connection-error advice would mislead."""
    if getattr(error, "status_code", None) != 429:
        return None
    text = str(error).lower()
    if "per-day" in text or "per_day" in text or "daily" in text:
        return (
            "Sage has used today's AI quota. It resets at 02:00 Kigali time "
            "(00:00 UTC); please try again after that."
        )
    return "Sage is receiving too many requests right now. Please try again in a minute."


GUARD_TOOL_COUNT = 5
# A clarifying question asks the user for missing input; keep it.
_CLARIFY_MAX_CHARS = 400
_EXPLAIN_REQUEST_RE = re.compile(
    r"(?i)^\s*(?:what\s+(?:is|are|does|do)\b|what'?s\s+(?:a|an|the)\b|explain\b|define\b|why\b|"
    r"how\s+does\b|meaning\s+of\b|difference\s+between\b|tell\s+me\s+about\s+(?:your|you)\b)"
)


def abdication_guard_enabled() -> bool:
    """SAGE_ABDICATION_GUARD: 1/true/yes turns the guard on (default off)."""
    return os.environ.get("SAGE_ABDICATION_GUARD", "0").strip().lower() in {"1", "true", "yes"}


def is_abdication(plan: SageTurnPlan, last_user_text: str, content: str | None, has_tool_calls: bool) -> bool:
    """True when a first-step answer is prose although the turn needs a tool."""
    if has_tool_calls or not plan.tools or plan.routing.is_small_talk:
        return False
    if _EXPLAIN_REQUEST_RE.search(last_user_text or ""):
        return False
    text = (content or "").strip()
    if text.endswith("?") and len(text) <= _CLARIFY_MAX_CHARS:
        return False
    return True


async def guard_tools(
    last_user_text: str,
    history: list[dict],
    full_tools: list[dict],
    *,
    k: int = GUARD_TOOL_COUNT,
    embed: Embedder | None = embed_texts,
) -> list[dict]:
    """The ``k`` best-ranked tools from the full catalog for the forced retry."""
    shortlist = await hybrid_shortlist(
        last_user_text, history, full_tools, k=k, embed=embed, cache=_TOOL_EMBEDDINGS,
    )
    return shortlist.tools
