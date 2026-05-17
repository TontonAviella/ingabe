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


async def _handle_get_field_health(ctx: LegacyToolContext) -> Dict[str, Any]:
    """Live vegetation health stats for a polygon (NDVI/NDWI/BSI via Sentinel Hub).

    Extracted from src/routes/message_routes.py:3062-3104. Auto-buffers
    Point/MultiPoint geometries to a 500m polygon so the LLM can pass a
    single coordinate without having to call new_layer + buffer first.
    Sync `_sa_get_field_stats` is offloaded to the default executor.

    Args (from ctx.arguments):
      - geometry: GeoJSON dict (any type; Points auto-buffered to 500m)
      - date_from, date_to: ISO date strings
      - index: 'ndvi' (default), 'ndwi', 'bsi'
    """
    import asyncio as _aio

    try:
        from src.services.satellite_analytics import get_field_stats as _sa_get_field_stats

        geom = ctx.arguments.get("geometry")
        # Auto-buffer Point/MultiPoint geometries to 500m so the LLM doesn't
        # have to create a buffer first. Lifted verbatim from the inline.
        if geom and geom.get("type") in ("Point", "MultiPoint"):
            from shapely.geometry import shape as _shape, mapping as _mapping
            from shapely.ops import transform as _stransform
            from pyproj import Transformer as _Transformer

            pt = _shape(geom)
            to_utm = _Transformer.from_crs("EPSG:4326", "EPSG:32735", always_xy=True)
            to_wgs = _Transformer.from_crs("EPSG:32735", "EPSG:4326", always_xy=True)
            pt_utm = _stransform(to_utm.transform, pt)
            buf_utm = pt_utm.buffer(500)  # 500m radius
            buf_wgs = _stransform(to_wgs.transform, buf_utm)
            geom = _mapping(buf_wgs)
            logger.info("get_field_health: auto-buffered Point to 500m polygon")

        result_data = await _aio.get_event_loop().run_in_executor(
            None,
            lambda: _sa_get_field_stats(
                geometry=geom,
                date_from=ctx.arguments.get("date_from"),
                date_to=ctx.arguments.get("date_to"),
                index=ctx.arguments.get("index", "ndvi"),
            ),
        )
        if "error" in result_data:
            return {"status": "error", "error": result_data["error"]}
        return {"status": "success", "field_stats": result_data}
    except Exception as e:
        logger.exception("get_field_health tool failed")
        return {"status": "error", "error": str(e)}


async def _handle_get_parcel_ndvi_stats(ctx: LegacyToolContext) -> Dict[str, Any]:
    """Read parcel-level NDVI from the postgres cache (ndvi_parcel_cache).

    Extracted from src/routes/message_routes.py:3828-3894. Returns the
    last 100 most-recently-computed rows, optionally filtered by parcel
    name (ILIKE) or by layer_id. Pure cache read — never falls back to
    real-time (parcel boundaries are user-uploaded so cache freshness is
    the nightly Dagster job's problem).

    Args (from ctx.arguments):
      - parcel_name: str (optional, ILIKE %name%)
      - layer_id: str (optional, exact match)
    """
    try:
        parcel = ctx.arguments.get("parcel_name")
        layer = ctx.arguments.get("layer_id")

        # Build WHERE clause + params positionally — safer than f-string
        # interpolation of values. Column names ARE hardcoded.
        where: list[str] = []
        params: list[Any] = []
        idx = 1
        if parcel:
            where.append(f"parcel_name ILIKE ${idx}")
            params.append(f"%{parcel}%")
            idx += 1
        if layer:
            where.append(f"layer_id = ${idx}")
            params.append(layer)
            idx += 1
        where_sql = f"WHERE {' AND '.join(where)}" if where else ""

        rows = await ctx.conn.fetch(
            f"SELECT parcel_id, parcel_name, layer_id, week_start, "
            f"mean_ndvi, std_ndvi, min_ndvi, max_ndvi, valid_pixels, area_ha "
            f"FROM ndvi_parcel_cache {where_sql} "
            f"ORDER BY computed_at DESC LIMIT 100",
            *params,
        )
        if not rows:
            return {
                "status": "success",
                "source": "postgres_cache",
                "parcel_ndvi_stats": [],
                "message": (
                    "No parcel NDVI data yet. Upload field boundaries through "
                    "Mundi UI and tag with rwanda_parcels=true in layer "
                    "metadata. The nightly pipeline processes them."
                ),
            }
        return {
            "status": "success",
            "source": "postgres_cache",
            "count": len(rows),
            "parcel_ndvi_stats": [
                {
                    "parcel_id": r["parcel_id"],
                    "parcel_name": r["parcel_name"],
                    "layer_id": r["layer_id"],
                    "week_start": str(r["week_start"]) if r["week_start"] else None,
                    "mean_ndvi": round(r["mean_ndvi"], 4) if r["mean_ndvi"] else None,
                    "std_ndvi": round(r["std_ndvi"], 4) if r["std_ndvi"] else None,
                    "min_ndvi": round(r["min_ndvi"], 4) if r["min_ndvi"] else None,
                    "max_ndvi": round(r["max_ndvi"], 4) if r["max_ndvi"] else None,
                    "valid_pixels": r["valid_pixels"],
                    "area_ha": r["area_ha"],
                }
                for r in rows
            ],
        }
    except Exception as e:
        logger.exception("get_parcel_ndvi_stats tool failed")
        return {"status": "error", "error": str(e)}


_NDVI_VIS_INSTRUCTIONS = (
    "To visualise these NDVI stats on the map, call new_layer_from_postgis with "
    "postgis_connection_id='{pgc_id}'. IMPORTANT: the query MUST return columns "
    "named 'id' and 'geom'. Available tables: rwanda_district_boundaries (district, "
    "geom), rwanda_cell_boundaries (cell_id, cell_name, district_name, geom). "
    "Example: SELECT ROW_NUMBER() OVER() AS id, district AS district_name, geom "
    "FROM rwanda_district_boundaries. Then call add_layer_to_map and "
    "set_layer_style to colour districts by NDVI. DO NOT reuse an existing layer "
    "— always create a NEW layer from PostGIS."
)


async def _handle_get_ndvi_stats(ctx: LegacyToolContext) -> Dict[str, Any]:
    """District-level NDVI stats with 3-tier fallback (cache → DE Africa
    real-time → STAC COG).

    Extracted from src/routes/message_routes.py:3350-3592. Big handler
    with three independent data sources tried in order:

      1. PostgreSQL `ndvi_field_cache` populated by the nightly Dagster
         job. Returns recent rows (top 50 per district, top 200 across
         all districts).
      2. If cache is empty OR latest week is >14 days stale, fetch live
         from Digital Earth Africa via `satellite_analytics.get_field_stats`
         for each district's PostGIS geometry. Aggregates per-district
         means/stdev/min/max from the satellite-returned intervals.
      3. If both above produce nothing, STAC COG fallback via
         `stac_service.compute_admin_ndvi` over each district's bbox.

    Returns 'source' field indicating which tier(s) the data came from.
    Includes kue_instructions for the LLM to visualize via PostGIS.

    Args (from ctx.arguments):
      - district: str (optional; filters to one district)
    """
    import asyncio as _aio
    import json as _json
    from datetime import date as _date, datetime as _datetime, timedelta as _td

    try:
        # Tier 1: postgres cache read.
        cached_rows: list = []
        try:
            district = ctx.arguments.get("district")
            if district:
                cached_rows = await ctx.conn.fetch(
                    "SELECT district, week_start, mean_ndvi, std_ndvi, min_ndvi, "
                    "max_ndvi, valid_pixels FROM ndvi_field_cache "
                    "WHERE district = $1 ORDER BY week_start DESC LIMIT 50",
                    district,
                )
            else:
                cached_rows = await ctx.conn.fetch(
                    "SELECT district, week_start, mean_ndvi, std_ndvi, min_ndvi, "
                    "max_ndvi, valid_pixels FROM ndvi_field_cache "
                    "ORDER BY week_start DESC, district LIMIT 200"
                )
        except Exception:
            logger.debug(
                "PostgreSQL NDVI cache not available, will try real-time DE Africa"
            )

        ndvi_stats: list = []
        for r in cached_rows:
            ndvi_stats.append({
                "district": r["district"],
                "week_start": str(r["week_start"]) if r["week_start"] else None,
                "mean_ndvi": round(r["mean_ndvi"], 4) if r["mean_ndvi"] else None,
                "std_ndvi": round(r["std_ndvi"], 4) if r["std_ndvi"] else None,
                "min_ndvi": round(r["min_ndvi"], 4) if r["min_ndvi"] else None,
                "max_ndvi": round(r["max_ndvi"], 4) if r["max_ndvi"] else None,
                "valid_pixels": r["valid_pixels"],
                "source": "deafrica_cache",
            })

        # Tier 2: DE Africa real-time fallback if cache is empty OR stale (>14d).
        need_realtime = len(ndvi_stats) == 0
        if ndvi_stats:
            latest = max(
                (s["week_start"] for s in ndvi_stats if s["week_start"]),
                default=None,
            )
            if latest and latest < str(_date.today() - _td(days=14)):
                need_realtime = True

        realtime_stats: list = []
        if need_realtime:
            try:
                from src.services.satellite_analytics import get_field_stats as _sa_get_field_stats
                import numpy as _np

                dfilter = ctx.arguments.get("district")
                where_clause = "WHERE district = $1" if dfilter else ""
                query_params: list = [dfilter] if dfilter else []
                async with ctx.conn.transaction():
                    dist_rows = await ctx.conn.fetch(
                        f"SELECT district, ST_AsGeoJSON(geom) as geom "
                        f"FROM rwanda_district_boundaries {where_clause} "
                        f"ORDER BY district",
                        *query_params,
                    )

                now = _datetime.utcnow()
                rt_from = (now - _td(days=7)).strftime("%Y-%m-%d")
                rt_to = now.strftime("%Y-%m-%d")

                for dr in dist_rows:
                    try:
                        geom = _json.loads(dr["geom"])
                        stats = _sa_get_field_stats(
                            geometry=geom, date_from=rt_from,
                            date_to=rt_to, index="ndvi",
                        )
                        if "error" in stats:
                            continue
                        intervals = stats.get("intervals", [])
                        if not intervals:
                            continue
                        means = [
                            iv["ndvi"]["mean"]
                            for iv in intervals
                            if "ndvi" in iv and iv["ndvi"].get("valid_pixels", 0) > 0
                        ]
                        if not means:
                            continue
                        backend_tag = stats.get("backend", "satellite")
                        realtime_stats.append({
                            "district": dr["district"],
                            "week_start": rt_from,
                            "mean_ndvi": round(float(_np.mean(means)), 4),
                            "std_ndvi": round(float(_np.std(means)), 4),
                            "min_ndvi": round(float(_np.min(means)), 4),
                            "max_ndvi": round(float(_np.max(means)), 4),
                            "valid_pixels": sum(
                                iv["ndvi"].get("valid_pixels", 0)
                                for iv in intervals if "ndvi" in iv
                            ),
                            "source": f"{backend_tag}_realtime",
                        })
                    except Exception as e:
                        logger.debug(
                            "Satellite realtime failed for %s: %s", dr["district"], e
                        )
            except Exception as e:
                logger.warning("Satellite real-time NDVI failed: %s", e)

        # Merge + sort by week descending.
        all_stats = ndvi_stats + realtime_stats
        all_stats.sort(
            key=lambda s: (s.get("week_start") or "", s.get("district") or ""),
            reverse=True,
        )

        # Lazy-import the rwanda PostGIS connection helper (defined in
        # message_routes.py — same file as the inline handler we lifted from).
        from src.routes.message_routes import _ensure_rwanda_postgis_connection

        if all_stats:
            sources = sorted(set(s.get("source", "cache") for s in all_stats))
            result = {
                "status": "success",
                "source": " + ".join(sources),
                "count": len(all_stats),
                "cached_records": len(ndvi_stats),
                "realtime_records": len(realtime_stats),
                "note": (
                    "NDVI values: 0.6-0.8 = dense vegetation, 0.3-0.5 = cropland, "
                    "0.1-0.3 = sparse vegetation, <0.1 = bare soil/cloud contaminated. "
                    "Negative values indicate heavy cloud cover during the observation period. "
                    "Source: Sentinel-2 L2A via Digital Earth Africa (free, public). "
                    "Each record has a 'source' field: 'deafrica_cache' (nightly batch) "
                    "or 'deafrica_realtime' (live COG query)."
                ),
                "ndvi_stats": all_stats,
            }
            pgc_id = await _ensure_rwanda_postgis_connection(
                ctx.conn, ctx.project_id, ctx.user_id,
            )
            if pgc_id:
                result["postgis_connection_id"] = pgc_id
                result["kue_instructions"] = _NDVI_VIS_INSTRUCTIONS.format(pgc_id=pgc_id)
            return result

        # Tier 3: STAC COG fallback (free, no API key).
        stac_stats: list = []
        try:
            from src.services.stac_service import get_stac_service as _get_stac

            stac = _get_stac()
            sdist = ctx.arguments.get("district")
            if sdist:
                bbox_rows = await ctx.conn.fetch(
                    "SELECT district, bbox_west, bbox_south, bbox_east, bbox_north "
                    "FROM rwanda_district_boundaries WHERE LOWER(district) = LOWER($1)",
                    sdist,
                )
            else:
                bbox_rows = await ctx.conn.fetch(
                    "SELECT district, bbox_west, bbox_south, bbox_east, bbox_north "
                    "FROM rwanda_district_boundaries ORDER BY district LIMIT 10"
                )

            for sbr in bbox_rows:
                bbox = [
                    float(sbr["bbox_west"]), float(sbr["bbox_south"]),
                    float(sbr["bbox_east"]), float(sbr["bbox_north"]),
                ]
                stac_ts = await _aio.get_event_loop().run_in_executor(
                    None,
                    lambda bb=bbox: stac.compute_admin_ndvi(bb, days=30, max_scenes=4),
                )
                if "error" in stac_ts:
                    continue
                for obs in stac_ts.get("observations", []):
                    stac_stats.append({
                        "district": sbr["district"],
                        "week_start": obs.get("datetime", "")[:10] if obs.get("datetime") else None,
                        "mean_ndvi": obs.get("mean_ndvi"),
                        "std_ndvi": obs.get("std_ndvi"),
                        "min_ndvi": obs.get("min_ndvi"),
                        "max_ndvi": obs.get("max_ndvi"),
                        "valid_pixels": obs.get("valid_pixel_count"),
                        "source": "stac_cog_realtime",
                    })
        except Exception as e:
            logger.warning("STAC NDVI fallback failed: %s", e)

        if stac_stats:
            result = {
                "status": "success",
                "source": "stac_cog_realtime",
                "count": len(stac_stats),
                "cached_records": 0,
                "realtime_records": len(stac_stats),
                "note": (
                    "NDVI computed in real-time from Sentinel-2 COGs via STAC "
                    "(free, no API key). Values: 0.6-0.8 = dense vegetation, "
                    "0.3-0.5 = cropland, 0.1-0.3 = sparse vegetation, <0.1 = bare soil."
                ),
                "ndvi_stats": stac_stats,
            }
            pgc_id = await _ensure_rwanda_postgis_connection(
                ctx.conn, ctx.project_id, ctx.user_id,
            )
            if pgc_id:
                result["postgis_connection_id"] = pgc_id
                result["kue_instructions"] = _NDVI_VIS_INSTRUCTIONS.format(pgc_id=pgc_id)
            return result

        # All three tiers empty.
        return {
            "status": "success",
            "ndvi_stats": [],
            "message": (
                "No NDVI data available. Cache is empty and real-time query "
                "found no cloud-free Sentinel-2 scenes via Digital Earth Africa. "
                "The nightly job populates this cache automatically."
            ),
        }
    except Exception as e:
        logger.exception("get_ndvi_stats tool failed")
        return {"status": "error", "error": str(e)}


