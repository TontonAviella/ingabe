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


async def _handle_add_layer_to_map(ctx: LegacyToolContext) -> Dict[str, Any]:
    """Attach an existing (unattached) layer to the current map, renaming it.

    Extracted from src/routes/message_routes.py:2463-2536. Sage calls this
    when the user wants to surface a layer that was created earlier (e.g.
    output of a previous geoprocessing tool) but isn't currently on the
    map. Verifies owner_uuid matches the caller so a holder of the
    HMAC gateway secret can't surface another partner's layers.

    Args (from ctx.arguments):
      - layer_id: str (must already exist in map_layers, owner_uuid = ctx.user_id)
      - new_name: str (sets the displayed legend label)

    Returns a tool_result dict; auto-zooms to the layer's bounds.
    """
    import asyncio
    import json  # noqa: F401 — kept for parity with message_routes.py shape

    from src.routes.websocket import kue_ephemeral_action

    layer_id_to_add = ctx.arguments.get("layer_id")
    new_name = ctx.arguments.get("new_name")
    if not layer_id_to_add or not new_name:
        return {
            "status": "error",
            "error": "Missing required parameters (layer_id or new_name).",
        }

    _layer_bounds = None
    tool_result: Dict[str, Any]
    async with kue_ephemeral_action(
        ctx.conversation_id, "Adding layer to map...", update_style_json=True,
    ):
        layer_exists = await ctx.conn.fetchrow(
            """
            SELECT layer_id, bounds FROM map_layers
            WHERE layer_id = $1 AND owner_uuid = $2
            """,
            layer_id_to_add, ctx.user_id,
        )
        if not layer_exists:
            tool_result = {
                "status": "error",
                "error": (
                    f"Layer ID '{layer_id_to_add}' not found or you do not "
                    f"have permission to use it."
                ),
            }
        else:
            await ctx.conn.execute(
                "UPDATE map_layers SET name = $1 WHERE layer_id = $2",
                new_name, layer_id_to_add,
            )
            await ctx.conn.execute(
                """
                UPDATE user_mundiai_maps
                SET layers = CASE WHEN layers IS NULL THEN ARRAY[$1]
                                  ELSE array_append(layers, $1) END
                WHERE id = $2 AND (layers IS NULL OR NOT ($1 = ANY(layers)))
                """,
                layer_id_to_add, ctx.map_id,
            )
            _layer_bounds = layer_exists["bounds"]
            tool_result = {
                "status": (
                    f"Layer '{new_name}' (ID: {layer_id_to_add}) added to "
                    f"map '{ctx.map_id}'."
                ),
                "layer_id": layer_id_to_add,
                "name": new_name,
            }
            if _layer_bounds and len(_layer_bounds) == 4:
                tool_result["bounds"] = list(_layer_bounds)
                tool_result["kue_instructions"] = (
                    f"Layer added. Call zoom_to_bounds with bounds "
                    f"{list(_layer_bounds)} so the user can see it."
                )

    # Auto-zoom to the newly added layer (separate ephemeral action so the
    # UI shows the "Zooming…" status distinct from "Adding…").
    if _layer_bounds and len(_layer_bounds) == 4:
        async with kue_ephemeral_action(
            ctx.conversation_id, f"Zooming to {new_name}...",
            bounds=list(_layer_bounds),
        ):
            await asyncio.sleep(0.3)

    return tool_result


async def _handle_set_layer_style(ctx: LegacyToolContext) -> Dict[str, Any]:
    """Apply a new MapLibre style to an existing layer.

    Extracted from src/routes/message_routes.py:2620-2690. Sage calls this
    after a geoprocessing result the user should SEE differently (drought
    severity → red ramp, NDVI → green ramp, etc.). Delegates the actual
    style-record creation to set_layer_style_route in layer_router.py so
    we share the same style insertion + map_layer_styles linkage logic.

    Args (from ctx.arguments):
      - layer_id: str
      - maplibre_json_layers_str: str (JSON-encoded array of MapLibre layer objects)

    Returns a tool_result dict with the new style_id on success.
    """
    import json
    from fastapi import HTTPException

    from src.database.models import MapLayer
    from src.routes.layer_router import (
        SetStyleRequest,
        set_layer_style as set_layer_style_route,
    )
    from src.routes.websocket import kue_ephemeral_action

    layer_id = ctx.arguments.get("layer_id")
    maplibre_json_layers_str = ctx.arguments.get("maplibre_json_layers_str")
    if not layer_id or not maplibre_json_layers_str:
        return {
            "status": "error",
            "error": "Missing required parameters (layer_id or maplibre_json_layers_str).",
        }

    try:
        layers = json.loads(maplibre_json_layers_str)
        layer_row = await ctx.conn.fetchrow(
            """
            SELECT * FROM map_layers
            WHERE layer_id = $1 AND owner_uuid = $2
            """,
            layer_id, ctx.user_id,
        )
        if not layer_row:
            raise HTTPException(404, f"Layer {layer_id} not found")
        layer = MapLayer(**dict(layer_row))

        async with kue_ephemeral_action(
            ctx.conversation_id, f"Styling layer {layer.name}...",
            update_style_json=True,
        ):
            style_response = await set_layer_style_route(
                request=SetStyleRequest(
                    maplibre_json_layers=layers,
                    map_id=ctx.map_id,
                ),
                layer=layer,
                user_id=ctx.user_id,
            )
        return {
            "status": "success",
            "style_id": style_response.style_id,
            "layer_id": style_response.layer_id,
            "message": (
                f"Style {style_response.style_id} created and applied to "
                f"layer {layer_id}"
            ),
        }
    except json.JSONDecodeError as e:
        return {
            "status": "error",
            "error": f"Invalid JSON format: {str(e)}",
            "layer_id": layer_id,
        }
    except HTTPException as e:
        return {
            "status": "error",
            "error": f"Failed to create and apply style: {e.detail}",
            "layer_id": layer_id,
        }
    except Exception as e:
        return {
            "status": "error",
            "error": f"Failed to create and apply style: {str(e)}",
            "layer_id": layer_id,
        }


