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

    Extracted from src/routes/message_routes.py:1977-2462 (the
    `elif function_name == "new_layer_from_postgis":` block). Behavior
    matches the hand-rolled loop's exactly: validates the SQL, checks
    EXPLAIN plan is read-only, auto-wraps queries with ROW_NUMBER() when
    `id` column isn't an integer (ST_AsMVT requires int id), computes
    feature_count + geometry_type + transformed bounds, generates default
    MapLibre style, inserts the layer + style + map_layer_styles rows,
    appends to user_mundiai_maps.layers, and kicks off PMTiles generation
    in the background.

    Args (from ctx.arguments):
      - postgis_connection_id: str (12-char C-prefixed)
      - query: str (must alias geometry column as `geom`)
      - layer_name: str (human-readable)

    Returns the tool_result dict in the same shape the chat loop's inline
    handler returns — status=success on the happy path, status=error
    otherwise. Never raises (caller depends on the no-raise invariant).
    """
    import asyncio
    import json
    import re
    from fastapi import HTTPException

    # Lazy imports — break the import-time circular dep with message_routes.
    # FastAPI has fully imported message_routes by the time any request hits
    # this function, so the modules are warm.
    from src.routes.message_routes import (
        validate_sql_query,
        check_postgis_readonly,
        _generate_postgis_pmtiles_background,
    )
    from src.utils import generate_id
    from src.symbology.llm import generate_maplibre_layers_for_layer_id
    from src.routes.websocket import kue_ephemeral_action
    from src.dependencies.postgres_connection import PostgresConnectionManager

    # Resolve arguments from the LLM call.
    postgis_connection_id = ctx.arguments.get("postgis_connection_id")
    raw_query = ctx.arguments.get("query")
    layer_name = ctx.arguments.get("layer_name")

    if not postgis_connection_id or not raw_query:
        return {
            "status": "error",
            "error": "Missing required parameters (postgis_connection_id or query).",
        }

    # Validate SQL safety BEFORE any f-string interpolation. Raises HTTPException
    # on dangerous patterns; we catch and return as a tool_result so the LLM
    # can apologize cleanly (HTTPException would 500 the dispatch route).
    try:
        query = validate_sql_query(raw_query)
    except HTTPException as e:
        return {
            "status": "error",
            "error": f"Query validation failed: {e.detail}",
        }

    # Verify the PostGIS connection exists and the caller has access. Mirrors
    # the chat loop's check at message_routes.py:1996. Falls back to
    # project-level access for shared internal connections (e.g.
    # CRwandaIntDB shared across users in the same project).
    connection_result = await ctx.conn.fetchrow(
        """
        SELECT connection_uri FROM project_postgres_connections
        WHERE id = $1 AND (user_id = $2 OR project_id = $3)
        AND soft_deleted_at IS NULL
        """,
        postgis_connection_id,
        ctx.user_id,
        ctx.project_id,
    )
    if not connection_result:
        return {
            "status": "error",
            "error": f"PostGIS connection '{postgis_connection_id}' not found or you do not have access to it.",
        }

    feature_count: Optional[int] = None
    bounds: Optional[list[float]] = None
    geometry_type: Optional[str] = None
    metadata_dict: Dict[str, Any] = {}
    attribute_names: list[str] = []

    # We open the dispatch's own connection_manager instance — same singleton
    # pattern as the chat loop uses. PostgresConnectionManager handles
    # connection pooling per connection_id.
    connection_manager = PostgresConnectionManager()

    async with kue_ephemeral_action(
        ctx.conversation_id, "Adding layer from PostGIS...", update_style_json=True
    ):
        try:
            pg = await connection_manager.connect_to_postgres(postgis_connection_id)
            try:
                # 1. Sanity-check the query via EXPLAIN. Catches typos AND
                #    blocks any ModifyTable plan nodes (read-only guarantee).
                explain_result = await pg.fetch(f"EXPLAIN (FORMAT JSON) {query}")
                query_plan = json.loads(explain_result[0]["QUERY PLAN"])
                check_postgis_readonly(query_plan[0]["Plan"])

                # 2. Get column types so we can verify the query exposes
                #    `geom` and detect whether `id` is an integer (ST_AsMVT
                #    requires integer id for tile rendering).
                prepared = await pg.prepare(f"SELECT * FROM ({query}) AS sub LIMIT 1")
                column_info = prepared.get_attributes()
                column_names = [a.name for a in column_info]
                if "geom" not in column_names:
                    raise ValueError("Query must return a column named 'geom'")

                # 3. Auto-wrap with ROW_NUMBER() if id missing or not integer.
                #    Mirrors message_routes.py:2061-2116. Without this,
                #    tile rendering would fail at runtime with
                #    "mvt_agg_transfn: Could not find column 'id' of integer type".
                _INT_OIDS = {21, 23, 20}  # int2, int4, int8
                id_attr = next((a for a in column_info if a.name == "id"), None)
                _id_oid = id_attr.type.oid if id_attr is not None else None
                if id_attr is None or _id_oid not in _INT_OIDS:
                    _SAFE_COL = re.compile(r"^[a-zA-Z_][a-zA-Z0-9_]{0,62}$")
                    inner_cols: list[str] = []
                    for _attr in column_info:
                        if not _SAFE_COL.match(_attr.name):
                            raise ValueError(
                                f"Unsafe column name in PostGIS query: {_attr.name!r}"
                            )
                        if _attr.name == "id":
                            inner_cols.append('_inner."id" AS id_original')
                        else:
                            inner_cols.append(f'_inner."{_attr.name}"')
                    query = (
                        "SELECT ROW_NUMBER() OVER()::bigint AS id, "
                        + ", ".join(inner_cols)
                        + f" FROM ({query}) _inner"
                    )
                    # Refresh column_info after the wrap.
                    prepared = await pg.prepare(
                        f"SELECT * FROM ({query}) AS sub LIMIT 1"
                    )
                    column_info = prepared.get_attributes()
                    column_names = [a.name for a in column_info]
                    logger.info(
                        "Auto-wrapped PostGIS layer query with ROW_NUMBER() id "
                        "(original id_oid=%s)", _id_oid,
                    )

                attribute_names = [
                    name for name in column_names if name not in ("geom", "id")
                ]

                # 4. Feature count + geometry type detection (for default styling).
                count_result = await pg.fetchval(
                    f"SELECT COUNT(*) FROM ({query}) AS sub"
                )
                feature_count = int(count_result) if count_result is not None else None

                geom_row = await pg.fetchrow(
                    f"""
                    SELECT ST_GeometryType(geom) as geom_type, COUNT(*) as count
                    FROM ({query}) AS sub WHERE geom IS NOT NULL
                    GROUP BY ST_GeometryType(geom)
                    ORDER BY count DESC LIMIT 1
                    """
                )
                if geom_row and geom_row["geom_type"]:
                    geometry_type = geom_row["geom_type"].replace("ST_", "").lower()

                    # 5. Bounds in WGS84, transforming from the source SRID if needed.
                    #    Treat SRID 0 as 4326 (most geospatial data without
                    #    explicit SRID is WGS84 in practice).
                    bounds_row = await pg.fetchrow(
                        f"""
                        WITH extent_data AS (
                            SELECT
                                ST_Extent(geom) as extent_geom,
                                COALESCE(NULLIF((SELECT ST_SRID(geom) FROM ({query}) AS sub2 WHERE geom IS NOT NULL LIMIT 1), 0), 4326) as original_srid
                            FROM ({query}) AS sub WHERE geom IS NOT NULL
                        )
                        SELECT
                            CASE WHEN original_srid = 4326 THEN ST_XMin(extent_geom)
                                 ELSE ST_XMin(ST_Transform(ST_SetSRID(extent_geom, original_srid), 4326)) END as xmin,
                            CASE WHEN original_srid = 4326 THEN ST_YMin(extent_geom)
                                 ELSE ST_YMin(ST_Transform(ST_SetSRID(extent_geom, original_srid), 4326)) END as ymin,
                            CASE WHEN original_srid = 4326 THEN ST_XMax(extent_geom)
                                 ELSE ST_XMax(ST_Transform(ST_SetSRID(extent_geom, original_srid), 4326)) END as xmax,
                            CASE WHEN original_srid = 4326 THEN ST_YMax(extent_geom)
                                 ELSE ST_YMax(ST_Transform(ST_SetSRID(extent_geom, original_srid), 4326)) END as ymax,
                            original_srid
                        FROM extent_data WHERE extent_geom IS NOT NULL
                        """
                    )
                    if bounds_row and all(
                        bounds_row[k] is not None for k in ("xmin", "ymin", "xmax", "ymax")
                    ):
                        bounds = [
                            float(bounds_row["xmin"]),
                            float(bounds_row["ymin"]),
                            float(bounds_row["xmax"]),
                            float(bounds_row["ymax"]),
                        ]
                        if bounds_row["original_srid"] is not None:
                            try:
                                metadata_dict["original_srid"] = int(bounds_row["original_srid"])
                            except (ValueError, TypeError):
                                pass

                # 6. Spatial-index advisory: scan EXPLAIN's referenced tables,
                #    record whether GIST indexes exist. Tile performance hint
                #    only, never blocks layer creation.
                try:
                    def _extract_tables(plan_node):
                        tables = set()
                        if "Relation Name" in plan_node:
                            tables.add(plan_node["Relation Name"])
                        for sub in plan_node.get("Plans", []):
                            tables.update(_extract_tables(sub))
                        return tables

                    referenced = _extract_tables(query_plan[0]["Plan"])
                    if referenced:
                        idx_count = await pg.fetchval(
                            """
                            SELECT COUNT(*) FROM pg_indexes
                            WHERE tablename = ANY($1::text[])
                            AND indexdef ILIKE '%gist%geom%'
                            """,
                            list(referenced),
                        )
                        if idx_count and idx_count > 0:
                            metadata_dict["has_spatial_index"] = True
                        else:
                            metadata_dict["spatial_index_warning"] = (
                                f"No GIST index on geometry column for tables: "
                                f"{', '.join(referenced)}. Tile performance may "
                                f"be degraded."
                            )
                except Exception as e:
                    logger.warning("Spatial index check failed: %s", e)
            finally:
                await pg.close()

            # 7. Generate a new layer ID + default MapLibre style.
            layer_id = generate_id(prefix="L")
            maplibre_layers = None
            if geometry_type:
                try:
                    maplibre_layers = generate_maplibre_layers_for_layer_id(
                        layer_id, geometry_type
                    )
                except Exception as e:
                    logger.warning(
                        "Failed to generate default style for PostGIS layer: %s", e
                    )

            # 8. Persist the layer + style rows.
            await ctx.conn.execute(
                """
                INSERT INTO map_layers
                (layer_id, owner_uuid, name, type, postgis_connection_id, postgis_query,
                 metadata, feature_count, bounds, geometry_type, source_map_id,
                 created_on, last_edited, postgis_attribute_column_list)
                VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11,
                        CURRENT_TIMESTAMP, CURRENT_TIMESTAMP, $12)
                """,
                layer_id,
                ctx.user_id,
                layer_name,
                "postgis",
                postgis_connection_id,
                query,
                json.dumps(metadata_dict),
                feature_count,
                bounds,
                geometry_type,
                ctx.map_id,
                attribute_names,
            )
            if maplibre_layers:
                style_id = generate_id(prefix="S")
                await ctx.conn.execute(
                    """
                    INSERT INTO layer_styles (style_id, layer_id, style_json, created_by, created_on)
                    VALUES ($1, $2, $3, $4, CURRENT_TIMESTAMP)
                    """,
                    style_id, layer_id, json.dumps(maplibre_layers), ctx.user_id,
                )
                await ctx.conn.execute(
                    """
                    INSERT INTO map_layer_styles (map_id, layer_id, style_id)
                    VALUES ($1, $2, $3)
                    """,
                    ctx.map_id, layer_id, style_id,
                )

            # 9. Attach to the map's layer list. `layers` may be NULL (not []),
            #    so we COALESCE-then-append.
            await ctx.conn.execute(
                """
                UPDATE user_mundiai_maps
                SET layers = CASE WHEN layers IS NULL THEN ARRAY[$1]
                                  ELSE array_append(layers, $1) END
                WHERE id = $2 AND (layers IS NULL OR NOT ($1 = ANY(layers)))
                """,
                layer_id, ctx.map_id,
            )

            # 10. Background PMTiles generation. The chat loop kicks this off
            #     as a fire-and-forget asyncio task — we do the same. If the
            #     PMTiles build is still running when the Hermes turn ends,
            #     that's fine, it persists across requests.
            if feature_count and feature_count > 0:
                asyncio.create_task(
                    _generate_postgis_pmtiles_background(
                        layer_id, postgis_connection_id, query,
                        feature_count, ctx.user_id, ctx.project_id,
                        conversation_id=ctx.conversation_id,
                    )
                )

            tool_result: Dict[str, Any] = {
                "status": "success",
                "message": (
                    f"PostGIS layer created successfully with ID: {layer_id} "
                    f"and added to map"
                ),
                "layer_id": layer_id,
                "query": query,
                "added_to_map": True,
            }
            if feature_count is not None:
                tool_result["feature_count"] = feature_count
            if geometry_type:
                tool_result["geometry_type"] = geometry_type
            if attribute_names:
                tool_result["attribute_columns"] = attribute_names
            if bounds and len(bounds) == 4:
                tool_result["bounds"] = bounds
        except HTTPException as e:
            tool_result = {
                "status": "error",
                "error": f"Failed to connect to PostGIS database: {e.detail}",
            }
        except Exception as e:
            tool_result = {
                "status": "error",
                "error": f"Query validation failed: {str(e)}",
            }

    # 11. Auto-zoom to the new layer (the chat loop does this in a separate
    #     ephemeral action — we keep that UX behavior here too).
    _bounds = tool_result.get("bounds")
    if _bounds and len(_bounds) == 4:
        async with kue_ephemeral_action(
            ctx.conversation_id, f"Zooming to {layer_name or 'layer'}...",
            bounds=_bounds,
        ):
            await asyncio.sleep(0.3)

    return tool_result


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