async def _handle_get_cell_ndvi_stats(ctx: LegacyToolContext) -> Dict[str, Any]:
    """Sector/cell-level NDVI stats with cache → real-time fallback.

    Extracted from src/routes/message_routes.py:3594-3761. Like
    get_ndvi_stats but one admin level finer (cell granularity, with
    sector_name from the boundary join). Falls back to sector-level
    real-time DE Africa NDVI if the cell-level cache is empty.

    Args (from ctx.arguments):
      - cell_name: str (optional, ILIKE %name%)
      - sector: str (optional, ILIKE)
      - district: str (optional, ILIKE on the cell row's district_name)
    """
    import json as _json
    from datetime import datetime as _datetime, timedelta as _td

    try:
        cell = ctx.arguments.get("cell_name")
        sector = ctx.arguments.get("sector")
        district = ctx.arguments.get("district")

        # Tier 1: cache JOIN against rwanda_cell_boundaries to pick up sector.
        where: list[str] = []
        params: list[Any] = []
        pidx = 1
        if cell:
            where.append(f"nc.cell_name ILIKE ${pidx}")
            params.append(f"%{cell}%")
            pidx += 1
        if sector:
            where.append(f"cb.sector_name ILIKE ${pidx}")
            params.append(f"%{sector}%")
            pidx += 1
        if district:
            where.append(f"nc.district_name ILIKE ${pidx}")
            params.append(f"%{district}%")
            pidx += 1
        where_sql = f"WHERE {' AND '.join(where)}" if where else ""
        rows = await ctx.conn.fetch(
            f"SELECT nc.cell_name, cb.sector_name, nc.district_name, nc.week_start, "
            f"nc.mean_ndvi, nc.std_ndvi, nc.min_ndvi, nc.max_ndvi, nc.valid_pixels "
            f"FROM ndvi_cell_cache nc "
            f"JOIN rwanda_cell_boundaries cb "
            f"ON nc.cell_name = cb.cell_name AND nc.district_name = cb.district_name "
            f"{where_sql} "
            f"ORDER BY cb.sector_name, nc.cell_name, nc.computed_at DESC LIMIT 200",
            *params,
        )

        result: Dict[str, Any]
        if rows:
            result = {
                "status": "success",
                "source": "postgres_cache",
                "count": len(rows),
                "cell_ndvi_stats": [
                    {
                        "cell_name": r["cell_name"],
                        "sector_name": r["sector_name"],
                        "district_name": r["district_name"],
                        "week_start": str(r["week_start"]) if r["week_start"] else None,
                        "mean_ndvi": round(r["mean_ndvi"], 4) if r["mean_ndvi"] else None,
                        "std_ndvi": round(r["std_ndvi"], 4) if r["std_ndvi"] else None,
                        "min_ndvi": round(r["min_ndvi"], 4) if r["min_ndvi"] else None,
                        "max_ndvi": round(r["max_ndvi"], 4) if r["max_ndvi"] else None,
                        "valid_pixels": r["valid_pixels"],
                    }
                    for r in rows
                ],
            }
        else:
            # Tier 2: sector-level real-time fallback. Note: drops to sector
            # granularity since cell-level DE Africa pulls would be too slow.
            realtime_stats: list = []
            try:
                from src.services.satellite_analytics import get_field_stats as _sa_get_field_stats
                import numpy as _np

                now = _datetime.utcnow()
                rt_from = (now - _td(days=10)).strftime("%Y-%m-%d")
                rt_to = now.strftime("%Y-%m-%d")

                sec_where: list[str] = []
                sec_params: list[Any] = []
                sec_pidx = 1
                if sector:
                    sec_where.append(f"sector_name ILIKE ${sec_pidx}")
                    sec_params.append(f"%{sector}%")
                    sec_pidx += 1
                if district:
                    sec_where.append(f"district_name ILIKE ${sec_pidx}")
                    sec_params.append(f"%{district}%")
                    sec_pidx += 1
                sec_where_sql = (
                    f"WHERE {' AND '.join(sec_where)}" if sec_where else ""
                )
                sec_rows = await ctx.conn.fetch(
                    f"SELECT sector_name, district_name, ST_AsGeoJSON(geom) as geom "
                    f"FROM rwanda_sector_boundaries {sec_where_sql} "
                    f"ORDER BY sector_name",
                    *sec_params,
                )

                for sr in sec_rows:
                    try:
                        geom = _json.loads(sr["geom"])
                        stats = _sa_get_field_stats(
                            geometry=geom, date_from=rt_from,
                            date_to=rt_to, index="ndvi",
                        )
                        if "error" in stats:
                            continue
                        intervals = stats.get("intervals", [])
                        if not intervals:
                            continue
                        means = [
                            iv["ndvi"]["mean"]
                            for iv in intervals
                            if "ndvi" in iv and iv["ndvi"].get("valid_pixels", 0) > 0
                        ]
                        if not means:
                            continue
                        realtime_stats.append({
                            "sector_name": sr["sector_name"],
                            "district_name": sr["district_name"],
                            "week_start": rt_from,
                            "mean_ndvi": round(float(_np.mean(means)), 4),
                            "std_ndvi": round(float(_np.std(means)), 4),
                            "min_ndvi": round(float(_np.min(means)), 4),
                            "max_ndvi": round(float(_np.max(means)), 4),
                            "valid_pixels": sum(
                                iv["ndvi"].get("valid_pixels", 0)
                                for iv in intervals if "ndvi" in iv
                            ),
                        })
                    except Exception as e:
                        logger.debug(
                            "Sector realtime NDVI failed for %s: %s",
                            sr["sector_name"], e,
                        )
            except Exception as e:
                logger.warning("Sector real-time NDVI fallback failed: %s", e)

            if realtime_stats:
                result = {
                    "status": "success",
                    "source": "deafrica_realtime",
                    "level": "sector",
                    "count": len(realtime_stats),
                    "note": (
                        "Real-time sector-level NDVI (cell-level cache not "
                        "yet populated)"
                    ),
                    "sector_ndvi_stats": realtime_stats,
                }
            else:
                result = {
                    "status": "success",
                    "source": "none",
                    "cell_ndvi_stats": [],
                    "message": (
                        "No NDVI data available — cache empty and real-time "
                        "satellite fetch returned no results for this area."
                    ),
                }

        # Auto-provision PostGIS connection so the LLM can visualize via
        # new_layer_from_postgis. Mentions cell + sector boundary tables.
        if result.get("count", 0) > 0:
            from src.routes.message_routes import _ensure_rwanda_postgis_connection
            pgc_id = await _ensure_rwanda_postgis_connection(
                ctx.conn, ctx.project_id, ctx.user_id,
            )
            if pgc_id:
                result["postgis_connection_id"] = pgc_id
                result["kue_instructions"] = (
                    "To visualise these stats on the map, call "
                    f"new_layer_from_postgis with postgis_connection_id='{pgc_id}'. "
                    "IMPORTANT: the query MUST return columns named 'id' and 'geom'. "
                    "Available tables: rwanda_sector_boundaries (sector_id, "
                    "sector_name, district_name, geom), rwanda_cell_boundaries "
                    "(cell_id, cell_name, sector_name, district_name, geom). "
                    "DO NOT reuse an existing layer — always create a NEW layer "
                    "from PostGIS."
                )
        return result
    except Exception as e:
        logger.exception("get_cell_ndvi_stats tool failed")
        return {"status": "error", "error": str(e)}


