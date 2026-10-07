from fastapi import APIRouter, HTTPException, status, Request, Depends
from fastapi.responses import JSONResponse
from typing import Any, List, Optional, Union
from collections import defaultdict
from pydantic import BaseModel, Field
import asyncpg
import base64
import copy
import hashlib
import logging
import os
import json
import re
import time
from urllib.parse import quote
from fastapi import BackgroundTasks
from opentelemetry import trace
import asyncio
import traceback
import uuid as _uuid
from src.services.legacy_tool_shim import (
    LEGACY_HANDLERS,
    LegacyToolContext,
    execute_legacy_tool,
)
from src.dependencies.dag import get_map
from typing import Callable
from src.dependencies.rate_limiter import expensive_limit
from src.dependencies.redis_client import get_redis_client
from openai.types.chat.chat_completion_message import ChatCompletionMessage
from openai.types.chat.chat_completion_tool_message_param import (
    ChatCompletionToolMessageParam,
)
from openai.types.chat.chat_completion_user_message_param import (
    ChatCompletionUserMessageParam,
)
from openai.types.chat.chat_completion_system_message_param import (
    ChatCompletionSystemMessageParam,
)
from openai.types.chat.chat_completion_message_param import (
    ChatCompletionMessageParam,
)
from openai.types.chat import ChatCompletionMessageToolCall
from openai import APIError, BadRequestError


from src.structures import (
    async_conn,
    SanitizedMessage,
    convert_mundi_message_to_sanitized,
)
from src.utils import get_chat_client_for_model, get_openai_client
from src.llm_defaults import supports_strict_tool_schema
from src.models.messages import _parse_tool_args as _clean_tool_args
from src.routes.postgres_routes import get_map_description
from src.services.map_service import (
    generate_id,
)
from src.services.life_harness import (
    life_harness_tool_signature,
    repeated_life_harness_tool_error,
    validate_life_harness_tool_args,
)
from src.services.tool_call_scrubber import _ToolCallTextScrubber
from src.services.posthog_analytics import capture_for_session, elapsed_ms
from src.services.sage_flight_recorder import sage_turn_trace
from src.services import data_coverage
from src.services.sage_result_checks import apply_result_checks
from src.geoprocessing.dispatch import (
    get_tools,
)
from src.dependencies.conversation import get_or_create_conversation
from src.dependencies.postgis import get_postgis_provider
from src.dependencies.layer_describer import LayerDescriber, get_layer_describer
from src.dependencies.chat_completions import ChatArgsProvider, get_chat_args_provider
from src.dependencies.map_state import (
    MapStateProvider,
    get_map_state_provider,
    SelectedFeature,
)
from src.dependencies.system_prompt import (
    SystemPromptProvider,
    get_system_prompt_provider,
)
from src.dependencies.sage_routing import (
    ADMIN_BOUNDARY_TOOL,
    RASTER_FACT_TOOL,
    RASTER_H3_CONTEXT_TOOL,
    RASTER_OBJECT_CANDIDATES_TOOL,
    build_fast_tool_call,
    detect_raster_building_count_question,
    extract_last_user_text,
    select_fast_raster_layer,
)
from src.dependencies.sage_turn_request import (
    abdication_guard_enabled,
    apply_tool_shortlist,
    build_sage_tools_payload,
    guard_tool_calls,
    guard_tools,
    RATE_LIMIT_RETRIES,
    is_abdication,
    plan_sage_turn,
    rate_limit_retry_after,
    rate_limit_user_message,
    tool_shortlist_k,
)
from src.dependencies.session import (
    verify_session_required,
    UserContext,
)
from src.dependencies.postgres_connection import (
    PostgresConnectionManager,
    get_postgres_connection_manager,
)
from src.database.models import (
    MundiChatCompletionMessage,
    MundiMap,
    MapLayer,
    Conversation,
)
from src.routes.websocket import kue_ephemeral_action, kue_notify_error, kue_stream_token
from src.dependencies.pydantic_tools import (
    get_pydantic_tool_calls,
    PydanticToolRegistry,
)

logger = logging.getLogger(__name__)
tracer = trace.get_tracer(__name__)

# Compact deterministic IDs for each project's internal Rwanda PostGIS
# connection. The database column is varchar(12), so keep these short.
RWANDA_INTERNAL_CONNECTION_NAME = "Rwanda Agriculture (internal)"
INTERNAL_RWANDA_ALLOWED_TABLES = frozenset(
    {
        "rwanda_province_boundaries",
        "rwanda_district_boundaries",
        "rwanda_sector_boundaries",
        "rwanda_cell_boundaries",
        "rwanda_village_boundaries",
        "ndvi_cell_cache",
        "ndvi_field_cache",
        "ndvi_parcel_cache",
        "agri_indices_cache",
        "crop_classification_cache",
        "drought_cache",
        "emissions_annual_cache",
        "phenology_cache",
        "weather_daily_cache",
        "yield_risk_cache",
    }
)
_SQL_TABLE_REF_RE = re.compile(
    r'\b(?:from|join)\s+((?:"?[a-zA-Z_][a-zA-Z0-9_]*"?\.)?"?[a-zA-Z_][a-zA-Z0-9_]*"?)',
    re.IGNORECASE,
)


def _compact_project_hash(prefix: str, project_id: str) -> str:
    digest = hashlib.blake2s(project_id.encode("utf-8"), digest_size=8).digest()
    token = base64.b32encode(digest).decode("ascii").lower().rstrip("=")
    return f"{prefix}{token[:11]}"


def _rwanda_internal_conn_id(project_id: str) -> str:
    return _compact_project_hash("C", project_id)


def _rwanda_internal_summary_id(project_id: str) -> str:
    return _compact_project_hash("S", project_id)


def _referenced_sql_tables(query: str) -> set[str]:
    tables: set[str] = set()
    for match in _SQL_TABLE_REF_RE.finditer(query):
        ref = match.group(1).replace('"', "")
        tables.add(ref.split(".")[-1].lower())
    return tables


def validate_internal_rwanda_query(query: str) -> None:
    referenced = _referenced_sql_tables(query)
    if not referenced:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Internal Rwanda queries must reference an allowed Rwanda table",
        )
    disallowed = sorted(referenced - INTERNAL_RWANDA_ALLOWED_TABLES)
    if disallowed:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=(
                "Internal Rwanda connection can only query approved Rwanda "
                f"tables; blocked: {', '.join(disallowed)}"
            ),
        )


def is_internal_rwanda_connection(
    connection_id: str,
    project_id: str,
    connection_name: str | None,
) -> bool:
    return (
        connection_id == _rwanda_internal_conn_id(project_id)
        and connection_name == RWANDA_INTERNAL_CONNECTION_NAME
    )


async def _ensure_rwanda_postgis_connection(
    conn, project_id: str, user_id: str,
) -> str | None:
    """Auto-provision an internal PostGIS connection for Rwanda data.

    Creates a project_postgres_connections row pointing to the app's own
    database so Sage can use new_layer_from_postgis to create layers from
    rwanda_district_boundaries, rwanda_cell_boundaries, etc.

    Returns the connection ID, or None on failure.
    """
    try:
        connection_id = _rwanda_internal_conn_id(project_id)
        summary_id = _rwanda_internal_summary_id(project_id)
        existing = await conn.fetchrow(
            """
            SELECT id, project_id, user_id, soft_deleted_at, connection_uri
            FROM project_postgres_connections
            WHERE id = $1
            """,
            connection_id,
        )
        pg_host = os.environ.get("POSTGRES_HOST", "postgresdb")
        pg_port = os.environ.get("POSTGRES_PORT", "5432")
        pg_db = os.environ.get("POSTGRES_DB", "mundidb")
        pg_user = os.environ.get("POSTGRES_USER", "mundiuser")
        pg_pass = os.environ.get("POSTGRES_PASSWORD", "changeme")
        uri = (
            f"postgresql://{quote(pg_user, safe='')}:{quote(pg_pass, safe='')}"
            f"@{pg_host}:{pg_port}/{quote(pg_db, safe='')}?sslmode=disable"
        )

        if existing:
            if existing["project_id"] != project_id:
                logger.error(
                    "Rwanda internal connection ID collision: id=%s existing_project=%s new_project=%s",
                    connection_id,
                    existing["project_id"],
                    project_id,
                )
                return None
            # Un-delete if soft-deleted, and ensure correct project + user
            needs_update = (
                existing["soft_deleted_at"] is not None
                or str(existing["user_id"]) != str(user_id)
                or existing["connection_uri"] != uri
            )
            if needs_update:
                await conn.execute(
                    """
                    UPDATE project_postgres_connections
                    SET project_id = $1,
                        user_id = $2,
                        connection_uri = $3,
                        soft_deleted_at = NULL
                    WHERE id = $4
                    """,
                    project_id, user_id, uri, connection_id,
                )
                logger.info(
                    "Updated Rwanda PostGIS connection: project=%s soft_deleted=%s uri_changed=%s",
                    project_id,
                    existing["soft_deleted_at"] is not None,
                    existing["connection_uri"] != uri,
                )
        else:
            await conn.execute(
                """
                INSERT INTO project_postgres_connections
                (id, project_id, user_id, connection_uri, connection_name)
                VALUES ($1, $2, $3, $4, $5)
                ON CONFLICT (id) DO NOTHING
                """,
                connection_id,
                project_id,
                user_id,
                uri,
                RWANDA_INTERNAL_CONNECTION_NAME,
            )
            logger.info("Auto-provisioned Rwanda PostGIS connection %s for project %s",
                         connection_id, project_id)

        # Province boundaries are derived from district polygons. Keeping this
        # as a view avoids a separate seed step while giving Sage a real
        # province polygon surface for map display.
        if await conn.fetchval(
            "SELECT to_regclass('public.rwanda_district_boundaries') IS NOT NULL"
        ):
            await conn.execute(
                """
                CREATE OR REPLACE VIEW rwanda_province_boundaries AS
                SELECT
                    province,
                    ST_Multi(ST_UnaryUnion(ST_Collect(geom)))::geometry(MultiPolygon, 4326) AS geom,
                    ST_XMin(ST_Extent(geom))::double precision AS bbox_west,
                    ST_YMin(ST_Extent(geom))::double precision AS bbox_south,
                    ST_XMax(ST_Extent(geom))::double precision AS bbox_east,
                    ST_YMax(ST_Extent(geom))::double precision AS bbox_north
                FROM (
                    SELECT
                        CASE district
                            WHEN 'Gasabo' THEN 'Kigali City'
                            WHEN 'Kicukiro' THEN 'Kigali City'
                            WHEN 'Nyarugenge' THEN 'Kigali City'
                            WHEN 'Burera' THEN 'Northern Province'
                            WHEN 'Gakenke' THEN 'Northern Province'
                            WHEN 'Gicumbi' THEN 'Northern Province'
                            WHEN 'Musanze' THEN 'Northern Province'
                            WHEN 'Rulindo' THEN 'Northern Province'
                            WHEN 'Gisagara' THEN 'Southern Province'
                            WHEN 'Huye' THEN 'Southern Province'
                            WHEN 'Kamonyi' THEN 'Southern Province'
                            WHEN 'Muhanga' THEN 'Southern Province'
                            WHEN 'Nyamagabe' THEN 'Southern Province'
                            WHEN 'Nyanza' THEN 'Southern Province'
                            WHEN 'Nyaruguru' THEN 'Southern Province'
                            WHEN 'Ruhango' THEN 'Southern Province'
                            WHEN 'Bugesera' THEN 'Eastern Province'
                            WHEN 'Gatsibo' THEN 'Eastern Province'
                            WHEN 'Kayonza' THEN 'Eastern Province'
                            WHEN 'Kirehe' THEN 'Eastern Province'
                            WHEN 'Ngoma' THEN 'Eastern Province'
                            WHEN 'Nyagatare' THEN 'Eastern Province'
                            WHEN 'Rwamagana' THEN 'Eastern Province'
                            WHEN 'Karongi' THEN 'Western Province'
                            WHEN 'Ngororero' THEN 'Western Province'
                            WHEN 'Nyabihu' THEN 'Western Province'
                            WHEN 'Nyamasheke' THEN 'Western Province'
                            WHEN 'Rubavu' THEN 'Western Province'
                            WHEN 'Rusizi' THEN 'Western Province'
                            WHEN 'Rutsiro' THEN 'Western Province'
                        END AS province,
                        geom
                    FROM rwanda_district_boundaries
                ) districts
                WHERE province IS NOT NULL
                GROUP BY province
                """
            )

        # Dynamically count which Rwanda admin tables actually exist
        _RWANDA_TABLES = [
            "rwanda_province_boundaries",
            "rwanda_district_boundaries",
            "rwanda_sector_boundaries",
            "rwanda_cell_boundaries",
            "rwanda_village_boundaries",
        ]
        existing_tables = await conn.fetch(
            """
            SELECT table_name FROM information_schema.tables
            WHERE table_schema = 'public'
              AND table_name = ANY($1::text[])
            """,
            _RWANDA_TABLES,
        )
        _table_count = len(existing_tables)

        # Build schema summary including only tables that actually exist
        _existing_set = {r["table_name"] for r in existing_tables}
        _summary_parts = ["## Rwanda Administrative Boundaries\n"]

        if "rwanda_province_boundaries" in _existing_set:
            _summary_parts.append(
                "### rwanda_province_boundaries\n"
                "All 5 Rwanda provinces/city areas derived from district polygons.\n"
                "| Column | Type | Description |\n"
                "|--------|------|-------------|\n"
                "| province | text | Province name (Kigali City, Eastern Province, Northern Province, Southern Province, Western Province) |\n"
                "| geom | geometry(MultiPolygon, 4326) | Province boundary |\n"
            )
        if "rwanda_district_boundaries" in _existing_set:
            _summary_parts.append(
                "### rwanda_district_boundaries\n"
                "All 30 Rwanda districts with polygon geometries.\n"
                "| Column | Type | Description |\n"
                "|--------|------|-------------|\n"
                "| district | text | District name (primary key, e.g. 'Nyagatare', 'Bugesera') |\n"
                "| geom | geometry(MultiPolygon, 4326) | District boundary |\n"
            )
        if "rwanda_sector_boundaries" in _existing_set:
            _summary_parts.append(
                "### rwanda_sector_boundaries\n"
                "All Rwanda sectors with polygon geometries.\n"
                "| Column | Type | Description |\n"
                "|--------|------|-------------|\n"
                "| sector_id | integer | Primary key |\n"
                "| sector_name | text | Sector name |\n"
                "| district_name | text | Parent district |\n"
                "| geom | geometry(MultiPolygon, 4326) | Sector boundary |\n"
            )
        if "rwanda_cell_boundaries" in _existing_set:
            _summary_parts.append(
                "### rwanda_cell_boundaries\n"
                "All ~2,148 Rwanda cells with polygon geometries.\n"
                "| Column | Type | Description |\n"
                "|--------|------|-------------|\n"
                "| cell_id | integer | Primary key |\n"
                "| cell_name | text | Cell name |\n"
                "| sector_name | text | Parent sector |\n"
                "| district_name | text | Parent district |\n"
                "| geom | geometry(MultiPolygon, 4326) | Cell boundary |\n"
            )
        if "rwanda_village_boundaries" in _existing_set:
            _summary_parts.append(
                "### rwanda_village_boundaries\n"
                "All ~14,815 Rwanda villages with polygon geometries.\n"
                "| Column | Type | Description |\n"
                "|--------|------|-------------|\n"
                "| village_id | integer | Primary key |\n"
                "| village_name | text | Village name |\n"
                "| cell_name | text | Parent cell |\n"
                "| sector_name | text | Parent sector |\n"
                "| district_name | text | Parent district |\n"
                "| geom | geometry(MultiPolygon, 4326) | Village boundary |\n"
            )

        _summary_parts.append(
            "\n### Admin hierarchy\n"
            "Province (5) → District (30) → Sector (~416) → Cell (~2,148) → Village (~14,815)\n\n"
            "### Usage with new_layer_from_postgis\n"
            "Queries MUST return columns named `id` and `geom`.\n"
            "Example (provinces): `SELECT province AS id, province, geom "
            "FROM rwanda_province_boundaries`\n"
            "Example (districts): `SELECT district AS id, geom "
            "FROM rwanda_district_boundaries`\n"
            "Example (sectors): `SELECT sector_id AS id, sector_name, "
            "district_name, geom FROM rwanda_sector_boundaries "
            "WHERE district_name = 'Nyagatare'`\n"
            "Example (cells): `SELECT cell_id AS id, cell_name, sector_name, "
            "district_name, geom FROM rwanda_cell_boundaries "
            "WHERE district_name = 'Nyagatare'`\n"
            "Example (villages): `SELECT village_id AS id, village_name, cell_name, "
            "sector_name, district_name, geom FROM rwanda_village_boundaries "
            "WHERE district_name = 'Gasabo'`\n"
        )
        _summary_md = "\n".join(_summary_parts)

        # Always upsert summary so table_count stays accurate
        await conn.execute(
            """
            INSERT INTO project_postgres_summary
            (id, connection_id, friendly_name, summary_md, table_count)
            VALUES ($1, $2, $3, $4, $5)
            ON CONFLICT (id) DO UPDATE
            SET summary_md = EXCLUDED.summary_md,
                table_count = EXCLUDED.table_count
            """,
            summary_id,
            connection_id,
            "Rwanda Administrative Boundaries",
            _summary_md,
            _table_count,
        )

        return connection_id
    except Exception:
        logger.exception("Failed to auto-provision Rwanda PostGIS connection")
        return None