async def _handle_query_duckdb_sql(ctx: LegacyToolContext) -> Dict[str, Any]:
    """Run a DuckDB SQL query against vector-layer attributes.

    Extracted from src/routes/message_routes.py:2537-2619. Sage uses this
    for tabular analysis on user-uploaded vector layers (FlatGeoBuf,
    GeoJSON, KML) — DuckDB loads each layer_id as a virtual table.

    Args (from ctx.arguments):
      - layer_ids: list[str] (only the FIRST layer_id is used; multi-layer
        joins inside DuckDB aren't supported by the underlying executor)
      - sql_query: str (DuckDB-flavored SELECT)
      - head_n_rows: int (default 20, used to truncate the result)

    Returns the tool_result dict in CSV-string form. 25,000-char ceiling
    on the result to keep token usage reasonable for the LLM.
    """
    import csv
    import io
    import json  # noqa: F401 — kept for parity with message_routes.py shape

    from fastapi import HTTPException

    from src.duckdb import execute_duckdb_query
    from src.routes.websocket import kue_ephemeral_action

    layer_ids = ctx.arguments.get("layer_ids") or []
    layer_id = layer_ids[0] if layer_ids else None
    sql_query = ctx.arguments.get("sql_query")
    head_n_rows = ctx.arguments.get("head_n_rows", 20)

    layer_exists = await ctx.conn.fetchrow(
        """
        SELECT layer_id FROM map_layers
        WHERE layer_id = $1 AND owner_uuid = $2
        """,
        layer_id, ctx.user_id,
    )
    if not layer_exists:
        return {
            "status": "error",
            "error": (
                f"Layer ID '{layer_id}' not found or you do not have "
                f"permission to access it."
            ),
        }

    try:
        async with kue_ephemeral_action(
            ctx.conversation_id, "Querying with SQL...", layer_id=layer_id,
        ):
            result = await execute_duckdb_query(
                sql_query=sql_query, layer_id=layer_id,
                max_n_rows=head_n_rows, timeout=30,
            )
        # CSV-format result so the LLM can read tabular data without parsing
        # JSON. Same format the chat loop emits.
        buf = io.StringIO()
        writer = csv.writer(buf)
        writer.writerow(result["headers"])
        writer.writerows(result["result"])
        result_text = buf.getvalue()
        if len(result_text) > 25000:
            return {
                "status": "error",
                "error": (
                    f"DuckDB CSV result too large: {len(result_text)} "
                    f"characters exceeds 25,000 character limit, try "
                    f"reducing columns or head_n_rows"
                ),
            }
        return {
            "status": "success",
            "result": result_text,
            "row_count": result["row_count"],
            "query": sql_query,
        }
    except HTTPException as e:
        return {"status": "error", "error": f"DuckDB query error: {e.detail}"}
    except Exception as e:
        return {"status": "error", "error": f"Error executing SQL query: {str(e)}"}


