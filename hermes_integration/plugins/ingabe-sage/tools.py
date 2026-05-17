"""Ingabe Sage tools.

Day 3: search_location (Nominatim) — pure HTTP, no context needed.
Day 4: whoami — demo tool that reads IngabeContext, proves context wiring.
Day 5+: 60 generated tool registrations from mundi.ai's pydantic_tools registry.
"""
from __future__ import annotations

import json
import logging
from typing import Any, Dict

import httpx

from .context import get_ingabe_context

logger = logging.getLogger(__name__)

# OpenAI function-calling schema — matches the shape Hermes expects.
SEARCH_LOCATION_SCHEMA: Dict[str, Any] = {
    "name": "search_location",
    "description": (
        "Geocode a place name to latitude/longitude using OpenStreetMap's "
        "Nominatim service. Use when the user asks 'where is X?' or you "
        "need coordinates for a place name. Returns the top result with "
        "display_name, lat, lon, and bounding box. For Rwandan place names "
        "(districts, sectors, cells), works best with full English names "
        "(e.g. 'Huye District', 'Kigali, Rwanda'). Returns JSON string with "
        "fields: display_name, lat, lon, bbox, place_type. Returns "
        "{\"error\": ...} on failure."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "query": {
                "type": "string",
                "description": (
                    "Place name to geocode. Examples: 'Kigali', 'Huye "
                    "District Rwanda', 'Lake Kivu', '-1.9536, 30.0606'."
                ),
            },
        },
        "required": ["query"],
    },
}


def _handle_search_location(query: str, task_id: str | None = None) -> str:
    """Synchronous Nominatim call. Returns JSON string per Hermes contract.

    Why sync (not async): Hermes tool handlers are called synchronously from
    `handle_function_call()` in run_agent.py. Async handlers require the
    plugin loader's `wrap_async_handler` helper or asyncio.run inline; sync
    + httpx.Client is simpler for the PoC. Day 4 will revisit when wiring
    Sage tools that are natively async.
    """
    try:
        with httpx.Client(timeout=15.0, follow_redirects=True) as client:
            r = client.get(
                "https://nominatim.openstreetmap.org/search",
                params={
                    "q": query,
                    "format": "json",
                    "limit": 1,
                    "addressdetails": 1,
                },
                headers={
                    "User-Agent": "Ingabe-Sage/0.1 (Hermes PoC; contact@nozalabs.rw)"
                },
            )
            r.raise_for_status()
            results = r.json()
    except Exception as exc:
        logger.exception("search_location: Nominatim call failed (task_id=%s)", task_id)
        return json.dumps({"error": f"Nominatim request failed: {exc}"})

    if not results:
        return json.dumps({"error": f"No results for query: {query!r}"})

    top = results[0]
    payload = {
        "display_name": top.get("display_name"),
        "lat": float(top["lat"]),
        "lon": float(top["lon"]),
        "bbox": [float(x) for x in top.get("boundingbox", [])],  # [s, n, w, e]
        "place_type": top.get("type"),
        "class": top.get("class"),
    }
    return json.dumps(payload)


# ===========================================================================
# whoami — Day 4 demo tool that proves IngabeContext wiring works
# ===========================================================================

WHOAMI_SCHEMA: Dict[str, Any] = {
    "name": "ingabe_whoami",
    "description": (
        "Diagnostic tool that returns the current Ingabe session context: "
        "which user is making the request, which map they have open, which "
        "project / partner they belong to. Use this when the user asks "
        "'who am I?', 'what am I working on?', or 'what's my current "
        "session state?'. Also useful as a quick proof that the agent has "
        "the right credentials wired. Returns JSON with fields: user_uuid, "
        "conversation_id, map_id, project_id, partner_id, context_source "
        "(contextvar | env-var | none)."
    ),
    "parameters": {"type": "object", "properties": {}, "required": []},
}


def _handle_whoami(task_id: str | None = None) -> str:
    """Pull whatever IngabeContext is set and report it back."""
    ctx = get_ingabe_context(required=False)
    if ctx is None:
        return json.dumps({
            "user_uuid": None,
            "context_source": "none",
            "note": (
                "No IngabeContext available. Either Hermes was invoked "
                "outside mundi.ai (no contextvar set) and no INGABE_* env "
                "vars are present. Tools that need user_uuid / map_id will "
                "fail until context is wired."
            ),
        })
    return json.dumps({
        "user_uuid": ctx.user_uuid,
        "conversation_id": ctx.conversation_id,
        "map_id": ctx.map_id,
        "project_id": ctx.project_id,
        "partner_id": ctx.partner_id,
        "context_source": "contextvar-or-env",
        "task_id": task_id,
    })