redis = get_redis_client()


async def label_conversation_inline(conversation_id: int):
    """Generate a title for a conversation using OpenAI"""
    try:
        async with async_conn("label_conversation") as conn:
            messages = await conn.fetch(
                """
                SELECT message_json
                FROM chat_completion_messages
                WHERE conversation_id = $1
                ORDER BY created_at ASC
                LIMIT 5
                """,
                conversation_id,
            )

            if not messages:
                return

            conversation_content = []
            for msg in messages:
                message_data = json.loads(msg["message_json"])
                role = message_data.get("role", "")
                content = message_data.get("content", "")
                if content and role in ["user", "assistant"]:
                    conversation_content.append(f"{role}: {content[:200]}")

            if not conversation_content:
                return

            content_summary = "\n".join(conversation_content)

            request = Request({"type": "http", "method": "POST", "headers": []})
            openai_client, title_model = get_chat_client_for_model(request)

            response = await openai_client.chat.completions.create(
                model=title_model,
                messages=[
                    {
                        "role": "system",
                        "content": "Generate a short, descriptive title (3-6 words) for this conversation. The title should capture the main topic or request. Only return the title, nothing else.",
                    },
                    {"role": "user", "content": f"Conversation:\n{content_summary}"},
                ],
                # Thinking models (Nemotron, GPT-6 Luna) spend tokens on reasoning before the title:
                # with 20 Luna returned no title at all; with 150 it used about 70 (CODING_STANDARDS lesson).
                max_tokens=150,
                temperature=0.3,
            )

            title = (response.choices[0].message.content or "").strip()
            if title and len(title) > 0:
                await conn.execute(
                    """
                    UPDATE conversations
                    SET title = $1, updated_at = CURRENT_TIMESTAMP
                    WHERE id = $2
                    """,
                    title,
                    conversation_id,
                )
                logger.info("Generated title for conversation %s: %s", conversation_id, title)

    except Exception as e:
        logger.warning("Error labeling conversation %s: %s", conversation_id, e)


# Create router
router = APIRouter()


async def get_all_conversation_messages(
    conversation_id: int,
    session: UserContext,
) -> List[MundiChatCompletionMessage]:
    user_id = session.get_user_id()
    try:
        user_uuid = _uuid.UUID(user_id)
    except (ValueError, AttributeError):
        return []
    async with async_conn("get_all_conversation_messages", user_id=user_id) as conn:
        db_messages = await conn.fetch(
            """
            SELECT ccm.*
            FROM chat_completion_messages ccm
            JOIN conversations c ON ccm.conversation_id = c.id
            WHERE ccm.conversation_id = $1
            AND c.owner_uuid = $2
            AND c.soft_deleted_at IS NULL
            ORDER BY ccm.created_at ASC
            """,
            conversation_id,
            user_uuid,
        )

        messages: list[MundiChatCompletionMessage] = []
        for msg in db_messages:
            msg_dict = dict(msg)
            # Parse message_json ... when using raw asyncpg
            msg_dict["message_json"] = json.loads(msg_dict["message_json"])
            messages.append(MundiChatCompletionMessage(**msg_dict))
        return messages


class LayerInfo(BaseModel):
    layer_id: str
    name: str
    type: str
    geometry_type: str | None = None
    feature_count: int | None = None

    @classmethod
    def from_map_layer(cls, layer: MapLayer) -> "LayerInfo":
        return cls(
            layer_id=layer.layer_id,
            name=layer.name,
            type=layer.type,
            geometry_type=layer.geometry_type,
            feature_count=layer.feature_count,
        )


class LayerDiff(BaseModel):
    added_layers: List[LayerInfo]
    removed_layers: List[LayerInfo]


class MapNode(BaseModel):
    map_id: str
    messages: List[SanitizedMessage]
    fork_reason: str | None = None
    created_on: str
    diff_from_previous: LayerDiff | None = None


class MapTreeResponse(BaseModel):
    project_id: str
    tree: List[MapNode]


@router.get(
    "/{map_id}/tree",
    operation_id="get_map_tree",
    response_model=MapTreeResponse,
)
async def get_map_tree(
    map: MundiMap = Depends(get_map),
    conversation_id: int | None = None,
    session: UserContext = Depends(verify_session_required),
):
    leaf_map_id = map.id
    project_id = map.project_id

    # TODO: if you add a message to a previous map, it interrupts the chain.
    # adding a message should be considered creating a new node in the DAG...
    async with async_conn("describe_map_tree", user_id=session.get_user_id()) as conn:
        # Collect all map IDs in the parent chain
        map_ids: list[str] = []
        current_map_id: str | None = leaf_map_id

        while current_map_id:
            map_ids.insert(0, current_map_id)

            # Get parent map ID
            parent_result = await conn.fetchrow(
                """
                SELECT parent_map_id
                FROM user_mundiai_maps
                WHERE id = $1 AND soft_deleted_at IS NULL
                """,
                current_map_id,
            )
            if not parent_result:
                break

            if parent_result["parent_map_id"] in map_ids:
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail="Encountered loop in DAG inside describe_map_tree",
                )

            current_map_id = parent_result["parent_map_id"]

        # Fetch all map data including layers
        db_maps = await conn.fetch(
            """
            SELECT id, fork_reason, created_on, layers
            FROM user_mundiai_maps
            WHERE id = ANY($1) AND soft_deleted_at IS NULL
            ORDER BY array_position($1, id)
            """,
            map_ids,
        )
        db_maps: List[MundiMap] = [MundiMap(**dict(map)) for map in db_maps]

        # Fetch all unique layer IDs from all maps in the chain
        all_layer_ids = set()
        for db_map in db_maps:
            if db_map.layers:
                all_layer_ids.update(db_map.layers)

        # Fetch all layer data
        layers_by_id = {}
        if all_layer_ids:
            db_layers = await conn.fetch(
                """
                SELECT layer_id, owner_uuid, name, s3_key, type,
                       postgis_connection_id, postgis_query, metadata, bounds, geometry_type,
                       feature_count, size_bytes, source_map_id, created_on, last_edited
                FROM map_layers
                WHERE layer_id = ANY($1)
                """,
                list(all_layer_ids),
            )
            for layer_row in db_layers:
                layer_dict = dict(layer_row)
                layer_dict["metadata_json"] = layer_dict.pop("metadata")
                layers_by_id[layer_dict["layer_id"]] = MapLayer(**layer_dict)

        # Fetch all messages from the conversation if conversation_id is provided
        db_messages = []
        if conversation_id is not None:
            raw_uid = session.get_user_id()
            if not raw_uid:
                raise HTTPException(
                    status_code=status.HTTP_401_UNAUTHORIZED,
                    detail="Invalid session",
                )
            try:
                user_uuid = _uuid.UUID(raw_uid)
            except (ValueError, AttributeError):
                raise HTTPException(
                    status_code=status.HTTP_401_UNAUTHORIZED,
                    detail="Invalid session",
                )
            conv_ok = await conn.fetchrow(
                """
                SELECT 1
                FROM conversations c
                WHERE c.id = $1
                  AND c.owner_uuid = $2
                  AND c.project_id = $3
                  AND c.soft_deleted_at IS NULL
                """,
                conversation_id,
                user_uuid,
                map.project_id,
            )
            if not conv_ok:
                raise HTTPException(
                    status_code=status.HTTP_404_NOT_FOUND,
                    detail="Conversation not found",
                )

            db_messages = await conn.fetch(
                """
                SELECT ccm.*
                FROM chat_completion_messages ccm
                WHERE ccm.conversation_id = $1
                ORDER BY ccm.created_at ASC
                """,
                conversation_id,
            )
    # Group messages by map_id
    # some maps may have no messages
    messages_by_map: defaultdict[str, List[SanitizedMessage]] = defaultdict(list)
    for msg in db_messages:
        msg_dict = dict(msg)
        # Parse message_json when using raw asyncpg
        msg_dict["message_json"] = json.loads(msg_dict["message_json"])
        cc_message = MundiChatCompletionMessage(**msg_dict)
        if cc_message.message_json["role"] == "system":
            continue
        sanitized_payload = convert_mundi_message_to_sanitized(cc_message)

        messages_by_map[sanitized_payload.map_id].append(sanitized_payload)

    # Create MapNode objects with layer diffs
    nodes: List[MapNode] = []
    for i, map in enumerate(db_maps):
        # Calculate diff from previous map
        diff_from_previous = None
        if i > 0:
            prev_map = db_maps[i - 1]
            prev_layers = set(prev_map.layers or [])
            current_layers = set(map.layers or [])

            added_layer_ids = current_layers - prev_layers
            removed_layer_ids = prev_layers - current_layers

            added_layers = [
                LayerInfo.from_map_layer(layers_by_id[layer_id])
                for layer_id in added_layer_ids
                if layer_id in layers_by_id
            ]
            removed_layers = [
                LayerInfo.from_map_layer(layers_by_id[layer_id])
                for layer_id in removed_layer_ids
                if layer_id in layers_by_id
            ]

            diff_from_previous = LayerDiff(
                added_layers=added_layers, removed_layers=removed_layers
            )

        node = MapNode(
            map_id=map.id,
            messages=messages_by_map[map.id],
            fork_reason=map.fork_reason,
            created_on=map.created_on.isoformat(),
            diff_from_previous=diff_from_previous,
        )
        nodes.append(node)

    return MapTreeResponse(project_id=project_id, tree=nodes)


def check_postgis_readonly(plan: dict):
    if plan.get("Node Type") == "ModifyTable":
        raise ValueError("Write operations not allowed")
    for child in plan.get("Plans", []):
        check_postgis_readonly(child)


def validate_sql_query(query: str) -> str:
    """Validate that a SQL query is a safe SELECT statement.

    Prevents SQL injection by checking for dangerous patterns
    before the query is used in f-string interpolation.

    Raises HTTPException if the query is unsafe.
    """
    # Strip and normalize
    query = query.strip().rstrip(";").strip()

    if not query:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Query cannot be empty",
        )

    # Must start with SELECT (case-insensitive)
    if not re.match(r'^\s*SELECT\b', query, re.IGNORECASE):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Only SELECT queries are allowed",
        )

    # Block multiple statements (semicolons not inside quotes)
    # Simple check: no semicolons at all (we already stripped trailing ones)
    if ";" in query:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Multiple SQL statements are not allowed",
        )

    # Block dangerous keywords that should never appear in a read-only query
    dangerous_patterns = [
        r'\bINSERT\b', r'\bUPDATE\b', r'\bDELETE\b', r'\bDROP\b',
        r'\bALTER\b', r'\bCREATE\b', r'\bTRUNCATE\b', r'\bGRANT\b',
        r'\bREVOKE\b', r'\bEXEC\b', r'\bEXECUTE\b', r'\bINTO\b\s+\b(OUTFILE|DUMPFILE)\b',
        r'\bCOPY\b', r'\bpg_read_file\b', r'\bpg_read_binary_file\b',
        r'\bpg_write_file\b',
        r'\blo_import\b', r'\blo_export\b',
        r'\bpg_sleep\b',  # Prevent DoS via sleep
        r'\bdblink\b',  # Prevent lateral movement
        r'\bpg_shadow\b',  # Prevent credential access
        r'\bpg_authid\b',  # Prevent credential access
        r'\bpg_roles\b',  # Prevent role enumeration
        r'\binformation_schema\b',  # Prevent schema enumeration
    ]

    for pattern in dangerous_patterns:
        if re.search(pattern, query, re.IGNORECASE):
            keyword = pattern.replace(r'\b', '').replace(r'\s+', ' ')
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"Dangerous SQL keyword detected in query: {keyword}",
            )

    return query


