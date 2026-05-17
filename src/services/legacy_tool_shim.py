"""Legacy tool shim — adapts the inline elif chain in message_routes.py so
mundi-app's `/internal/tool-call` endpoint can dispatch any Sage tool, not
just the modern Pydantic-registered ones.

## Why this exists

Sage's tool surface is ~82 tools. Only 28 are cleanly registered in
`src/dependencies/pydantic_tools.py` with proper args models + async handlers.
The other 53 are inline `elif function_name == "X":` blocks inside the chat
loop in `src/routes/message_routes.py:1170-5500ish`. Each of those blocks
depends on the surrounding chat-loop scope: `conn`, `user_id`,
`current_project_id`, `connection_manager`, `conversation.id`, `map_id`,
plus various helpers.

When `MUNDI_USE_HERMES=1`, the Hermes-side plugin issues an HMAC-signed
`/internal/tool-call` POST per tool dispatch (see PR #55). That endpoint
currently only routes to the Pydantic registry — so 53 of Sage's most-used
tools (every weather/NDVI/insurance/satellite/QGIS handler) return 404
"unknown tool" when invoked through Hermes. From the LLM's perspective the
tool exists; from the user's perspective Sage just can't do it.

This shim is the **bridge until full Pydantic migration**. It accepts the
same `(tool_name, arguments)` pair as the Pydantic path, synthesizes the
chat-loop scope (`LegacyToolContext`), and re-runs the appropriate inline
handler. Each handler is extracted one at a time into this file as a
sibling async function — the existing inline elif in `message_routes.py`
stays in place (so `MUNDI_USE_HERMES=0` keeps working) and calls the
extracted helper too. Single source of truth, two callers.

## How to migrate a handler from message_routes.py into here

1. Pick a handler. Easiest first: the small data-fetch ones with no nested
   scope (`reverse_geocode_coordinates`, `query_postgis_database`).
2. Read its inline block in message_routes.py. Identify every variable it
   reads from outer scope.
3. Add fields for those variables to `LegacyToolContext` (most are already
   here — `conn`, `partner_id`, `user_id`, `conversation_id`, `map_id`,
   `project_id`, `connection_manager`).
4. Write an `async def _handle_<tool_name>(ctx: LegacyToolContext, args:
   dict) -> dict` that runs the same logic against `ctx.X` instead of
   `outer_scope.X`.
5. Register it in `LEGACY_HANDLERS` at the bottom of this file.
6. Replace the inline elif block in message_routes.py with a call to the
   new helper. Both paths now share one implementation.

## What lives here vs in pydantic_tools.py

| Source                          | Where                              | Modernization |
|--------------------------------|------------------------------------|---------------|
| 28 modern Pydantic handlers     | `pydantic_tools.py` + `src/tools/` | Already clean |
| 53 legacy inline elif handlers  | `message_routes.py` + this shim    | Migrate one at a time |

The end state is: every legacy handler also has a Pydantic args model and a
clean async function signature, at which point we can collapse this shim
back into the Pydantic registry and delete it. Until then, this exists to
unblock the Hermes runtime swap.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Dict, Optional

import asyncpg


logger = logging.getLogger(__name__)


@dataclass
class LegacyToolContext:
    """Mirrors the chat-loop scope variables that inline elif handlers
    reference. Built once per /internal/tool-call request and passed
    through to whichever legacy handler is dispatched.

    Field naming matches the variable names used inside the elif chain so
    extracted handlers need minimal rewriting.
    """
    # Identity (already RLS-scoping the connection)
    user_id: str               # Clerk user uuid
    partner_id: str            # Clerk org uuid (sets app.partner_id GUC)

    # Conversation context
    conversation_id: int       # int because asyncpg expects int for PK column
    map_id: str                # 12-char L-prefixed ID, looked up from chat_completion_messages
    project_id: str            # 12-char P-prefixed ID, from conversations table

    # Active asyncpg connection with RLS GUCs already set. The shim's caller
    # opens this via `async_conn(user_id=..., partner_id=...)`; handlers
    # inherit it instead of re-opening (saves a round-trip + keeps the
    # transaction scope cohesive).
    conn: asyncpg.Connection

    # Tool arguments parsed from the /internal/tool-call payload. Each
    # handler reads what it needs from this dict (e.g. `arguments.get("query")`).
    arguments: Dict[str, Any] = field(default_factory=dict)


# Type for handler functions registered in LEGACY_HANDLERS.
LegacyHandlerFn = Callable[[LegacyToolContext], Awaitable[Dict[str, Any]]]


# ---------------------------------------------------------------------------
# Handler registry: tool_name → async fn.
# Each entry is one migrated handler. Grows as we extract from message_routes.py.
# ---------------------------------------------------------------------------


async def _handle_new_layer_from_postgis(
    ctx: LegacyToolContext,
) -> Dict[str, Any]:
    """Create a PostGIS-backed layer and attach it to the current map.

    Extracted from src/routes/message_routes.py:1977 (the
    `elif function_name == "new_layer_from_postgis":` block). The original
    block is ~400 lines deep with SRID/bounds/index-check logic. This
    function lifts that into a standalone caller-agnostic shape.

    Args (from ctx.arguments):
      - postgis_connection_id: str (12-char C-prefixed)
      - query: str (must alias geometry column as `geom`)
      - layer_name: str (human-readable)

    Returns the tool_result dict that the chat-loop assembles for the
    LLM — same shape as the inline elif's `tool_result` variable.
    """
    # TODO(legacy-shim): full extraction. This is the structural placeholder
    # so the dispatch path works end-to-end; the actual SRID/bounds/wrapping
    # logic from message_routes.py:1977-2300 needs to land in a follow-up.
    # When that lands, replace this stub with the real body.
    return {
        "status": "not_yet_extracted",
        "tool_name": "new_layer_from_postgis",
        "message": (
            "new_layer_from_postgis is registered in the legacy shim but its "
            "implementation has not yet been extracted from "
            "src/routes/message_routes.py:1977. Sage cannot create PostGIS "
            "layers via the Hermes path until that extraction lands. The "
            "hand-rolled path (MUNDI_USE_HERMES=0) still works fine."
        ),
    }


# Names of tools that have inline elif handlers in message_routes.py but
# haven't been extracted into this shim yet. Each gets a stub handler at
# module load (see below) so the whitelist in tool_call_routes.py accepts
# them and the LLM gets a structured "not yet extracted" response instead
# of a 404. As each is extracted, remove its name from this list and add
# a real `_handle_<name>` function + `LEGACY_HANDLERS[name] = ...` entry.
#
# Derived from message_routes.py's `elif function_name == "X":` chain.
# Verify with: `grep -E 'elif function_name == "[a-z_]+"' src/routes/message_routes.py`
_NOT_YET_EXTRACTED: list[str] = [
    # Map/layer plumbing (7 hardcoded — no schemas in tools.json or pydantic_tools.py)
    "set_layer_style",
    "add_layer_to_map",
    "query_postgis_database",
    "query_duckdb_sql",
    "zonal_statistics",
    "reverse_geocode_coordinates",
    # Satellite / NDVI / soil / agriculture (in tools.json, no Pydantic handler)
    "query_rwanda_zonal_stats",
    "search_satellite_imagery",
    "get_field_health",
    "create_management_zones",
    "create_prescription_map",
    "create_soil_sampling_plan",
    "identify_parcel_crop",
    "confirm_crop_prediction",
    "get_ndvi_stats",
    "get_cell_ndvi_stats",
    "get_soil_properties",
    "get_parcel_ndvi_stats",
    "get_agri_indices",
    "query_worldcover_stats",
    "get_crop_classifications",
    "get_anomaly_alerts",
    "get_yield_risk",
    "get_drought_status",
    "get_crop_growth_stage",
    "get_weather_stats",
    "get_forecast",
    "get_forecast_accuracy",
    "get_emissions_stats",
    "detect_dry_spells",
    "get_insurance_accuracy",
    "get_insurance_intelligence",
    "search_brain",
    "get_entity",
    "add_observation",
    "add_land_cover_layer",
    # QGIS-processing (all dispatch via the qgis-processing sidecar)
    "gdal_warpreproject",
    "native_aggregate",
    "native_buffer",
    "native_dissolve",
    "native_fieldcalculator",
    "native_fixgeometries",
    "native_geometrybyexpression",
    "native_joinattributesbylocation",
    "native_mergevectorlayers",
    "native_reprojectlayer",
    "native_creategrid",
    "native_zonalstatisticsfb",
    "qgis_clip",
    "qgis_intersection",
    "qgis_joinbylocationsummary",
    "qgis_statisticsbycategories",
]


def _make_not_yet_extracted_handler(tool_name: str) -> LegacyHandlerFn:
    """Return a closure that always reports the tool isn't extracted yet.

    Used to populate `LEGACY_HANDLERS` for tools whose inline elif blocks
    in message_routes.py haven't been lifted into this shim. The LLM
    pattern-matches on `status: not_yet_extracted` and apologizes to the
    user instead of hallucinating success.
    """
    async def _handler(ctx: LegacyToolContext) -> Dict[str, Any]:
        logger.info(
            "legacy_tool_shim: %s called via /internal/tool-call but not yet "
            "extracted from message_routes.py (partner=%s user=%s conv=%s)",
            tool_name, ctx.partner_id, ctx.user_id, ctx.conversation_id,
        )
        return {
            "status": "not_yet_extracted",
            "tool_name": tool_name,
            "message": (
                f"Tool {tool_name!r} is part of Sage's surface but its handler "
                f"has not yet been extracted from src/routes/message_routes.py "
                f"into the Hermes-callable shim. The hand-rolled chat loop "
                f"(MUNDI_USE_HERMES=0) handles it correctly. Roll back the "
                f"flag or wait for the migration PR."
            ),
        }
    return _handler


# Registry: tool name → handler function. Grows one entry per migrated tool.
# Currently: 1 real handler (new_layer_from_postgis, stub'd) + 52 not-yet-extracted
# stubs. Each not_yet_extracted stub returns a structured message instead of 404,
# so the LLM can pattern-match on status and apologize cleanly to the user.
LEGACY_HANDLERS: Dict[str, LegacyHandlerFn] = {
    "new_layer_from_postgis": _handle_new_layer_from_postgis,
}
for _name in _NOT_YET_EXTRACTED:
    LEGACY_HANDLERS[_name] = _make_not_yet_extracted_handler(_name)
del _name  # don't pollute module namespace


async def execute_legacy_tool(
    tool_name: str,
    ctx: LegacyToolContext,
) -> Dict[str, Any]:
    """Dispatch a legacy tool by name. Returns the tool_result dict.

    The /internal/tool-call route calls this when `tool_name` is not in
    `get_pydantic_tool_calls()`. Caller is responsible for setting RLS GUCs
    on ctx.conn before invoking — this function trusts them.

    If tool_name isn't registered (handler hasn't been extracted yet),
    returns an explicit not_yet_extracted result so the LLM gets a parseable
    response instead of a 404. The Hermes plugin's proxy will pass this
    through verbatim, and the LLM can apologize to the user.
    """
    handler = LEGACY_HANDLERS.get(tool_name)
    if handler is None:
        logger.info(
            "execute_legacy_tool: %s not yet migrated to shim (Hermes path); "
            "hand-rolled loop still dispatches it via message_routes.py",
            tool_name,
        )
        return {
            "status": "not_yet_extracted",
            "tool_name": tool_name,
            "message": (
                f"Tool {tool_name!r} is supported by Sage but its handler has "
                f"not yet been extracted into the Hermes-callable shim. The "
                f"hand-rolled chat loop (MUNDI_USE_HERMES=0) supports it; the "
                f"Hermes runtime does not."
            ),
        }
    return await handler(ctx)