async def _handle_get_agri_indices(ctx: LegacyToolContext) -> Dict[str, Any]:
    """Multi-index Sentinel-2 stats (NDVI, EVI, NDWI, SAVI, NDRE, NDBI) for
    Rwanda admin boundaries, with cache write-back and direct layer creation.

    Extracted from src/routes/message_routes.py:3896-4305. The biggest
    inline handler — does six things in sequence:

      1. Selects the right admin table (district/sector/cell) based on
         the admin_level argument.
      2. Reads cache from agri_indices_cache with a 7-day TTL (Sentinel-2
         revisit ~5 days).
      3. For cache misses, calls satellite_analytics.get_agri_stats per
         admin unit, aggregates the six indices via numpy.
      4. Writes the fresh rows back to agri_indices_cache.
      5. Auto-provisions the Rwanda PostGIS connection and builds a
         `SELECT … FROM admin_tbl JOIN (VALUES …)` query embedding all
         the computed stats inline.
      6. CREATES the layer directly (inserts map_layers + layer_styles +
         map_layer_styles + appends to user_mundiai_maps.layers) with a
         red→green NDVI choropleth + 3D extrusion metadata. The LLM is
         instructed NOT to also call new_layer_from_postgis/set_layer_style
         because the work is already done.

    The inline layer-creation logic duplicates pieces of
    _handle_new_layer_from_postgis + _handle_set_layer_style. A
    follow-up PR can refactor this to call those shared helpers; for
    now keeping byte-for-byte parity with the inline handler to avoid
    behavior drift.

    Args (from ctx.arguments):
      - admin_level: 'district' (default) | 'sector' | 'cell'
      - name: str (optional, ILIKE on the admin name column)
      - district: str (optional, parent filter for sector/cell levels)
      - date_from / date_to: ISO date strings (defaults: last 7 days)
    """
    import json as _json
    from datetime import date as _date, datetime as _datetime, timedelta as _td

    try:
        from src.services.satellite_analytics import get_agri_stats as _get_agri_stats
        from src.routes.message_routes import _ensure_rwanda_postgis_connection
        from src.routes.websocket import kue_ephemeral_action
        from src.utils import generate_id
        import numpy as _np

        AGRI_INDICES = ["ndvi", "evi", "ndwi", "savi", "ndre", "ndbi"]
        CACHE_TTL_DAYS = 7  # Sentinel-2 revisit ~5 days

        level = ctx.arguments.get("admin_level", "district")
        name_filter = ctx.arguments.get("name")
        district_filter = ctx.arguments.get("district")
        date_from = ctx.arguments.get("date_from")
        date_to = ctx.arguments.get("date_to")
        if not date_to:
            date_to = _datetime.utcnow().strftime("%Y-%m-%d")
        if not date_from:
            date_from = (_datetime.utcnow() - _td(days=7)).strftime("%Y-%m-%d")

        table_map = {
            "district": ("rwanda_district_boundaries", "district", None),
            "sector": ("rwanda_sector_boundaries", "sector_name", "district_name"),
            "cell": ("rwanda_cell_boundaries", "cell_name", "district_name"),
        }
        tbl, name_col, parent_col = table_map.get(level, table_map["district"])

        # Build WHERE for admin boundary lookup.
        conditions: list[str] = []
        params: list[Any] = []
        pidx = 1
        if name_filter:
            conditions.append(f"{name_col} ILIKE ${pidx}")
            params.append(f"%{name_filter}%")
            pidx += 1
        if district_filter and parent_col:
            conditions.append(f"{parent_col} ILIKE ${pidx}")
            params.append(f"%{district_filter}%")
            pidx += 1
        where = f"WHERE {' AND '.join(conditions)}" if conditions else ""

        # Fetch admin boundaries (cap at 30 — covers all 30 districts, more
        # would slow real-time fetches beyond reasonable LLM-turn budget).
        async with ctx.conn.transaction():
            admin_rows = await ctx.conn.fetch(
                f"SELECT {name_col} AS name, "
                f"{parent_col + ' AS parent,' if parent_col else ''} "
                f"ST_AsGeoJSON(geom) AS geom "
                f"FROM {tbl} {where} "
                f"ORDER BY {name_col} LIMIT 30",
                *params,
            )

        if not admin_rows:
            return {
                "status": "success",
                "agri_indices": [],
                "message": f"No {level} boundaries found matching filters.",
            }

        # ---- Step 1: read cache ----
        admin_names = [r["name"] for r in admin_rows]
        cutoff = _datetime.utcnow() - _td(days=CACHE_TTL_DAYS)
        cached_rows = await ctx.conn.fetch(
            "SELECT admin_name, parent_name, week_start, "
            "ndvi_mean, ndvi_std, evi_mean, evi_std, "
            "ndwi_mean, ndwi_std, savi_mean, savi_std, "
            "ndre_mean, ndre_std, ndbi_mean, ndbi_std, "
            "valid_pixels, computed_at "
            "FROM agri_indices_cache "
            "WHERE admin_level = $1 "
            "AND admin_name = ANY($2::text[]) "
            "AND computed_at >= $3 "
            "ORDER BY computed_at DESC",
            level, admin_names, cutoff,
        )

        # Dedup cache rows: keep most-recent per admin_name.
        cached_by_name: Dict[str, Dict[str, Any]] = {}
        for cr in cached_rows:
            cname = cr["admin_name"]
            if cname not in cached_by_name:
                cached_by_name[cname] = {
                    "admin_level": level,
                    "name": cname,
                    "district": cr["parent_name"] if cr["parent_name"] else None,
                    "date_from": str(cr["week_start"]),
                    "date_to": date_to,
                    "ndvi_mean": cr["ndvi_mean"], "ndvi_std": cr["ndvi_std"],
                    "evi_mean": cr["evi_mean"], "evi_std": cr["evi_std"],
                    "ndwi_mean": cr["ndwi_mean"], "ndwi_std": cr["ndwi_std"],
                    "savi_mean": cr["savi_mean"], "savi_std": cr["savi_std"],
                    "ndre_mean": cr["ndre_mean"], "ndre_std": cr["ndre_std"],
                    "ndbi_mean": cr["ndbi_mean"], "ndbi_std": cr["ndbi_std"],
                    "valid_pixels": cr["valid_pixels"],
                    "source": "cache",
                }

        # ---- Step 2: identify cache misses ----
        miss_rows = [r for r in admin_rows if r["name"] not in cached_by_name]
        cache_hits = len(admin_names) - len(miss_rows)
        logger.info(
            "agri_indices cache: %d hits, %d misses for %s level",
            cache_hits, len(miss_rows), level,
        )

        # ---- Step 3: realtime fetch for misses + cache write-back ----
        results: list[Dict[str, Any]] = list(cached_by_name.values())
        errors: list[str] = []
        if miss_rows:
            for ar in miss_rows:
                geom = _json.loads(ar["geom"])
                aname = ar["name"]
                aparent = ar.get("parent")
                try:
                    stats = _get_agri_stats(
                        geometry=geom, date_from=date_from, date_to=date_to,
                    )
                    if "error" in stats:
                        errors.append(f"{aname}: {stats['error']}")
                        continue
                    intervals = stats.get("intervals", [])
                    if not intervals:
                        continue

                    row: Dict[str, Any] = {"admin_level": level, "name": aname}
                    if aparent:
                        row["district"] = aparent
                    row["date_from"] = date_from
                    row["date_to"] = date_to

                    total_px = 0
                    for idx in AGRI_INDICES:
                        means = [
                            iv[idx]["mean"]
                            for iv in intervals
                            if idx in iv and iv[idx].get("valid_pixels", 0) > 0
                        ]
                        if means:
                            row[f"{idx}_mean"] = round(float(_np.mean(means)), 4)
                            row[f"{idx}_std"] = round(float(_np.std(means)), 4)
                        else:
                            row[f"{idx}_mean"] = None
                            row[f"{idx}_std"] = None
                    for iv in intervals:
                        if "ndvi" in iv:
                            total_px += iv["ndvi"].get("valid_pixels", 0)
                    row["valid_pixels"] = total_px
                    row["source"] = stats.get("backend", "satellite_realtime")
                    results.append(row)

                    # Step 4: cache write-back (best-effort).
                    try:
                        week_start_date = (
                            _date.fromisoformat(date_from)
                            if isinstance(date_from, str) else date_from
                        )
                        await ctx.conn.execute(
                            "INSERT INTO agri_indices_cache "
                            "(admin_level, admin_name, parent_name, week_start, "
                            "ndvi_mean, ndvi_std, evi_mean, evi_std, "
                            "ndwi_mean, ndwi_std, savi_mean, savi_std, "
                            "ndre_mean, ndre_std, ndbi_mean, ndbi_std, "
                            "valid_pixels) VALUES "
                            "($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12, $13, $14, $15, $16, $17)",
                            level, aname, aparent, week_start_date,
                            row.get("ndvi_mean"), row.get("ndvi_std"),
                            row.get("evi_mean"), row.get("evi_std"),
                            row.get("ndwi_mean"), row.get("ndwi_std"),
                            row.get("savi_mean"), row.get("savi_std"),
                            row.get("ndre_mean"), row.get("ndre_std"),
                            row.get("ndbi_mean"), row.get("ndbi_std"),
                            total_px,
                        )
                    except Exception as ce:
                        logger.warning("Cache write failed for %s: %s", aname, ce)
                except Exception as e:
                    errors.append(f"{aname}: {str(e)}")

        results.sort(key=lambda r: r.get("name", ""))
        source_desc = "cache" if not miss_rows else (
            "satellite_realtime" if cache_hits == 0
            else f"mixed ({cache_hits} cached, {len(miss_rows)} realtime)"
        )

        tool_result: Dict[str, Any] = {
            "status": "success",
            "source": source_desc,
            "admin_level": level,
            "date_range": f"{date_from} to {date_to}",
            "count": len(results),
            "cache_hits": cache_hits,
            "cache_misses": len(miss_rows),
            "indices": list(AGRI_INDICES),
            "note": (
                "Sentinel-2 L2A data with SCL cloud masking. "
                "Cache TTL: 7 days (satellite revisit ~5 days). "
                "NDVI 0.6-0.8=dense vegetation, 0.3-0.5=cropland, <0.1=bare. "
                "EVI less sensitive to atmosphere. "
                "NDWI <0=vegetation, >0=water. "
                "SAVI adjusts for soil. NDRE=nitrogen/chlorophyll. "
                "NDBI >0=built-up, <0=vegetation."
            ),
            "agri_indices": results,
        }
        if errors:
            tool_result["errors"] = errors

        # ---- Steps 5-6: auto-provision PostGIS conn + create the layer ----
        pgc_id = await _ensure_rwanda_postgis_connection(
            ctx.conn, ctx.project_id, ctx.user_id,
        )
        if not (pgc_id and results):
            return tool_result

        tool_result["postgis_connection_id"] = pgc_id

        pg_tbl_map = {
            "district": ("rwanda_district_boundaries", "district"),
            "sector": ("rwanda_sector_boundaries", "sector_name"),
            "cell": ("rwanda_cell_boundaries", "cell_name"),
        }
        pg_tbl, pg_col = pg_tbl_map.get(level, pg_tbl_map["district"])

        # Build VALUES clause embedding the computed indices. Single-quote
        # escaping handled at row-build time (replace ' with '').
        val_rows: list[str] = []
        for r in results:
            sn = r["name"].replace("'", "''")
            nv = r.get("ndvi_mean") or 0
            ev = r.get("evi_mean") or 0
            wv = r.get("ndwi_mean") or 0
            sv = r.get("savi_mean") or 0
            rv = r.get("ndre_mean") or 0
            bv = r.get("ndbi_mean") or 0
            val_rows.append(f"('{sn}',{nv},{ev},{wv},{sv},{rv},{bv})")
        values_sql = ",".join(val_rows)
        postgis_query = (
            f"SELECT ROW_NUMBER() OVER() AS id, "
            f"d.{pg_col} AS name, "
            f"v.ndvi, v.evi, v.ndwi, v.savi, v.ndre, v.ndbi, "
            f"d.geom FROM {pg_tbl} d "
            f"JOIN (VALUES {values_sql}) "
            f"AS v(name,ndvi,evi,ndwi,savi,ndre,ndbi) "
            f"ON d.{pg_col} = v.name"
        )

        # Create the layer + style inline (bypasses Sage). Same DB-write
        # pattern as _handle_new_layer_from_postgis but with hardcoded
        # Rwanda bounds + 3D extrusion metadata. Follow-up PR can DRY this
        # up by calling the shared helper; for now byte-for-byte parity.
        layer_id = generate_id(prefix="L")
        layer_name = f"{level.title()} Agri Indices"
        nvals = [r.get("ndvi_mean", 0) for r in results if r.get("ndvi_mean") is not None]
        nmin = round(min(nvals), 2) if nvals else 0.0
        nmax = round(max(nvals), 2) if nvals else 0.8
        nmid1 = round(nmin + (nmax - nmin) * 0.33, 2)
        nmid2 = round(nmin + (nmax - nmin) * 0.66, 2)
        meta = {"deckgl_3d": True}
        attr_cols = ["name", "ndvi", "evi", "ndwi", "savi", "ndre", "ndbi"]
        bounds = [28.86, -2.84, 30.90, -1.05]  # Rwanda approximate bbox

        async with kue_ephemeral_action(
            ctx.conversation_id, "Creating agri indices layer...",
            update_style_json=True, bounds=bounds,
        ):
            await ctx.conn.execute(
                """
                INSERT INTO map_layers
                (layer_id, owner_uuid, name, type,
                 postgis_connection_id, postgis_query,
                 metadata, feature_count, bounds,
                 geometry_type, source_map_id,
                 created_on, last_edited,
                 postgis_attribute_column_list)
                VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,
                        CURRENT_TIMESTAMP,CURRENT_TIMESTAMP,$12)
                """,
                layer_id, ctx.user_id, layer_name, "postgis",
                pgc_id, postgis_query, _json.dumps(meta),
                len(results), bounds, "multipolygon",
                ctx.map_id, attr_cols,
            )

            # MapLibre choropleth + outline + label layers. source-layer
            # must match the MVT_LAYER_NAME constant ("reprojectedfgb").
            sl = "reprojectedfgb"
            ml_layers = [
                {
                    "id": f"{layer_id}-fill",
                    "type": "fill",
                    "source": layer_id,
                    "source-layer": sl,
                    "paint": {
                        "fill-color": [
                            "interpolate", ["linear"], ["get", "ndvi"],
                            nmin, "#d73027",
                            nmid1, "#fc8d59",
                            nmid2, "#fee08b",
                            nmax, "#1a9850",
                        ],
                        "fill-opacity": 0.85,
                    },
                },
                {
                    "id": f"{layer_id}-outline",
                    "type": "line",
                    "source": layer_id,
                    "source-layer": sl,
                    "paint": {"line-color": "#222222", "line-width": 1.5},
                },
                {
                    "id": f"{layer_id}-label",
                    "type": "symbol",
                    "source": layer_id,
                    "source-layer": sl,
                    "layout": {
                        "text-field": [
                            "concat", ["get", "name"], "\n",
                            "NDVI ", ["to-string", ["get", "ndvi"]],
                        ],
                        "text-size": 11,
                        "text-anchor": "center",
                        "text-allow-overlap": True,
                    },
                    "paint": {
                        "text-color": "#ffffff",
                        "text-halo-color": "#000000",
                        "text-halo-width": 1.5,
                    },
                },
            ]

            style_id = generate_id(prefix="S")
            await ctx.conn.execute(
                """
                INSERT INTO layer_styles
                (style_id, layer_id, style_json, created_by, created_on)
                VALUES ($1, $2, $3, $4, CURRENT_TIMESTAMP)
                """,
                style_id, layer_id, _json.dumps(ml_layers), ctx.user_id,
            )
            await ctx.conn.execute(
                """
                INSERT INTO map_layer_styles (map_id, layer_id, style_id)
                VALUES ($1, $2, $3)
                """,
                ctx.map_id, layer_id, style_id,
            )
            await ctx.conn.execute(
                """
                UPDATE user_mundiai_maps
                SET layers = CASE WHEN layers IS NULL THEN ARRAY[$1]
                                  ELSE array_append(layers, $1) END
                WHERE id = $2 AND (layers IS NULL OR NOT ($1 = ANY(layers)))
                """,
                layer_id, ctx.map_id,
            )

        tool_result["layer_id"] = layer_id
        tool_result["kue_instructions"] = (
            f"The layer '{layer_name}' (ID: {layer_id}) has been created "
            f"and added to the map with a Red→Green choropleth (NDVI) and 3D "
            f"extrusion. Do NOT call new_layer_from_postgis or set_layer_style "
            f"— it is already done.\n\nDescribe the results to the user: "
            f"which {level}s have the highest NDVI (greenest, healthiest "
            f"vegetation) and which have the lowest (stressed). Mention the "
            f"3D extrusion where taller = higher NDVI. Highlight any notable "
            f"patterns or outliers."
        )
        return tool_result
    except Exception as e:
        logger.exception("get_agri_indices tool failed")
        return {"status": "error", "error": str(e)}


async def _handle_identify_parcel_crop(ctx: LegacyToolContext) -> Dict[str, Any]:
    """Identify the dominant crop in a parcel from NDVI time-series.

    Pipeline: fetch NDVI intervals via satellite_analytics (DE Africa primary,
    SH fallback) → convert to time-series → run ml_inference.identify_crop.
    Auto-buffers Point geometries to 500m polygon (UTM 32735 → WGS84) so the
    LLM can pass a single pin and still get a usable result.

    Lifted byte-for-byte from message_routes.py:3190-3253.
    """
    args = ctx.arguments
    try:
        from src.services.satellite_analytics import get_field_timeseries as _sa_get_field_timeseries
        from src.services.ml_inference import get_ml_service

        _ic_geom = args.get("geometry")
        if not _ic_geom:
            return {"status": "error", "error": "geometry is required for crop identification"}

        _ic_months = args.get("months", 6)
        if _ic_months < 3:
            _ic_months = 3

        # Auto-buffer Point geometries
        if _ic_geom.get("type") in ("Point", "MultiPoint"):
            from shapely.geometry import shape as _shape, mapping as _mapping
            from pyproj import Transformer as _Transformer
            from shapely.ops import transform as _stransform
            _pt = _shape(_ic_geom)
            _to_utm = _Transformer.from_crs("EPSG:4326", "EPSG:32735", always_xy=True)
            _to_wgs = _Transformer.from_crs("EPSG:32735", "EPSG:4326", always_xy=True)
            _pt_utm = _stransform(_to_utm.transform, _pt)
            _buf_utm = _pt_utm.buffer(500)
            _buf_wgs = _stransform(_to_wgs.transform, _buf_utm)
            _ic_geom = _mapping(_buf_wgs)
            logger.info("identify_parcel_crop: auto-buffered Point to 500m polygon")

        # Step 1: Get NDVI time-series (DE Africa primary, SH fallback)
        ts_result = await asyncio.get_event_loop().run_in_executor(
            None, lambda: _sa_get_field_timeseries(
                geometry=_ic_geom,
                months=_ic_months,
            )
        )
        if "error" in ts_result:
            return {"status": "error", "error": ts_result["error"]}

        # Convert intervals to time-series format
        _ndvi_ts = []
        for interval in ts_result.get("intervals", []):
            _ndvi_data = interval.get("ndvi", {})
            if _ndvi_data.get("mean") is not None:
                _ndvi_ts.append({
                    "date": interval.get("date_from", ""),
                    "mean_ndvi": _ndvi_data["mean"],
                })

        if len(_ndvi_ts) < 4:
            return {
                "status": "error",
                "error": (
                    f"Insufficient data: only {len(_ndvi_ts)} cloud-free observations "
                    f"in {_ic_months} months. Need at least 4 for crop identification."
                ),
            }

        # Step 2: Run crop identification
        ml_service = get_ml_service()
        crop_result = ml_service.identify_crop(_ndvi_ts)
        if "error" in crop_result:
            return {"status": "error", "error": crop_result["error"]}
        return {"status": "success", "crop_identification": crop_result}
    except Exception as e:
        logger.exception("identify_parcel_crop failed")
        return {"status": "error", "error": str(e)}


async def _handle_confirm_crop_prediction(ctx: LegacyToolContext) -> Dict[str, Any]:
    """Record farmer feedback on a crop prediction into crop_feedback table.

    Auto-detects Rwanda agricultural season from current date when not given
    (Season A: Sep-Feb, Season B: Feb-Jul). Falls back to log-only if the
    crop_feedback table is missing — feedback is never silently dropped.

    Lifted byte-for-byte from message_routes.py:3263-3340.
    """
    args = ctx.arguments
    try:
        from datetime import date as _cdate

        _predicted = args.get("predicted_crop", "")
        _actual = args.get("actual_crop", "")
        _confirmed = args.get("confirmed", False)
        _season = args.get("season")
        _geom = args.get("geometry")

        # Auto-detect season from current date
        if not _season:
            _today = _cdate.today()
            _yr = _today.year
            if _today.month >= 9:
                _season = f"{_yr + 1}A"
            elif _today.month <= 2:
                _season = f"{_yr}A"
            else:
                _season = f"{_yr}B"

        # Store feedback in PostgreSQL
        try:
            await ctx.conn.execute(
                """INSERT INTO crop_feedback
                   (user_id, predicted_crop, actual_crop, confirmed,
                    season, geometry, created_at)
                   VALUES ($1, $2, $3, $4, $5, $6, NOW())""",
                str(ctx.user_id) if ctx.user_id else "anonymous",
                _predicted,
                _actual,
                _confirmed,
                _season,
                json.dumps(_geom) if _geom else None,
            )
            return {
                "status": "success",
                "message": (
                    f"Thank you! Recorded: prediction was '{_predicted}', "
                    f"actual crop is '{_actual}' "
                    f"({'confirmed correct' if _confirmed else 'corrected'}). "
                    f"Season: {_season}. This feedback improves future predictions."
                ),
                "feedback": {
                    "predicted_crop": _predicted,
                    "actual_crop": _actual,
                    "confirmed": _confirmed,
                    "season": _season,
                },
            }
        except Exception as _db_err:
            # Table might not exist yet — log feedback anyway
            logger.warning(
                "crop_feedback table not found (%s) — logging feedback",
                _db_err,
            )
            logger.info(
                "CROP_FEEDBACK: predicted=%s actual=%s confirmed=%s season=%s user=%s",
                _predicted, _actual, _confirmed, _season, ctx.user_id,
            )
            return {
                "status": "success",
                "message": (
                    f"Feedback recorded (log): prediction '{_predicted}', "
                    f"actual '{_actual}' ({'correct' if _confirmed else 'corrected'}). "
                    f"Season: {_season}."
                ),
                "feedback": {
                    "predicted_crop": _predicted,
                    "actual_crop": _actual,
                    "confirmed": _confirmed,
                    "season": _season,
                },
            }
    except Exception as e:
        logger.exception("confirm_crop_prediction failed")
        return {"status": "error", "error": str(e)}


