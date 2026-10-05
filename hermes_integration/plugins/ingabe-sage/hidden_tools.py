"""OpenAI-function-calling schemas for the 4 'hidden' Sage tools.

## Why this file exists

Sage's dispatch surface has three tiers:

  1. `src/dependencies/pydantic_tools.py` — Pydantic-validated tools, the
     modern path with type-checked args.
  2. `src/geoprocessing/tools.json` — schemas in OpenAI's function-calling
     format, mirrored into `generated_tools.py`.
  3. **Inline elif handlers in `src/routes/message_routes.py`** — historically
     hardcoded into the chat loop's tool dispatch but NEVER registered in
     either tools.json or the Pydantic registry. PR #57 ported these into
     `src/services/legacy_tool_shim.py` so `/internal/tool-call` can
     dispatch them, but the schemas were still missing from the plugin.

The 4 tools below live in tier 3. They are the most-used tools in production
(top-5 of the all-time tool-call leaderboard) but the LLM never saw them
through the Hermes path because the auto-generator that wrote
`generated_tools.py` never traversed `message_routes.py`'s inline elif chain.

Result before this file: when `MUNDI_USE_HERMES=1`, asking "show me Nyamagabe
on the map" produced a wall of reasoning text because Nemotron literally
could not pick `new_layer_from_postgis` or `add_layer_to_map` — they weren't
in its tool catalogue. It hallucinated tools that don't exist instead
("we have web search tool", "let's use the browser tool to look up Nyamagabe").

After this file: those names are advertised to the LLM with the same
shape as everything else in `GENERATED_SCHEMAS`, dispatched through the
same `make_proxy_handler` HMAC proxy, and executed by the same handlers
in `legacy_tool_shim.py`.

## Argument names must match the shim handlers exactly

Each schema below was authored after reading the corresponding `_handle_*`
function in `src/services/legacy_tool_shim.py`. The property keys map 1:1
to `ctx.arguments.get(...)` calls in the handler — see the explicit
references in each docstring.

If you add another hidden tool later, the discipline is:

  1. Add a handler to `legacy_tool_shim.py` + register it in `LEGACY_HANDLERS`.
  2. Write the schema here, naming properties identically to the
     `ctx.arguments.get(...)` keys.
  3. Add to `HIDDEN_SCHEMAS` below.
  4. Import + iterate alongside GENERATED_SCHEMAS in `__init__.py`.

## Description-writing rules

These descriptions are tuned against a real failure observed in prod logs:
the Nemotron-3-Super-120B reasoning model, on the OpenRouter `:free` tier,
likes to invent tool names from its training data ("web search tool",
"vision_analyze", "image_gen") rather than picking from the available
catalogue. To counter that:

  - Lead with the user-visible behaviour the tool produces (a new layer
    appears, a polygon shows on the map). Mechanism details come second.
  - Mention common user phrasings ("show me X", "click on the map", "what
    district is at...") so the LLM's semantic search picks the right tool.
  - Be explicit about ordering — e.g. `new_layer_from_postgis` MUST be
    followed by `add_layer_to_map`; `set_layer_style` is optional polish.
  - Pin enum / value constraints in the description, not just `required`,
    because reasoning models read prose better than they read JSON Schema
    metadata.
"""
from __future__ import annotations

from typing import Any, Dict


