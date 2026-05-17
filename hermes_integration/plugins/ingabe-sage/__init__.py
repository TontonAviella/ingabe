"""Ingabe Sage plugin — registers Sage tool schemas with Hermes.

Two tiers of tools:
  1. **Native handlers** (search_location, ingabe_whoami) — fully functional,
     pure-IO, no dependency on mundi-app. Run inside the hermes-gateway
     container without crossing the network boundary.
  2. **Proxied handlers** — every other Sage tool. The plugin declares the
     schema; the handler signs an HMAC request to mundi-app's
     `/internal/tool-call` endpoint, which dispatches the real handler with
     RLS-scoped DB access. See proxy.py for the wire-level details.

`register(ctx)` is called once by Hermes's PluginManager at gateway start.
"""
from __future__ import annotations

from .generated_tools import GENERATED_SCHEMAS
from .hidden_tools import HIDDEN_SCHEMAS
from .proxy import make_proxy_handler
from .tools import (
    SEARCH_LOCATION_SCHEMA,
    WHOAMI_SCHEMA,
    _handle_search_location,
    _handle_whoami,
)


def register(ctx) -> None:
    # --- Tier 1: native handlers, no proxy hop required -------------------
    ctx.register_tool(
        name="search_location",
        toolset="ingabe-sage",
        schema=SEARCH_LOCATION_SCHEMA,
        handler=lambda args, **kw: _handle_search_location(
            query=args.get("query", ""),
            task_id=kw.get("task_id"),
        ),
        emoji="🗺️",
    )
    ctx.register_tool(
        name="ingabe_whoami",
        toolset="ingabe-sage",
        schema=WHOAMI_SCHEMA,
        handler=lambda args, **kw: _handle_whoami(task_id=kw.get("task_id")),
        emoji="🪪",
    )

    # --- Tier 2: proxied handlers (HMAC → mundi-app /internal/tool-call) --
    # All schemas from generated_tools.py + hidden_tools.py get a proxy
    # handler. The real dispatch happens in mundi-app, where partner-scoped
    # DB access is set up. PR #54 ships the gateway-side wiring; PR #55
    # wires the mundi-app dispatch side so these tools start returning real
    # results instead of 503 ("upstream_unavailable").
    #
    # hidden_tools.py exists because 7 of Sage's most-used tools
    # (new_layer_from_postgis #1, set_layer_style #3, add_layer_to_map #4,
    # query_postgis_database #5, query_duckdb_sql, reverse_geocode_coordinates,
    # zonal_statistics) live as inline elif handlers in message_routes.py and
    # were never registered in tools.json or the Pydantic registry. The
    # auto-generator that produces generated_tools.py only reads those two
    # sources, so those 7 schemas were silently missing from the LLM's tool
    # catalogue. Without them, asking "show me Nyamagabe on the map" via the
    # Hermes path produces a wall of reasoning text because the LLM can't see
    # the tools that would actually do the job. PR #57 added the
    # corresponding handlers in legacy_tool_shim.py so /internal/tool-call
    # already accepts these names — this file is the matching schema side.
    #
    # By construction the two registries are disjoint (hidden tools are not
    # in tools.json or Pydantic, generated tools are). The test
    # test_hidden_tools_disjoint_from_generated guards against accidental
    # overlap. If a name ever appears in both, GENERATED_SCHEMAS wins (it's
    # second in the dict merge below).
    NATIVE = {"search_location", "ingabe_whoami"}
    merged_schemas: dict = {**HIDDEN_SCHEMAS, **GENERATED_SCHEMAS}
    for name, schema in merged_schemas.items():
        if name in NATIVE:
            continue  # don't shadow our native handlers
        ctx.register_tool(
            name=name,
            toolset="ingabe-sage-proxied",
            schema=schema,
            handler=make_proxy_handler(name),
            emoji="🧰",
        )