async def _handle_get_crop_classifications(ctx: LegacyToolContext) -> Dict[str, Any]:
    """Read crop_classification_cache, optionally reverse-geocoded by lat/lon.

    The cache is populated by a weekly Dagster job. On hit, auto-provisions a
    PostGIS connection so Sage can call new_layer_from_postgis to visualise
    classifications by district.

    Lifted byte-for-byte from message_routes.py:4669-4747.
    """
    args = ctx.arguments
    try:
        from src.routes.message_routes import _ensure_rwanda_postgis_connection

        _district = args.get("district")
        _cc_lat = args.get("lat")
        _cc_lon = args.get("lon")

        # Reverse-geocode lat/lon to district if not explicitly provided.
        # Opens its own asyncpg connection (no RLS scope needed for boundary lookup).
        if _cc_lat is not None and _cc_lon is not None and not _district:
            try:
                import asyncpg as _asyncpg_cc
                _pg_host_cc = os.environ.get("POSTGRES_HOST", "postgresdb")
                _pg_port_cc = int(os.environ.get("POSTGRES_PORT", "5432"))
                _pg_db_cc = os.environ.get("POSTGRES_DB", "mundidb")
                _pg_user_cc = os.environ.get("POSTGRES_USER", "mundiuser")
                _pg_pass_cc = os.environ.get("POSTGRES_PASSWORD", "gdalpassword")
                _pg_conn_cc = await _asyncpg_cc.connect(
                    host=_pg_host_cc, port=_pg_port_cc,
                    database=_pg_db_cc, user=_pg_user_cc, password=_pg_pass_cc,
                )
                try:
                    _rg_row = await _pg_conn_cc.fetchrow(
                        "SELECT district FROM rwanda_district_boundaries "
                        "WHERE ST_Contains(geom, ST_SetSRID(ST_Point($1, $2), 4326)) "
                        "LIMIT 1",
                        float(_cc_lon), float(_cc_lat),
                    )
                    if _rg_row:
                        _district = _rg_row["district"]
                        logger.info("Crop classifications: reverse-geocoded → district=%s", _district)
                finally:
                    await _pg_conn_cc.close()
            except Exception as _rg_err:
                logger.warning("Reverse-geocode failed for crop classifications: %s", _rg_err)

        if _district:
            _rows = await ctx.conn.fetch(
                "SELECT district, class_label, area_ha, pixel_count, confidence, job_id "
                "FROM crop_classification_cache WHERE district = $1 "
                "ORDER BY computed_at DESC LIMIT 50",
                _district,
            )
        else:
            _rows = await ctx.conn.fetch(
                "SELECT district, class_label, area_ha, pixel_count, confidence, job_id "
                "FROM crop_classification_cache ORDER BY computed_at DESC LIMIT 50"
            )

        if _rows:
            tool_result: Dict[str, Any] = {
                "status": "success",
                "source": "postgres_cache",
                "count": len(_rows),
                "classifications": [
                    {"district": r["district"], "class_label": r["class_label"], "area_ha": r["area_ha"],
                     "pixel_count": r["pixel_count"], "confidence": r["confidence"], "job_id": r["job_id"]}
                    for r in _rows
                ],
            }
            _pgc_id = await _ensure_rwanda_postgis_connection(
                ctx.conn, ctx.project_id, ctx.user_id,
            )
            if _pgc_id:
                tool_result["postgis_connection_id"] = _pgc_id
                tool_result["kue_instructions"] = (
                    "To visualise crop classifications on the map, call new_layer_from_postgis with "
                    f"postgis_connection_id='{_pgc_id}'. IMPORTANT: query MUST return 'id' and 'geom' columns. "
                    "Example: SELECT ROW_NUMBER() OVER() AS id, district AS district_name, geom FROM rwanda_district_boundaries "
                    "Then add_layer_to_map and set_layer_style. "
                    "DO NOT reuse an existing layer — always create a NEW layer from PostGIS."
                )
            return tool_result
        return {
            "status": "success",
            "source": "postgres_cache",
            "classifications": [],
            "message": "No classification data yet — Dagster weekly schedule populates this cache",
        }
    except Exception as e:
        logger.exception("get_crop_classifications tool failed")
        return {"status": "error", "error": str(e)}


async def _handle_get_anomaly_alerts(ctx: LegacyToolContext) -> Dict[str, Any]:
    """Read anomaly_alerts_cache filtered by severity/district.

    Returns z-score-sorted alerts (most negative first = worst anomalies).
    Auto-provisions PostGIS connection so Sage can colour districts by severity.

    Lifted byte-for-byte from message_routes.py:4757-4813.
    """
    args = ctx.arguments
    try:
        from src.routes.message_routes import _ensure_rwanda_postgis_connection

        _where: list[str] = []
        _params: list[Any] = []
        _pidx = 1
        if args.get("severity"):
            _where.append(f"severity = ${_pidx}")
            _params.append(args["severity"])
            _pidx += 1
        if args.get("district"):
            _where.append(f"district = ${_pidx}")
            _params.append(args["district"])
            _pidx += 1
        _where_sql = f"WHERE {' AND '.join(_where)}" if _where else ""
        _rows = await ctx.conn.fetch(
            f"SELECT district, anomaly_date, observed_ndvi, expected_ndvi, "
            f"z_score, severity FROM anomaly_alerts_cache {_where_sql} "
            f"ORDER BY z_score ASC LIMIT 30",
            *_params,
        )

        if _rows:
            tool_result: Dict[str, Any] = {
                "status": "success",
                "source": "postgres_cache",
                "count": len(_rows),
                "alerts": [
                    {"district": r["district"], "date": str(r["anomaly_date"]) if r["anomaly_date"] else None,
                     "observed_ndvi": r["observed_ndvi"], "expected_ndvi": r["expected_ndvi"],
                     "z_score": round(r["z_score"], 3) if r["z_score"] else None, "severity": r["severity"]}
                    for r in _rows
                ],
            }
            _pgc_id = await _ensure_rwanda_postgis_connection(
                ctx.conn, ctx.project_id, ctx.user_id,
            )
            if _pgc_id:
                tool_result["postgis_connection_id"] = _pgc_id
                tool_result["kue_instructions"] = (
                    "To visualise these anomaly alerts on the map, call new_layer_from_postgis with "
                    f"postgis_connection_id='{_pgc_id}'. IMPORTANT: query MUST return 'id' and 'geom' columns. "
                    "Available tables: rwanda_district_boundaries (district, geom). "
                    "Example: SELECT ROW_NUMBER() OVER() AS id, district AS district_name, geom FROM rwanda_district_boundaries "
                    "Then add_layer_to_map and set_layer_style to colour districts by severity. "
                    "DO NOT reuse an existing layer — always create a NEW layer from PostGIS."
                )
            return tool_result
        return {
            "status": "success",
            "source": "postgres_cache",
            "alerts": [],
            "message": "No anomaly alerts yet — Dagster weekly schedule populates this cache",
        }
    except Exception as e:
        logger.exception("get_anomaly_alerts tool failed")
        return {"status": "error", "error": str(e)}


async def _handle_get_yield_risk(ctx: LegacyToolContext) -> Dict[str, Any]:
    """Read yield_risk_cache (Mann-Kendall + seasonal deviation analysis).

    Lifted byte-for-byte from message_routes.py:4823-4870.
    """
    args = ctx.arguments
    try:
        from src.routes.message_routes import _ensure_rwanda_postgis_connection

        _district = args.get("district")
        _where = "WHERE district = $1" if _district else ""
        _params = [_district] if _district else []
        _rows = await ctx.conn.fetch(
            f"SELECT district, risk_level, risk_description, trend_slope, "
            f"kendall_tau, latest_ndvi, mean_ndvi, seasonal_deviation, observations "
            f"FROM yield_risk_cache {_where} "
            f"ORDER BY computed_at DESC LIMIT 50",
            *_params,
        )

        if _rows:
            tool_result: Dict[str, Any] = {
                "status": "success",
                "source": "postgres_cache",
                "count": len(_rows),
                "assessments": [
                    {"district": r["district"], "risk_level": r["risk_level"], "risk_description": r["risk_description"],
                     "trend_slope": r["trend_slope"], "kendall_tau": r["kendall_tau"], "latest_ndvi": r["latest_ndvi"],
                     "mean_ndvi": r["mean_ndvi"], "seasonal_deviation": r["seasonal_deviation"], "observations": r["observations"]}
                    for r in _rows
                ],
            }
            _pgc_id = await _ensure_rwanda_postgis_connection(
                ctx.conn, ctx.project_id, ctx.user_id,
            )
            if _pgc_id:
                tool_result["postgis_connection_id"] = _pgc_id
                tool_result["kue_instructions"] = (
                    "To visualise yield risk on the map, call new_layer_from_postgis with "
                    f"postgis_connection_id='{_pgc_id}'. IMPORTANT: query MUST return 'id' and 'geom' columns. "
                    "Available tables: rwanda_district_boundaries (district, geom). "
                    "Example: SELECT ROW_NUMBER() OVER() AS id, district AS district_name, geom FROM rwanda_district_boundaries "
                    "Then add_layer_to_map and set_layer_style to colour by risk level. "
                    "DO NOT reuse an existing layer — always create a NEW layer from PostGIS."
                )
            return tool_result
        return {
            "status": "success",
            "source": "postgres_cache",
            "assessments": [],
            "message": "No yield risk data yet — Dagster weekly schedule populates this cache",
        }
    except Exception as e:
        logger.exception("get_yield_risk tool failed")
        return {"status": "error", "error": str(e)}


async def _handle_get_drought_status(ctx: LegacyToolContext) -> Dict[str, Any]:
    """Read drought_cache OR fall back to real-time STAC COG computation.

    Two-tier: postgres cache (fast) → STAC Sentinel-2 COG (60-80s/district,
    capped at 3 districts when no specific district requested).

    Hardens against fabrication: marks insufficient_data districts explicitly
    AND adds a top-level note when ALL districts are insufficient, so the
    LLM does NOT claim drought from missing data.

    Lifted byte-for-byte from message_routes.py:4880-5061.
    """
    args = ctx.arguments
    try:
        from src.routes.message_routes import _ensure_rwanda_postgis_connection

        _where: list[str] = []
        _params: list[Any] = []
        _pidx = 1
        if args.get("district"):
            _where.append(f"district = ${_pidx}")
            _params.append(args["district"])
            _pidx += 1
        if args.get("status"):
            _where.append(f"drought_status = ${_pidx}")
            _params.append(args["status"])
            _pidx += 1
        _where_sql = f"WHERE {' AND '.join(_where)}" if _where else ""
        _rows = await ctx.conn.fetch(
            f"SELECT district, drought_status, current_vci, latest_ndvi, "
            f"latest_ndwi, drought_period_count, description "
            f"FROM drought_cache {_where_sql} "
            f"ORDER BY current_vci ASC LIMIT 50",
            *_params,
        )

        if _rows:
            _districts_out: list[Dict[str, Any]] = []
            for r in _rows:
                _d: Dict[str, Any] = {
                    "district": r["district"],
                    "drought_status": r["drought_status"],
                    "vci": r["current_vci"],
                    "latest_ndvi": r["latest_ndvi"],
                    "latest_ndwi": r["latest_ndwi"],
                    "drought_period_count": r["drought_period_count"],
                    "description": r["description"],
                }
                if r["drought_status"] == "insufficient_data":
                    _d["note"] = (
                        "Not enough historical data to assess "
                        "drought for this district yet."
                    )
                _districts_out.append(_d)
            tool_result: Dict[str, Any] = {
                "status": "success",
                "source": "postgres_cache",
                "count": len(_rows),
                "districts": _districts_out,
            }
            if all(
                d["drought_status"] == "insufficient_data"
                for d in _districts_out
            ):
                tool_result["note"] = (
                    "All queried districts have insufficient "
                    "historical NDVI data (<8 weeks) to compute "
                    "a reliable drought index. Do NOT report "
                    "drought status — instead tell the user that "
                    "not enough data has been collected yet."
                )
            _pgc_id = await _ensure_rwanda_postgis_connection(
                ctx.conn, ctx.project_id, ctx.user_id,
            )
            if _pgc_id:
                tool_result["postgis_connection_id"] = _pgc_id
                tool_result["kue_instructions"] = (
                    "To visualise drought status on the map, call new_layer_from_postgis with "
                    f"postgis_connection_id='{_pgc_id}'. IMPORTANT: query MUST return 'id' and 'geom' columns. "
                    "Available tables: rwanda_district_boundaries (district, geom). "
                    "Example: SELECT ROW_NUMBER() OVER() AS id, district AS district_name, geom FROM rwanda_district_boundaries "
                    "Then add_layer_to_map and set_layer_style to colour by drought status. "
                    "DO NOT reuse an existing layer — always create a NEW layer from PostGIS."
                )
            return tool_result

        # ── STAC COG real-time fallback ──
        try:
            from src.services.stac_service import get_stac_service as _get_stac

            _stac = _get_stac()
            _drought_district = args.get("district")

            if _drought_district:
                _bbox_rows = await ctx.conn.fetch(
                    "SELECT district, bbox_west, bbox_south, bbox_east, bbox_north "
                    "FROM rwanda_district_boundaries WHERE LOWER(district) = LOWER($1)",
                    _drought_district,
                )
            else:
                _bbox_rows = await ctx.conn.fetch(
                    "SELECT district, bbox_west, bbox_south, bbox_east, bbox_north "
                    "FROM rwanda_district_boundaries ORDER BY district"
                )

            _stac_districts: list[Dict[str, Any]] = []
            for _br in _bbox_rows:
                _d_bbox = [float(_br["bbox_west"]), float(_br["bbox_south"]),
                           float(_br["bbox_east"]), float(_br["bbox_north"])]
                _drought_result = await asyncio.get_event_loop().run_in_executor(
                    None, lambda bb=_d_bbox: _stac.compute_drought_indicators(bb),
                )
                if "error" not in _drought_result:
                    _stac_districts.append({
                        "district": _br["district"],
                        "drought_status": _drought_result.get("drought_status"),
                        "vci": _drought_result.get("current_vci"),
                        "latest_ndvi": _drought_result.get("latest_ndvi"),
                        "latest_ndwi": None,
                        "drought_period_count": None,
                        "description": _drought_result.get("description"),
                        "trend_slope": _drought_result.get("trend_slope"),
                        "scene_count": _drought_result.get("scene_count"),
                    })
                else:
                    logger.debug("STAC drought failed for %s: %s", _br["district"], _drought_result.get("error"))
                if not _drought_district and len(_stac_districts) >= 3:
                    break

            if _stac_districts:
                _all_insufficient = all(
                    d["drought_status"] == "insufficient_data"
                    for d in _stac_districts
                )
                if _all_insufficient:
                    _stac_note = (
                        "Not enough cloud-free Sentinel-2 scenes to compute "
                        "a reliable drought index. Do NOT report drought "
                        "status — tell the user there is insufficient data. "
                        "The weekly Dagster pipeline will accumulate enough "
                        "history over time for accurate VCI analysis."
                    )
                else:
                    _stac_note = (
                        "Drought status computed in real-time from Sentinel-2 COGs via STAC. "
                        "VCI (Vegetation Condition Index): <10=extreme, 10-20=severe, "
                        "20-35=moderate, 35-50=mild, >50=no drought."
                    )
                tool_result = {
                    "status": "success",
                    "source": "stac_cog_realtime",
                    "count": len(_stac_districts),
                    "note": _stac_note,
                    "districts": _stac_districts,
                }
                _pgc_id = await _ensure_rwanda_postgis_connection(
                    ctx.conn, ctx.project_id, ctx.user_id,
                )
                if _pgc_id:
                    tool_result["postgis_connection_id"] = _pgc_id
                    tool_result["kue_instructions"] = (
                        "To visualise drought status on the map, call new_layer_from_postgis with "
                        f"postgis_connection_id='{_pgc_id}'. IMPORTANT: query MUST return 'id' and 'geom' columns. "
                        "Available tables: rwanda_district_boundaries (district, geom). "
                        "Example: SELECT ROW_NUMBER() OVER() AS id, district AS district_name, geom FROM rwanda_district_boundaries "
                        "Then add_layer_to_map and set_layer_style to colour by drought status. "
                        "DO NOT reuse an existing layer — always create a NEW layer from PostGIS."
                    )
                return tool_result
            return {
                "status": "success",
                "source": "stac_cog_realtime",
                "districts": [],
                "message": (
                    "Could not compute drought indicators — insufficient cloud-free "
                    "Sentinel-2 scenes in the last 90 days for this area."
                ),
            }
        except Exception as _stac_err:
            logger.warning("STAC drought fallback failed: %s", _stac_err)
            return {
                "status": "success",
                "source": "postgres_cache",
                "districts": [],
                "message": "No drought data yet — Dagster weekly schedule populates this cache",
            }
    except Exception as e:
        logger.exception("get_drought_status tool failed")
        return {"status": "error", "error": str(e)}