HIDDEN_SCHEMAS: Dict[str, Dict[str, Any]] = {
    # ──────────────────────────────────────────────────────────────────────
    # new_layer_from_postgis — #1 most-used Sage tool (54 calls all-time)
    # Handler: legacy_tool_shim.py:_handle_new_layer_from_postgis
    # Args read: postgis_connection_id, query, layer_name
    # ──────────────────────────────────────────────────────────────────────
    "new_layer_from_postgis": {
        "name": "new_layer_from_postgis",
        "description": (
            "Create a new map layer from a PostGIS table or SQL query. "
            "Use this whenever the user wants to SEE geographic data on the map: "
            "Rwanda districts, sectors, cells, villages, parcels, "
            "drought status, anomaly alerts, weather "
            "data joined to admin polygons, etc. "
            "Example user prompts that should trigger this tool: "
            "'show me Nyamagabe on the map', 'add the district boundaries', "
            "'visualise the eastern province', 'put Musanze on the map'. "
            "The query MUST return at least an 'id' column (integer or any "
            "type — it gets wrapped in ROW_NUMBER() if needed) and a 'geom' "
            "column (geometry/geography). "
            "This tool CREATES the layer but does NOT yet show it — you MUST "
            "call add_layer_to_map next with the returned layer_id so it "
            "appears in the user's sidebar. Optionally call set_layer_style "
            "afterwards for custom colours."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "postgis_connection_id": {
                    "type": "string",
                    "description": (
                        "12-character C-prefixed PostGIS connection ID. Other "
                        "Sage tools return one of these in their result's "
                        "'postgis_connection_id' field (e.g. get_anomaly_alerts, "
                        "get_yield_risk, get_drought_status). If you don't have one yet, call "
                        "one of those tools first to obtain a connection ID."
                    ),
                },
                "query": {
                    "type": "string",
                    "description": (
                        "SELECT statement against the connected PostGIS database. "
                        "MUST return an 'id' column (any type) and a 'geom' column "
                        "(PostGIS geometry/geography). "
                        "Common Rwanda tables: rwanda_district_boundaries (columns: "
                        "district, geom), rwanda_sector_boundaries (sector_name, "
                        "district_name, geom), rwanda_cell_boundaries (cell_name, "
                        "sector_name, district_name, geom), rwanda_village_boundaries. "
                        "Examples: "
                        "  SELECT ROW_NUMBER() OVER() AS id, district AS name, geom "
                        "  FROM rwanda_district_boundaries "
                        "  WHERE LOWER(district) = LOWER('Nyamagabe'); "
                        "  SELECT ROW_NUMBER() OVER() AS id, sector_name AS name, geom "
                        "  FROM rwanda_sector_boundaries "
                        "  WHERE district_name = 'Musanze';"
                    ),
                },
                "layer_name": {
                    "type": "string",
                    "description": (
                        "Human-readable name shown in the map's layer sidebar. "
                        "Examples: 'Nyamagabe District Boundary', 'Musanze Sectors', "
                        "'Eastern Province Districts'."
                    ),
                },
            },
            "required": ["postgis_connection_id", "query", "layer_name"],
        },
    },
    # ──────────────────────────────────────────────────────────────────────
    # add_layer_to_map — #4 most-used (42 calls). Companion to
    # new_layer_from_postgis. Always called as the second step.
    # Handler: legacy_tool_shim.py:_handle_add_layer_to_map
    # Args read: layer_id, new_name
    # ──────────────────────────────────────────────────────────────────────
    "add_layer_to_map": {
        "name": "add_layer_to_map",
        "description": (
            "Attach an existing layer (just created by new_layer_from_postgis "
            "or another layer-producing tool) to the user's current map so it "
            "appears in their sidebar and auto-zooms to its bounds. "
            "ALWAYS call this immediately after new_layer_from_postgis — without "
            "it the layer exists in the database but the user can't see it. "
            "Do NOT use this to add Rwanda raster tile layers (use "
            "add_land_cover_layer for that)."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "layer_id": {
                    "type": "string",
                    "description": (
                        "12-character L-prefixed layer ID. Comes from a previous "
                        "tool that created the layer (most commonly "
                        "new_layer_from_postgis — check its 'layer_id' return field)."
                    ),
                },
                "new_name": {
                    "type": "string",
                    "description": (
                        "Display name shown in the map's layer sidebar. Usually "
                        "matches the layer_name you passed to new_layer_from_postgis. "
                        "Examples: 'Nyamagabe District Boundary', 'Western Province "
                        "Sectors'."
                    ),
                },
            },
            "required": ["layer_id", "new_name"],
        },
    },
    # ──────────────────────────────────────────────────────────────────────
    # set_layer_style — #3 most-used (49 calls). Optional polish after
    # add_layer_to_map.
    # Handler: legacy_tool_shim.py:_handle_set_layer_style
    # Args read: layer_id, maplibre_json_layers_str
    # ──────────────────────────────────────────────────────────────────────
    "set_layer_style": {
        "name": "set_layer_style",
        "description": (
            "Apply custom MapLibre styling to a layer that's already on the map: "
            "fill colours, line colours and widths, choropleth ramps (e.g. "
            "drought severity → red ramp, NDVI → green ramp), 3D extrusion. "
            "Call this AFTER add_layer_to_map when the user wants specific "
            "visual styling (e.g. 'colour districts by NDVI', 'show high-risk "
            "areas in red'). Skip it if the default style is fine."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "layer_id": {
                    "type": "string",
                    "description": (
                        "12-character L-prefixed layer ID to restyle (must already "
                        "be on the user's current map)."
                    ),
                },
                "maplibre_json_layers_str": {
                    "type": "string",
                    "description": (
                        "JSON-stringified array of MapLibre layer specifications. "
                        "Must be a valid JSON STRING (not an object). Each element "
                        "is a MapLibre layer object with id, type, paint, AND a "
                        "REQUIRED `source` field that MUST equal the layer_id "
                        "passed in the layer_id argument above. Optional fields: "
                        "filter, source-layer. The handler rejects with "
                        "\"Layer source must be '<layer_id>'\" if source is "
                        "missing or wrong, so it is NOT optional — every layer "
                        "object in the array must carry it. "
                        "Example for layer_id='L5uovU1RdgC8': "
                        "'[{\"id\":\"fill\",\"type\":\"fill\","
                        "\"source\":\"L5uovU1RdgC8\","
                        "\"paint\":{\"fill-color\":\"#1e90ff\","
                        "\"fill-opacity\":0.5,\"fill-outline-color\":\"#000\"}},"
                        "{\"id\":\"outline\",\"type\":\"line\","
                        "\"source\":\"L5uovU1RdgC8\","
                        "\"paint\":{\"line-color\":\"#000\",\"line-width\":1}}]'."
                    ),
                },
            },
            "required": ["layer_id", "maplibre_json_layers_str"],
        },
    },
    # ──────────────────────────────────────────────────────────────────────
    # reverse_geocode_coordinates — lat/lon → Rwanda admin hierarchy.
    # Handler: legacy_tool_shim.py:_handle_reverse_geocode_coordinates
    # Args read: lat, lon
    # ──────────────────────────────────────────────────────────────────────
    "reverse_geocode_coordinates": {
        "name": "reverse_geocode_coordinates",
        "description": (
            "Resolve a (latitude, longitude) point in Rwanda to its full "
            "administrative hierarchy: village → cell → sector → district → "
            "province. Returns whichever levels actually contain the point — "
            "if the village table doesn't cover that area, you get back "
            "cell/sector/district/province. Use when the user clicks on the "
            "map, gives raw coordinates ('-2.6, 29.7'), or refers to a "
            "location by lat/lon and you need to know which Rwanda admin "
            "unit they mean. NOT a global geocoder — Rwanda only."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "lat": {
                    "type": "number",
                    "description": (
                        "Latitude in decimal degrees, WGS84. Rwanda spans roughly "
                        "-2.84 to -1.05 — values outside this range will return "
                        "'not_found'."
                    ),
                },
                "lon": {
                    "type": "number",
                    "description": (
                        "Longitude in decimal degrees, WGS84. Rwanda spans roughly "
                        "28.86 to 30.90 — values outside this range will return "
                        "'not_found'."
                    ),
                },
            },
            "required": ["lat", "lon"],
        },
    },
}


# Public for tests + introspection. Stable shape: identical to GENERATED_SCHEMAS
# so consumers can treat them as one combined registry.
__all__ = ["HIDDEN_SCHEMAS"]