async def _generate_postgis_pmtiles_background(
    layer_id: str,
    postgis_connection_id: str,
    query: str,
    feature_count: int,
    user_id: str,
    project_id: str,
    conversation_id: int | None = None,
) -> None:
    """Fire-and-forget wrapper for PostGIS PMTiles generation.

    Never raises — all exceptions are logged and swallowed so the chat
    flow is never interrupted.

    When conversation_id is provided, sends a WebSocket style_json update
    after PMTiles is ready so the frontend refetches the style with
    pmtiles:// URLs instead of the .mvt fallback.
    """
    try:
        from src.upload.pmtiles import generate_pmtiles_for_postgis_layer

        pmtiles_key = await generate_pmtiles_for_postgis_layer(
            layer_id, postgis_connection_id, query,
            feature_count, user_id, project_id,
        )
        logger.info(
            "Background PMTiles generation completed for PostGIS layer %s -> %s",
            layer_id, pmtiles_key,
        )

        # Notify frontend to refetch style.json now that PMTiles is available
        if conversation_id is not None and pmtiles_key:
            try:
                async with kue_ephemeral_action(
                    conversation_id,
                    "Vector tiles ready",
                    update_style_json=True,
                ):
                    pass  # Just need the active→completed cycle to trigger refetch
            except Exception:
                logger.debug("Failed to send PMTiles-ready notification", exc_info=True)
    except Exception:
        logger.warning(
            "Background PMTiles generation failed for PostGIS layer %s",
            layer_id, exc_info=True,
        )


def _admin_sql_literal(value: object) -> str:
    return "'" + str(value).replace("'", "''") + "'"


def _admin_boundary_style(layer_id: str) -> list[dict]:
    source_layer = "reprojectedfgb"
    return [
        {
            "id": f"{layer_id}-fill",
            "type": "fill",
            "source": layer_id,
            "source-layer": source_layer,
            "paint": {
                "fill-color": "#ff4d4d",
                "fill-opacity": 0.16,
            },
        },
        {
            "id": f"{layer_id}-outline",
            "type": "line",
            "source": layer_id,
            "source-layer": source_layer,
            "paint": {
                "line-color": "#ff2d2d",
                "line-width": 3,
            },
        },
    ]


async def _resolve_admin_boundary_query(
    conn,
    args: dict[str, object],
) -> dict[str, object]:
    requested_level = str(args.get("admin_level") or "auto").strip().lower()
    name = str(args.get("name") or "").strip()
    if not name:
        return {"status": "error", "error": "Missing boundary name."}

    levels = {
        "province": {
            "table": "rwanda_province_boundaries",
            "name_col": "province",
            "attrs": ["province"],
            "label": "province",
        },
        "district": {
            "table": "rwanda_district_boundaries",
            "name_col": "district",
            "attrs": ["district"],
            "label": "district",
        },
        "sector": {
            "table": "rwanda_sector_boundaries",
            "name_col": "sector_name",
            "attrs": ["sector_name", "district_name"],
            "label": "sector",
        },
        "cell": {
            "table": "rwanda_cell_boundaries",
            "name_col": "cell_name",
            "attrs": ["cell_name", "sector_name", "district_name"],
            "label": "cell",
        },
        "village": {
            "table": "rwanda_village_boundaries",
            "name_col": "village_name",
            "attrs": ["village_name", "cell_name", "sector_name", "district_name"],
            "label": "village",
        },
    }

    def _filters_for(level: str, *, sql: bool) -> tuple[list[str], list[object]]:
        spec = levels[level]
        filters: list[str] = []
        params: list[object] = []
        if name not in {"*", "all"}:
            if sql:
                filters.append(f"LOWER({spec['name_col']}) = LOWER({_admin_sql_literal(name)})")
            else:
                params.append(name)
                filters.append(f"LOWER({spec['name_col']}) = LOWER(${len(params)})")
        for arg_key, col in (
            ("district", "district_name"),
            ("sector", "sector_name"),
            ("cell", "cell_name"),
        ):
            value = args.get(arg_key)
            if not value:
                continue
            available_cols = set(spec["attrs"]) | {spec["name_col"]}
            if col not in available_cols:
                continue
            if sql:
                filters.append(f"LOWER({col}) = LOWER({_admin_sql_literal(value)})")
            else:
                params.append(value)
                filters.append(f"LOWER({col}) = LOWER(${len(params)})")
        return filters, params

    async def _match(level: str) -> dict[str, object]:
        spec = levels[level]
        filters, params = _filters_for(level, sql=False)
        where = " AND ".join(filters) if filters else "TRUE"
        try:
            rows = await conn.fetch(
                f"""
                SELECT {', '.join(spec['attrs'])},
                       ST_XMin(ST_Extent(geom)) AS xmin,
                       ST_YMin(ST_Extent(geom)) AS ymin,
                       ST_XMax(ST_Extent(geom)) AS xmax,
                       ST_YMax(ST_Extent(geom)) AS ymax,
                       COUNT(*) OVER() AS match_count
                FROM {spec['table']}
                WHERE {where}
                GROUP BY {', '.join(spec['attrs'])}
                ORDER BY {', '.join(spec['attrs'])}
                LIMIT 12
                """,
                *params,
            )
        except asyncpg.exceptions.UndefinedTableError:
            logger.warning("Admin boundary table missing: %s", spec["table"])
            return {"status": "not_found", "admin_level": level}
        if not rows:
            return {"status": "not_found", "admin_level": level}
        total = int(rows[0]["match_count"])
        candidates = [dict(row) for row in rows]

        sql_filters, _ = _filters_for(level, sql=True)
        sql_where = " AND ".join(sql_filters) if sql_filters else "TRUE"
        attr_select = ", ".join(spec["attrs"])
        query = (
            f"SELECT ROW_NUMBER() OVER()::bigint AS id, {attr_select}, geom "
            f"FROM {spec['table']} WHERE {sql_where}"
        )
        if name in {"*", "all"} or total > 1:
            extent_row = await conn.fetchrow(
                f"""
                SELECT ST_XMin(ST_Extent(geom)) AS xmin,
                       ST_YMin(ST_Extent(geom)) AS ymin,
                       ST_XMax(ST_Extent(geom)) AS xmax,
                       ST_YMax(ST_Extent(geom)) AS ymax
                FROM {spec['table']}
                WHERE {where}
                """,
                *params,
            )
            bounds_source = extent_row or rows[0]
        else:
            bounds_source = rows[0]
        bounds = [
            float(bounds_source["xmin"]),
            float(bounds_source["ymin"]),
            float(bounds_source["xmax"]),
            float(bounds_source["ymax"]),
        ]
        first = dict(rows[0])
        display_name = (
            str(first.get(spec["name_col"]) or name)
            if name not in {"*", "all"}
            else f"{level.title()} Boundaries"
        )
        if total > 1 and name not in {"*", "all"}:
            return {
                "status": "ambiguous",
                "admin_level": level,
                "admin_name": display_name,
                "query": query,
                "bounds": bounds,
                "feature_count": total,
                "attribute_columns": spec["attrs"],
                "layer_name": f"{display_name} {level.title()} Matches",
                "candidates": candidates,
                "match_count": total,
            }
        return {
            "status": "success",
            "admin_level": level,
            "admin_name": display_name,
            "query": query,
            "bounds": bounds,
            "feature_count": total if name in {"*", "all"} else 1,
            "attribute_columns": spec["attrs"],
            "layer_name": (
                f"{display_name} {level.title()} Boundary"
                if name not in {"*", "all"}
                else f"{display_name}"
            ),
        }

    search_levels = (
        list(levels)
        if requested_level == "auto"
        else [requested_level]
    )
    for level in search_levels:
        if level not in levels:
            continue
        result = await _match(level)
        if result["status"] != "not_found":
            return result
    return {
        "status": "not_found",
        "admin_level": requested_level,
        "admin_name": name,
        "error": f"No Rwanda administrative boundary found for {name!r}.",
    }


def _admin_boundary_fast_reply(result: dict[str, object]) -> str:
    name = str(result.get("admin_name") or "that area")
    level = str(result.get("admin_level") or "admin")
    if result.get("status") == "success":
        count = result.get("feature_count")
        if isinstance(count, int) and count > 1:
            return f"I added {count} {level} boundaries to the map."
        return f"I added {name} {level} boundary to the map."
    if result.get("status") == "ambiguous":
        examples: list[str] = []
        candidates = result.get("candidates")
        if isinstance(candidates, list):
            for candidate in candidates[:5]:
                if not isinstance(candidate, dict):
                    continue
                parts = [
                    str(candidate[key])
                    for key in ("village_name", "cell_name", "sector_name", "district_name", "province")
                    if candidate.get(key)
                ]
                if parts:
                    examples.append(" / ".join(parts))
        suffix = f" Examples: {'; '.join(examples)}." if examples else ""
        count = result.get("match_count") or result.get("feature_count")
        count_text = f" {count}" if isinstance(count, int) and count > 1 else ""
        if result.get("layer_id"):
            return (
                f"I found{count_text} matches for {name} and added them to the map in red. "
                f"Specify the parent district, sector, or cell if you want only one.{suffix}"
            )
        return f"I found{count_text} matches for {name}. Please specify the parent district, sector, or cell.{suffix}"
    return str(result.get("error") or f"I couldn't find {name}.")


async def _run_abdication_guard(
    client,
    attempt_kwargs: dict,
    last_user_text: str,
    history: list[dict],
    full_tools: list[dict],
) -> dict[int, dict]:
    """One forced-tool retry over the guard tools. Returns tool calls in the
    loop's accumulator shape, or {} when the retry produced none (logged)."""
    tools = copy.deepcopy(await guard_tools(last_user_text, history, full_tools))
    if not supports_strict_tool_schema(str(attempt_kwargs.get("model") or "")):
        for tool in tools:
            tool.get("function", {}).pop("strict", None)
    try:
        response = await client.chat.completions.create(
            **{**attempt_kwargs, "tools": tools, "tool_choice": "required"}, stream=False,
        )
    except Exception:
        logger.warning("sage_routing: abdication guard retry failed; keeping the prose answer", exc_info=True)
        return {}
    calls = guard_tool_calls(getattr(response.choices[0].message, "tool_calls", None) or [])
    logger.info(
        "sage_routing: abdication guard fired (tools=%s) -> %s",
        ",".join(t["function"]["name"] for t in tools),
        ",".join(c.function.name for c in calls) or "no tool call",
    )
    return {
        i: {"id": c.id, "type": "function",
            "function": {"name": c.function.name, "arguments": c.function.arguments or "{}"}}
        for i, c in enumerate(calls)
    }


async def _maybe_run_fast_admin_boundary_turn(
    *,
    map_id: str,
    session: UserContext,
    user_id: str,
    conversation: Conversation,
    openai_messages: list[dict],
) -> bool:
    if os.environ.get("SAGE_FAST_ADMIN_BOUNDARIES", "1").strip().lower() in {
        "0", "false", "no", "off",
    }:
        return False

    fast_call = build_fast_tool_call(extract_last_user_text(openai_messages))
    if not fast_call or fast_call.tool_name != ADMIN_BOUNDARY_TOOL:
        return False

    started = asyncio.get_running_loop().time()
    turn_id = f"fast-admin-{conversation.id}-{_uuid.uuid4().hex[:8]}"
    partner_id = session.get_org_id()

    async with async_conn(
        "sage.fast_admin_boundary",
        user_id=user_id,
        partner_id=partner_id,
    ) as conn:
        project_row = await conn.fetchrow(
            "SELECT project_id FROM user_mundiai_maps WHERE id = $1",
            map_id,
        )
        if not project_row:
            return False
        project_id = project_row["project_id"]
        pgc_id = await _ensure_rwanda_postgis_connection(conn, project_id, user_id)
        if not pgc_id:
            return False

        result = await _resolve_admin_boundary_query(conn, fast_call.arguments)
        if result.get("status") in {"success", "ambiguous"} and result.get("query"):
            layer_id = generate_id(prefix="L")
            style_id = generate_id(prefix="S")
            layer_name = str(result["layer_name"])
            query = str(result["query"])
            bounds = result.get("bounds")
            attr_cols = result.get("attribute_columns") or []
            feature_count = int(result.get("feature_count") or 0)

            async with kue_ephemeral_action(
                conversation.id,
                "Showing admin boundary...",
                update_style_json=True,
                bounds=bounds if isinstance(bounds, list) and len(bounds) == 4 else None,
            ):
                async with conn.transaction():
                    await conn.execute(
                        """
                        INSERT INTO map_layers
                        (layer_id, owner_uuid, name, type,
                         postgis_connection_id, postgis_query,
                         metadata, feature_count, bounds, geometry_type, source_map_id,
                         created_on, last_edited, postgis_attribute_column_list)
                        VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,
                                CURRENT_TIMESTAMP,CURRENT_TIMESTAMP,$12)
                        """,
                        layer_id,
                        user_id,
                        layer_name,
                        "postgis",
                        pgc_id,
                        query,
                        json.dumps({"fast_admin_boundary": True}),
                        feature_count,
                        bounds,
                        "multipolygon",
                        map_id,
                        attr_cols,
                    )
                    await conn.execute(
                        """
                        INSERT INTO layer_styles
                        (style_id, layer_id, style_json, created_by, created_on)
                        VALUES ($1, $2, $3, $4, CURRENT_TIMESTAMP)
                        """,
                        style_id,
                        layer_id,
                        json.dumps(_admin_boundary_style(layer_id)),
                        user_id,
                    )
                    await conn.execute(
                        """
                        INSERT INTO map_layer_styles (map_id, layer_id, style_id)
                        VALUES ($1, $2, $3)
                        """,
                        map_id,
                        layer_id,
                        style_id,
                    )
                    await conn.execute(
                        """
                        UPDATE user_mundiai_maps
                        SET layers = CASE
                            WHEN layers IS NULL THEN ARRAY[$1]
                            ELSE array_append(layers, $1)
                        END
                        WHERE id = $2 AND (layers IS NULL OR NOT ($1 = ANY(layers)))
                        """,
                        layer_id,
                        map_id,
                    )
            result["layer_id"] = layer_id

        assistant_text = _admin_boundary_fast_reply(result)
        await conn.execute(
            """
            INSERT INTO chat_completion_messages
            (map_id, sender_id, message_json, conversation_id)
            VALUES ($1, $2, $3, $4)
            """,
            map_id,
            user_id,
            json.dumps({"role": "assistant", "content": assistant_text}),
            conversation.id,
        )

    await kue_stream_token(conversation.id, assistant_text, turn_id=turn_id)
    await kue_stream_token(conversation.id, "", done=True, turn_id=turn_id)
    logger.info(
        "Sage fast admin boundary route: conv=%s args=%s status=%s elapsed=%.3fs",
        conversation.id,
        json.dumps(fast_call.arguments, default=str),
        result.get("status"),
        asyncio.get_running_loop().time() - started,
    )
    return True


def _format_fast_ha(value: object) -> str:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return str(value)
    if number >= 100:
        return f"{number:,.0f}"
    return f"{number:,.1f}".rstrip("0").rstrip(".")