async def _handle_get_crop_growth_stage(ctx: LegacyToolContext) -> Dict[str, Any]:
    """Read phenology_cache (current growth stage by district/stage filter).

    Lifted byte-for-byte from message_routes.py:5071-5127.
    """
    args = ctx.arguments
    try:
        from src.routes.message_routes import _ensure_rwanda_postgis_connection

        _where: list[str] = []
        _params: list[Any] = []
        _pidx = 1
        if args.get("district"):
            _where.append(f"district = ${_pidx}")
            _params.append(args["district"])
            _pidx += 1
        if args.get("stage"):
            _where.append(f"current_stage = ${_pidx}")
            _params.append(args["stage"])
            _pidx += 1
        _where_sql = f"WHERE {' AND '.join(_where)}" if _where else ""
        _rows = await ctx.conn.fetch(
            f"SELECT district, current_stage, peak_ndvi, peak_date, "
            f"green_up_start, senescence_start, harvest_date, observations "
            f"FROM phenology_cache {_where_sql} "
            f"ORDER BY computed_at DESC LIMIT 50",
            *_params,
        )

        if _rows:
            tool_result: Dict[str, Any] = {
                "status": "success",
                "source": "postgres_cache",
                "count": len(_rows),
                "districts": [
                    {"district": r["district"], "current_stage": r["current_stage"], "peak_ndvi": r["peak_ndvi"],
                     "peak_date": r["peak_date"], "green_up_start": r["green_up_start"],
                     "senescence_start": r["senescence_start"], "harvest_date": r["harvest_date"], "observations": r["observations"]}
                    for r in _rows
                ],
            }
            _pgc_id = await _ensure_rwanda_postgis_connection(
                ctx.conn, ctx.project_id, ctx.user_id,
            )
            if _pgc_id:
                tool_result["postgis_connection_id"] = _pgc_id
                tool_result["kue_instructions"] = (
                    "To visualise crop growth stages on the map, call new_layer_from_postgis with "
                    f"postgis_connection_id='{_pgc_id}'. IMPORTANT: query MUST return 'id' and 'geom' columns. "
                    "Available tables: rwanda_district_boundaries (district, geom). "
                    "Example: SELECT ROW_NUMBER() OVER() AS id, district AS district_name, geom FROM rwanda_district_boundaries "
                    "Then add_layer_to_map and set_layer_style to colour by growth stage. "
                    "DO NOT reuse an existing layer — always create a NEW layer from PostGIS."
                )
            return tool_result
        return {
            "status": "success",
            "source": "postgres_cache",
            "districts": [],
            "message": "No phenology data yet — Dagster weekly schedule populates this cache",
        }
    except Exception as e:
        logger.exception("get_crop_growth_stage tool failed")
        return {"status": "error", "error": str(e)}


async def _handle_get_soil_properties(ctx: LegacyToolContext) -> Dict[str, Any]:
    """Query iSDAsoil for a single lat/lon, enrich with display_layer hints.

    Returns soil property values + a `displayable_layers` list keyed by
    style_hint so Sage can paint the map with the same COG it just queried.
    `display_bbox` suggests a ~5km auto-zoom around the queried point.

    Lifted byte-for-byte from message_routes.py:3763-3815.
    """
    args = ctx.arguments
    try:
        from src.services.isdasoil_service import (
            query_soil_point,
            _cog_url,
        )

        lon = args.get("longitude")
        lat = args.get("latitude")
        properties = args.get("properties")
        depth = args.get("depth", "0-20")

        result_data = await asyncio.get_event_loop().run_in_executor(
            None, lambda: query_soil_point(
                lon=lon,
                lat=lat,
                properties=properties,
                depth=depth,
            )
        )

        if "error" in result_data:
            return {"status": "error", "error": result_data["error"]}

        _style_hint_map = {
            "nitrogen_total": "soil_nitrogen",
            "phosphorous_extractable": "soil_phosphorus",
            "potassium_extractable": "soil_potassium",
            "ph": "soil_ph",
            "carbon_organic": "soil_organic_carbon",
            "clay_content": "soil_clay",
            "sand_content": "soil_sand",
        }
        _display_layers: list[dict] = []
        for prop_name in (result_data.get("properties") or {}).keys():
            if prop_name in _style_hint_map:
                _display_layers.append({
                    "asset_url": _cog_url(prop_name),
                    "style_hint": _style_hint_map[prop_name],
                    "title": result_data["properties"][prop_name].get("label", prop_name),
                    "band_index": 1 if depth == "0-20" else 2,
                })
        _half_deg = 0.05
        result_data["display_bbox"] = (
            f"{lon - _half_deg},{lat - _half_deg},"
            f"{lon + _half_deg},{lat + _half_deg}"
        )
        result_data["displayable_layers"] = _display_layers
        return result_data
    except Exception as e:
        logger.exception("get_soil_properties tool failed")
        return {"status": "error", "error": str(e)}


async def _handle_get_weather_stats(ctx: LegacyToolContext) -> Dict[str, Any]:
    """AgERA5 (PostgreSQL cache) + Open-Meteo recent-gap fill, merged by date.

    AgERA5 is the source of truth for historical (~5-8 day latency); Open-Meteo
    NWP reanalysis fills the recent days. Returns up to 300 records sorted by
    date desc, district. Auto-provisions Rwanda PostGIS connection.

    Lifted byte-for-byte from message_routes.py:5137-5288.
    """
    args = ctx.arguments
    try:
        from src.routes.message_routes import _ensure_rwanda_postgis_connection

        # ── 1. Query AgERA5 cache (PostgreSQL) ──
        _agera5_rows: list = []
        try:
            _where: list[str] = []
            _params: list[Any] = []
            _pidx = 1
            if args.get("district"):
                _where.append(f"district = ${_pidx}")
                _params.append(args["district"])
                _pidx += 1
            if args.get("date_from"):
                _where.append(f"observation_date >= ${_pidx}")
                _params.append(args["date_from"])
                _pidx += 1
            if args.get("date_to"):
                _where.append(f"observation_date <= ${_pidx}")
                _params.append(args["date_to"])
                _pidx += 1
            if not args.get("date_from") and not args.get("date_to"):
                _where.append("observation_date >= CURRENT_DATE - INTERVAL '30 days'")
            _where_sql = f"WHERE {' AND '.join(_where)}" if _where else ""
            _agera5_rows = await ctx.conn.fetch(
                f"SELECT district, observation_date, temperature_mean, "
                f"temperature_max, temperature_min, precipitation, "
                f"solar_radiation "
                f"FROM weather_daily_cache {_where_sql} "
                f"ORDER BY observation_date DESC, district LIMIT 500",
                *_params,
            )
        except Exception:
            logger.debug("PostgreSQL cache not available, will use Open-Meteo only")

        # Build result list from AgERA5
        _agera5_dates: set[str] = set()
        _weather_stats: list[Dict[str, Any]] = []
        for r in _agera5_rows:
            _dt = str(r["observation_date"]) if r["observation_date"] else None
            if _dt:
                _agera5_dates.add(_dt)
            _weather_stats.append({
                "district": r["district"],
                "date": _dt,
                "temperature_mean_c": r["temperature_mean"],
                "temperature_max_c": r["temperature_max"],
                "temperature_min_c": r["temperature_min"],
                "precipitation_mm_day": r["precipitation"],
                "solar_radiation_mj_m2_day": r["solar_radiation"],
                "source": "agera5",
            })

        # ── 2. Fill recent gap with Open-Meteo ──
        _openmeteo_stats: list[Dict[str, Any]] = []
        try:
            from src.services.weather_service import get_weather_service as _get_ws

            _centroids: list = []
            async with ctx.conn.transaction():
                _cent_rows = await ctx.conn.fetch(
                    "SELECT district, "
                    "round(ST_Y(ST_Centroid(geom))::numeric, 4) as lat, "
                    "round(ST_X(ST_Centroid(geom))::numeric, 4) as lon "
                    "FROM rwanda_district_boundaries ORDER BY district"
                )
                _centroids = [
                    {"district": r["district"], "lat": float(r["lat"]), "lon": float(r["lon"])}
                    for r in _cent_rows
                ]

            if _centroids:
                _ws = _get_ws()
                if _ws:
                    _om_data = _ws.fetch_openmeteo_districts(_centroids, past_days=10)
                    _filter_district = args.get("district")
                    _filter_from = args.get("date_from")
                    _filter_to = args.get("date_to")
                    for om in _om_data:
                        if om["date"] in _agera5_dates:
                            continue  # AgERA5 is more accurate, skip
                        if _filter_district and om["district"] != _filter_district:
                            continue
                        if _filter_from and om["date"] < _filter_from:
                            continue
                        if _filter_to and om["date"] > _filter_to:
                            continue
                        _openmeteo_stats.append({
                            "district": om["district"],
                            "date": om["date"],
                            "temperature_mean_c": om["temperature_mean"],
                            "temperature_max_c": om["temperature_max"],
                            "temperature_min_c": om["temperature_min"],
                            "precipitation_mm_day": om["precipitation"],
                            "solar_radiation_mj_m2_day": om["solar_radiation"],
                            "source": "nwp-reanalysis",
                        })
        except Exception as _om_err:
            logger.warning("Open-Meteo supplement failed: %s", _om_err)

        # ── 3. Merge and sort ──
        _all_stats = _weather_stats + _openmeteo_stats
        _all_stats.sort(key=lambda s: (s.get("date") or "", s.get("district") or ""), reverse=True)
        _all_stats = _all_stats[:300]

        if _all_stats:
            _sources = set(s.get("source", "agera5") for s in _all_stats)
            _source_str = " + ".join(sorted(_sources))
            tool_result: Dict[str, Any] = {
                "status": "success",
                "source": _source_str,
                "spatial_resolution": "district-level (~10km grid, one value per district)",
                "count": len(_all_stats),
                "agera5_records": len(_weather_stats),
                "openmeteo_records": len(_openmeteo_stats),
                "note": (
                    "This data is aggregated at DISTRICT level from a ~10km grid. "
                    "Actual weather varies within a district due to elevation and terrain. "
                    "If the user asks about a specific sector or location, note that these are "
                    "district-level averages and suggest using get_forecast with exact lat/lon "
                    "for more precise local conditions. "
                    "AgERA5 (Copernicus reanalysis) covers older dates. "
                    "NWP reanalysis (ECMWF/GFS/ICON) covers recent days."
                ),
                "weather_stats": _all_stats,
            }
            _pgc_id = await _ensure_rwanda_postgis_connection(
                ctx.conn, ctx.project_id, ctx.user_id,
            )
            if _pgc_id:
                tool_result["postgis_connection_id"] = _pgc_id
                tool_result["kue_instructions"] = (
                    "To visualise weather data on the map, call new_layer_from_postgis with "
                    f"postgis_connection_id='{_pgc_id}'. IMPORTANT: query MUST return 'id' and 'geom' columns. "
                    "Available tables: rwanda_district_boundaries (district, geom). "
                    "Example: SELECT ROW_NUMBER() OVER() AS id, district AS district_name, geom FROM rwanda_district_boundaries "
                    "Then add_layer_to_map and set_layer_style to colour by temperature or precipitation. "
                    "DO NOT reuse an existing layer — always create a NEW layer from PostGIS."
                )
            return tool_result
        return {
            "status": "success",
            "weather_stats": [],
            "message": (
                "No weather data available. DuckDB cache is empty and real-time weather "
                "fetch did not return results. Check network connectivity."
            ),
        }
    except Exception as e:
        logger.exception("get_weather_stats tool failed")
        return {"status": "error", "error": str(e)}


async def _handle_get_forecast_accuracy(ctx: LegacyToolContext) -> Dict[str, Any]:
    """Forecast vs AgERA5 reanalysis (ground truth), MAE + bias per district.

    Compares forecast.daily.temperature_max & precipitation_mm against the
    weather_daily_cache. Defaults to 30-day lookback, configurable. Iterates
    district centroids; failures per-district are silently skipped (LLM gets
    the surviving sample).

    Lifted byte-for-byte from message_routes.py:5355-5473.
    """
    args = ctx.arguments
    try:
        import asyncio as _aio2
        from src.services.forecast_service import get_farm_forecast

        _acc_district = args.get("district")

        if _acc_district:
            _acc_rows = await ctx.conn.fetch(
                "SELECT district, "
                "round(ST_Y(ST_Centroid(geom))::numeric, 4) as lat, "
                "round(ST_X(ST_Centroid(geom))::numeric, 4) as lon "
                "FROM rwanda_district_boundaries WHERE district ILIKE $1",
                _acc_district,
            )
        else:
            _acc_rows = await ctx.conn.fetch(
                "SELECT district, "
                "round(ST_Y(ST_Centroid(geom))::numeric, 4) as lat, "
                "round(ST_X(ST_Centroid(geom))::numeric, 4) as lon "
                "FROM rwanda_district_boundaries ORDER BY district"
            )

        from datetime import date as _date, timedelta as _td
        _lookback = int(args.get("lookback_days", 30))
        _cutoff = _date.today() - _td(days=_lookback)
        _obs_rows = await ctx.conn.fetch(
            "SELECT district, observation_date, temperature_mean, "
            "temperature_max, temperature_min, precipitation "
            "FROM weather_daily_cache "
            "WHERE observation_date >= $1 "
            "ORDER BY observation_date DESC, district",
            _cutoff,
        )

        _obs_lookup: Dict[tuple, Dict[str, Any]] = {}
        for r in _obs_rows:
            key = (r["district"], str(r["observation_date"]))
            _obs_lookup[key] = {
                "temp_mean": float(r["temperature_mean"]) if r["temperature_mean"] else None,
                "temp_max": float(r["temperature_max"]) if r["temperature_max"] else None,
                "temp_min": float(r["temperature_min"]) if r["temperature_min"] else None,
                "precip": float(r["precipitation"]) if r["precipitation"] else None,
            }

        _model_errors: Dict[str, list] = {"temp_errors": [], "precip_errors": [], "comparisons": []}

        for _r in _acc_rows:
            _d_name = _r["district"]
            _d_lat, _d_lon = float(_r["lat"]), float(_r["lon"])

            try:
                _fc = await _aio2.get_event_loop().run_in_executor(
                    None,
                    lambda lat=_d_lat, lon=_d_lon: get_farm_forecast(
                        lat, lon, forecast_days=3,
                    ),
                )
            except Exception:
                continue

            _fc_daily = _fc.get("daily", [])
            for _fd in _fc_daily:
                _fd_date = _fd.get("date")
                _obs = _obs_lookup.get((_d_name, _fd_date))
                if not _obs:
                    continue

                _fc_tmax = _fd.get("temperature_max")
                _fc_precip = _fd.get("precipitation_mm")

                if _fc_tmax is not None and _obs["temp_max"] is not None:
                    _fc_t = _fc_tmax["mean"] if isinstance(_fc_tmax, dict) else _fc_tmax
                    _model_errors["temp_errors"].append(_fc_t - _obs["temp_max"])

                if _fc_precip is not None and _obs["precip"] is not None:
                    _fc_p = _fc_precip["mean"] if isinstance(_fc_precip, dict) else _fc_precip
                    _model_errors["precip_errors"].append(_fc_p - _obs["precip"])

                _model_errors["comparisons"].append({
                    "district": _d_name,
                    "date": _fd_date,
                    "forecast_tmax": _fc_tmax["mean"] if isinstance(_fc_tmax, dict) else _fc_tmax,
                    "observed_tmax": _obs["temp_max"],
                    "forecast_precip": _fc_precip["mean"] if isinstance(_fc_precip, dict) else _fc_precip,
                    "observed_precip": _obs["precip"],
                })

        _te = _model_errors["temp_errors"]
        _pe = _model_errors["precip_errors"]
        _accuracy_result = {
            "comparison_count": len(_model_errors["comparisons"]),
            "temperature": {
                "mae_celsius": round(sum(abs(e) for e in _te) / len(_te), 2) if _te else None,
                "bias_celsius": round(sum(_te) / len(_te), 2) if _te else None,
            },
            "precipitation": {
                "mae_mm": round(sum(abs(e) for e in _pe) / len(_pe), 2) if _pe else None,
                "bias_mm": round(sum(_pe) / len(_pe), 2) if _pe else None,
            },
            "sample_comparisons": _model_errors["comparisons"][:10],
        }

        _obs_dates = sorted(set(str(r["observation_date"]) for r in _obs_rows))
        return {
            "status": "success",
            "source": "Multi-model ensemble — ECMWF IFS + GFS + ICON + GraphCast",
            "note": (
                "Accuracy = forecast vs AgERA5 reanalysis (ground truth). "
                "MAE = mean absolute error. Bias = systematic over/under prediction. "
                "Positive bias = forecast runs hot/wet. "
                "AgERA5 has ~5-8 day latency so comparisons are for recent overlapping dates."
            ),
            "observed_dates": _obs_dates,
            **_accuracy_result,
        }
    except Exception as e:
        logger.exception("get_forecast_accuracy tool failed")
        return {"status": "error", "error": str(e)}


