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
    # All schemas from generated_tools.py get a proxy handler. The real
    # dispatch happens in mundi-app, where partner-scoped DB access is set
    # up. PR #54 ships the gateway-side wiring; PR #55 wires the mundi-app
    # dispatch side so these tools start returning real results instead of
    # 503 ("upstream_unavailable").
    NATIVE = {"search_location", "ingabe_whoami"}
    for name, schema in GENERATED_SCHEMAS.items():
        if name in NATIVE:
            continue  # don't shadow our native handlers
        ctx.register_tool(
            name=name,
            toolset="ingabe-sage-proxied",
            schema=schema,
            handler=make_proxy_handler(name),
            emoji="🧰",
        )