def _raster_area_fast_reply(result: dict) -> str:
    name = str(result.get("name") or "That raster")
    if result.get("error"):
        return f"I found the raster layer, but I could not measure it: {result['error']}"

    area_valid = result.get("area_valid_ha")
    area_bbox = result.get("area_bbox_ha") or result.get("area_ha")
    area_label = result.get("area_label")
    valid_fraction = result.get("valid_pixel_fraction")

    if area_valid is not None:
        reply = f"{name} covers {_format_fast_ha(area_valid)} ha of valid imagery/field footprint."
        if area_bbox is not None:
            reply += f" The full rectangular raster extent is {_format_fast_ha(area_bbox)} ha."
        if valid_fraction is not None:
            try:
                reply += f" Valid pixels are {float(valid_fraction) * 100:.1f}% of that extent."
            except (TypeError, ValueError):
                pass
    elif area_label:
        reply = f"{name} covers {area_label}."
    elif area_bbox is not None:
        reply = f"{name} covers about {_format_fast_ha(area_bbox)} ha by its raster bounding box."
    else:
        reply = f"I found {name}, but it does not have enough geospatial metadata to calculate hectares yet."

    cog_status = result.get("cog_status")
    if cog_status and cog_status != "ready":
        reply += f" The optimized COG is still {cog_status}, so this uses the best metadata available now."
    return reply


def _raster_context_fast_reply(
    result: dict,
    layer_name: str,
    *,
    requested_building_count: bool = False,
) -> str:
    if result.get("error"):
        return (
            f"I found {layer_name}, but the raster analysis failed: {result['error']}"
        )

    summary = result.get("summary") if isinstance(result.get("summary"), dict) else {}
    domain = str(summary.get("domain") or "mixed")
    cell_count = summary.get("cell_count") or result.get("geojson_feature_count")
    high_count = summary.get("high_or_severe_cell_count")
    max_score = summary.get("max_score")
    evidence_basis = str(summary.get("evidence_basis") or "")

    stats_sentence = ""
    if cell_count:
        stats_sentence = f" The layer has {cell_count} screening cells"
        if high_count is not None:
            stats_sentence += f", with {high_count} higher-priority cells"
        if max_score is not None:
            stats_sentence += f" and a top proxy score of {max_score}"
        stats_sentence += "."

    if requested_building_count:
        reply = (
            f"This view of {layer_name} highlights built-up-looking areas, "
            "but it does not mark individual houses yet."
        )
        reply += stats_sentence
        reply += (
            " For a house count, I need the small roof/house shapes outlined "
            "on the map. Once those marks are visible, treat the number as "
            "marks to review until the important ones are spot-checked."
        )
        return reply

    if domain == "housing":
        reply = (
            f"I screened {layer_name} itself for settlement-looking visual "
            "patterns and added proxy cells on top of the orthophoto."
        )
    elif domain == "agriculture":
        reply = (
            f"I screened {layer_name} itself for vegetation and field-condition "
            "patterns and added proxy cells on top of the orthophoto."
        )
    elif domain == "infrastructure":
        reply = (
            f"I screened {layer_name} itself for road, drainage, and other "
            "infrastructure-looking visual patterns and added proxy cells on "
            "top of the orthophoto."
        )
    elif domain == "environment":
        reply = (
            f"I screened {layer_name} itself for environmental surface patterns "
            "and added proxy cells on top of the orthophoto."
        )
    else:
        reply = (
            f"I screened {layer_name} itself and added visual proxy cells on top "
            "of the orthophoto."
        )

    reply += stats_sentence

    if domain in {"housing", "infrastructure"}:
        reply += (
            " This is a fast visual screen from the uploaded image, so use it "
            "to find where to look first. It is not an exact count of houses, "
            "roads, or other assets."
        )
    elif evidence_basis:
        reply += f" Evidence basis: {evidence_basis}."

    return reply


async def _maybe_run_fast_raster_context_turn(
    *,
    map_id: str,
    session: UserContext,
    user_id: str,
    conversation: Conversation,
    openai_messages: list[dict],
) -> bool:
    if os.environ.get("SAGE_FAST_RASTER_CONTEXT", "1").strip().lower() in {
        "0", "false", "no", "off",
    }:
        return False

    user_text = extract_last_user_text(openai_messages)
    fast_call = build_fast_tool_call(user_text)
    if not fast_call or fast_call.tool_name != RASTER_H3_CONTEXT_TOOL:
        return False

    requested_building_count = detect_raster_building_count_question(user_text)
    started = asyncio.get_running_loop().time()
    turn_id = f"fast-raster-context-{conversation.id}-{_uuid.uuid4().hex[:8]}"
    partner_id = session.get_org_id()

    async with async_conn(
        "sage.fast_raster_context",
        user_id=user_id,
        partner_id=partner_id,
    ) as conn:
        project_row = await conn.fetchrow(
            "SELECT project_id FROM user_mundiai_maps WHERE id = $1",
            map_id,
        )
        if not project_row:
            return False

        rows = await conn.fetch(
            """
            SELECT ml.layer_id, ml.name
            FROM user_mundiai_maps m
            JOIN LATERAL unnest(m.layers) WITH ORDINALITY AS map_layer(layer_id, ord)
              ON TRUE
            JOIN map_layers ml ON ml.layer_id = map_layer.layer_id
            WHERE m.id = $1
              AND ml.type = 'raster'
            ORDER BY map_layer.ord
            """,
            map_id,
        )

    layer = select_fast_raster_layer(user_text, rows)
    if not layer:
        return False

    from src.tools.pyd import IngabeToolCallMetaArgs
    from src.tools.raster_h3_context import (
        CreateRasterH3ContextLayerArgs,
        create_raster_h3_context_layer,
    )

    tool_args = dict(fast_call.arguments)
    tool_args["layer_id"] = str(layer["layer_id"])
    layer_name = str(layer.get("name") or "that raster")

    async with kue_ephemeral_action(
        conversation.id,
        "Analyzing orthophoto pixels...",
        layer_id=str(layer["layer_id"]),
    ):
        result = await create_raster_h3_context_layer(
            CreateRasterH3ContextLayerArgs(**tool_args),
            IngabeToolCallMetaArgs(
                user_uuid=user_id,
                conversation_id=conversation.id,
                map_id=map_id,
                project_id=str(project_row["project_id"]),
                session=session,
            ),
        )

    assistant_text = _raster_context_fast_reply(
        result,
        layer_name,
        requested_building_count=requested_building_count,
    )
    summary = result.get("summary") if isinstance(result.get("summary"), dict) else {}
    capture_for_session(
        "backend_sage_fast_raster_context_answered",
        session,
        {
            "map_id": map_id,
            "conversation_id": conversation.id,
            "layer_id": str(layer["layer_id"]),
            "domain": tool_args.get("domain"),
            "status": result.get("status"),
            "requested_building_count": requested_building_count,
            "answer_evidence_class": (
                "proxy_not_counted" if requested_building_count else "raster_proxy"
            ),
            "cell_count": summary.get("cell_count") or result.get("geojson_feature_count"),
            "high_or_severe_cell_count": summary.get("high_or_severe_cell_count"),
            "max_score": summary.get("max_score"),
            "duration_ms": int((asyncio.get_running_loop().time() - started) * 1000),
        },
    )
    async with async_conn(
        "sage.fast_raster_context.persist",
        user_id=user_id,
        partner_id=partner_id,
    ) as conn:
        await conn.execute(
            """
            INSERT INTO chat_completion_messages
            (map_id, sender_id, message_json, conversation_id)
            VALUES ($1, $2, $3, $4)
            """,
            map_id,
            user_id,
            json.dumps({"role": "assistant", "content": assistant_text}),
            conversation.id,
        )

    await kue_stream_token(conversation.id, assistant_text, turn_id=turn_id)
    await kue_stream_token(conversation.id, "", done=True, turn_id=turn_id)
    logger.info(
        "Sage fast raster context route: conv=%s layer_id=%s domain=%s status=%s elapsed=%.3fs",
        conversation.id,
        layer["layer_id"],
        tool_args.get("domain"),
        result.get("status"),
        asyncio.get_running_loop().time() - started,
    )
    return True


def _raster_object_fast_reply(
    result: dict,
    layer_name: str,
    *,
    requested_building_count: bool = False,
) -> str:
    if result.get("status") != "success":
        if result.get("status") == "timeout":
            return (
                f"I could not finish marking the houses in {layer_name} within the live chat limit. "
                "I stopped this attempt so Sage does not keep thinking forever. "
                "Zoom into the village area and ask again, or run the deeper map analysis job."
            )
        detail = result.get("error") or result.get("status") or "unknown error"
        return (
            f"I could not mark the requested features in {layer_name}: {detail}"
        )

    summary = result.get("summary") if isinstance(result.get("summary"), dict) else {}
    candidate_count = int(summary.get("candidate_count") or result.get("geojson_feature_count") or 0)
    class_counts = summary.get("class_counts") if isinstance(summary.get("class_counts"), dict) else {}
    class_text = ", ".join(f"{klass}: {count}" for klass, count in sorted(class_counts.items())) or "building candidates"
    performance_note = summary.get("performance_note")
    engines = result.get("engines") if isinstance(result.get("engines"), dict) else {}
    selection = (
        engines.get("selection")
        if isinstance(engines.get("selection"), dict)
        else {}
    )
    engine_used = str(selection.get("used") or summary.get("screening_model") or "").strip()
    used_fastsam = engine_used == "fastsam_s_candidate_masks_v1"
    rendered_layer_id = result.get("layer_id")
    if not rendered_layer_id:
        render_engine = engines.get("render") if isinstance(engines.get("render"), dict) else {}
        rendered_layer_id = render_engine.get("layer_id")
    delivery = result.get("delivery") if isinstance(result.get("delivery"), dict) else {}
    delivery_status = str(delivery.get("status") or "unverified")
    delivery_verified = delivery_status == "verified" and bool(
        delivery.get("verified")
    )
    visible_layer_name = (
        f"House/Roof Masks - {layer_name}"
        if requested_building_count
        else f"Feature Masks - {layer_name}"
    )
    if delivery_verified and rendered_layer_id:
        delivery_sentence = "I added them as a colored mask layer."
    elif delivery_status == "preview_only":
        delivery_sentence = (
            "I prepared a colored preview, but I could not verify a persistent "
            "map layer, so I am not claiming that it is visible yet."
        )
    else:
        delivery_sentence = (
            "The analysis finished, but I could not verify that its colored mask "
            "layer reached the map, so I am not claiming that it is visible yet."
        )
    layer_hint = (
        f" Turn on the layer `{visible_layer_name}`"
        f" to see the colored polygons on the orthophoto"
        f"{f' (layer {rendered_layer_id})' if rendered_layer_id else ''}."
        if delivery_verified and rendered_layer_id
        else ""
    )
    is_capped = bool(summary.get("candidate_count_capped"))
    plain_performance_note = _plain_raster_object_performance_note(performance_note)
    performance_suffix = f" {plain_performance_note}" if plain_performance_note else ""
    cap_note = ""
    if is_capped:
        max_candidates = int(summary.get("max_candidates") or candidate_count)
        cap_note = (
            f" I displayed {candidate_count} mask polygons because live map "
            f"answers are capped at {max_candidates}; read that as the visible "
            "overlay size, not a full object count."
        )
    suffix = f"{cap_note} {plain_performance_note}".strip()
    suffix = f" {suffix}" if suffix else ""
    engine_note = ""
    if not used_fastsam and engine_used:
        engine_note = (
            " FastSAM was not available for this live run, so I used the quick "
            "image screener. Treat this as a rough overlay until FastSAM is active."
        )
    if requested_building_count and (class_counts.get("building") or summary.get("candidate_building_count")):
        building_candidates = int(class_counts.get("building") or summary.get("candidate_building_count") or 0)
        if is_capped:
            max_candidates = int(summary.get("max_candidates") or candidate_count)
            return (
                f"I found {candidate_count} possible roof/house shapes in {layer_name} "
                f"from the image. {delivery_sentence} The analysis result is capped at "
                f"{max_candidates}, so this is not the final house count."
                f"{layer_hint} Some roofs may still be missing or mixed with trees, "
                "shadows, and bare ground."
                f"{engine_note}{performance_suffix}"
            )
        return (
            f"I found {building_candidates} possible roof/house shapes in {layer_name} "
            f"from the image. {delivery_sentence}"
            f"{layer_hint} Treat this as a map overlay to review, not a final house count yet. "
            "Spot-check the important areas, "
            "especially where roofs touch trees, shadows, or roads."
            f"{engine_note}{suffix}"
        )
    return (
        f"I found {candidate_count} possible features in {layer_name} ({class_text}) "
        f"from the image. {delivery_sentence}"
        f"{layer_hint} Review the overlay against the image before treating the counts as final."
        f"{engine_note}{suffix}"
    )


def _plain_raster_object_performance_note(note: object) -> str:
    if not note:
        return ""
    lowered = str(note).lower()
    if "timed out" in lowered or "fallback" in lowered or "rasterio" in lowered:
        return "I used the quick image-analysis path for this live answer; a deeper pass can refine the marks later."
    return ""


def _fast_raster_object_turn_timeout_seconds() -> float:
    raw = os.environ.get("SAGE_FAST_RASTER_OBJECTS_TIMEOUT_SECONDS", "600")
    try:
        return max(15.0, float(raw))
    except (TypeError, ValueError):
        return 600.0


def _tool_timeout_seconds() -> float:
    raw = os.environ.get("SAGE_TOOL_TIMEOUT_SECONDS", "120")
    try:
        return max(15.0, float(raw))
    except (TypeError, ValueError):
        return 120.0


def _pair_tool_results(messages: list[Any]) -> list[Any]:
    """Every tool call in the replayed history gets exactly one result, or the provider rejects the whole
    conversation (HTTP 400) on every later message. A turn cut off mid-tool (a restart, a crash) left calls
    without results; a call dropped while cleaning the history can leave a result without its call."""
    def calls(m: Any) -> list[str]:
        if not isinstance(m, dict) or m.get("role") != "assistant":
            return []
        return [tc["id"] for tc in m.get("tool_calls") or [] if isinstance(tc, dict) and tc.get("id")]

    called = {call_id for m in messages for call_id in calls(m)}
    answered = {m.get("tool_call_id") for m in messages if isinstance(m, dict) and m.get("role") == "tool"}
    unfinished = json.dumps({"status": "error", "error": "This tool did not finish: the turn was interrupted."})
    out: list[Any] = []
    missing: list[str] = []
    for m in messages:
        if isinstance(m, dict) and m.get("role") == "tool":
            if m.get("tool_call_id") in called:
                out.append(m)
            else:
                logger.warning("Dropped a tool result with no call before it: %s", m.get("tool_call_id"))
            continue
        out.extend({"role": "tool", "tool_call_id": call_id, "content": unfinished} for call_id in missing)
        out.append(m)
        missing = [call_id for call_id in calls(m) if call_id not in answered]
        if missing:
            logger.warning("Gave %d unfinished tool call(s) a result: %s", len(missing), missing)
    out.extend({"role": "tool", "tool_call_id": call_id, "content": unfinished} for call_id in missing)
    return out