async def _handle_get_insurance_accuracy(ctx: LegacyToolContext) -> Dict[str, Any]:
    """Thin wrapper around weather_accuracy.compute_insurance_accuracy.

    Lifted byte-for-byte from message_routes.py:5509-5520.
    """
    args = ctx.arguments
    try:
        from src.services.weather_accuracy import compute_insurance_accuracy as _compute_ins
        return await _compute_ins(
            ctx.conn,
            district=args.get("district"),
            season=args.get("season"),
            threshold_mm=float(args.get("threshold_mm", 5.0)),
        )
    except Exception as e:
        logger.exception("get_insurance_accuracy tool failed")
        return {"status": "error", "error": str(e)}


async def _handle_get_emissions_stats(ctx: LegacyToolContext) -> Dict[str, Any]:
    """EDGAR v8.0 emissions cache, multi-dim filter (year, type, sector, district).

    Returns up to 500 records sorted by year desc. Defaults to last 7 years
    when no year filter is given. Auto-provisions Rwanda PostGIS connection
    with a join-template in kue_instructions for choropleth maps.

    Lifted byte-for-byte from message_routes.py:5758-5850.
    """
    args = ctx.arguments
    try:
        from src.routes.message_routes import _ensure_rwanda_postgis_connection

        _em_where: list[str] = []
        _em_params: list[Any] = []
        _em_pidx = 1
        if args.get("district"):
            _em_where.append(f"district = ${_em_pidx}")
            _em_params.append(args["district"])
            _em_pidx += 1
        if args.get("year"):
            _em_where.append(f"year = ${_em_pidx}")
            _em_params.append(int(args["year"]))
            _em_pidx += 1
        if args.get("year_from"):
            _em_where.append(f"year >= ${_em_pidx}")
            _em_params.append(int(args["year_from"]))
            _em_pidx += 1
        if args.get("year_to"):
            _em_where.append(f"year <= ${_em_pidx}")
            _em_params.append(int(args["year_to"]))
            _em_pidx += 1
        if args.get("emission_type"):
            _em_where.append(f"emission_type = ${_em_pidx}")
            _em_params.append(args["emission_type"])
            _em_pidx += 1
        if args.get("sector"):
            _em_where.append(f"sector = ${_em_pidx}")
            _em_params.append(args["sector"])
            _em_pidx += 1
        if not args.get("year") and not args.get("year_from") and not args.get("year_to"):
            _em_where.append("year >= EXTRACT(YEAR FROM CURRENT_DATE)::int - 6")

        _em_where_sql = f"WHERE {' AND '.join(_em_where)}" if _em_where else ""
        _em_rows = await ctx.conn.fetch(
            f"SELECT district, year, emission_type, sector, "
            f"sector_label, total_tonnes, grid_cells "
            f"FROM emissions_annual_cache {_em_where_sql} "
            f"ORDER BY year DESC, district, emission_type, sector "
            f"LIMIT 500",
            *_em_params,
        )

        _emissions_stats: list[Dict[str, Any]] = []
        for r in _em_rows:
            _emissions_stats.append({
                "district": r["district"],
                "year": r["year"],
                "emission_type": r["emission_type"],
                "sector": r["sector"],
                "sector_label": r["sector_label"],
                "total_tonnes": round(r["total_tonnes"], 2) if r["total_tonnes"] else None,
                "grid_cells": r["grid_cells"],
            })

        if _emissions_stats:
            tool_result: Dict[str, Any] = {
                "status": "success",
                "source": "EDGAR v8.0 (JRC)",
                "count": len(_emissions_stats),
                "note": (
                    "EDGAR v8.0 emissions data from the Joint Research Centre. "
                    "Values are total tonnes per district per year. "
                    "Sectors: AGS=Agricultural soils, ENF=Enteric fermentation, "
                    "MNM=Manure management, AWB=Agricultural waste burning."
                ),
                "emissions_stats": _emissions_stats,
            }
            _pgc_id = await _ensure_rwanda_postgis_connection(
                ctx.conn, ctx.project_id, ctx.user_id,
            )
            if _pgc_id:
                tool_result["postgis_connection_id"] = _pgc_id
                tool_result["kue_instructions"] = (
                    "To visualise emissions data on the map, call new_layer_from_postgis with "
                    f"postgis_connection_id='{_pgc_id}'. IMPORTANT: query MUST return 'id' and 'geom' columns. "
                    "Join emissions_annual_cache with rwanda_district_boundaries on district. "
                    "Example: SELECT ROW_NUMBER() OVER() AS id, e.district, e.total_tonnes, e.emission_type, "
                    "e.year, b.geom FROM emissions_annual_cache e JOIN rwanda_district_boundaries b "
                    "ON e.district = b.district WHERE e.emission_type = 'CH4' AND e.year = 2022 "
                    "Then add_layer_to_map and set_layer_style to colour by total_tonnes. "
                    "DO NOT reuse an existing layer — always create a NEW layer from PostGIS."
                )
            return tool_result
        return {
            "status": "success",
            "emissions_stats": [],
            "message": (
                "No emissions data available. The emissions_annual_cache table "
                "may not be populated yet. Trigger the annual_emissions_ingest "
                "Dagster asset to load EDGAR data."
            ),
        }
    except Exception as e:
        logger.exception("get_emissions_stats tool failed")
        return {"status": "error", "error": str(e)}


async def _handle_search_brain(ctx: LegacyToolContext) -> Dict[str, Any]:
    """Hybrid (BM25 + embedding) search over Brain pages, with query expansion.

    Generates query variants, embeds in batch, runs N parallel hybrid
    searches (each on its own RLS-scoped connection), dedupes by
    (slug, chunk-text-prefix), and returns top-K by score.

    Lifted byte-for-byte from message_routes.py:6175-6228.
    """
    args = ctx.arguments
    try:
        from src.dependencies.brain_dep import get_brain_service
        from src.database.pool import get_async_db_connection
        from src.services.brain_embeddings import _get_embeddings, expand_query
        import asyncio as _aio

        _brain_svc = get_brain_service()
        _query = args.get("query", "")
        _type = args.get("type")
        _limit = args.get("limit", 10)

        # Multi-query expansion
        try:
            _variants = await expand_query(_query)
            _all_embeddings, _ = await _get_embeddings(_variants)
        except Exception:
            logger.debug("Query expansion/embedding failed, falling back to keyword-only")
            _variants = [_query]
            _all_embeddings = []

        async def _search_variant(vq: str, vemb):
            async with get_async_db_connection(user_id=ctx.user_id, partner_id=ctx.partner_id) as vc:
                return await _brain_svc.search_hybrid(
                    vc, vq, embedding=vemb, limit=_limit, type=_type
                )

        _variant_args = [
            (_vq, _all_embeddings[_vi] if _vi < len(_all_embeddings) else None)
            for _vi, _vq in enumerate(_variants)
        ]
        _all_results = await _aio.gather(
            *(_search_variant(vq, vemb) for vq, vemb in _variant_args)
        )

        _seen_keys: set[str] = set()
        _results: list = []
        for _vresults in _all_results:
            for _r in _vresults:
                _key = f"{_r.slug}:{_r.chunk_text[:80] if _r.chunk_text else ''}"
                if _key not in _seen_keys:
                    _seen_keys.add(_key)
                    _results.append(_r)
        _results.sort(key=lambda r: r.score, reverse=True)
        _results = _results[:_limit]

        return {
            "status": "success",
            "results": [
                {
                    "slug": r.slug,
                    "title": r.title,
                    "type": r.type,
                    "chunk_text": r.chunk_text,
                    "score": r.score,
                }
                for r in _results
            ],
            "count": len(_results),
        }
    except Exception as e:
        logger.exception("search_brain tool failed")
        return {"status": "error", "error": str(e)}


async def _handle_get_entity(ctx: LegacyToolContext) -> Dict[str, Any]:
    """Fetch a Brain page by slug with timeline (20 most recent), tags, links.

    Returns `not_found` status (no error) when slug doesn't exist so the LLM
    can tell the user the page hasn't been created yet vs. surface an error.

    Lifted byte-for-byte from message_routes.py:6241-6267.
    """
    args = ctx.arguments
    try:
        from src.dependencies.brain_dep import get_brain_service
        from src.database.pool import get_async_db_connection

        _brain_svc = get_brain_service()
        _slug = args.get("slug", "")
        async with get_async_db_connection(user_id=ctx.user_id, partner_id=ctx.partner_id) as _brain_conn:
            _page = await _brain_svc.get_page(_brain_conn, _slug)
            if _page:
                _timeline = await _brain_svc.get_timeline(_brain_conn, _slug, limit=20)
                _tags = await _brain_svc.get_tags(_brain_conn, _slug)
                _links = await _brain_svc.get_links(_brain_conn, _slug)
                return {
                    "status": "success",
                    "slug": _page.slug,
                    "title": _page.title,
                    "type": _page.type,
                    "compiled_truth": _page.compiled_truth,
                    "timeline": [
                        {"date": str(t.get("date", "")), "source": t.get("source", ""), "summary": t.get("summary", "")}
                        for t in _timeline
                    ],
                    "tags": _tags,
                    "links": [{"to_slug": _l.get("to_slug", ""), "link_type": _l.get("link_type", "")} for _l in _links],
                }
            return {"status": "not_found", "slug": _slug}
    except Exception as e:
        logger.exception("get_entity tool failed")
        return {"status": "error", "error": str(e)}


async def _handle_add_observation(ctx: LegacyToolContext) -> Dict[str, Any]:
    """Append a timeline observation to a Brain page.

    Date defaults to today when not supplied. Source defaults to 'user_report'.

    Lifted byte-for-byte from message_routes.py:6280-6307.
    """
    args = ctx.arguments
    try:
        from src.dependencies.brain_dep import get_brain_service
        from src.database.pool import get_async_db_connection
        from datetime import date as _date_type
        from src.services.brain_service import TimelineInput

        _brain_svc = get_brain_service()
        _slug = args.get("slug", "")
        _summary = args.get("summary", "")
        _detail = args.get("detail", "")
        _date_str = args.get("date")
        _source = args.get("source", "user_report")
        _entry = TimelineInput(
            date=_date_type.fromisoformat(_date_str) if _date_str else _date_type.today(),
            summary=_summary,
            detail=_detail,
            source=_source,
        )
        async with get_async_db_connection(user_id=ctx.user_id, partner_id=ctx.partner_id) as _brain_conn:
            _entry_id = await _brain_svc.add_timeline_entry(
                _brain_conn, _slug, _entry, owner_uuid=ctx.user_id
            )
        return {
            "status": "success",
            "entry_id": _entry_id,
            "slug": _slug,
            "summary": _summary,
        }
    except Exception as e:
        logger.exception("add_observation tool failed")
        return {"status": "error", "error": str(e)}


async def _handle_search_satellite_imagery(ctx: LegacyToolContext) -> Dict[str, Any]:
    """STAC search + opportunistic NDVI compute for the first item with B04+B08.

    NDVI sample fails silently to None so the search results are still useful
    when the first scene happens to be missing bands.

    Lifted byte-for-byte from message_routes.py:3002-3049.
    """
    args = ctx.arguments
    try:
        from src.services.stac_service import get_stac_service

        bbox_str = args.get("bbox")
        parsed_bbox = None
        if bbox_str:
            parsed_bbox = [float(x) for x in bbox_str.split(",")]

        service = get_stac_service()
        result_data = await asyncio.get_event_loop().run_in_executor(
            None, lambda: service.search_imagery(
                bbox=parsed_bbox,
                datetime_range=args.get("datetime_range"),
                max_cloud_cover=args.get("max_cloud_cover", 20.0),
                limit=args.get("limit", 10),
            )
        )

        if "error" in result_data:
            return {"status": "error", "error": result_data["error"]}

        ndvi_computed = None
        items = result_data.get("items", [])
        if items:
            first_item = items[0]
            assets = first_item.get("assets", {})
            if "B04" in assets and "B08" in assets:
                try:
                    ndvi_computed = await asyncio.get_event_loop().run_in_executor(
                        None, lambda: service.compute_ndvi_from_item(first_item)
                    )
                    if "error" in ndvi_computed:
                        logger.warning(
                            "NDVI computation failed for first item: %s",
                            ndvi_computed.get("error")
                        )
                        ndvi_computed = None
                except Exception as e:
                    logger.warning("NDVI computation failed: %s", e)
                    ndvi_computed = None

        return {
            "status": "success",
            "search_results": result_data,
            "ndvi_sample": ndvi_computed,
        }
    except Exception as e:
        logger.exception("STAC search tool failed")
        return {"status": "error", "error": str(e)}