async def _handle_query_postgis_database(ctx: LegacyToolContext) -> Dict[str, Any]:
    """Run a PostGIS SQL query against a partner-connected database.

    Extracted from src/routes/message_routes.py:2691-2863. Sage uses this
    for ad-hoc data exploration (e.g. "how many districts in BK's
    portfolio", "what crops are in this season's data"). Hard-cap of
    LIMIT 1000 prevents accidental result-set flooding.

    Args (from ctx.arguments):
      - postgis_connection_id: str (12-char C-prefixed)
      - sql_query: str (must contain LIMIT clause, value <= 1000)

    Returns the tool_result dict in tab-separated text form (mirrors the
    chat loop's formatting). 25,000-char ceiling on the result.
    """
    import re
    import json  # noqa: F401 — parity with message_routes.py shape

    from fastapi import HTTPException

    from src.routes.message_routes import validate_sql_query
    from src.dependencies.postgres_connection import PostgresConnectionManager
    from src.routes.websocket import kue_ephemeral_action

    postgis_connection_id = ctx.arguments.get("postgis_connection_id")
    raw_query = ctx.arguments.get("sql_query")
    sql_query = raw_query

    # Validate before any execution. validate_sql_query raises HTTPException;
    # we catch + return so the LLM gets a parseable result rather than 500.
    if sql_query:
        try:
            sql_query = validate_sql_query(sql_query)
        except HTTPException as e:
            return {"status": "error", "error": f"Query validation failed: {e.detail}"}

    if not postgis_connection_id or not sql_query:
        return {
            "status": "error",
            "error": "Missing required parameters (postgis_connection_id or sql_query)",
        }

    # Owner / project access check, same fallback as new_layer_from_postgis.
    connection_result = await ctx.conn.fetchrow(
        """
        SELECT connection_uri FROM project_postgres_connections
        WHERE id = $1 AND (user_id = $2 OR project_id = $3)
        AND soft_deleted_at IS NULL
        """,
        postgis_connection_id, ctx.user_id, ctx.project_id,
    )
    if not connection_result:
        return {
            "status": "error",
            "error": (
                f"PostGIS connection '{postgis_connection_id}' not found or "
                f"you do not have access to it."
            ),
        }

    limited_query = sql_query.strip()
    limit_match = re.search(r"\bLIMIT\s+(\d+)\b", limited_query, re.IGNORECASE)
    if not limit_match:
        return {
            "status": "error",
            "error": "Query must include a LIMIT clause with a value less than 1000",
        }
    if int(limit_match.group(1)) > 1000:
        return {
            "status": "error",
            "error": (
                f"LIMIT value {int(limit_match.group(1))} exceeds maximum "
                f"allowed limit of 1000"
            ),
        }

    connection_manager = PostgresConnectionManager()
    try:
        async with kue_ephemeral_action(
            ctx.conversation_id, "Querying PostgreSQL database...",
        ):
            postgres_conn = await connection_manager.connect_to_postgres(
                postgis_connection_id
            )
            try:
                rows = await postgres_conn.fetch(limited_query)
                if not rows:
                    return {
                        "status": "success",
                        "message": "Query executed successfully but returned no rows",
                        "row_count": 0,
                        "query": limited_query,
                    }
                result_data = [dict(row) for row in rows]
                # Format: single-value, or tab-separated table.
                if len(result_data) == 1 and len(result_data[0]) == 1:
                    single_value = next(iter(result_data[0].values()))
                    result_text = f"Query result: {single_value}"
                else:
                    headers = list(result_data[0].keys())
                    lines = ["\t".join(headers)]
                    for row in result_data:
                        lines.append("\t".join(str(row.get(h, "")) for h in headers))
                    result_text = "\n".join(lines)
                if len(result_text) > 25000:
                    return {
                        "status": "error",
                        "error": (
                            f"Query result too large: {len(result_text)} "
                            f"characters exceeds 25,000 character limit. Try "
                            f"reducing the number of columns or rows."
                        ),
                    }
                return {
                    "status": "success",
                    "result": result_text,
                    "row_count": len(result_data),
                    "query": limited_query,
                }
            finally:
                await postgres_conn.close()
    except HTTPException as e:
        return {
            "status": "error",
            "error": f"Failed to connect to PostGIS database: {e.detail}",
        }
    except Exception as e:
        return {
            "status": "error",
            "error": f"PostgreSQL query error: {str(e)}",
            "query": limited_query,
        }