async def _within_tool_limit(function_name: str, call: Any) -> Any:
    """Runs one tool call. A tool that never returns (a stalled download, say) must not hold the
    person's turn for ever: it is stopped and the model told, so it answers with what it has."""
    limit = _tool_timeout_seconds()
    try:
        return await asyncio.wait_for(call, timeout=limit)
    except TimeoutError:
        logger.warning("Sage tool %s stopped after %.0f s", function_name, limit)
        return {
            "status": "error",
            "error": (f"{function_name} did not finish within {limit:.0f} seconds and was stopped. "
                      "Answer with what the other tools found, and say plainly which part is missing."),
        }


async def _maybe_run_fast_raster_object_turn(
    *,
    map_id: str,
    session: UserContext,
    user_id: str,
    conversation: Conversation,
    openai_messages: list[dict],
) -> bool:
    if os.environ.get("SAGE_FAST_RASTER_OBJECTS", "1").strip().lower() in {
        "0", "false", "no", "off",
    }:
        return False

    user_text = extract_last_user_text(openai_messages)
    fast_call = build_fast_tool_call(user_text)
    if not fast_call or fast_call.tool_name != RASTER_OBJECT_CANDIDATES_TOOL:
        return False

    requested_building_count = detect_raster_building_count_question(user_text)

    started = asyncio.get_running_loop().time()
    turn_id = f"fast-raster-objects-{conversation.id}-{_uuid.uuid4().hex[:8]}"
    partner_id = session.get_org_id()

    async with async_conn(
        "sage.fast_raster_objects",
        user_id=user_id,
        partner_id=partner_id,
    ) as conn:
        project_row = await conn.fetchrow(
            "SELECT project_id FROM user_mundiai_maps WHERE id = $1",
            map_id,
        )
        if not project_row:
            return False

        rows = await conn.fetch(
            """
            SELECT ml.layer_id, ml.name
            FROM user_mundiai_maps m
            JOIN LATERAL unnest(m.layers) WITH ORDINALITY AS map_layer(layer_id, ord)
              ON TRUE
            JOIN map_layers ml ON ml.layer_id = map_layer.layer_id
            WHERE m.id = $1
              AND ml.type = 'raster'
            ORDER BY map_layer.ord
            """,
            map_id,
        )

    layer = select_fast_raster_layer(user_text, rows)
    if not layer:
        return False

    from src.tools.pyd import IngabeToolCallMetaArgs
    from src.tools.raster_object_candidates import (
        AnalyzeRasterObjectCandidatesArgs,
        analyze_raster_object_candidates,
    )

    tool_args = dict(fast_call.arguments)
    tool_args["layer_id"] = str(layer["layer_id"])
    layer_name = str(layer.get("name") or "that raster")
    fast_timeout_seconds = _fast_raster_object_turn_timeout_seconds()

    capture_for_session(
        "backend_sage_fast_raster_objects_started",
        session,
        {
            "map_id": map_id,
            "conversation_id": conversation.id,
            "layer_id": str(layer["layer_id"]),
            "requested_building_count": requested_building_count,
            "target_classes_csv": ",".join(
                str(value) for value in tool_args.get("target_classes", []) if value
            ),
            "max_candidates": tool_args.get("max_candidates"),
            "engine_preference": tool_args.get("engine_preference"),
            "timeout_seconds": fast_timeout_seconds,
        },
    )

    async with kue_ephemeral_action(
        conversation.id,
        (
            "Marking likely houses on the orthophoto..."
            if requested_building_count
            else "Marking requested features on the orthophoto..."
        ),
        layer_id=str(layer["layer_id"]),
    ):
        try:
            result = await asyncio.wait_for(
                analyze_raster_object_candidates(
                    AnalyzeRasterObjectCandidatesArgs(**tool_args),
                    IngabeToolCallMetaArgs(
                        user_uuid=user_id,
                        conversation_id=conversation.id,
                        map_id=map_id,
                        project_id=str(project_row["project_id"]),
                        session=session,
                    ),
                ),
                timeout=fast_timeout_seconds,
            )
        except asyncio.TimeoutError:
            logger.warning(
                "Sage fast raster object route timed out: conv=%s layer_id=%s timeout=%.1fs",
                conversation.id,
                layer["layer_id"],
                fast_timeout_seconds,
            )
            result = {
                "status": "timeout",
                "error": (
                    "Raster object marking exceeded the live chat timeout "
                    f"of {fast_timeout_seconds:.0f}s."
                ),
                "summary": {
                    "source_layer_id": str(layer["layer_id"]),
                    "source_layer_name": layer_name,
                    "candidate_count": 0,
                    "candidate_building_count": 0,
                    "confirmed_count_available": False,
                    "requested_targets": tool_args.get("target_classes", []),
                    "count_semantics": "not_available_timeout",
                    "timeout_seconds": fast_timeout_seconds,
                },
                "engines": {
                    "selection": {
                        "requested": tool_args.get("engine_preference"),
                        "used": "timeout_before_result",
                    }
                },
            }
        except Exception as exc:
            logger.exception(
                "Sage fast raster object route crashed: conv=%s layer_id=%s",
                conversation.id,
                layer["layer_id"],
            )
            result = {
                "status": "error",
                "error": f"{type(exc).__name__}: {exc}",
                "summary": {
                    "source_layer_id": str(layer["layer_id"]),
                    "source_layer_name": layer_name,
                    "candidate_count": 0,
                    "candidate_building_count": 0,
                    "confirmed_count_available": False,
                    "requested_targets": tool_args.get("target_classes", []),
                    "count_semantics": "not_available_error",
                },
                "engines": {
                    "selection": {
                        "requested": tool_args.get("engine_preference"),
                        "used": "error_before_result",
                    }
                },
            }

    assistant_text = _raster_object_fast_reply(
        result,
        layer_name,
        requested_building_count=requested_building_count,
    )
    summary = result.get("summary") if isinstance(result.get("summary"), dict) else {}
    engines = result.get("engines") if isinstance(result.get("engines"), dict) else {}
    selection = (
        engines.get("selection")
        if isinstance(engines.get("selection"), dict)
        else {}
    )
    capture_for_session(
        "backend_sage_fast_raster_objects_answered",
        session,
        {
            "map_id": map_id,
            "conversation_id": conversation.id,
            "layer_id": str(layer["layer_id"]),
            "status": result.get("status"),
            "candidate_count": summary.get("candidate_count")
            or result.get("geojson_feature_count"),
            "candidate_building_count": summary.get("candidate_building_count"),
            "confirmed_count_available": summary.get("confirmed_count_available"),
            "requested_building_count": requested_building_count,
            "count_semantics": summary.get("count_semantics"),
            "class_counts": json.dumps(summary.get("class_counts") or {}),
            "source_storage": summary.get("source_storage"),
            "engine_requested": selection.get("requested"),
            "engine_used": selection.get("used"),
            "screening_model": summary.get("screening_model")
            or selection.get("used"),
            "candidate_count_capped": summary.get("candidate_count_capped"),
            "duration_ms": int((asyncio.get_running_loop().time() - started) * 1000),
        },
    )
    async with async_conn(
        "sage.fast_raster_objects.persist",
        user_id=user_id,
        partner_id=partner_id,
    ) as conn:
        await conn.execute(
            """
            INSERT INTO chat_completion_messages
            (map_id, sender_id, message_json, conversation_id)
            VALUES ($1, $2, $3, $4)
            """,
            map_id,
            user_id,
            json.dumps({"role": "assistant", "content": assistant_text}),
            conversation.id,
        )

    await kue_stream_token(conversation.id, assistant_text, turn_id=turn_id)
    await kue_stream_token(conversation.id, "", done=True, turn_id=turn_id)
    logger.info(
        "Sage fast raster object route: conv=%s layer_id=%s status=%s elapsed=%.3fs",
        conversation.id,
        layer["layer_id"],
        result.get("status"),
        asyncio.get_running_loop().time() - started,
    )
    return True


async def _maybe_run_fast_raster_fact_turn(
    *,
    map_id: str,
    session: UserContext,
    user_id: str,
    conversation: Conversation,
    openai_messages: list[dict],
) -> bool:
    if os.environ.get("SAGE_FAST_RASTER_FACTS", "1").strip().lower() in {
        "0", "false", "no", "off",
    }:
        return False

    user_text = extract_last_user_text(openai_messages)
    fast_call = build_fast_tool_call(user_text)
    if not fast_call or fast_call.tool_name != RASTER_FACT_TOOL:
        return False

    started = asyncio.get_running_loop().time()
    turn_id = f"fast-raster-{conversation.id}-{_uuid.uuid4().hex[:8]}"
    partner_id = session.get_org_id()

    async with async_conn(
        "sage.fast_raster_fact",
        user_id=user_id,
        partner_id=partner_id,
    ) as conn:
        project_row = await conn.fetchrow(
            "SELECT project_id FROM user_mundiai_maps WHERE id = $1",
            map_id,
        )
        if not project_row:
            return False

        rows = await conn.fetch(
            """
            SELECT ml.layer_id, ml.name
            FROM user_mundiai_maps m
            JOIN LATERAL unnest(m.layers) WITH ORDINALITY AS map_layer(layer_id, ord)
              ON TRUE
            JOIN map_layers ml ON ml.layer_id = map_layer.layer_id
            WHERE m.id = $1
              AND ml.type = 'raster'
            ORDER BY map_layer.ord
            """,
            map_id,
        )

    layer = select_fast_raster_layer(user_text, rows)
    if not layer:
        return False

    from src.tools.pyd import IngabeToolCallMetaArgs
    from src.tools.raster_query import DescribeUserRasterArgs, describe_user_raster

    async with kue_ephemeral_action(
        conversation.id,
        "Measuring raster area...",
        layer_id=str(layer["layer_id"]),
    ):
        result = await describe_user_raster(
            DescribeUserRasterArgs(layer_id=str(layer["layer_id"])),
            IngabeToolCallMetaArgs(
                user_uuid=user_id,
                conversation_id=conversation.id,
                map_id=map_id,
                project_id=str(project_row["project_id"]),
                session=session,
            ),
        )

    assistant_text = _raster_area_fast_reply(result)
    async with async_conn(
        "sage.fast_raster_fact.persist",
        user_id=user_id,
        partner_id=partner_id,
    ) as conn:
        await conn.execute(
            """
            INSERT INTO chat_completion_messages
            (map_id, sender_id, message_json, conversation_id)
            VALUES ($1, $2, $3, $4)
            """,
            map_id,
            user_id,
            json.dumps({"role": "assistant", "content": assistant_text}),
            conversation.id,
        )

    await kue_stream_token(conversation.id, assistant_text, turn_id=turn_id)
    await kue_stream_token(conversation.id, "", done=True, turn_id=turn_id)
    logger.info(
        "Sage fast raster fact route: conv=%s layer_id=%s elapsed=%.3fs",
        conversation.id,
        layer["layer_id"],
        asyncio.get_running_loop().time() - started,
    )
    return True


async def _run_first_fast_path(
    *,
    map_id: str,
    session: UserContext,
    user_id: str,
    conversation: Conversation,
    openai_messages: list[dict],
) -> str | None:
    """Run the proven single-purpose paths in order; the name of the one
    that answered the turn, or None when the model has to plan it."""
    # Built at call time so tests can patch individual handlers.
    handlers = (
        ("admin_boundary", _maybe_run_fast_admin_boundary_turn),
        ("raster_object", _maybe_run_fast_raster_object_turn),
        ("raster_context", _maybe_run_fast_raster_context_turn),
        ("raster_fact", _maybe_run_fast_raster_fact_turn),
    )
    for name, handler in handlers:
        if await handler(
            map_id=map_id,
            session=session,
            user_id=user_id,
            conversation=conversation,
            openai_messages=openai_messages,
        ):
            return name
    return None


async def _maybe_run_deterministic_turn_before_hermes(
    *,
    map_id: str,
    session: UserContext,
    user_id: str,
    conversation: Conversation,
) -> bool:
    """Run proven single-purpose paths before constructing a general agent.

    These routes are faster and more reliable than asking Hermes to rediscover
    the same plan. Complex or genuinely multi-step requests still fall through
    to Hermes when it is enabled.
    """

    rows = await get_all_conversation_messages(conversation.id, session)
    messages = [
        row.message_json
        for row in rows
        if isinstance(getattr(row, "message_json", None), dict)
    ]
    return await _run_first_fast_path(
        map_id=map_id,
        session=session,
        user_id=user_id,
        conversation=conversation,
        openai_messages=messages,
    ) is not None