async def _handle_query_worldcover_stats(ctx: LegacyToolContext) -> Dict[str, Any]:
    """ESRI 10m LULC stats — admin precomputed, on-the-fly bbox, or largest_cropland CC analysis.

    Three branches:
    1. query_type=largest_cropland → connected-component analysis on cropland
       pixels within the resolved boundary (cell > sector > district > bbox).
    2. land_cover + bbox → on-the-fly zonal stats from COGs via WarpedVRT.
    3. land_cover + admin (or no filter) → read worldcover_admin_stats cache.

    Auto-reverse-geocodes lat/lon to the most-specific admin (cell > sector >
    district) when no admin/bbox was passed.

    Lifted byte-for-byte from message_routes.py:4307-4655. The original
    `continue` statements at validation points become early `return`s here.
    """
    args = ctx.arguments
    try:
        _wc_query_type = args.get("query_type", "land_cover")
        _wc_district = args.get("district")
        _wc_sector = args.get("sector")
        _wc_cell = args.get("cell")
        _wc_bbox = args.get("bbox")
        _wc_lat = args.get("lat")
        _wc_lon = args.get("lon")
        _wc_limit = args.get("limit", 10)

        # Reverse-geocode lat/lon → most-specific admin when nothing else given
        if _wc_lat is not None and _wc_lon is not None and not (_wc_district or _wc_sector or _wc_cell or _wc_bbox):
            try:
                import asyncpg as _asyncpg_rg
                _pg_host_rg = os.environ.get("POSTGRES_HOST", "postgresdb")
                _pg_port_rg = int(os.environ.get("POSTGRES_PORT", "5432"))
                _pg_db_rg = os.environ.get("POSTGRES_DB", "mundidb")
                _pg_user_rg = os.environ.get("POSTGRES_USER", "mundiuser")
                _pg_pass_rg = os.environ.get("POSTGRES_PASSWORD", "gdalpassword")
                _pg_conn_rg = await _asyncpg_rg.connect(
                    host=_pg_host_rg, port=_pg_port_rg,
                    database=_pg_db_rg, user=_pg_user_rg, password=_pg_pass_rg,
                )
                try:
                    _rg_row = await _pg_conn_rg.fetchrow(
                        "SELECT cell_name, sector_name, district_name "
                        "FROM rwanda_cell_boundaries "
                        "WHERE ST_Contains(geom, ST_SetSRID(ST_Point($1, $2), 4326)) "
                        "LIMIT 1",
                        float(_wc_lon), float(_wc_lat),
                    )
                    if _rg_row:
                        _wc_cell = _rg_row["cell_name"]
                        _wc_sector = _rg_row["sector_name"]
                        _wc_district = _rg_row["district_name"]
                        logger.info(
                            "Reverse-geocoded %.4f,%.4f → cell=%s sector=%s district=%s",
                            _wc_lat, _wc_lon, _wc_cell, _wc_sector, _wc_district,
                        )
                    else:
                        _rg_row = await _pg_conn_rg.fetchrow(
                            "SELECT district FROM rwanda_district_boundaries "
                            "WHERE ST_Contains(geom, ST_SetSRID(ST_Point($1, $2), 4326)) "
                            "LIMIT 1",
                            float(_wc_lon), float(_wc_lat),
                        )
                        if _rg_row:
                            _wc_district = _rg_row["district"]
                            logger.info(
                                "Reverse-geocoded %.4f,%.4f → district=%s",
                                _wc_lat, _wc_lon, _wc_district,
                            )
                finally:
                    await _pg_conn_rg.close()
            except Exception as _rg_err:
                logger.warning("Reverse-geocode failed for %.4f,%.4f: %s", _wc_lat, _wc_lon, _rg_err)

        if _wc_query_type == "largest_cropland":
            import asyncpg as _asyncpg
            import numpy as _np
            from rasterio.merge import merge as _rio_merge
            from rasterio.features import geometry_mask as _geo_mask
            from scipy.ndimage import label as _scipy_label

            _PIXEL_HA = 0.01  # 10m x 10m = 0.01 ha

            if _wc_cell:
                _boundary_sql = (
                    "SELECT cell_name, sector_name, district_name, "
                    "ST_AsGeoJSON(geom)::text, bbox_west, bbox_south, bbox_east, bbox_north "
                    "FROM rwanda_cell_boundaries "
                    "WHERE LOWER(cell_name) = LOWER($1) LIMIT 1"
                )
                _boundary_params = [_wc_cell]
                _admin_level = "cell"
            elif _wc_sector:
                _boundary_sql = (
                    "SELECT sector_name, sector_name, district_name, "
                    "ST_AsGeoJSON(geom)::text, bbox_west, bbox_south, bbox_east, bbox_north "
                    "FROM rwanda_sector_boundaries "
                    "WHERE LOWER(sector_name) = LOWER($1) LIMIT 1"
                )
                _boundary_params = [_wc_sector]
                _admin_level = "sector"
            elif _wc_district:
                _boundary_sql = (
                    "SELECT district, district, district, "
                    "ST_AsGeoJSON(geom)::text, bbox_west, bbox_south, bbox_east, bbox_north "
                    "FROM rwanda_district_boundaries "
                    "WHERE LOWER(district) = LOWER($1) LIMIT 1"
                )
                _boundary_params = [_wc_district]
                _admin_level = "district"
            elif _wc_bbox and isinstance(_wc_bbox, list) and len(_wc_bbox) == 4:
                _boundary_sql = None
                _boundary_params = None
                _admin_level = "bbox"
            else:
                return {
                    "status": "error",
                    "error": "Please specify a district, sector, cell, or bbox for cropland analysis.",
                }

            if _admin_level == "bbox":
                _w, _s, _e, _n = _wc_bbox
                _boundary_name = f"bbox({_w:.4f},{_s:.4f},{_e:.4f},{_n:.4f})"
                _geom = {
                    "type": "Polygon",
                    "coordinates": [[
                        [_w, _s], [_e, _s], [_e, _n], [_w, _n], [_w, _s],
                    ]],
                }
                _bbox = (_w, _s, _e, _n)
            else:
                _pg_host = os.environ.get("POSTGRES_HOST", "postgresdb")
                _pg_port = int(os.environ.get("POSTGRES_PORT", "5432"))
                _pg_db = os.environ.get("POSTGRES_DB", "mundidb")
                _pg_user = os.environ.get("POSTGRES_USER", "mundiuser")
                _pg_pass = os.environ.get("POSTGRES_PASSWORD", "gdalpassword")
                _pg_conn = await _asyncpg.connect(
                    host=_pg_host, port=_pg_port,
                    database=_pg_db, user=_pg_user, password=_pg_pass,
                )
                try:
                    _brow = await _pg_conn.fetchrow(_boundary_sql, *_boundary_params)
                finally:
                    await _pg_conn.close()

                if not _brow:
                    return {
                        "status": "error",
                        "error": f"Boundary not found: {_wc_cell or _wc_sector or _wc_district}",
                    }

                _boundary_name = _brow[0]
                _geom = json.loads(_brow[3])
                _bbox = (_brow[4], _brow[5], _brow[6], _brow[7])

            from src.worldcover import open_rwanda_datasets_warped as _open_warped
            from src.worldcover import CROPLAND_CLASS as _CROP_CLS

            _wc_pairs = []
            try:
                _wc_pairs = _open_warped()
                _wc_datasets = [vrt for vrt, _ds in _wc_pairs]

                _buf = 0.001
                _bounds = (
                    _bbox[0] - _buf, _bbox[1] - _buf,
                    _bbox[2] + _buf, _bbox[3] + _buf,
                )
                _arr, _tfm = _rio_merge(_wc_datasets, bounds=_bounds)
                _data = _arr[0]
                _h, _w_dim = _data.shape

                _mask = _geo_mask(
                    [_geom], out_shape=(_h, _w_dim),
                    transform=_tfm, invert=True,
                )

                _cropland = ((_data == _CROP_CLS) & _mask).astype(_np.uint8)
                _labeled, _num = _scipy_label(_cropland)

                _regions = []
                if _num > 0:
                    _rids, _rcounts = _np.unique(_labeled, return_counts=True)
                    _pairs = sorted(
                        [
                            (int(_r), int(_c))
                            for _r, _c in zip(_rids, _rcounts)
                            if _r > 0
                        ],
                        key=lambda x: x[1],
                        reverse=True,
                    )[: _wc_limit]

                    for _rank, (_rid, _pc) in enumerate(_pairs, 1):
                        _ha = round(_pc * _PIXEL_HA, 2)
                        _ys, _xs = _np.where(_labeled == _rid)
                        _cy = int(_np.mean(_ys))
                        _cx = int(_np.mean(_xs))
                        _lon, _lat = _tfm * (_cx, _cy)
                        _regions.append({
                            "rank": _rank,
                            "area_hectares": _ha,
                            "centroid_lon": round(_lon, 6),
                            "centroid_lat": round(_lat, 6),
                        })

                return {
                    "status": "success",
                    "query_type": "largest_cropland",
                    "admin_level": _admin_level,
                    "boundary_name": _boundary_name,
                    "total_cropland_pixels": int(_np.sum(_cropland)),
                    "total_cropland_hectares": round(
                        float(_np.sum(_cropland)) * _PIXEL_HA, 2
                    ),
                    "num_regions": _num,
                    "count": len(_regions),
                    "data": _regions,
                }
            finally:
                for _vrt, _raw in _wc_pairs:
                    _vrt.close()
                    _raw.close()
        # land_cover branch
        if _wc_bbox and isinstance(_wc_bbox, list) and len(_wc_bbox) == 4:
            import numpy as _np
            from rasterio.merge import merge as _rio_merge
            from rasterio.features import geometry_mask as _geo_mask
            from src.worldcover import open_rwanda_datasets_warped as _open_warped
            from src.worldcover import CLASS_NAMES as _CLASS_NAMES

            _PIXEL_HA = 0.01
            _w, _s, _e, _n = _wc_bbox
            _geom = {
                "type": "Polygon",
                "coordinates": [[
                    [_w, _s], [_e, _s], [_e, _n], [_w, _n], [_w, _s],
                ]],
            }

            _wc_pairs = []
            try:
                _wc_pairs = _open_warped()
                _wc_datasets = [vrt for vrt, _ds in _wc_pairs]

                _buf = 0.001
                _bounds = (_w - _buf, _s - _buf, _e + _buf, _n + _buf)
                _arr, _tfm = _rio_merge(_wc_datasets, bounds=_bounds)
                _data = _arr[0]
                _h, _ww = _data.shape

                _mask = _geo_mask(
                    [_geom], out_shape=(_h, _ww),
                    transform=_tfm, invert=True,
                )
                _masked = _data[_mask]

                _classes, _counts = _np.unique(_masked, return_counts=True)
                _lc_data = []
                for _cls, _cnt in sorted(zip(_classes, _counts), key=lambda x: x[1], reverse=True):
                    _cls_int = int(_cls)
                    if _cls_int == 0:
                        continue  # nodata
                    _lc_data.append({
                        "class_id": _cls_int,
                        "class_name": _CLASS_NAMES.get(_cls_int, f"class_{_cls_int}"),
                        "area_hectares": round(float(_cnt) * _PIXEL_HA, 2),
                        "pixel_count": int(_cnt),
                    })

                return {
                    "status": "success",
                    "query_type": "land_cover",
                    "area": "custom_bbox",
                    "bbox": _wc_bbox,
                    "count": len(_lc_data),
                    "data": _lc_data,
                }
            finally:
                for _vrt, _raw in _wc_pairs:
                    _vrt.close()
                    _raw.close()
        else:
            _sql = "SELECT admin_level, admin_name, district_name, class_name, area_hectares FROM worldcover_admin_stats"
            _where: list[str] = []
            _params: list[Any] = []
            _pidx = 1

            if _wc_cell:
                _where.append(f"admin_level = 'cell' AND LOWER(admin_name) = LOWER(${_pidx})")
                _params.append(_wc_cell)
                _pidx += 1
            elif _wc_sector:
                _where.append(f"admin_level = 'sector' AND LOWER(admin_name) = LOWER(${_pidx})")
                _params.append(_wc_sector)
                _pidx += 1
            elif _wc_district:
                _where.append(f"admin_level = 'district' AND LOWER(admin_name) = LOWER(${_pidx})")
                _params.append(_wc_district)
                _pidx += 1
            else:
                _where.append("admin_level = 'district'")

            if _where:
                _sql += " WHERE " + " AND ".join(_where)
            _sql += " ORDER BY area_hectares DESC"
            _rows = await ctx.conn.fetch(_sql, *_params)

            if _rows:
                return {
                    "status": "success",
                    "query_type": "land_cover",
                    "count": len(_rows),
                    "data": [
                        {
                            "admin_level": r["admin_level"], "admin_name": r["admin_name"],
                            "district": r["district_name"], "class_name": r["class_name"],
                            "area_hectares": r["area_hectares"],
                        }
                        for r in _rows
                    ],
                }
            return {
                "status": "success",
                "query_type": "land_cover",
                "count": 0,
                "data": [],
                "note": "No data yet. Run the worldcover_zonal_stats Dagster asset first.",
            }
    except Exception as e:
        logger.exception("query_worldcover_stats tool failed")
        return {"status": "error", "error": str(e)}