async def _handle_zonal_statistics(ctx: LegacyToolContext) -> Dict[str, Any]:
    """Compute zonal statistics (mean, sum, min, max, count, stdev) for raster
    values within polygon boundaries.

    Extracted from src/routes/message_routes.py:2865-2942. Delegates the
    actual raster-over-polygon math to compute_zonal_statistics in
    src/geoprocessing/zonal_stats.py. This handler just owns the
    ownership-checks-then-dispatch pattern.

    Args (from ctx.arguments):
      - raster_layer_id: str
      - zones_layer_id: str
      - stats: list[str] (optional; defaults to mean/sum/min/max/count/stdev)
    """
    from fastapi import HTTPException

    from src.routes.websocket import kue_ephemeral_action

    raster_layer_id = ctx.arguments.get("raster_layer_id")
    zones_layer_id = ctx.arguments.get("zones_layer_id")
    stats = ctx.arguments.get("stats")

    if not raster_layer_id or not zones_layer_id:
        return {
            "status": "error",
            "error": "Missing required parameters (raster_layer_id or zones_layer_id).",
        }

    raster_exists = await ctx.conn.fetchrow(
        "SELECT layer_id, type FROM map_layers WHERE layer_id = $1 AND owner_uuid = $2",
        raster_layer_id, ctx.user_id,
    )
    zones_exists = await ctx.conn.fetchrow(
        "SELECT layer_id, type FROM map_layers WHERE layer_id = $1 AND owner_uuid = $2",
        zones_layer_id, ctx.user_id,
    )
    if not raster_exists:
        return {
            "status": "error",
            "error": (
                f"Raster layer '{raster_layer_id}' not found or you do not "
                f"have access to it."
            ),
        }
    if not zones_exists:
        return {
            "status": "error",
            "error": (
                f"Zones layer '{zones_layer_id}' not found or you do not "
                f"have access to it."
            ),
        }

    try:
        async with kue_ephemeral_action(
            ctx.conversation_id, "Computing zonal statistics...",
        ):
            # Local import: avoid GDAL/rasterio at module load if the shim
            # is imported in a context that doesn't need this tool.
            from src.geoprocessing.zonal_stats import compute_zonal_statistics

            return await compute_zonal_statistics(
                raster_layer_id=raster_layer_id,
                zones_layer_id=zones_layer_id,
                stats=stats,
                timeout=30,
            )
    except HTTPException as e:
        return {"status": "error", "error": f"Zonal statistics error: {e.detail}"}
    except Exception as e:
        logger.exception(
            "Error computing zonal statistics for raster=%s, zones=%s",
            raster_layer_id, zones_layer_id,
        )
        return {
            "status": "error",
            "error": f"Failed to compute zonal statistics: {str(e)}",
        }


# Province-to-district mapping (stable since the 2006 administrative reform).
# Lifted verbatim from message_routes.py:6061-6074 — keeping the same list so
# the shim and the hand-rolled loop return identical province values.
_RWANDA_DISTRICT_TO_PROVINCE: Dict[str, str] = {
    # Kigali City (3 districts)
    "Gasabo": "Kigali City", "Kicukiro": "Kigali City", "Nyarugenge": "Kigali City",
    # Northern (5)
    "Burera": "Northern", "Gakenke": "Northern", "Gicumbi": "Northern",
    "Musanze": "Northern", "Rulindo": "Northern",
    # Southern (8)
    "Gisagara": "Southern", "Huye": "Southern", "Kamonyi": "Southern",
    "Muhanga": "Southern", "Nyamagabe": "Southern", "Nyanza": "Southern",
    "Nyaruguru": "Southern", "Ruhango": "Southern",
    # Eastern (7)
    "Bugesera": "Eastern", "Gatsibo": "Eastern", "Kayonza": "Eastern",
    "Kirehe": "Eastern", "Ngoma": "Eastern", "Nyagatare": "Eastern",
    "Rwamagana": "Eastern",
    # Western (7)
    "Karongi": "Western", "Ngororero": "Western", "Nyabihu": "Western",
    "Nyamasheke": "Western", "Rubavu": "Western", "Rusizi": "Western",
    "Rutsiro": "Western",
}


