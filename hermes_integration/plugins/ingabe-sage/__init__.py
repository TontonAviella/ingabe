"""Ingabe Sage plugin — registers all Sage tool schemas with Hermes.

Two tiers of tools:
  1. **Hand-wired tools** (search_location, ingabe_whoami) — fully functional,
     pure-IO, no mundi.ai in-process deps.
  2. **Generated stubs** — 60 GDAL/QGIS/raster tool schemas from
     mundi.ai/src/geoprocessing/tools.json. Schemas visible to Hermes so the
     LLM sees the full tool surface. Handlers return "not yet wired" until
     Phase 2 (in-process integration) lands.

`register(ctx)` is called once by Hermes's PluginManager.
"""
from __future__ import annotations

from .generated_tools import GENERATED_SCHEMAS, _make_stub_handler
from .tools import (
    SEARCH_LOCATION_SCHEMA,
    WHOAMI_SCHEMA,
    _handle_search_location,
    _handle_whoami,
)


def register(ctx) -> None:
    # --- Tier 1: hand-wired, fully functional tools -----------------------
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

    # --- Tier 2: generated schemas with stub handlers ---------------------
    # 60 GDAL/QGIS/raster tools from src/geoprocessing/tools.json. Phase 2 of
    # the Hermes migration replaces each stub with a real handler that imports
    # from mundi.ai's processing pipeline when Hermes runs in-process.
    HAND_WIRED = {"search_location", "ingabe_whoami"}
    for name, schema in GENERATED_SCHEMAS.items():
        if name in HAND_WIRED:
            continue  # don't shadow our real handlers
        ctx.register_tool(
            name=name,
            toolset="ingabe-sage-generated",
            schema=schema,
            handler=_make_stub_handler(name),
            emoji="🧰",
        )