async def _handle_add_land_cover_layer(ctx: LegacyToolContext) -> Dict[str, Any]:
    """Insert an ESRI 10m LULC 2024 raster overlay layer with admin clipping.

    Reverse-geocodes lat/lon → admin (cell > sector > district), then:
    1. Resolves bounds (admin bbox via _lookup_admin_bbox > explicit bbox >
       Rwanda national bounds).
    2. Inserts map_layers + layer_styles + map_layer_styles rows.
    3. Emits a kue_ephemeral_action so the frontend shows "Adding ..." +
       updates the style JSON and recenters to bounds.

    Lifted byte-for-byte from message_routes.py:5863-6038.
    """
    args = ctx.arguments
    try:
        from src.services.map_service import generate_id
        from src.routes.websocket import kue_ephemeral_action

        _wc_mode = args.get("mode", "all")
        if _wc_mode not in ("all", "cropland"):
            _wc_mode = "all"

        _layer_id = generate_id(prefix="L")
        _style_id = generate_id(prefix="S")

        _wc_district = args.get("district")
        _wc_sector = args.get("sector")
        _wc_cell = args.get("cell")
        _wc_bbox = args.get("bbox")
        _wc_lat = args.get("lat")
        _wc_lon = args.get("lon")

        if _wc_lat is not None and _wc_lon is not None and not (_wc_district or _wc_sector or _wc_cell or _wc_bbox):
            try:
                import asyncpg as _asyncpg_lc
                _pg_host_lc = os.environ.get("POSTGRES_HOST", "postgresdb")
                _pg_port_lc = int(os.environ.get("POSTGRES_PORT", "5432"))
                _pg_db_lc = os.environ.get("POSTGRES_DB", "mundidb")
                _pg_user_lc = os.environ.get("POSTGRES_USER", "mundiuser")
                _pg_pass_lc = os.environ.get("POSTGRES_PASSWORD", "gdalpassword")
                _pg_conn_lc = await _asyncpg_lc.connect(
                    host=_pg_host_lc, port=_pg_port_lc,
                    database=_pg_db_lc, user=_pg_user_lc, password=_pg_pass_lc,
                )
                try:
                    _rg_row = await _pg_conn_lc.fetchrow(
                        "SELECT cell_name, sector_name, district_name "
                        "FROM rwanda_cell_boundaries "
                        "WHERE ST_Contains(geom, ST_SetSRID(ST_Point($1, $2), 4326)) "
                        "LIMIT 1",
                        float(_wc_lon), float(_wc_lat),
                    )
                    if _rg_row:
                        _wc_cell = _rg_row["cell_name"]
                        _wc_sector = _rg_row["sector_name"]
                        _wc_district = _rg_row["district_name"]
                    else:
                        _rg_row = await _pg_conn_lc.fetchrow(
                            "SELECT district FROM rwanda_district_boundaries "
                            "WHERE ST_Contains(geom, ST_SetSRID(ST_Point($1, $2), 4326)) "
                            "LIMIT 1",
                            float(_wc_lon), float(_wc_lat),
                        )
                        if _rg_row:
                            _wc_district = _rg_row["district"]
                finally:
                    await _pg_conn_lc.close()
            except Exception as _rg_err:
                logger.warning("Reverse-geocode failed for land cover: %s", _rg_err)

        _admin_name = _wc_cell or _wc_sector or _wc_district

        _area_label = _admin_name or ("Clipped" if _wc_bbox else None)
        _layer_name = (
            f"ESRI Land Cover — Cropland ({_area_label})"
            if _wc_mode == "cropland" and _area_label
            else f"ESRI Land Cover ({_area_label})"
            if _area_label
            else "ESRI Land Cover — Cropland"
            if _wc_mode == "cropland"
            else "ESRI Land Cover 2024"
        )

        _wc_meta: Dict[str, Any] = {
            "worldcover": True,
            "worldcover_mode": _wc_mode,
        }
        if _wc_district:
            _wc_meta["clip_district"] = _wc_district
        if _wc_sector:
            _wc_meta["clip_sector"] = _wc_sector
        if _wc_cell:
            _wc_meta["clip_cell"] = _wc_cell
        if _wc_bbox and isinstance(_wc_bbox, list) and len(_wc_bbox) == 4:
            _wc_meta["clip_bbox"] = _wc_bbox
        _meta = json.dumps(_wc_meta)

        _bounds = [28.86, -2.84, 30.90, -1.05]
        if _wc_district or _wc_sector or _wc_cell:
            try:
                from src.routes.rwanda_routes import _lookup_admin_bbox
                _admin_bbox = await _lookup_admin_bbox(
                    district=_wc_district,
                    sector=_wc_sector,
                    cell=_wc_cell,
                )
                if _admin_bbox:
                    _bounds = _admin_bbox
            except Exception:
                pass
        elif _wc_bbox and isinstance(_wc_bbox, list) and len(_wc_bbox) == 4:
            _bounds = _wc_bbox

        async with kue_ephemeral_action(
            ctx.conversation_id,
            f"Adding {_layer_name} layer...",
            update_style_json=True,
            bounds=_bounds,
        ):
            await ctx.conn.execute(
                """
                INSERT INTO map_layers
                (layer_id, owner_uuid, name, type,
                 metadata, bounds, source_map_id,
                 created_on, last_edited)
                VALUES ($1, $2, $3, 'raster',
                        $4, $5, $6,
                        CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)
                """,
                _layer_id, ctx.user_id, _layer_name,
                _meta, _bounds, ctx.map_id,
            )

            await ctx.conn.execute(
                """
                INSERT INTO layer_styles
                (style_id, layer_id, style_json, created_by, created_on)
                VALUES ($1, $2, $3, $4, CURRENT_TIMESTAMP)
                """,
                _style_id, _layer_id, "[]", ctx.user_id,
            )

            await ctx.conn.execute(
                """
                INSERT INTO map_layer_styles (map_id, layer_id, style_id)
                VALUES ($1, $2, $3)
                """,
                ctx.map_id, _layer_id, _style_id,
            )

            await ctx.conn.execute(
                """
                UPDATE user_mundiai_maps
                SET layers = CASE
                    WHEN layers IS NULL THEN ARRAY[$1]
                    ELSE array_append(layers, $1)
                END
                WHERE id = $2 AND (layers IS NULL OR NOT ($1 = ANY(layers)))
                """,
                _layer_id, ctx.map_id,
            )

        _class_desc = (
            "Cropland highlighted in green, other land cover muted"
            if _wc_mode == "cropland"
            else "All 9 ESRI land cover classes: water, trees, flooded vegetation, crops, built area, bare ground, snow/ice, clouds, rangeland"
        )

        return {
            "status": "success",
            "layer_id": _layer_id,
            "layer_name": _layer_name,
            "mode": _wc_mode,
            "source": "ESRI / Impact Observatory 10m Annual LULC 2024",
            "classes": _class_desc,
            "kue_instructions": (
                f"The layer '{_layer_name}' (ID: {_layer_id}) has been created and "
                f"added to the map as a raster tile overlay. Mode: {_wc_mode}. "
                f"{_class_desc}. "
                "Do NOT call add_layer_to_map or set_layer_style — it is already done. "
                "Describe the layer to the user and explain what the colours mean."
            ),
        }
    except Exception as e:
        logger.exception("add_land_cover_layer failed")
        return {"status": "error", "error": str(e)}


async def _handle_create_management_zones(ctx: LegacyToolContext) -> Dict[str, Any]:
    """Sync precision_ag_service.create_management_zones in executor.

    Lifted byte-for-byte from message_routes.py:3106-3122.
    """
    args = ctx.arguments
    try:
        from src.services.precision_ag_service import create_management_zones

        result_data = await asyncio.get_event_loop().run_in_executor(
            None,
            lambda: create_management_zones(
                geometry=args.get("geometry"),
                num_zones=args.get("num_zones", 3),
                date_from=args.get("date_from"),
                date_to=args.get("date_to"),
            ),
        )
        if "error" in result_data:
            return {"status": "error", "error": result_data["error"]}
        return {"status": "success", "management_zones": result_data}
    except Exception as e:
        logger.exception("create_management_zones failed")
        return {"status": "error", "error": str(e)}


async def _handle_create_prescription_map(ctx: LegacyToolContext) -> Dict[str, Any]:
    """Sync precision_ag_service.create_prescription_map in executor.

    Lifted byte-for-byte from message_routes.py:3135-3150.
    """
    args = ctx.arguments
    try:
        from src.services.precision_ag_service import create_prescription_map

        result_data = await asyncio.get_event_loop().run_in_executor(
            None,
            lambda: create_prescription_map(
                geometry=args.get("geometry"),
                crop_type=args.get("crop_type", "maize"),
                num_zones=args.get("num_zones", 3),
            ),
        )
        if "error" in result_data:
            return {"status": "error", "error": result_data["error"]}
        return {"status": "success", "prescription_map": result_data}
    except Exception as e:
        logger.exception("create_prescription_map failed")
        return {"status": "error", "error": str(e)}


async def _handle_create_soil_sampling_plan(ctx: LegacyToolContext) -> Dict[str, Any]:
    """Sync precision_ag_service.create_soil_sampling_plan in executor.

    Lifted byte-for-byte from message_routes.py:3163-3177.
    """
    args = ctx.arguments
    try:
        from src.services.precision_ag_service import create_soil_sampling_plan

        result_data = await asyncio.get_event_loop().run_in_executor(
            None,
            lambda: create_soil_sampling_plan(
                geometry=args.get("geometry"),
                num_zones=args.get("num_zones", 3),
            ),
        )
        if "error" in result_data:
            return {"status": "error", "error": result_data["error"]}
        return {"status": "success", "sampling_plan": result_data}
    except Exception as e:
        logger.exception("create_soil_sampling_plan failed")
        return {"status": "error", "error": str(e)}


async def _handle_query_rwanda_zonal_stats(ctx: LegacyToolContext) -> Dict[str, Any]:
    """Rwanda lakehouse query (district summary or NDVI timeseries).

    Two query_types:
    - district_summary: by province and/or week_start
    - ndvi_timeseries: by h3_index OR parcel_id, with date range

    Lifted byte-for-byte from message_routes.py:2944-2992.
    """
    args = ctx.arguments
    query_type = args.get("query_type")

    try:
        from fastapi import HTTPException
        from src.services.rwanda_lakehouse import get_rwanda_lakehouse_manager
        rwanda_mgr = get_rwanda_lakehouse_manager()

        if query_type == "district_summary":
            province = args.get("province")
            week_start = args.get("week_start")
            result_data = rwanda_mgr.query_district_summary(
                province=province,
                week_start=week_start,
            )
            return {"status": "success", "data": result_data}

        if query_type == "ndvi_timeseries":
            h3_index = args.get("h3_index")
            parcel_id = args.get("parcel_id")
            date_from = args.get("date_from")
            date_to = args.get("date_to")
            result_data = rwanda_mgr.query_ndvi_timeseries(
                h3_index=h3_index,
                parcel_id=parcel_id,
                date_from=date_from,
                date_to=date_to,
            )
            return {"status": "success", "data": result_data}

        return {
            "status": "error",
            "error": f"Unknown query_type: {query_type}. Must be 'district_summary' or 'ndvi_timeseries'."
        }
    except HTTPException as e:
        return {
            "status": "error",
            "error": f"Rwanda lakehouse query error: {e.detail}",
        }
    except Exception as e:
        logger.exception(
            "Error querying Rwanda lakehouse: query_type=%s",
            query_type,
        )
        return {
            "status": "error",
            "error": f"Failed to query Rwanda lakehouse: {str(e)}",
        }


class _SyntheticFunction:
    """Lightweight stand-in for OpenAI's tool_call.function object.

    run_geoprocessing_tool reads .name (str) and .arguments (JSON str)
    off this attribute. We don't need any of the real SDK behaviour, just
    the duck-typed shape.
    """
    __slots__ = ("name", "arguments")

    def __init__(self, name: str, arguments: str) -> None:
        self.name = name
        self.arguments = arguments


class _SyntheticToolCall:
    """Lightweight stand-in for an OpenAI ChatCompletionMessageToolCall.

    run_geoprocessing_tool needs .id (str) for error wrapping (via
    RecoverableToolCallError) and .function (the synthetic above). Built
    fresh per-call so concurrent dispatches don't share state.
    """
    __slots__ = ("id", "function")

    def __init__(self, tool_id: str, name: str, arguments: str) -> None:
        self.id = tool_id
        self.function = _SyntheticFunction(name=name, arguments=arguments)


def _make_qgis_handler(tool_name: str) -> LegacyHandlerFn:
    """Closure that delegates to run_geoprocessing_tool with a synthetic tool_call.

    All 16 QGIS-processing tools (native_*, qgis_*, gdal_warpreproject) share
    the same dispatch path: build the QGIS request from tool args + map state,
    POST to the qgis-processing sidecar, download outputs from S3, register
    new map_layers rows. Rather than re-implement each one, we delegate to
    the existing run_geoprocessing_tool which already handles all of them
    generically by reading the tool name from the call.

    The synthetic tool_call gives run_geoprocessing_tool the SDK-shaped
    object it expects (.function.name, .function.arguments, .id) without
    requiring the Hermes path to construct a real OpenAI tool call.
    """
    async def _handler(ctx: LegacyToolContext) -> Dict[str, Any]:
        try:
            from src.routes.message_routes import run_geoprocessing_tool

            synthetic_call = _SyntheticToolCall(
                tool_id=f"shim-{tool_name}-{ctx.conversation_id}",
                name=tool_name,
                arguments=json.dumps(ctx.arguments),
            )
            return await run_geoprocessing_tool(
                synthetic_call,
                ctx.conn,
                ctx.user_id,
                ctx.map_id,
                ctx.conversation_id,
            )
        except Exception as e:
            logger.exception("%s (QGIS shim) failed", tool_name)
            return {"status": "error", "error": str(e), "algorithm_id": tool_name.replace("_", ":")}

    _handler.__name__ = f"_handle_{tool_name}"
    return _handler


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
    # query_rwanda_zonal_stats extracted (Rwanda lakehouse query router).
    # search_satellite_imagery extracted (STAC + NDVI sample).
    # NOTE: get_field_health + get_parcel_ndvi_stats extracted.
    # create_management_zones + create_prescription_map +
    # create_soil_sampling_plan extracted (precision_ag service trio).
    # identify_parcel_crop + confirm_crop_prediction extracted.
    # get_ndvi_stats + get_cell_ndvi_stats extracted.
    # get_soil_properties extracted (iSDAsoil + display_layer hints).
    # get_agri_indices extracted (cache + DE Africa + inline layer creation).
    # query_worldcover_stats + add_land_cover_layer extracted (ESRI LULC).
    # get_crop_classifications + get_anomaly_alerts + get_yield_risk +
    # get_drought_status + get_crop_growth_stage extracted (5 cache-read tools).
    # NOTE: get_forecast + detect_dry_spells + get_insurance_intelligence
    # have been extracted (the insurance flow).
    # get_weather_stats + get_forecast_accuracy + get_emissions_stats +
    # get_insurance_accuracy extracted (weather/accuracy/emissions batch).
    # search_brain + get_entity + add_observation extracted (brain trio).
    # add_land_cover_layer extracted (LULC raster overlay).
    # QGIS-processing tools (all 16) extracted via _make_qgis_handler —
    # they share run_geoprocessing_tool as their common dispatch path.
]


# All 16 QGIS-processing tools share one generic handler that delegates
# to run_geoprocessing_tool. Names mirror the inline elif chain in
# message_routes.py and tools.json exactly.
_QGIS_TOOL_NAMES = [
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
    "get_field_health": _handle_get_field_health,
    "get_parcel_ndvi_stats": _handle_get_parcel_ndvi_stats,
    "get_ndvi_stats": _handle_get_ndvi_stats,
    "get_cell_ndvi_stats": _handle_get_cell_ndvi_stats,
    "get_agri_indices": _handle_get_agri_indices,
    "identify_parcel_crop": _handle_identify_parcel_crop,
    "confirm_crop_prediction": _handle_confirm_crop_prediction,
    "get_crop_classifications": _handle_get_crop_classifications,
    "get_anomaly_alerts": _handle_get_anomaly_alerts,
    "get_yield_risk": _handle_get_yield_risk,
    "get_drought_status": _handle_get_drought_status,
    "get_crop_growth_stage": _handle_get_crop_growth_stage,
    "get_soil_properties": _handle_get_soil_properties,
    "get_weather_stats": _handle_get_weather_stats,
    "get_forecast_accuracy": _handle_get_forecast_accuracy,
    "get_insurance_accuracy": _handle_get_insurance_accuracy,
    "get_emissions_stats": _handle_get_emissions_stats,
    "search_brain": _handle_search_brain,
    "get_entity": _handle_get_entity,
    "add_observation": _handle_add_observation,
    "search_satellite_imagery": _handle_search_satellite_imagery,
    "query_worldcover_stats": _handle_query_worldcover_stats,
    "add_land_cover_layer": _handle_add_land_cover_layer,
    "create_management_zones": _handle_create_management_zones,
    "create_prescription_map": _handle_create_prescription_map,
    "create_soil_sampling_plan": _handle_create_soil_sampling_plan,
    "query_rwanda_zonal_stats": _handle_query_rwanda_zonal_stats,
}
# Register the 16 QGIS-processing tools through the generic delegator
for _qname in _QGIS_TOOL_NAMES:
    LEGACY_HANDLERS[_qname] = _make_qgis_handler(_qname)
del _qname
if _NOT_YET_EXTRACTED:
    # Stub-handler safety net for any tool that wasn't extracted yet.
    # As of the QGIS-processing batch landing, this list is empty —
    # every legacy tool has a real handler — but the machinery stays
    # so that if message_routes.py grows a new inline elif before its
    # shim handler exists, it still returns a parseable result instead
    # of a 404 to Hermes.
    for _name in _NOT_YET_EXTRACTED:
        LEGACY_HANDLERS[_name] = _make_not_yet_extracted_handler(_name)
    del _name


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