async def process_chat_interaction_task(
    request: Request,  # Keep request for get_map_messages
    map_id: str,
    session: UserContext,  # Pass session for auth
    user_id: str,  # Pass user_id directly
    chat_args: ChatArgsProvider,
    map_state: MapStateProvider,
    conversation: Conversation,
    system_prompt_provider: SystemPromptProvider,
    connection_manager: PostgresConnectionManager,
    pydantic_tool_calls: PydanticToolRegistry,
    client_turn_id: str | None = None,
    user_message_id: str | None = None,
):
    # Hermes handles complex requests only after deterministic fast paths have
    # had the first chance to answer. This keeps admin lookups and raster/FastSAM
    # work fast, bounded, and independent of agent planning quality.
    from src.services.hermes_runtime import hermes_is_enabled, run_sage_turn_via_hermes
    if hermes_is_enabled():
        if await _maybe_run_deterministic_turn_before_hermes(
            map_id=map_id,
            session=session,
            user_id=user_id,
            conversation=conversation,
        ):
            return
        logger.info(
            "Routing complex Sage turn through scoped Hermes runtime "
            "(map=%s user=%s conversation=%s)",
            map_id, user_id, conversation.id,
        )
        # No fallback to the legacy planner: by the time Hermes fails it may
        # have streamed text or run tools with side effects, and a second
        # planner would answer (and act) twice. The runtime already shows the
        # error toast and ends the turn.
        return await run_sage_turn_via_hermes(
            request=request, map_id=map_id, session=session, user_id=user_id,
            chat_args=chat_args, map_state=map_state, conversation=conversation,
            system_prompt_provider=system_prompt_provider,
            connection_manager=connection_manager,
            pydantic_tool_calls=pydantic_tool_calls,
        )

    # kick it off with a quick sleep, to detach from the event loop blocking /send
    await asyncio.sleep(0.1)
    partner_id = session.get_org_id()

    _lock_key = f"chat_lock:{conversation.id}"
    # tool_call_id -> (tool name, arguments), for result checks.
    _tool_calls_by_id: dict[str, tuple[str, Any]] = {}

    async def add_chat_completion_message(
        message: Union[ChatCompletionMessage, ChatCompletionMessageParam],
    ):
        message_dict = (
            message.model_dump() if isinstance(message, BaseModel) else message
        )
        if isinstance(message_dict, dict) and message_dict.get("role") == "tool":
            # A known failure gets the facts to fix it before the model sees it.
            _call_name, _call_args = _tool_calls_by_id.get(
                str(message_dict.get("tool_call_id") or ""), ("", {})
            )
            _checked = await apply_result_checks(
                _call_name,
                _call_args,
                message_dict.get("content"),
                open_conn=lambda: async_conn("tool_result_check"),
                project_id=current_project_id,
                user_id=user_id,
                connection_manager=connection_manager,
                admin_boundary_tool=ADMIN_BOUNDARY_TOOL,
            )
            if _checked is not None:
                turn_trace.flag(f"result_checked:{_checked['error_kind']}")
                message_dict = {**message_dict, "content": json.dumps(_checked)}
            # Say what one value covers (a district, a cell, a forecast square).
            _covered = data_coverage.annotate(_call_name, _call_args, message_dict.get("content"))
            if _covered is not None:
                message_dict = {**message_dict, "content": _covered}

        async with async_conn("add_chat_message") as msg_conn:
            await msg_conn.execute(
                """
                INSERT INTO chat_completion_messages
                (map_id, sender_id, message_json, conversation_id)
                VALUES ($1, $2, $3, $4)
                """,
                map_id,
                user_id,
                json.dumps(message_dict),
                conversation.id,
            )
        if isinstance(message_dict, dict) and message_dict.get("role") == "tool":
            turn_trace.tool_finished(
                str(message_dict.get("tool_call_id") or ""), message_dict.get("content")
            )

    with tracer.start_as_current_span("app.process_chat_interaction") as span, sage_turn_trace(
        session_id=str(conversation.id),
        user_id=user_id,
        metadata={
            "map_id": map_id,
            "partner_id": partner_id,
            "client_turn_id": client_turn_id,
            "message_id": user_message_id,
        },
    ) as turn_trace:
        _consecutive_tool_errors = 0
        _MAX_CONSECUTIVE_TOOL_ERRORS = 3
        _recent_tool_signatures: list[str] = []

        # Initialised inside the loop per LLM call, but referenced by the
        # pre-LLM cancellation break too (so the WS clear event below has
        # a turn_id to attach when the very first iteration cancels).
        turn_id: str | None = None

        for i in range(25):
            if i == 24:
                turn_trace.flag("step_limit")
            # Check if the message processing has been cancelled
            try:
                if redis.get(f"messages:{map_id}:cancelled"):
                    redis.delete(f"messages:{map_id}:cancelled")
                    turn_trace.flag("cancelled")
                    # Emit a WS done=True so the frontend clears the loading
                    # state and finalises whatever partial message it has.
                    # Without this, the cancel button "succeeds" on the server
                    # but the UI sits on a zombie spinner until the user
                    # refreshes. See feedback memory on cancel WS-emit.
                    try:
                        await kue_stream_token(conversation.id, "", done=True, turn_id=turn_id)
                    except Exception:
                        logger.debug("kue_stream_token done=True (pre-LLM cancel) failed", exc_info=True)
                    break
            except Exception:
                logger.debug("Redis unavailable for cancellation check")

            # Refresh messages to include any new system messages we just added
            with tracer.start_as_current_span("kue.fetch_messages"):
                updated_messages_response = await get_all_conversation_messages(
                    conversation.id, session
                )

            # Fields added by the OpenAI SDK that non-OpenAI providers reject
            # Strip fields that are null/empty — providers like DeepSeek, Groq,
            # Cerebras reject null tool_calls, annotations, audio, etc.
            _STRIP_NULL_FIELDS = {"annotations", "audio", "refusal", "function_call", "tool_calls"}
            # Always strip these regardless of value (waste tokens in history)
            _ALWAYS_STRIP_FIELDS = {"reasoning", "reasoning_details"}

            openai_messages = []
            for msg in updated_messages_response:
                m = msg.message_json
                if isinstance(m, dict):
                    m = {k: v for k, v in m.items()
                         if k not in _ALWAYS_STRIP_FIELDS and
                         (k not in _STRIP_NULL_FIELDS or (v is not None and v != []))}
                    # Defense-in-depth for rows persisted before write-time
                    # cleaning landed. gemma4:31b sometimes (a) streams
                    # tool_call.arguments with trailing tokens / concatenated
                    # JSON objects, or (b) fuses two tool names into one string
                    # (e.g. "add_layer_to_mapzoom_to_bounds"). Both cause Ollama
                    # to reject the replayed history with HTTP 400 "invalid tool
                    # call arguments" → "Error connecting to LLM". Fix: clean
                    # args + repair name via longest-prefix match.
                    _tcs = m.get("tool_calls")
                    if _tcs:
                        # Full tool name universe: pydantic/tools.json tools + hardcoded
                        # message_routes tools that aren't in get_tools().
                        _HARDCODED_TOOL_NAMES = {
                            "add_layer_to_map", "zoom_to_bounds", "set_layer_style",
                            "new_layer_from_postgis", "create_point_layer",
                        }
                        _all_tool_names: list[str] | None = None
                        for _tc in _tcs:
                            _fn = _tc.get("function") if isinstance(_tc, dict) else None
                            if not _fn:
                                continue
                            _tc_name = _fn.get("name", "")
                            if _tc_name:
                                if _all_tool_names is None:
                                    from src.dependencies.pydantic_tools import get_pydantic_tool_calls
                                    _all_tool_names = list(
                                        _HARDCODED_TOOL_NAMES
                                        | {t["function"]["name"] for t in get_tools()}
                                        | set(get_pydantic_tool_calls().keys())
                                    )
                                if _tc_name not in _all_tool_names:
                                    _match = max(
                                        (n for n in _all_tool_names if _tc_name.startswith(n)),
                                        key=len,
                                        default=None,
                                    )
                                    if _match:
                                        logger.warning(
                                            "Repaired malformed tool_call name %r → %r in history replay",
                                            _tc_name, _match,
                                        )
                                        _fn["name"] = _match
                                    else:
                                        logger.warning(
                                            "Dropping unrepaiable tool_call name %r from history replay",
                                            _tc_name,
                                        )
                                        _fn["name"] = "__dropped__"
                            if isinstance(_fn.get("arguments"), str):
                                try:
                                    _fn["arguments"] = json.dumps(
                                        _clean_tool_args(_fn["arguments"])
                                    )
                                except Exception:
                                    pass
                        # Remove tool calls with dropped/invalid names
                        m["tool_calls"] = [
                            _tc for _tc in _tcs
                            if not (isinstance(_tc, dict)
                                    and isinstance(_tc.get("function"), dict)
                                    and _tc["function"].get("name") == "__dropped__")
                        ] or None  # type: ignore[assignment]
                        if m["tool_calls"] is None:
                            m.pop("tool_calls", None)
                    # OpenAI spec allows content=null for assistant messages with
                    # tool_calls, but Ollama's gemma adapter rejects with HTTP 400
                    # "invalid message content type: <nil>". Coerce to empty string
                    # so the replayed history is accepted across providers.
                    if "content" in m and m["content"] is None:
                        m["content"] = ""
                openai_messages.append(m)
            openai_messages = _pair_tool_results(openai_messages)

            _fast_path = await _run_first_fast_path(
                map_id=map_id,
                session=session,
                user_id=user_id,
                conversation=conversation,
                openai_messages=openai_messages,
            )
            if _fast_path is not None:
                turn_trace.fast_path(_fast_path, user_text=extract_last_user_text(openai_messages))
                return

            with tracer.start_as_current_span("kue.fetch_unattached_layers"):
                async with async_conn("fetch_unattached_layers") as ul_conn:
                    unattached_layers = await ul_conn.fetch(
                        """
                        SELECT ml.layer_id, ml.created_on, ml.last_edited, ml.type, ml.name
                        FROM map_layers ml
                        WHERE ml.owner_uuid = $1
                        AND NOT EXISTS (
                            SELECT 1 FROM user_mundiai_maps m
                            WHERE ml.layer_id = ANY(m.layers) AND m.owner_uuid = $2
                        )
                        ORDER BY ml.created_on DESC
                        LIMIT 10
                        """,
                        user_id,
                        user_id,
                    )

            layer_enum = {}
            for layer in unattached_layers:
                layer_name = (
                    layer.get("name") or f"Unnamed Layer ({layer['layer_id'][:8]})"
                )
                layer_enum[layer["layer_id"]] = (
                    f"{layer_name} (type: {layer.get('type', 'unknown')}, created: {layer['created_on']})"
                )

            client = get_openai_client(request)

            tools_payload = build_sage_tools_payload(pydantic_tool_calls, layer_enum)

            chat_completions_args = await chat_args.get_args(
                user_id, "send_map_message_async"
            )

            # --- Sage routing fast-path ---
            # On every turn we send ~6.7K tokens of system prompt and ~13.4K
            # tokens of tool schemas. For trivial small-talk this is wasted
            # transatlantic prefill on a 31B model. The router classifies the
            # last user message:
            #   - small-talk ("hi", "thanks") -> drop tools, swap in a 1-line
            #     system prompt, route to the local 7B model.
            #   - high-confidence intent (clearly map-edit / agriculture /
            #     user-raster / brain) -> filter the tool list to that domain
            #     plus an always-on display set.
            #   - uncertain -> fall through to current behavior (full list).
            _last_user_text = extract_last_user_text(openai_messages)
            _full_tools_payload = tools_payload
            _turn_plan = plan_sage_turn(
                _last_user_text,
                openai_messages,
                tools_payload,
                system_prompt_provider.get_system_prompt,
            )
            _shortlist_k = tool_shortlist_k()
            if _shortlist_k:
                _turn_plan = await apply_tool_shortlist(
                    _turn_plan,
                    _last_user_text,
                    openai_messages[:-1],
                    _full_tools_payload,
                    k=_shortlist_k,
                )
            _routing = _turn_plan.routing
            _system_prompt_content = _turn_plan.system_prompt
            tools_payload = _turn_plan.tools
            if _turn_plan.model_override:
                chat_completions_args = {
                    **chat_completions_args,
                    "model": _turn_plan.model_override,
                }
            if i == 0:
                turn_trace.routing(
                    user_text=_last_user_text,
                    reason=_routing.reason,
                    categories=list(_routing.selected_categories),
                    small_talk=_routing.is_small_talk,
                    tools=tools_payload,
                    shortlist=_turn_plan.shortlist,
                    model=str(chat_completions_args.get("model") or ""),
                )
            if _routing.is_small_talk:
                logger.info(
                    "sage_routing: small-talk fast-path engaged (model=%s, "
                    "msg_len=%d)",
                    chat_completions_args.get("model"),
                    len(_last_user_text),
                )

            _llm_messages = [
                {
                    "role": "system",
                    "content": _system_prompt_content,
                }
            ] + openai_messages

            # --- Context window overflow protection ---
            # chars/3 underestimates real token counts by 1.5-1.7x for JSON-heavy
            # content (observed ratios: 1.498, 1.616). Factor 2.0 guarantees
            # no overflow for any realistic content composition.
            _MODEL_CONTEXT_LIMIT = int(os.environ.get(
                "LLM_CONTEXT_LIMIT", "131072"
            ))
            _DESIRED_OUTPUT_TOKENS = 4096
            _UNDERESTIMATE_FACTOR = 2.0
            _TOOLS_TOKEN_ESTIMATE = (
                len(json.dumps(tools_payload)) // 3
                if tools_payload else 0
            )

            def _estimate_tokens_for_messages(msgs: list) -> int:
                return sum(len(json.dumps(m)) // 3 for m in msgs)

            # Max estimated input (msgs+tools) that won't overflow when
            # real tokens are up to 2x our estimate:
            #   est_input <= (limit - output) / 2.0
            _max_estimated_input = int(
                (_MODEL_CONTEXT_LIMIT - _DESIRED_OUTPUT_TOKENS) / _UNDERESTIMATE_FACTOR
            )
            _budget = _max_estimated_input - _TOOLS_TOKEN_ESTIMATE
            _system = _llm_messages[:1]
            _history = _llm_messages[1:]
            _kept: list = []
            _kept_tokens = _estimate_tokens_for_messages(_system)
            for msg in reversed(_history):
                msg_tokens = len(json.dumps(msg)) // 3
                if _kept_tokens + msg_tokens > _budget:
                    break
                _kept.insert(0, msg)
                _kept_tokens += msg_tokens
            if len(_kept) < len(_history):
                _llm_messages = _system + _kept if _kept else _system
                logger.info(
                    "Context truncation: %d→%d messages, ~%d est tokens "
                    "(limit %d, budget %d, max_est_input %d)",
                    len(_system) + len(_history),
                    len(_llm_messages),
                    _kept_tokens + _TOOLS_TOKEN_ESTIMATE,
                    _MODEL_CONTEXT_LIMIT,
                    _budget,
                    _max_estimated_input,
                )

            _input_est = (
                _estimate_tokens_for_messages(_llm_messages)
                + _TOOLS_TOKEN_ESTIMATE
            )
            _max_tokens = max(
                512,
                min(
                    _DESIRED_OUTPUT_TOKENS,
                    _MODEL_CONTEXT_LIMIT - int(_input_est * _UNDERESTIMATE_FACTOR),
                ),
            )

            _llm_kwargs = dict(
                **chat_completions_args,
                messages=_llm_messages,
                tools=tools_payload if tools_payload else None,
                tool_choice=_turn_plan.tool_choice,
                max_tokens=_max_tokens,
            )

            turn_id = str(_uuid.uuid4())
            async with kue_ephemeral_action(conversation.id, "Sage is thinking..."):
                with tracer.start_as_current_span(
                    "kue.openai.chat.completions.create"
                ):
                    # Build the model fallback chain. Tries primary first, then
                    # walks through OPENROUTER_FALLBACK_MODELS (comma-separated, in
                    # order). Falls back ONLY on upstream 5xx and ONLY if no tokens
                    # have streamed yet (so we never deliver a half-written response).
                    # OPENROUTER_FALLBACK_MODEL (singular, legacy) still works as a
                    # single-item chain.
                    _primary_model = _llm_kwargs.get("model")
                    _chain_env = (
                        os.environ.get("OPENROUTER_FALLBACK_MODELS", "").strip()
                        or os.environ.get("OPENROUTER_FALLBACK_MODEL", "").strip()
                    )
                    _model_chain: list[str] = [_primary_model] + [
                        m.strip()
                        for m in _chain_env.split(",")
                        if m.strip() and m.strip() != _primary_model
                    ]
                    # Dedupe while preserving order
                    _seen: set[str] = set()
                    _model_chain = [m for m in _model_chain if not (m in _seen or _seen.add(m))]

                    _last_err: Optional[APIError] = None
                    _attempted_models: list[str] = []
                    _rate_limit_retries = 0

                    content_parts: list[str] = []
                    tool_calls_acc: dict[int, dict] = {}
                    # Abdication guard: on the first model call of a turn, hold
                    # streamed prose back until we know the model called no
                    # tool; a guarded retry may replace it with a tool call.
                    _guard_armed = (
                        abdication_guard_enabled()
                        and bool(tools_payload)
                        and bool(openai_messages)
                        and openai_messages[-1].get("role") == "user"
                    )

                    for _model_idx, _model_name in enumerate(_model_chain):
                        # Reset accumulators for each attempt
                        content_parts = []
                        tool_calls_acc = {}
                        _attempted_models.append(_model_name)
                        _attempt_tools = copy.deepcopy(tools_payload) if tools_payload else None
                        if _attempt_tools and not supports_strict_tool_schema(_model_name):
                            for tool in _attempt_tools:
                                tool.get("function", {}).pop("strict", None)
                        _attempt_kwargs = {
                            **_llm_kwargs,
                            "model": _model_name,
                            "tools": _attempt_tools,
                            "tool_choice": "auto" if _attempt_tools else None,
                        }
                        # Provider routing: an `ollama:<tag>` chain entry is
                        # served by the local Ollama container (OpenAI-compat
                        # endpoint). Everything else uses the configured cloud
                        # client (OpenRouter / Vercel / OpenAI / etc).
                        if _model_name.startswith("ollama:"):
                            from openai import AsyncOpenAI
                            _attempt_kwargs["model"] = _model_name.split(":", 1)[1]
                            _attempt_client = AsyncOpenAI(
                                base_url=os.environ.get(
                                    "OLLAMA_BASE_URL", "http://ollama:11434/v1"
                                ),
                                api_key="ollama",
                            )
                        else:
                            _attempt_client = client
                        _generation = turn_trace.generation(
                            model=_model_name,
                            messages=_llm_messages,
                            tools=_attempt_tools,
                            step=i,
                            attempt=_model_idx,
                            parameters={
                                "max_tokens": _max_tokens,
                                "tool_choice": _attempt_kwargs["tool_choice"],
                                "input_tokens_estimate": _input_est,
                            },
                        )
                        try:
                            # Per-attempt scrubber so Nemotron's
                            # `<tool_call>...</tool_call>` text emissions don't
                            # leak into the user-visible chat. See class docstring.
                            _xml_scrub = _ToolCallTextScrubber()
                            stream = await _attempt_client.chat.completions.create(
                                **_attempt_kwargs, stream=True,
                            )
                            async for chunk in stream:
                                if not chunk.choices:
                                    continue
                                _generation.first_token()
                                delta = chunk.choices[0].delta
                                if delta.content:
                                    _safe = _xml_scrub.feed(delta.content)
                                    if _safe:
                                        content_parts.append(_safe)
                                        if not _guard_armed:
                                            await kue_stream_token(conversation.id, _safe, turn_id=turn_id)
                                if delta.tool_calls:
                                    for tc in delta.tool_calls:
                                        idx = tc.index
                                        if idx not in tool_calls_acc:
                                            tool_calls_acc[idx] = {
                                                "id": "", "type": "function",
                                                "function": {"name": "", "arguments": ""},
                                            }
                                        if tc.id:
                                            tool_calls_acc[idx]["id"] = tc.id
                                        if tc.function:
                                            if tc.function.name:
                                                tool_calls_acc[idx]["function"]["name"] += tc.function.name
                                            if tc.function.arguments:
                                                tool_calls_acc[idx]["function"]["arguments"] += tc.function.arguments
                            # End of stream — flush the XML scrubber's lookback
                            # tail. Anything still inside an unclosed `<tool_call>`
                            # is silently dropped (real tool_call already routed
                            # via delta.tool_calls accumulation above).
                            _tail = _xml_scrub.flush()
                            if _tail:
                                content_parts.append(_tail)
                                if not _guard_armed:
                                    await kue_stream_token(conversation.id, _tail, turn_id=turn_id)
                            # Success
                            _generation.end(output={
                                "content": "".join(content_parts) or None,
                                "tool_calls": [tool_calls_acc[k] for k in sorted(tool_calls_acc)],
                            })
                            if _model_name != _primary_model:
                                turn_trace.flag("fallback_model")
                            _last_err = None
                            break
                        except APIError as _api_err:
                            _last_err = _api_err
                            _err_str = str(_api_err)
                            _generation.end(level="ERROR", status=_err_str)
                            _is_upstream_5xx = (
                                "Provider returned error" in _err_str
                                or " 500" in _err_str or " 502" in _err_str or " 503" in _err_str or " 504" in _err_str
                                or (hasattr(_api_err, "status_code") and getattr(_api_err, "status_code", 0) >= 500)
                                or (hasattr(_api_err, "code") and str(getattr(_api_err, "code", "") or "") in ("500", "502", "503", "504"))
                            )
                            # Gemma/Ollama payload rejection (HTTP 400). Common
                            # patterns: "invalid message content type: <nil>",
                            # "invalid tool call arguments", "invalid_request_error"
                            # — all from gemma's stricter-than-OpenAI parser.
                            # Qwen accepts these payloads, so fall over to it
                            # automatically. Excludes context_length_exceeded
                            # (handled by its own UX path at line 1710+) and
                            # auth 4xx (401/403/404 — not recoverable by retry).
                            _err_lower = _err_str.lower()
                            _is_context_overflow_err = (
                                "context_length_exceeded" in _err_lower
                                or "context length" in _err_lower
                                or "maximum context" in _err_lower
                                or "context window" in _err_lower
                            )
                            _is_payload_400 = (
                                not _is_context_overflow_err
                                and (
                                    isinstance(_api_err, BadRequestError)
                                    or " 400" in _err_str
                                    or "Error code: 400" in _err_str
                                    or (hasattr(_api_err, "status_code") and getattr(_api_err, "status_code", 0) == 400)
                                )
                            )
                            # A per-minute rate limit (free models: 20/min) is
                            # waited out and the same model retried, as long as
                            # nothing has streamed; a daily cap is not.
                            _rl_wait = rate_limit_retry_after(_api_err)
                            if (
                                _rl_wait is not None
                                and _rate_limit_retries < RATE_LIMIT_RETRIES
                                and not content_parts
                                and not tool_calls_acc
                            ):
                                _rate_limit_retries += 1
                                turn_trace.flag("rate_limited")
                                logger.warning(
                                    "LLM model %s rate-limited; retrying in %.1fs (%d/%d)",
                                    _model_name, _rl_wait, _rate_limit_retries, RATE_LIMIT_RETRIES,
                                )
                                await asyncio.sleep(_rl_wait)
                                _model_chain.insert(_model_idx + 1, _model_name)
                                continue
                            _has_more_in_chain = _model_idx + 1 < len(_model_chain)
                            # A rate limit that waiting cannot fix (a daily
                            # cap) moves on to the next model in the chain.
                            _is_rate_limited = getattr(_api_err, "status_code", None) == 429
                            _can_retry = (
                                _has_more_in_chain
                                and (_is_upstream_5xx or _is_payload_400 or _is_rate_limited)
                                and len(content_parts) == 0
                                and len(tool_calls_acc) == 0
                            )
                            if _can_retry:
                                _next = _model_chain[_model_idx + 1]
                                logger.warning(
                                    "LLM model %s failed (%s) with no content streamed — falling back to %s (chain step %d/%d)",
                                    _model_name, _err_str[:120], _next,
                                    _model_idx + 2, len(_model_chain),
                                )
                                continue
                            break

                    try:
                        if _last_err is not None:
                            raise _last_err
                        # Recorded whether or not the guard is on, so the
                        # flight recorder shows the abdication rate either way.
                        _abdicated = (
                            bool(openai_messages)
                            and openai_messages[-1].get("role") == "user"
                            and is_abdication(
                                _turn_plan, _last_user_text, "".join(content_parts), bool(tool_calls_acc)
                            )
                        )
                        if _abdicated:
                            turn_trace.flag("abdication")
                        if _guard_armed:
                            if _abdicated:
                                turn_trace.flag("guard_fired")
                                _guard_step = turn_trace.observe(
                                    "abdication_guard",
                                    kind="guardrail",
                                    input={"held_back_reply": "".join(content_parts)},
                                )
                                _guard_calls = await _run_abdication_guard(
                                    _attempt_client, _attempt_kwargs, _last_user_text,
                                    openai_messages[:-1], _full_tools_payload,
                                )
                                _guard_step.end(
                                    output={"tool_calls": [_guard_calls[k] for k in sorted(_guard_calls)]},
                                    level=None if _guard_calls else "WARNING",
                                    status=None if _guard_calls else "retry made no tool call; prose kept",
                                )
                                if _guard_calls:
                                    turn_trace.flag("guard_recovered")
                                    tool_calls_acc = _guard_calls
                                    content_parts = []
                            if content_parts:
                                # Release the held-back prose in one piece.
                                await kue_stream_token(conversation.id, "".join(content_parts), turn_id=turn_id)
                        if content_parts:
                            await kue_stream_token(conversation.id, "", done=True, turn_id=turn_id)
                        full_content = "".join(content_parts) or None
                        # gemma4:31b sometimes streams tool_call.function.name
                        # as two fused tool names (e.g. "add_layer_to_map" +
                        # "zoom_to_bounds" → "add_layer_to_mapzoom_to_bounds") and
                        # tool_call.arguments as concatenated JSON objects. Both
                        # cause HTTP 400 from Ollama on next turn. Fix at write
                        # time so the DB record is always clean.
                        if tool_calls_acc:
                            from src.dependencies.pydantic_tools import get_pydantic_tool_calls
                            _wt_tool_names = list(
                                {"add_layer_to_map", "zoom_to_bounds", "set_layer_style",
                                 "new_layer_from_postgis", "create_point_layer"}
                                | {t["function"]["name"] for t in get_tools()}
                                | set(get_pydantic_tool_calls().keys())
                            )
                            for _wt_idx in tool_calls_acc:
                                _wt_fn = tool_calls_acc[_wt_idx].get("function", {})
                                _wt_name = _wt_fn.get("name", "")
                                if _wt_name and _wt_name not in _wt_tool_names:
                                    _wt_match = max(
                                        (n for n in _wt_tool_names if _wt_name.startswith(n)),
                                        key=len,
                                        default=None,
                                    )
                                    if _wt_match:
                                        logger.warning(
                                            "Write-time: repaired fused tool_call name %r → %r",
                                            _wt_name, _wt_match,
                                        )
                                        _wt_fn["name"] = _wt_match
                                _wt_raw_args = _wt_fn.get("arguments", "")
                                if _wt_raw_args:
                                    _wt_fn["arguments"] = json.dumps(
                                        _clean_tool_args(_wt_raw_args)
                                    )
                        full_tool_calls = (
                            [ChatCompletionMessageToolCall(**tool_calls_acc[i])
                             for i in sorted(tool_calls_acc)]
                            if tool_calls_acc else None
                        )
                        assistant_message = ChatCompletionMessage(
                            role="assistant",
                            content=full_content,
                            tool_calls=full_tool_calls,
                        )
                        if len(_attempted_models) > 1:
                            logger.info(
                                "LLM completion succeeded with fallback model after primary failed. attempted=%s",
                                _attempted_models,
                            )
                    except APIError as e:
                        if content_parts:
                            await kue_stream_token(conversation.id, "", done=True, turn_id=turn_id)
                        logger.error("LLM APIError (code=%s): %s", e.code, e, exc_info=True)
                        turn_trace.flag("llm_error")
                        _is_context_overflow = (
                            e.code == "context_length_exceeded"
                            or "context length" in str(e).lower()
                            or "maximum context" in str(e).lower()
                        )
                        _quota_message = rate_limit_user_message(e)
                        if _quota_message:
                            turn_trace.flag("rate_limited")
                            await kue_notify_error(conversation.id, _quota_message)
                        elif _is_context_overflow:
                            await kue_notify_error(
                                conversation.id,
                                "Maximum context length for LLM has been reached. Please create a new chat to continue using the chat feature.",
                            )
                        else:
                            await kue_notify_error(
                                conversation.id,
                                "Error connecting to LLM. If trying again doesn't work, create a new chat in the top right to reset the chat history.",
                            )
                        span.set_status(
                            trace.Status(trace.StatusCode.ERROR, str(e))
                        )
                        span.set_attribute(
                            "error.traceback", traceback.format_exc()
                        )
                        break
                    except Exception as e:
                        if content_parts:
                            await kue_stream_token(conversation.id, "", done=True, turn_id=turn_id)
                        logger.error("LLM unexpected error: %s", e, exc_info=True)
                        turn_trace.flag("llm_error")
                        await kue_notify_error(
                            conversation.id,
                            "Error connecting to LLM. This is probably a bug with Mundi, please open a new issue on GitHub.",
                        )
                        span.set_status(
                            trace.Status(trace.StatusCode.ERROR, str(e))
                        )
                        span.set_attribute(
                            "error.traceback", traceback.format_exc()
                        )
                        break

            # after chat completions is a pretty common spot to get a cancelled message
            try:
                if redis.get(f"messages:{map_id}:cancelled"):
                    redis.delete(f"messages:{map_id}:cancelled")
                    turn_trace.flag("cancelled")
                    # Same WS done=True signal as the pre-LLM cancel branch,
                    # so the frontend clears its spinner.
                    try:
                        await kue_stream_token(conversation.id, "", done=True, turn_id=turn_id)
                    except Exception:
                        logger.debug("kue_stream_token done=True (post-LLM cancel) failed", exc_info=True)
                    break
            except Exception:
                logger.debug("Redis unavailable for cancellation check")

            # Store the assistant message in the database
            await add_chat_completion_message(assistant_message)

            if not assistant_message.tool_calls:
                turn_trace.set_output(assistant_message.content)
                break

            # Fetch project_id for this map once for all tool calls
            async with async_conn("tool.project_id_for_map") as proj_conn:
                row = await proj_conn.fetchrow(
                    "SELECT project_id FROM user_mundiai_maps WHERE id = $1",
                    map_id,
                )
                assert row is not None
                current_project_id: str = row["project_id"]

            # Process each tool call returned by the assistant
            # Wrap tool processing in its own connection scope
            async with async_conn("tool_execution") as conn:
                for tool_call in assistant_message.tool_calls:
                    tool_call: ChatCompletionMessageToolCall = tool_call
                    function_name = tool_call.function.name
                    tool_args = _clean_tool_args(tool_call.function.arguments or "{}")
                    turn_trace.tool_started(tool_call.id, function_name, tool_args)
                    _tool_calls_by_id[tool_call.id] = (function_name, tool_args)
                    tool_result = {}

                    _recent_tool_signatures.append(
                        life_harness_tool_signature(function_name, tool_args)
                    )
                    tool_result = repeated_life_harness_tool_error(
                        _recent_tool_signatures
                    ) or validate_life_harness_tool_args(
                        function_name,
                        tool_args,
                        tools_payload,
                    ) or {}
                    if tool_result:
                        await add_chat_completion_message(
                            ChatCompletionToolMessageParam(
                                role="tool",
                                tool_call_id=tool_call.id,
                                content=json.dumps(tool_result),
                            ),
                        )
                        continue

                    if function_name in pydantic_tool_calls:
                        fn, ArgModel, MundiModel = pydantic_tool_calls[function_name]
                        try:
                            parsed_args = ArgModel(**(tool_args or {}))

                        except Exception as e:
                            tool_result = {
                                "status": "error",
                                "error": f"Invalid arguments for {function_name}: {e}",
                            }
                            await add_chat_completion_message(
                                ChatCompletionToolMessageParam(
                                    role="tool",
                                    tool_call_id=tool_call.id,
                                    content=json.dumps(tool_result),
                                ),
                            )
                            continue

                        span.add_event(
                            "kue.tool_call_started",
                            {"tool_name": function_name},
                        )
                        with tracer.start_as_current_span(f"kue.{function_name}"):
                            try:
                                mundi_args = MundiModel(
                                    user_uuid=user_id,
                                    conversation_id=conversation.id,
                                    map_id=map_id,
                                    project_id=current_project_id,
                                    session=session,
                                )
                                tool_result = await _within_tool_limit(function_name, fn(parsed_args, mundi_args))

                            except Exception as e:
                                logger.exception("Tool execution failed for %s", tool_call.function.name)
                                tool_result = {
                                    "status": "error",
                                    "error": f"{function_name} failed: {e}",
                                }

                        await add_chat_completion_message(
                            ChatCompletionToolMessageParam(
                                role="tool",
                                tool_call_id=tool_call.id,
                                content=json.dumps(tool_result),
                            ),
                        )
                        continue

                    span.add_event(
                        "kue.tool_call_started",
                        {"tool_name": function_name},
                    )
                    with tracer.start_as_current_span(f"kue.{function_name}") as span:
                        if function_name in LEGACY_HANDLERS:
                            # Tools with no branch above run through the same
                            # handlers /internal/tool-call (Hermes) uses, on this
                            # loop's tool connection (handlers that read the Brain
                            # open their own user- and partner-scoped connection).
                            tool_result = await _within_tool_limit(function_name, execute_legacy_tool(
                                function_name,
                                LegacyToolContext(
                                    user_id=user_id,
                                    partner_id=partner_id or "",
                                    conversation_id=conversation.id,
                                    map_id=map_id,
                                    project_id=current_project_id,
                                    conn=conn,
                                    arguments=tool_args,
                                ),
                            ))
                            await add_chat_completion_message(
                                ChatCompletionToolMessageParam(
                                    role="tool",
                                    tool_call_id=tool_call.id,
                                    content=json.dumps(tool_result, default=str),
                                )
                            )

                        else:
                            # A name the model made up: tell it, instead of ending the turn.
                            tool_result = {
                                "status": "error",
                                "error": f"There is no tool named {function_name!r}.",
                            }
                            await add_chat_completion_message(
                                ChatCompletionToolMessageParam(
                                    role="tool",
                                    tool_call_id=tool_call.id,
                                    content=json.dumps(tool_result),
                                )
                            )

            # Track consecutive rounds where tool calls returned errors.
            # This prevents the LLM from retrying the same failing tool in a
            # loop until it exhausts the provider rate limit.
            if assistant_message.tool_calls:
                if isinstance(tool_result, dict) and tool_result.get("status") == "error":
                    _consecutive_tool_errors += 1
                else:
                    _consecutive_tool_errors = 0

                if _consecutive_tool_errors >= _MAX_CONSECUTIVE_TOOL_ERRORS:
                    logger.warning(
                        "Breaking tool call loop after %d consecutive error rounds "
                        "for conversation %s",
                        _consecutive_tool_errors, conversation.id,
                    )
                    await kue_notify_error(
                        conversation.id,
                        "The tool keeps failing. Please try rephrasing your request "
                        "or start a new chat.",
                    )
                    break

        # Label the conversation if it still has the default "title pending"
        # if conversation.title == "title pending":
        #     await label_conversation_inline(conversation.id)

    # Unlock the conversation when processing is complete
    try:
        redis.delete(_lock_key)
    except Exception:
        logger.debug("Redis unavailable for chat lock cleanup")


async def process_chat_interaction_task_safely(
    request: Request,
    map_id: str,
    session: UserContext,
    user_id: str,
    chat_args: ChatArgsProvider,
    map_state: MapStateProvider,
    conversation: Conversation,
    system_prompt_provider: SystemPromptProvider,
    connection_manager: PostgresConnectionManager,
    pydantic_tool_calls: PydanticToolRegistry,
    client_turn_id: str | None = None,
    user_message_id: str | None = None,
):
    started_at = time.monotonic()
    lock_key = f"chat_lock:{conversation.id}"

    async def clear_streaming_state() -> None:
        try:
            await kue_stream_token(conversation.id, "", done=True, turn_id=None)
        except Exception:
            logger.debug(
                "kue_stream_token done=True (safe wrapper) failed",
                exc_info=True,
            )

    try:
        await process_chat_interaction_task(
            request,
            map_id,
            session,
            user_id,
            chat_args,
            map_state,
            conversation,
            system_prompt_provider,
            connection_manager,
            pydantic_tool_calls,
            client_turn_id=client_turn_id,
            user_message_id=user_message_id,
        )
        capture_for_session(
            "backend_sage_message_completed",
            session,
            {
                "map_id": map_id,
                "conversation_id": conversation.id,
                "client_turn_id": client_turn_id,
                "message_id": user_message_id,
                "duration_ms": elapsed_ms(started_at),
            },
        )
    except asyncio.CancelledError:
        logger.warning(
            "Sage chat processing was cancelled for conversation %s",
            conversation.id,
        )
        capture_for_session(
            "backend_sage_message_failed",
            session,
            {
                "map_id": map_id,
                "conversation_id": conversation.id,
                "client_turn_id": client_turn_id,
                "message_id": user_message_id,
                "duration_ms": elapsed_ms(started_at),
                "error_type": "CancelledError",
            },
        )
        await clear_streaming_state()
        await kue_notify_error(
            conversation.id,
            "Sage stopped before finishing this request. Please try again.",
        )
        raise
    except HTTPException as exc:
        detail = exc.detail if isinstance(exc.detail, str) else "Sage could not process this request."
        logger.warning(
            "Sage chat processing failed for conversation %s: %s",
            conversation.id,
            detail,
        )
        capture_for_session(
            "backend_sage_message_failed",
            session,
            {
                "map_id": map_id,
                "conversation_id": conversation.id,
                "client_turn_id": client_turn_id,
                "message_id": user_message_id,
                "duration_ms": elapsed_ms(started_at),
                "error_type": type(exc).__name__,
                "status_code": exc.status_code,
            },
        )
        await clear_streaming_state()
        await kue_notify_error(conversation.id, detail)
    except Exception as exc:
        logger.exception("Sage chat processing crashed for conversation %s", conversation.id)
        capture_for_session(
            "backend_sage_message_failed",
            session,
            {
                "map_id": map_id,
                "conversation_id": conversation.id,
                "client_turn_id": client_turn_id,
                "message_id": user_message_id,
                "duration_ms": elapsed_ms(started_at),
                "error_type": type(exc).__name__,
            },
        )
        await clear_streaming_state()
        await kue_notify_error(
            conversation.id,
            "Sage hit an internal error while processing this request. Please try again.",
        )
    finally:
        try:
            redis.delete(lock_key)
        except Exception:
            logger.debug("Redis unavailable for chat lock cleanup")


class MessageSendRequest(BaseModel):
    message: ChatCompletionUserMessageParam
    selected_feature: SelectedFeature | None
    viewport_bounds: list[float] | None = None  # [lon_min, lat_min, lon_max, lat_max]
    client_turn_id: str | None = Field(default=None, max_length=80)


class MessageSendResponse(BaseModel):
    conversation_id: int
    sent_message: SanitizedMessage
    message_id: str
    status: str


@router.post(
    "/conversations/{conversation_id}/maps/{map_id}/send",
    response_model=MessageSendResponse,
    operation_id="send_map_message",
)
@expensive_limit
async def send_map_message(
    request: Request,
    map_id: str,
    body: MessageSendRequest,
    background_tasks: BackgroundTasks,
    await_end: bool = False,
    conversation: Conversation = Depends(get_or_create_conversation),
    session: UserContext = Depends(verify_session_required),
    postgis_provider: Callable = Depends(get_postgis_provider),
    layer_describer: LayerDescriber = Depends(get_layer_describer),
    chat_args: ChatArgsProvider = Depends(get_chat_args_provider),
    map_state: MapStateProvider = Depends(get_map_state_provider),
    system_prompt_provider: SystemPromptProvider = Depends(get_system_prompt_provider),
    connection_manager: PostgresConnectionManager = Depends(
        get_postgres_connection_manager
    ),
    pydantic_tool_calls: PydanticToolRegistry = Depends(get_pydantic_tool_calls),
):
    # get_conversation authenticates
    started_at = time.monotonic()
    logger.info("send_map_message called: conversation=%s map=%s", conversation.id, map_id)
    user_id = session.get_user_id()
    partner_id = session.get_org_id()
    capture_for_session(
        "backend_sage_message_received",
        session,
        {
            "map_id": map_id,
            "conversation_id": conversation.id,
            "client_turn_id": body.client_turn_id,
            "await_end": await_end,
            "has_selected_feature": body.selected_feature is not None,
            "has_viewport_bounds": body.viewport_bounds is not None,
        },
    )

    # Check if map is already being processed
    lock_key = f"chat_lock:{conversation.id}"
    try:
        if redis.get(lock_key):
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="Conversation is currently being processed by another request",
            )
        # Lock the conversation for processing
        redis.set(lock_key, "locked", ex=30)  # 30 second expiry
    except HTTPException:
        raise  # Re-raise the 409 conflict
    except Exception:
        logger.warning("Redis unavailable for chat lock, proceeding without lock")

    # Use map state provider to generate system messages
    messages_response = await get_all_conversation_messages(conversation.id, session)
    current_messages = [msg.message_json for msg in messages_response]

    current_map_description = await get_map_description(
        request,
        map_id,
        session,
        postgis_provider=postgis_provider,
        layer_describer=layer_describer,
        connection_manager=connection_manager,
    )
    description_text = current_map_description.body.decode("utf-8")

    # Get system messages from the provider. Thread viewport_bounds so the
    # provider can synthesize a <CurrentAOI> hint anchoring every spatial tool
    # call to the user's actual map focus (selected_feature → viewport → country).
    system_messages = await map_state.get_system_messages(
        current_messages, description_text, body.selected_feature, body.viewport_bounds
    )

    # Inject a compact, query-aware Brain packet: semantic memory, spatial
    # memory without large payloads.
    try:
        from src.dependencies.brain_dep import get_brain_service
        from src.database.pool import get_async_db_connection
        from src.services.brain_context import (
            build_brain_context_packet,
            extract_user_message_text,
        )

        brain_svc = get_brain_service()
        async with get_async_db_connection(user_id=user_id, partner_id=partner_id) as brain_conn:
            map_layer_ids_row = await brain_conn.fetchrow(
                "SELECT layers FROM user_mundiai_maps WHERE id = $1 AND soft_deleted_at IS NULL",
                map_id,
            )
            visible_layer_ids = (
                list(map_layer_ids_row["layers"] or [])
                if map_layer_ids_row
                else []
            )
            brain_text = await build_brain_context_packet(
                brain_conn,
                brain_svc,
                query_text=extract_user_message_text(body.message),
                viewport_bounds=body.viewport_bounds,
                visible_layer_ids=visible_layer_ids,
            )
            if brain_text:
                system_messages.append({"role": "system", "content": brain_text})
    except Exception:
        logger.debug("Brain context injection skipped (tables may not exist yet)")

    async with async_conn("send_map_message.update_messages", user_id=user_id) as conn:
        # Add any generated system messages to the database
        for system_msg in system_messages:
            system_message = ChatCompletionSystemMessageParam(
                role="system",
                content=system_msg["content"],
            )

            await conn.execute(
                """
                INSERT INTO chat_completion_messages
                (map_id, sender_id, message_json, conversation_id)
                VALUES ($1, $2, $3, $4)
                """,
                map_id,
                user_id,
                json.dumps(system_message),
                conversation.id,
            )

        # Add user's message to DB
        user_msg_db = await conn.fetchrow(
            """
            INSERT INTO chat_completion_messages
            (map_id, sender_id, message_json, conversation_id)
            VALUES ($1, $2, $3, $4)
            RETURNING id, conversation_id, map_id, sender_id, message_json, created_at
            """,
            map_id,
            user_id,
            json.dumps(body.message),
            conversation.id,
        )

        user_msg_dict = dict(user_msg_db)
        user_msg_dict["message_json"] = json.loads(user_msg_dict["message_json"])

        user_msg = MundiChatCompletionMessage(**user_msg_dict)
        sanitized_user_msg = convert_mundi_message_to_sanitized(user_msg)
        user_message_id = str(user_msg_db["id"])

    # Start processing either synchronously (await_end=True) or in background
    if await_end:
        await process_chat_interaction_task(
            request,
            map_id,
            session,
            user_id,
            chat_args,
            map_state,
            conversation,
            system_prompt_provider,
            connection_manager,
            pydantic_tool_calls,
            client_turn_id=body.client_turn_id,
            user_message_id=user_message_id,
        )
    else:
        background_tasks.add_task(
            process_chat_interaction_task_safely,
            request,
            map_id,
            session,
            user_id,
            chat_args,
            map_state,
            conversation,
            system_prompt_provider,
            connection_manager,
            pydantic_tool_calls,
            client_turn_id=body.client_turn_id,
            user_message_id=user_message_id,
        )

    capture_for_session(
        "backend_sage_message_queued",
        session,
        {
            "map_id": map_id,
            "conversation_id": conversation.id,
            "client_turn_id": body.client_turn_id,
            "message_id": user_message_id,
            "await_end": await_end,
            "chat_history_count": len(current_messages),
            "system_context_count": len(system_messages),
            "duration_ms": elapsed_ms(started_at),
        },
    )
    return MessageSendResponse(
        conversation_id=conversation.id,
        sent_message=sanitized_user_msg,
        message_id=str(user_msg_db["id"]),
        status="processing_started",
    )


@router.post(
    "/{map_id}/messages/cancel",
    operation_id="cancel_map_message",
    response_class=JSONResponse,
)
async def cancel_map_message(
    request: Request,
    map_id: str,
    session: UserContext = Depends(verify_session_required),
):
    async with async_conn("cancel_map_message") as conn:
        # Authenticate and check map
        map_result = await conn.fetchrow(
            "SELECT owner_uuid FROM user_mundiai_maps WHERE id = $1 AND soft_deleted_at IS NULL",
            map_id,
        )

        if not map_result:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND, detail="Map not found"
            )

        if session.get_user_id() != str(map_result["owner_uuid"]):
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Authentication required",
                headers={"WWW-Authenticate": "Bearer"},
            )

        try:
            redis.set(f"messages:{map_id}:cancelled", "true", ex=300)  # 5 minute expiry
        except Exception:
            logger.debug("Redis unavailable for message cancellation")

        return JSONResponse(content={"status": "cancelled"})