async def _handle_reverse_geocode_coordinates(ctx: LegacyToolContext) -> Dict[str, Any]:
    """Resolve lat/lon to Rwanda admin hierarchy (province → district → sector
    → cell → village).

    Extracted from src/routes/message_routes.py:6053-6171. Cascades through
    boundary tables in order of specificity (most-precise village first,
    fall back to coarser admin levels). Province is derived from district
    via a hardcoded table since rwanda_district_boundaries doesn't store
    province directly.

    Args (from ctx.arguments):
      - lat: float
      - lon: float

    Returns hierarchy with whatever level was the most specific match.
    Returns status=not_found if the point is outside Rwanda.
    """
    import os
    import asyncpg

    lat = ctx.arguments.get("lat")
    lon = ctx.arguments.get("lon")
    if lat is None or lon is None:
        return {"status": "error", "error": "lat and lon are required"}

    # Open a fresh asyncpg connection — same pattern as the inline handler.
    # Future cleanup: consider using ctx.conn (mundi's mundiuser conn already
    # has the right credentials) once we verify the rwanda_*_boundaries
    # tables don't have RLS that filters on session GUCs.
    try:
        pg = await asyncpg.connect(
            host=os.environ.get("POSTGRES_HOST", "postgresdb"),
            port=int(os.environ.get("POSTGRES_PORT", "5432")),
            database=os.environ.get("POSTGRES_DB", "mundidb"),
            user=os.environ.get("POSTGRES_USER", "mundiuser"),
            password=os.environ.get("POSTGRES_PASSWORD", "gdalpassword"),
        )
    except Exception as e:
        logger.exception("reverse_geocode_coordinates: connection failed")
        return {"status": "error", "error": str(e)}

    result: Dict[str, Any] = {
        "province": None, "district": None,
        "sector": None, "cell": None, "village": None,
    }
    try:
        # Cascade: village → cell → sector → district. Stop at the most
        # specific match.
        row = await pg.fetchrow(
            "SELECT village_name, cell_name, sector_name, district_name "
            "FROM rwanda_village_boundaries "
            "WHERE ST_Contains(geom, ST_SetSRID(ST_Point($1, $2), 4326)) "
            "LIMIT 1",
            float(lon), float(lat),
        )
        if row:
            result["village"] = row["village_name"]
            result["cell"] = row["cell_name"]
            result["sector"] = row["sector_name"]
            result["district"] = row["district_name"]
        else:
            row = await pg.fetchrow(
                "SELECT cell_name, sector_name, district_name "
                "FROM rwanda_cell_boundaries "
                "WHERE ST_Contains(geom, ST_SetSRID(ST_Point($1, $2), 4326)) "
                "LIMIT 1",
                float(lon), float(lat),
            )
            if row:
                result["cell"] = row["cell_name"]
                result["sector"] = row["sector_name"]
                result["district"] = row["district_name"]
            else:
                row = await pg.fetchrow(
                    "SELECT sector_name, district_name "
                    "FROM rwanda_sector_boundaries "
                    "WHERE ST_Contains(geom, ST_SetSRID(ST_Point($1, $2), 4326)) "
                    "LIMIT 1",
                    float(lon), float(lat),
                )
                if row:
                    result["sector"] = row["sector_name"]
                    result["district"] = row["district_name"]
                else:
                    row = await pg.fetchrow(
                        "SELECT district FROM rwanda_district_boundaries "
                        "WHERE ST_Contains(geom, ST_SetSRID(ST_Point($1, $2), 4326)) "
                        "LIMIT 1",
                        float(lon), float(lat),
                    )
                    if row:
                        result["district"] = row["district"]

        # Derive province from the district name table lookup.
        if result["district"]:
            result["province"] = _RWANDA_DISTRICT_TO_PROVINCE.get(result["district"])

        if result["district"]:
            return {
                "status": "success",
                "coordinates": {"lat": lat, "lon": lon},
                **result,
            }
        return {
            "status": "not_found",
            "error": f"Coordinates ({lat}, {lon}) are not within Rwanda boundaries.",
            "coordinates": {"lat": lat, "lon": lon},
        }
    except Exception as e:
        logger.exception("reverse_geocode_coordinates failed")
        return {"status": "error", "error": str(e)}
    finally:
        await pg.close()


async def _handle_get_forecast(ctx: LegacyToolContext) -> Dict[str, Any]:
    """Multi-model weather forecast (ECMWF + GFS + ICON + GraphCast) for a
    Rwanda location.

    Extracted from src/routes/message_routes.py:5301-5353. Delegates the
    actual model fusion to `get_farm_forecast`. Adds a district→centroid
    convenience lookup so Sage can say "forecast for Bugesera" without
    knowing coordinates. Defaults to Kigali if everything is missing.

    Args (from ctx.arguments):
      - latitude / longitude: float (optional if district given)
      - district: str (optional; resolved to centroid via rwanda_district_boundaries)
      - forecast_days: int (1-16, default 10)
    """
    import asyncio as _aio

    try:
        from src.services.forecast_service import get_farm_forecast

        lat = ctx.arguments.get("latitude")
        lon = ctx.arguments.get("longitude")
        district = ctx.arguments.get("district")
        days = min(max(1, ctx.arguments.get("forecast_days", 10)), 16)

        # District → centroid fallback if no explicit lat/lon.
        if district and (lat is None or lon is None):
            try:
                row = await ctx.conn.fetchrow(
                    "SELECT round(ST_Y(ST_Centroid(geom))::numeric, 4) as lat, "
                    "round(ST_X(ST_Centroid(geom))::numeric, 4) as lon "
                    "FROM rwanda_district_boundaries "
                    "WHERE district ILIKE $1 LIMIT 1",
                    district,
                )
                if row:
                    lat = float(row["lat"])
                    lon = float(row["lon"])
            except Exception:
                pass  # Centroid lookup is best-effort; fall through to Kigali default.

        # Final fallback: center of Kigali.
        if lat is None:
            lat = -1.9403
        if lon is None:
            lon = 29.8739

        # `get_farm_forecast` is sync (calls weather model HTTP APIs serially),
        # so we offload to the default executor so the event loop can serve
        # other requests during the multi-second fetch.
        result = await _aio.get_event_loop().run_in_executor(
            None,
            lambda: get_farm_forecast(lat, lon, forecast_days=days),
        )
        return {"status": "success", **result}
    except Exception as e:
        logger.exception("get_forecast tool failed")
        return {"status": "error", "error": str(e)}


async def _handle_detect_dry_spells(ctx: LegacyToolContext) -> Dict[str, Any]:
    """Detect historical dry spells (consecutive days under a rainfall
    threshold) from AgERA5 observed weather data.

    Extracted from src/routes/message_routes.py:5486-5507. Pure delegation
    to `detect_dry_spells` in src.services.weather_accuracy — same shape
    as the inline handler. Defaults match the chat loop (2mm/day threshold,
    10-day minimum duration).

    Args (from ctx.arguments):
      - district: str (optional, filters to one district)
      - date_from / date_to: ISO date strings
      - threshold_mm: float (default 2.0)
      - min_duration_days: int (default 10)
    """
    try:
        from src.services.weather_accuracy import detect_dry_spells as _detect_ds
        return await _detect_ds(
            ctx.conn,
            district=ctx.arguments.get("district"),
            date_from=ctx.arguments.get("date_from"),
            date_to=ctx.arguments.get("date_to"),
            threshold_mm=float(ctx.arguments.get("threshold_mm", 2.0)),
            min_duration_days=int(ctx.arguments.get("min_duration_days", 10)),
        )
    except Exception as e:
        logger.exception("detect_dry_spells tool failed")
        return {"status": "error", "error": str(e)}


def _build_insurance_briefing(data: Dict[str, Any], fired: list) -> str:
    """Render the insurance-intelligence dict into a natural-language briefing.

    Extracted unchanged from src/routes/message_routes.py:5559-5657. Sage's
    LLM consumes the briefing string as a pre-digested summary so it doesn't
    have to interpret raw SPI/NDVI/ET numbers itself. Kept verbatim for
    output parity with the hand-rolled path.
    """
    loc = data.get("location", "?")
    season = data.get("season", "?")
    phase = data.get("growth_phase", "?")
    dap = data.get("days_after_planting", "?")
    status_ = data.get("overall_status", "?")
    confidence = data.get("confidence_score", "?")
    rain = data.get("season_rainfall_mm", "?")
    spi = data.get("spi")

    spi_str = ""
    if spi is not None:
        if spi <= -2.0:
            spi_str = f"SPI is {spi} — this is an extreme drought signal, meaning rainfall is far below what's normal for this time of year"
        elif spi <= -1.5:
            spi_str = f"SPI is {spi} — severe drought conditions, significantly less rain than expected"
        elif spi <= -1.0:
            spi_str = f"SPI is {spi} — moderate drought, rainfall noticeably below normal"
        elif spi >= 1.0:
            spi_str = f"SPI is {spi} — wetter than normal conditions"
        else:
            spi_str = f"SPI is {spi} — within normal range"

    ndvi = data.get("ndvi_z_score")
    ndvi_str = ""
    if ndvi is not None:
        if ndvi >= 1.0:
            ndvi_str = f"NDVI z-score {ndvi} — vegetation is thriving, well above average greenness for this area and time of year"
        elif ndvi >= 0.3:
            ndvi_str = f"NDVI z-score {ndvi} — vegetation looks healthy, slightly above average"
        elif ndvi >= -0.5:
            ndvi_str = f"NDVI z-score {ndvi} — vegetation is about normal"
        elif ndvi >= -1.0:
            ndvi_str = f"NDVI z-score {ndvi} — vegetation is showing stress, below average greenness"
        else:
            ndvi_str = f"NDVI z-score {ndvi} — vegetation is in poor condition, significantly less green than normal"

    sm = data.get("soil_moisture_pct")
    sm_str = ""
    if sm is not None:
        if sm >= 80:
            sm_str = f"Soil moisture at {sm}% — the ground is well saturated, plenty of water available to roots"
        elif sm >= 50:
            sm_str = f"Soil moisture at {sm}% — adequate water in the soil"
        elif sm >= 30:
            sm_str = f"Soil moisture at {sm}% — getting low, plants may start feeling water stress"
        else:
            sm_str = f"Soil moisture at {sm}% — very dry soil, crops are likely under water stress"

    et = data.get("et_anomaly_pct")
    et_str = ""
    if et is not None:
        if et < -20:
            et_str = f"ET anomaly {et}% — plants are transpiring much less than normal, a sign of water stress or poor crop condition"
        elif et < -5:
            et_str = f"ET anomaly {et}% — slightly reduced water uptake by plants"
        elif et > 10:
            et_str = f"ET anomaly +{et}% — plants are actively growing and using more water than usual"
        else:
            et_str = f"ET anomaly {et}% — normal plant water use"

    dry = data.get("max_dry_spell_days")
    dry_str = ""
    if dry is not None:
        if dry >= 15:
            dry_str = f"Longest dry spell: {dry} consecutive days without rain — this is damaging, especially during flowering"
        elif dry >= 10:
            dry_str = f"Longest dry spell: {dry} days — worth watching but not yet critical"
        elif dry > 0:
            dry_str = f"Longest dry spell: {dry} days — not a concern"

    drought_diag = data.get("drought_diagnostic_label", "")

    fired_str = ""
    if fired:
        parts = [
            f"{t['signal']}: current {t['current_value']} crossed the {t['threshold']} threshold"
            for t in fired
        ]
        fired_str = "TRIGGERED ALERTS: " + "; ".join(parts)

    briefing_parts = [
        f"Location: {loc}, Season {season}, currently in {phase} (day {dap}). Overall status: {status_} (confidence {confidence}/100).",
        f"Rainfall this season: {rain}mm so far. {spi_str}.",
    ]
    if ndvi_str:
        briefing_parts.append(ndvi_str + ".")
    if sm_str:
        briefing_parts.append(sm_str + ".")
    if et_str:
        briefing_parts.append(et_str + ".")
    if dry_str:
        briefing_parts.append(dry_str + ".")
    if drought_diag:
        briefing_parts.append(f"Drought assessment: {drought_diag}.")
    if fired_str:
        briefing_parts.append(fired_str)
    return " ".join(briefing_parts)


async def _handle_get_insurance_intelligence(ctx: LegacyToolContext) -> Dict[str, Any]:
    """Comprehensive agricultural situation report for a Rwanda location.

    Extracted from src/routes/message_routes.py:5530-5756. The flagship
    Sage tool for BK Insurance underwriters. Calls the insurance engine
    to compute the multi-signal status (SPI, NDVI, soil moisture, ET,
    dry spells, parametric triggers), renders the result into a natural-
    language briefing (via _build_insurance_briefing), and saves the
    full report to the Brain knowledge graph as a page + timeline entry
    for audit trail / future retrieval.

    Args (from ctx.arguments):
      - crop, season, district, sector, cell, village: location/crop scope
      - audience: 'agronomist' | 'underwriter' | 'farmer'
      - compare_level: if set, returns comparison mode across multiple areas
    """
    import json as _json
    from datetime import date as _date_cls

    try:
        from src.services.insurance_engine import compute_insurance_intelligence

        compare_level = ctx.arguments.get("compare_level")
        result = await compute_insurance_intelligence(
            ctx.conn,
            crop=ctx.arguments.get("crop", ""),
            season=ctx.arguments.get("season"),
            district=ctx.arguments.get("district"),
            sector=ctx.arguments.get("sector"),
            cell=ctx.arguments.get("cell"),
            village=ctx.arguments.get("village"),
            audience=ctx.arguments.get("audience", "agronomist"),
            compare_level=compare_level,
        )

        # Comparison mode: light formatting hint for the LLM and we're done.
        if result.get("mode") == "comparison" and result.get("status") == "ok":
            result["instruction"] = (
                "Present the comparison naturally. Highlight which areas stand out "
                "(wettest, driest, best NDVI, worst soil moisture, etc). "
                "Use a short table if >3 areas, otherwise describe in sentences. "
                "Mention the most interesting contrasts — don't list every number for every area. "
                "End with sources in parentheses."
            )
            result.pop("geometry", None)
            return result

        # Single-location mode: build the briefing + persist to Brain.
        if result.get("status") == "ok":
            result["_report_for_brain"] = result.pop("report", "")
            data = result.get("data", {})
            triggers = data.get("triggers", [])
            fired = [t for t in triggers if t.get("triggered")]

            result["situation"] = _build_insurance_briefing(data, fired)

            if fired:
                result["triggers_fired"] = "TRIGGERED ALERTS: " + "; ".join(
                    f"{t['signal']}: current {t['current_value']} crossed the {t['threshold']} threshold"
                    for t in fired
                )
            result["triggers_total"] = (
                f"{data.get('triggers_activated', 0)} of "
                f"{data.get('triggers_total', 0)} thresholds crossed"
            )
            result["recommendation"] = data.get("recommendation", "")
            result["sources"] = data.get("sources", [])
            result["period"] = f"{data.get('period_start', '')} to {data.get('period_end', '')}"

            # Forecast narrative (multi-model consensus on payout risk).
            fo = data.get("forecast_outlook")
            if fo:
                fo_risk = fo.get("rainfall_trigger_risk", "?")
                fo_prob = round(fo.get("rainfall_trigger_probability", 0) * 100)
                fo_total = fo.get("projected_season_total_mm", "?")
                fo_thresh = fo.get("rainfall_trigger_threshold_mm", "?")
                fo_models = ", ".join(fo.get("models_used", []))
                agreement = fo.get("model_agreement")
                if agreement == "HIGH":
                    agreement_str = "The models agree closely on this."
                elif agreement == "LOW":
                    agreement_str = "There is some disagreement between models, so uncertainty is higher."
                else:
                    agreement_str = "Models are in moderate agreement."
                result["forecast"] = (
                    f"Looking ahead: 4 weather models ({fo_models}) project the season "
                    f"will end with about {fo_total}mm total rainfall "
                    f"(range {fo.get('projected_season_p10_mm', '?')}-"
                    f"{fo.get('projected_season_p90_mm', '?')}mm). "
                    f"The insurance payout threshold is {fo_thresh}mm — "
                    f"risk of triggering a payout is {fo_risk} ({fo_prob}% probability). "
                    f"{agreement_str}"
                )

            del result["data"]
            result["instruction"] = (
                "You are briefing someone who cares about this area. "
                "Speak naturally — like a knowledgeable colleague explaining the situation over coffee, not reading a report. "
                "Use the technical terms (SPI, NDVI, ET) but always pair them with what they mean in plain language — the 'situation' field already does this for you. "
                "Tell a coherent story: what's the headline, what's surprising or interesting, what should they watch. "
                "If the forecast is present, weave it in — don't list it separately. "
                "3-5 sentences. No bullet points, no tables, no metric dumps. "
                "End with sources in parentheses."
            )

        # Brain save — best-effort audit trail. Failure here MUST NOT
        # propagate; the user still gets their briefing.
        if result.get("status") == "ok" and not compare_level:
            try:
                from src.dependencies.brain_dep import get_brain_service
                from src.services.brain_service import PageInput, TimelineInput
                brain = get_brain_service()
                slug = result.get("slug", "insurance-report").lower()
                geom = result.get("geometry")
                geom_str = _json.dumps(geom) if geom else None
                page_data = result.get("data", {}) if "data" in result else {}
                # Re-pull data from the original result snapshot — we already
                # deleted result["data"] above for the LLM-facing output,
                # but the Brain save needs the structured fields.
                # Recompute from what's still in result.
                data_for_brain = {
                    "crop": ctx.arguments.get("crop"),
                    "season": ctx.arguments.get("season"),
                    "location": (
                        ctx.arguments.get("village")
                        or ctx.arguments.get("cell")
                        or ctx.arguments.get("sector")
                        or ctx.arguments.get("district")
                    ),
                }
                page_input = PageInput(
                    type="insurance_intelligence",
                    title=f"Insurance: {data_for_brain['location'] or ''} Season {data_for_brain['season'] or ''}",
                    compiled_truth=result.get("_report_for_brain", ""),
                    frontmatter={
                        "type": "insurance_intelligence",
                        **data_for_brain,
                    },
                    geom_geojson=geom_str,
                )
                await brain.put_page(
                    ctx.conn, slug, page_input,
                    owner_uuid=ctx.user_id or "00000000-0000-0000-0000-000000000000",
                )
                timeline_input = TimelineInput(
                    date=_date_cls.today(),
                    summary=(
                        f"insurance_intelligence: {data_for_brain['crop'] or ''} "
                        f"in {data_for_brain['location'] or ''} "
                        f"Season {data_for_brain['season'] or ''}"
                    ),
                    source="insurance_engine",
                    detail=_json.dumps(data_for_brain, default=str),
                )
                await brain.add_timeline_entry(
                    ctx.conn, slug, timeline_input,
                    owner_uuid=ctx.user_id or "00000000-0000-0000-0000-000000000000",
                )
            except Exception:
                logger.warning("insurance brain save failed", exc_info=True)
            result.pop("_report_for_brain", None)

        # Strip geometry from the LLM-facing result. The admin-boundary
        # GeoJSON is needed only for the Brain page geom_geojson above;
        # leaving it in here bloats conversation history by 100-185 KB per
        # call and overflows context after a handful of turns.
        result.pop("geometry", None)
        return result
    except Exception:
        logger.exception("get_insurance_intelligence tool failed")
        return {
            "status": "error",
            "error": "Insurance intelligence computation failed. Please try again.",
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
    # Map/layer plumbing — all 7 hardcoded tools NOW EXTRACTED:
    # new_layer_from_postgis, add_layer_to_map, set_layer_style,
    # query_postgis_database, query_duckdb_sql, zonal_statistics,
    # reverse_geocode_coordinates. None remain in this section.
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
    # NOTE: get_forecast + detect_dry_spells + get_insurance_intelligence
    # have been extracted (the insurance flow).
    "get_forecast_accuracy",
    "get_emissions_stats",
    "get_insurance_accuracy",
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
# As of this commit: 3 real handlers (new_layer_from_postgis, add_layer_to_map,
# set_layer_style) + 50 not-yet-extracted stubs. Each not_yet_extracted stub
# returns a structured message instead of 404, so the LLM can pattern-match
# on status and apologize cleanly to the user.
LEGACY_HANDLERS: Dict[str, LegacyHandlerFn] = {
    "new_layer_from_postgis": _handle_new_layer_from_postgis,
    "add_layer_to_map": _handle_add_layer_to_map,
    "set_layer_style": _handle_set_layer_style,
    "query_duckdb_sql": _handle_query_duckdb_sql,
    "query_postgis_database": _handle_query_postgis_database,
    "zonal_statistics": _handle_zonal_statistics,
    "reverse_geocode_coordinates": _handle_reverse_geocode_coordinates,
    "get_forecast": _handle_get_forecast,
    "detect_dry_spells": _handle_detect_dry_spells,
    "get_insurance_intelligence": _handle_get_insurance_intelligence,
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
