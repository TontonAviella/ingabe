from fastapi import APIRouter, HTTPException, status, Request, Depends
from fastapi.responses import JSONResponse
from typing import List, Union
from collections import defaultdict
from pydantic import BaseModel
import logging
import os
import json
import re
from fastapi import BackgroundTasks
from opentelemetry import trace
import io
import csv
import asyncio
import traceback
from src.dependencies.dag import get_map
from fastapi import UploadFile
import httpx
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
from openai import APIError

from src.symbology.llm import generate_maplibre_layers_for_layer_id

from src.routes.layer_router import (
    set_layer_style as set_layer_style_route,
    SetStyleRequest,
)
from src.structures import (
    async_conn,
    SanitizedMessage,
    convert_mundi_message_to_sanitized,
)
from src.utils import get_openai_client
from src.routes.postgres_routes import get_map_description
from src.services.map_service import (
    generate_id,
    internal_upload_layer,
    InternalLayerUploadResponse,
)
from src.geoprocessing.dispatch import (
    UnsupportedAlgorithmError,
    InvalidInputFormatError,
    get_tools,
)
from src.dependencies.conversation import get_or_create_conversation
from src.duckdb import execute_duckdb_query
from src.utils import get_async_s3_client, get_bucket_name
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
from src.routes.websocket import kue_ephemeral_action, kue_notify_error
from src.tools.pyd import tool_from as tool_from_pyd
from src.dependencies.pydantic_tools import (
    get_pydantic_tool_calls,
    PydanticToolRegistry,
)

logger = logging.getLogger(__name__)
tracer = trace.get_tracer(__name__)

# Fixed connection ID for the internal Rwanda PostGIS connection
_RWANDA_INTERNAL_CONN_ID = "CRwandaIntDB"


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
        existing = await conn.fetchrow(
            "SELECT id, project_id, soft_deleted_at FROM project_postgres_connections WHERE id = $1",
            _RWANDA_INTERNAL_CONN_ID,
        )
        if existing:
            # Un-delete if soft-deleted, and ensure correct project + user
            needs_update = (
                existing["soft_deleted_at"] is not None
                or existing["project_id"] != project_id
            )
            if needs_update:
                await conn.execute(
                    """
                    UPDATE project_postgres_connections
                    SET project_id = $1, user_id = $2, soft_deleted_at = NULL
                    WHERE id = $3
                    """,
                    project_id, user_id, _RWANDA_INTERNAL_CONN_ID,
                )
                logger.info(
                    "Updated Rwanda PostGIS connection: project=%s soft_deleted=%s",
                    project_id, existing["soft_deleted_at"] is not None,
                )
        else:
            pg_host = os.environ.get("POSTGRES_HOST", "postgresdb")
            pg_port = os.environ.get("POSTGRES_PORT", "5432")
            pg_db = os.environ.get("POSTGRES_DB", "mundidb")
            pg_user = os.environ.get("POSTGRES_USER", "mundiuser")
            pg_pass = os.environ.get("POSTGRES_PASSWORD", "gdalpassword")
            uri = f"postgresql://{pg_user}:{pg_pass}@{pg_host}:{pg_port}/{pg_db}?sslmode=disable"

            await conn.execute(
                """
                INSERT INTO project_postgres_connections
                (id, project_id, user_id, connection_uri, connection_name)
                VALUES ($1, $2, $3, $4, $5)
                ON CONFLICT (id) DO NOTHING
                """,
                _RWANDA_INTERNAL_CONN_ID,
                project_id,
                user_id,
                uri,
                "Rwanda Agriculture (internal)",
            )
            logger.info("Auto-provisioned Rwanda PostGIS connection %s for project %s",
                         _RWANDA_INTERNAL_CONN_ID, project_id)

        # Dynamically count which Rwanda admin tables actually exist
        _RWANDA_TABLES = [
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
            "District (30) → Sector (~416) → Cell (~2,148) → Village (~14,815)\n\n"
            "### Usage with new_layer_from_postgis\n"
            "Queries MUST return columns named `id` and `geom`.\n"
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
            "SRwandaAdmin",
            _RWANDA_INTERNAL_CONN_ID,
            "Rwanda Administrative Boundaries",
            _summary_md,
            _table_count,
        )

        return _RWANDA_INTERNAL_CONN_ID
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
            openai_client = get_openai_client(request)

            response = await openai_client.chat.completions.create(
                model=os.environ.get("OPENAI_MODEL", "gpt-4.1-nano"),
                messages=[
                    {
                        "role": "system",
                        "content": "Generate a short, descriptive title (3-6 words) for this conversation. The title should capture the main topic or request. Only return the title, nothing else.",
                    },
                    {"role": "user", "content": f"Conversation:\n{content_summary}"},
                ],
                max_tokens=20,
                temperature=0.3,
            )

            title = response.choices[0].message.content.strip()
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


class ChatCompletionMessageRow(BaseModel):
    id: int
    map_id: str
    sender_id: str
    message_json: Union[
        ChatCompletionMessageParam,
        ChatCompletionMessage,
        dict,
    ]
    created_at: str


async def get_all_conversation_messages(
    conversation_id: int,
    session: UserContext,
) -> List[MundiChatCompletionMessage]:
    user_id = session.get_user_id()
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
            session.get_user_id(),
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
    async with async_conn("describe_map_tree") as conn:
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
                session.get_user_id(),
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


class RecoverableToolCallError(Exception):
    def __init__(self, message: str, tool_call_id: str):
        self.message = message
        self.tool_call_id = tool_call_id
        super().__init__(message)


def is_layer_id(s: str) -> bool:
    return isinstance(s, str) and s[0] == "L" and len(s) == 12


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
        r'\bCOPY\b', r'\bpg_read_file\b', r'\bpg_write_file\b',
        r'\blo_import\b', r'\blo_export\b',
        r'\bpg_sleep\b',  # Prevent DoS via sleep
        r'\bdblink\b',  # Prevent lateral movement
    ]

    for pattern in dangerous_patterns:
        if re.search(pattern, query, re.IGNORECASE):
            keyword = pattern.replace(r'\b', '').replace(r'\s+', ' ')
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"Dangerous SQL keyword detected in query: {keyword}",
            )

    return query


async def run_geoprocessing_tool(
    tool_call: ChatCompletionToolMessageParam,
    conn,
    user_id: str,
    map_id: str,
    conversation_id: int,
):
    function_name = tool_call.function.name
    tool_args = json.loads(tool_call.function.arguments)

    all_tools = get_tools()
    for tool in all_tools:
        if function_name == tool["function"]["name"]:
            tool_def = tool
            break
    assert tool_def is not None

    algorithm_id = tool_def["function"]["name"].replace("_", ":")

    mapped_args = tool_args.copy()
    mapped_args["map_id"] = map_id
    mapped_args["user_uuid"] = user_id

    # Convert buffer DISTANCE from kilometres to degrees for EPSG:4326 layers.
    # All layers in the system are stored in EPSG:4326, so the QGIS native:buffer
    # algorithm interprets DISTANCE in degrees. 1 degree ≈ 111.32 km at equator;
    # for Rwanda (~-2° latitude) cos(2°) ≈ 0.9994, so using 111.32 is close enough.
    if algorithm_id == "native:buffer" and "DISTANCE" in mapped_args:
        try:
            km_distance = float(mapped_args["DISTANCE"])
            mapped_args["DISTANCE"] = km_distance / 111.32
            logger.info(
                "Buffer distance converted: %.2f km → %.6f degrees",
                km_distance,
                mapped_args["DISTANCE"],
            )
        except (ValueError, TypeError):
            pass  # leave as-is if not numeric

    logger.info(
        "Geoprocessing tool call: %s (algorithm=%s) args=%s",
        function_name, algorithm_id,
        json.dumps({k: v for k, v in tool_args.items() if k != "user_uuid"}, default=str)[:500],
    )

    with tracer.start_as_current_span(f"geoprocessing.{algorithm_id}") as span:
        try:
            async with (
                kue_ephemeral_action(
                    conversation_id, f"QGIS running {algorithm_id}..."
                ),
                async_conn("get_layer_for_geoprocessing") as conn,
            ):
                input_params = {}
                input_urls = {}

                for key, val in mapped_args.items():
                    if key == "OUTPUT":
                        continue
                    elif is_layer_id(val):
                        # Get OGR source for any layer type (S3, remote URL, PostGIS)
                        try:
                            layer_row = await conn.fetchrow(
                                """
                                SELECT *
                                FROM map_layers
                                WHERE layer_id = $1 AND owner_uuid = $2
                                """,
                                val,
                                user_id,
                            )
                            if not layer_row:
                                raise HTTPException(404, f"Layer {val} not found")
                            layer = MapLayer(**dict(layer_row))

                            ogr_source_context = await layer.get_ogr_source(
                                never_return_local_file=True
                            )
                            async with ogr_source_context as ogr_source:
                                input_urls[key] = ogr_source
                        except Exception as e:
                            logger.warning("Layer %s could not be accessed for geoprocessing: %s", val, e)
                            raise RecoverableToolCallError(
                                f"Layer {val} could not be accessed for geoprocessing",
                                tool_call.id,
                            )
                    else:
                        input_params[key] = str(val)

                map_data = await conn.fetchrow(
                    """
                    SELECT project_id FROM user_mundiai_maps
                    WHERE id = $1
                    """,
                    map_id,
                )
                project_id = map_data["project_id"]

                output_layer_mappings = {}

                # Generate presigned PUT URLs for all output parameters
                s3_client = await get_async_s3_client()
                bucket_name = get_bucket_name()
                output_presigned_put_urls = {}

                # Generate output layer ID and S3 key for this output
                output_layer_id = generate_id(prefix="L")
                # Determine file extension based on tool description
                tool_description = tool_def["function"]["description"].lower()
                vector_count = tool_description.count("vector")
                raster_count = tool_description.count("raster")

                if vector_count > raster_count:
                    file_extension = ".fgb"
                    layer_type = "vector"
                else:
                    file_extension = ".tif"
                    layer_type = "raster"

                output_s3_key = (
                    f"uploads/{user_id}/{project_id}/{output_layer_id}{file_extension}"
                )

                # Generate presigned PUT URL for this output
                output_presigned_url = await s3_client.generate_presigned_url(
                    "put_object",
                    Params={
                        "Bucket": bucket_name,
                        "Key": output_s3_key,
                        "ContentType": "application/x-www-form-urlencoded",
                    },
                    ExpiresIn=3600,  # 1 hour
                )

                output_presigned_put_urls["OUTPUT"] = output_presigned_url
                output_layer_mappings["OUTPUT"] = {
                    "layer_id": output_layer_id,
                    "s3_key": output_s3_key,
                    "layer_type": layer_type,
                    "file_extension": file_extension,
                }

                qgis_request = {
                    "algorithm_id": algorithm_id,
                    "qgis_inputs": input_params,
                    "output_presigned_put_urls": output_presigned_put_urls,
                    "input_urls": input_urls,
                }

                # Call QGIS processing service
                _qgis_timeout = float(
                    os.environ.get("QGIS_PROCESSING_TIMEOUT_SEC", "120")
                )
                async with httpx.AsyncClient() as client:
                    response = await client.post(
                        os.environ["QGIS_PROCESSING_URL"] + "/run_qgis_process",
                        json=qgis_request,
                        timeout=_qgis_timeout,
                    )

                if response.status_code != 200:
                    # Parse QGIS error details for logging and LLM feedback
                    qgis_error_detail = ""
                    try:
                        err_body = response.json()
                        detail = err_body.get("detail", err_body)
                        if isinstance(detail, dict):
                            stderr = detail.get("stderr", "")
                            stdout = detail.get("stdout", "")
                            qgis_error_detail = stderr or stdout
                        else:
                            qgis_error_detail = str(detail)
                    except Exception:
                        qgis_error_detail = response.text[:2000]

                    logger.error(
                        "QGIS processing failed for %s (HTTP %s): %s",
                        algorithm_id,
                        response.status_code,
                        qgis_error_detail[:1000],
                    )

                    # Give the LLM a concise, actionable error message
                    return {
                        "status": "error",
                        "error": f"QGIS algorithm {algorithm_id} failed. Details: {qgis_error_detail[:500]}",
                        "algorithm_id": algorithm_id,
                    }

                qgis_result = response.json()

                # Check if all layer outputs were successfully uploaded
                upload_results = qgis_result.get("upload_results", {})

                for param_name in output_layer_mappings.keys():
                    if (
                        param_name not in upload_results
                        or not upload_results[param_name]["uploaded"]
                    ):
                        upload_err = upload_results.get(param_name, {}).get("error", "unknown")
                        logger.error(
                            "QGIS %s output %s not uploaded: %s",
                            algorithm_id, param_name, upload_err,
                        )
                        return {
                            "status": "error",
                            "error": f"QGIS processing completed but output file {param_name} was not uploaded: {upload_err}",
                            "qgis_result": qgis_result,
                        }

                # Create new layers from the uploaded results
                created_layers = []

                for param_name, layer_info in output_layer_mappings.items():
                    # Download the output file from S3
                    downloaded_file = await s3_client.get_object(
                        Bucket=bucket_name, Key=layer_info["s3_key"]
                    )
                    file_content = await downloaded_file["Body"].read()

                    # Create an UploadFile-like object
                    filename = f"{layer_info['layer_id']}{layer_info['file_extension']}"
                    upload_file = UploadFile(
                        filename=filename,
                        file=io.BytesIO(file_content),
                    )

                    upload_result: InternalLayerUploadResponse = (
                        await internal_upload_layer(
                            map_id=map_id,
                            file=upload_file,
                            layer_name=filename,
                            add_layer_to_map=False,
                            user_id=user_id,
                            project_id=project_id,
                        )
                    )

                    created_layers.append(
                        {
                            "param_name": param_name,
                            "layer_id": upload_result.id,
                            "layer_name": filename,
                            "layer_type": layer_info["layer_type"],
                        }
                    )

                # Prepare the response
                logger.info(
                    "Geoprocessing %s completed: %d layers created",
                    algorithm_id, len(created_layers),
                )
                result = {
                    "status": "success",
                    "message": f"{function_name} completed successfully",
                    "algorithm_id": algorithm_id,
                    "qgis_result": qgis_result,
                    "created_layers": created_layers,
                }

                # Add instructions about available layers
                if created_layers:
                    layer_names = [layer["layer_name"] for layer in created_layers]
                    layer_ids = [layer["layer_id"] for layer in created_layers]
                    result["kue_instructions"] = (
                        f"New layers available: {', '.join(layer_names)} "
                        f"(IDs: {', '.join(layer_ids)}), not added to map. "
                        'Use "add_layer_to_map" with the layer_id and descriptive new_name for layers that should be visible to the user. DO NOT include feature count or CRS in name, those are already visible to the user.'
                    )

                return result

        except UnsupportedAlgorithmError as e:
            return {
                "status": "error",
                "error": f"Unsupported algorithm parameter: {str(e)}",
            }
        except InvalidInputFormatError as e:
            return {
                "status": "error",
                "error": f"Invalid input format: {str(e)}",
            }
        except Exception as e:
            logger.exception(
                "Unexpected error running geoprocessing algorithm %s: %s",
                algorithm_id, e,
            )
            span.set_status(trace.Status(trace.StatusCode.ERROR, str(e)))
            span.set_attribute("error.traceback", traceback.format_exc())
            return {
                "status": "error",
                "error": f"Unexpected error running {algorithm_id}: {str(e)[:300]}",
                "algorithm_id": algorithm_id,
            }


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
):
    # kick it off with a quick sleep, to detach from the event loop blocking /send
    await asyncio.sleep(0.1)

    async def add_chat_completion_message(
        message: Union[ChatCompletionMessage, ChatCompletionMessageParam],
    ):
        message_dict = (
            message.model_dump() if isinstance(message, BaseModel) else message
        )

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

    with tracer.start_as_current_span("app.process_chat_interaction") as span:
        _consecutive_tool_errors = 0
        _MAX_CONSECUTIVE_TOOL_ERRORS = 3

        for i in range(25):
            # Check if the message processing has been cancelled
            try:
                if redis.get(f"messages:{map_id}:cancelled"):
                    redis.delete(f"messages:{map_id}:cancelled")
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
                openai_messages.append(m)

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

            tools_payload = [
                {
                    "type": "function",
                    "function": {
                        "name": "new_layer_from_postgis",
                        "strict": True,
                        "description": "Creates a new layer, given a PostGIS connection and query, and adds it to the map so the user can see it. Layer will automatically pull data from PostGIS. Modify style using the set_layer_style tool.",
                        "parameters": {
                            "type": "object",
                            "properties": {
                                "postgis_connection_id": {
                                    "type": "string",
                                    "description": "Unique PostGIS connection ID used as source",
                                },
                                "query": {
                                    "type": "string",
                                    "description": "SQL query to execute against PostGIS database for this layer, should list fetched columns for attributes that might be used for symbology (+ shape geometry). This query MUST alias the geometry column as 'geom' AND have a unique numeric id aliased as 'id'. Include newlines+spaces at ~55 column wrap",
                                },
                                "layer_name": {
                                    "type": "string",
                                    "description": "Sets a human-readable name for this layer. This name will appear in the layer list/legend for the user.",
                                },
                            },
                            "required": [
                                "postgis_connection_id",
                                "query",
                                "layer_name",
                            ],
                            "additionalProperties": False,
                        },
                    },
                },
                {
                    "type": "function",
                    "function": {
                        "name": "add_layer_to_map",
                        "strict": True,
                        "description": "Shows a newly created or existing unattached layer on the user's current map and layer list. Use this after a geoprocessing step that creates a layer, or if the user asks to see an existing layer that isn't currently on their map.",
                        "parameters": {
                            "type": "object",
                            "properties": {
                                "layer_id": {
                                    "type": "string",
                                    "description": "The ID of the layer to add to the map. Choose from available unattached layers.",
                                    "enum": list(layer_enum.keys())
                                    if layer_enum
                                    else ["NO_UNATTACHED_LAYERS"],
                                },
                                "new_name": {
                                    "type": "string",
                                    "description": "Sets a new human-readable name for this layer. This name will appear in the layer list/legend for the user.",
                                },
                            },
                            "required": ["layer_id", "new_name"],
                            "additionalProperties": False,
                        },
                    },
                },
                {
                    "type": "function",
                    "function": {
                        "name": "set_layer_style",
                        "strict": True,
                        "description": "Creates a new style for a layer with MapLibre JSON layers and immediately applies it as the active style",
                        "parameters": {
                            "type": "object",
                            "properties": {
                                "layer_id": {
                                    "type": "string",
                                    "description": "The ID of the layer to create and apply a style for",
                                },
                                "maplibre_json_layers_str": {
                                    "type": "string",
                                    "description": 'JSON string of MapLibre layer objects. Example: [{"id": "LZJ5RmuZr6qN-line", "type": "line", "source": "LZJ5RmuZr6qN", "paint": {"line-color": "#1E90FF"}}]',
                                },
                            },
                            "required": ["layer_id", "maplibre_json_layers_str"],
                            "additionalProperties": False,
                        },
                    },
                },
                {
                    "type": "function",
                    "function": {
                        "name": "query_duckdb_sql",
                        "strict": True,
                        "description": "Execute a SQL query against vector layer data using DuckDB. Use query_postgis_database for layers created from PostGIS connections instead.",
                        "parameters": {
                            "type": "object",
                            "required": ["layer_ids", "sql_query", "head_n_rows"],
                            "properties": {
                                "layer_ids": {
                                    "type": "array",
                                    "description": "Load these vector layer IDs as tables",
                                    "items": {"type": "string"},
                                },
                                "sql_query": {
                                    "type": "string",
                                    "description": "DuckDB-flavored SELECT ... SQL query. Include newlines+spaces at ~55 column wrap for readability e.g. SELECT name_en,county\n    FROM LCH6Na2SBvJr\n    ORDER BY id",
                                },
                                "head_n_rows": {
                                    "type": "number",
                                    "description": "Truncate result to n rows (increase gingerly, MUST specify returned columns), n=20 is good",
                                },
                            },
                            "additionalProperties": False,
                        },
                    },
                },
                {
                    "type": "function",
                    "function": {
                        "name": "query_postgis_database",
                        "strict": True,
                        "description": "Execute SQL queries on connected PostgreSQL/PostGIS databases. Use for data analysis, spatial queries, and exploring database tables. The query MUST include a LIMIT clause with a value less than 1000.",
                        "parameters": {
                            "type": "object",
                            "properties": {
                                "postgis_connection_id": {
                                    "type": "string",
                                    "description": "User's PostGIS connection ID to query against",
                                },
                                "sql_query": {
                                    "type": "string",
                                    "description": "SQL query to execute. Use newlines+spaces at ~55 column wrap. Examples: 'SELECT COUNT(*) FROM table_name', 'SELECT * FROM spatial_table LIMIT 10', 'SELECT column_name FROM information_schema.columns WHERE table_name = \"my_table\"'. Use standard SQL syntax.",
                                },
                            },
                            "required": ["postgis_connection_id", "sql_query"],
                            "additionalProperties": False,
                        },
                    },
                },
                {
                    "type": "function",
                    "function": {
                        "name": "zonal_statistics",
                        "strict": True,
                        "description": "Calculates zonal statistics (mean, sum, min, max, count, stdev) for raster values within polygon boundaries. Uses exact pixel-polygon coverage calculations for accurate results.",
                        "parameters": {
                            "type": "object",
                            "properties": {
                                "raster_layer_id": {
                                    "type": "string",
                                    "description": "The layer ID of the raster dataset to analyze",
                                },
                                "zones_layer_id": {
                                    "type": "string",
                                    "description": "The layer ID of the vector polygon dataset defining the zones",
                                },
                                "stats": {
                                    "type": "array",
                                    "description": "List of statistics to compute. Defaults to: mean, sum, min, max, count, stdev, variance. Other options: median, mode, majority, minority, variety, coefficient_of_variation, weighted_mean, weighted_sum.",
                                    "items": {"type": "string"},
                                },
                            },
                            "required": ["raster_layer_id", "zones_layer_id"],
                            "additionalProperties": False,
                        },
                    },
                },
                {
                    "type": "function",
                    "function": {
                        "name": "reverse_geocode_coordinates",
                        "strict": True,
                        "description": "Given latitude and longitude, returns the Rwanda administrative divisions (province, district, sector, cell, village) that contain that point. Use this whenever the user provides coordinates and asks what location they correspond to.",
                        "parameters": {
                            "type": "object",
                            "properties": {
                                "lat": {
                                    "type": "number",
                                    "description": "Latitude (e.g. -1.9403)",
                                },
                                "lon": {
                                    "type": "number",
                                    "description": "Longitude (e.g. 29.8739)",
                                },
                            },
                            "required": ["lat", "lon"],
                            "additionalProperties": False,
                        },
                    },
                },
            ]

            # add pydantic-defined tools to the payload
            for name, (fn, arg_model, _mundi_model) in pydantic_tool_calls.items():
                tools_payload.append(tool_from_pyd(fn, arg_model))

            all_tools = get_tools()
            tools_payload.extend(all_tools)
            geoprocessing_function_names = [
                tool["function"]["name"] for tool in all_tools
            ]

            if not layer_enum:
                add_layer_tool = next(
                    tool
                    for tool in tools_payload
                    if tool["function"]["name"] == "add_layer_to_map"
                )
                add_layer_tool["function"]["parameters"]["properties"][
                    "layer_id"
                ].pop("enum", None)

            # Strip "strict" from tool defs when using non-OpenAI providers
            _model = os.environ.get("OPENAI_MODEL", "gpt-4.1-nano")
            if not _model.startswith("gpt-") and not _model.startswith("o1") and not _model.startswith("o3"):
                for tool in tools_payload:
                    tool.get("function", {}).pop("strict", None)

            # Replace the thinking ephemeral updates with context manager
            async with kue_ephemeral_action(conversation.id, "Sage is thinking..."):
                chat_completions_args = await chat_args.get_args(
                    user_id, "send_map_message_async"
                )
                with tracer.start_as_current_span(
                    "kue.openai.chat.completions.create"
                ):
                    # chat.completions.create fails for bad messages and tools, so
                    # if we have orphaned tool calls then we'll get an error - but not
                    # handling it properly makes for a horrible user experience
                    try:
                        response = await client.chat.completions.create(
                            **chat_completions_args,
                            messages=[
                                {
                                    "role": "system",
                                    "content": system_prompt_provider.get_system_prompt(),
                                }
                            ]
                            + openai_messages,
                            tools=tools_payload if tools_payload else None,
                            tool_choice="auto" if tools_payload else None,
                        )
                    except APIError as e:
                        logger.error("LLM APIError (code=%s): %s", e.code, e, exc_info=True)
                        if e.code == "context_length_exceeded":
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
                        logger.error("LLM unexpected error: %s", e, exc_info=True)
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
            assistant_message: ChatCompletionMessageParam = response.choices[
                0
            ].message

            # after chat completions is a pretty common spot to get a cancelled message
            try:
                if redis.get(f"messages:{map_id}:cancelled"):
                    redis.delete(f"messages:{map_id}:cancelled")
                    break
            except Exception:
                logger.debug("Redis unavailable for cancellation check")

            # Store the assistant message in the database
            await add_chat_completion_message(assistant_message)

            if not assistant_message.tool_calls:
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
                    tool_args = json.loads(tool_call.function.arguments)
                    tool_result = {}

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

                        try:
                            mundi_args = MundiModel(
                                user_uuid=user_id,
                                conversation_id=conversation.id,
                                map_id=map_id,
                                project_id=current_project_id,
                                session=session,
                            )
                            # Execute tool (all tools are async)
                            tool_result = await fn(parsed_args, mundi_args)

                        except Exception:
                            logger.exception("Tool execution failed for %s", tool_call.function.name)
                            tool_result = {
                                "status": "error",
                                "error": "Tool execution failed. Please try again or adjust the inputs.",
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
                        if function_name == "new_layer_from_postgis":
                            postgis_connection_id = tool_args.get(
                                "postgis_connection_id"
                            )
                            query = tool_args.get("query")
                            # Validate query for SQL injection before any f-string usage
                            query = validate_sql_query(query)
                            layer_name = tool_args.get("layer_name")

                            if not postgis_connection_id or not query:
                                tool_result = {
                                    "status": "error",
                                    "error": "Missing required parameters (postgis_connection_id or query).",
                                }
                            else:
                                # Verify the PostGIS connection exists and user has access.
                                # Fall back to project-level access for internal connections
                                # (e.g. CRwandaIntDB) which may have a different user_id
                                # when multiple users share a project.
                                connection_result = await conn.fetchrow(
                                    """
                                    SELECT connection_uri FROM project_postgres_connections
                                    WHERE id = $1 AND (user_id = $2 OR project_id = $3)
                                    AND soft_deleted_at IS NULL
                                    """,
                                    postgis_connection_id,
                                    user_id,
                                    current_project_id,
                                )

                                if not connection_result:
                                    tool_result = {
                                        "status": "error",
                                        "error": f"PostGIS connection '{postgis_connection_id}' not found or you do not have access to it.",
                                    }
                                else:
                                    async with kue_ephemeral_action(
                                        conversation.id,
                                        "Adding layer from PostGIS...",
                                        update_style_json=True,
                                    ):
                                        try:
                                            # Use connection manager for PostGIS operations
                                            pg = await connection_manager.connect_to_postgres(
                                                postgis_connection_id
                                            )
                                            try:
                                                # 1. Make sure the SQL parsers and planners are happy
                                                explain_result = await pg.fetch(
                                                    f"EXPLAIN (FORMAT JSON) {query}"
                                                )

                                                # Parse the JSON string from QUERY PLAN
                                                query_plan = json.loads(
                                                    explain_result[0]["QUERY PLAN"]
                                                )
                                                check_postgis_readonly(
                                                    query_plan[0]["Plan"]
                                                )

                                                # Get column names using prepared statement
                                                prepared = await pg.prepare(
                                                    f"SELECT * FROM ({query}) AS sub LIMIT 1"
                                                )
                                                column_info = prepared.get_attributes()
                                                column_names = [
                                                    attr.name for attr in column_info
                                                ]

                                                # Make sure it returns a geometry column called geom and id
                                                if "geom" not in column_names:
                                                    raise ValueError(
                                                        "Query must return a column named 'geom'"
                                                    )
                                                if "id" not in column_names:
                                                    raise ValueError(
                                                        "Query must return a column named 'id'"
                                                    )

                                                attribute_names = [
                                                    name
                                                    for name in column_names
                                                    if name not in ["geom", "id"]
                                                ]

                                                # Check for GIST spatial index on source table(s)
                                                try:
                                                    import re as _re
                                                    _table_matches = _re.findall(
                                                        r'\bFROM\s+"?(\w+)"?(?:\."?(\w+)"?)?',
                                                        query, _re.IGNORECASE,
                                                    )
                                                    _gist_warnings = []
                                                    for _m in _table_matches:
                                                        _tbl = _m[1] if _m[1] else _m[0]
                                                        _sch = _m[0] if _m[1] else "public"
                                                        _idx_count = await pg.fetchval(
                                                            "SELECT COUNT(*) FROM pg_indexes "
                                                            "WHERE schemaname = $1 AND tablename = $2 "
                                                            "AND indexdef ILIKE '%gist%'",
                                                            _sch, _tbl,
                                                        )
                                                        if _idx_count == 0:
                                                            _gist_warnings.append(
                                                                f"No GIST spatial index on {_sch}.{_tbl}. "
                                                                f"Tile rendering may be slow. Consider: "
                                                                f"CREATE INDEX ON {_sch}.{_tbl} USING GIST (geom);"
                                                            )
                                                    if _gist_warnings:
                                                        logger.warning(
                                                            "PostGIS layer missing GIST index: %s",
                                                            "; ".join(_gist_warnings),
                                                        )
                                                except Exception:
                                                    pass  # Non-critical: index check failure should not block layer creation

                                                # Calculate feature count, bounds, and geometry type for the PostGIS layer
                                                feature_count = None
                                                bounds = None
                                                geometry_type = None
                                                metadata_dict = {}

                                                # Calculate feature count
                                                count_result = await pg.fetchval(
                                                    f"SELECT COUNT(*) FROM ({query}) AS sub"
                                                )
                                                feature_count = (
                                                    int(count_result)
                                                    if count_result is not None
                                                    else None
                                                )

                                                # Detect geometry type for styling
                                                geometry_type_result = (
                                                    await pg.fetchrow(
                                                        f"""
                                                        SELECT ST_GeometryType(geom) as geom_type, COUNT(*) as count
                                                        FROM ({query}) AS sub
                                                        WHERE geom IS NOT NULL
                                                        GROUP BY ST_GeometryType(geom)
                                                        ORDER BY count DESC
                                                        LIMIT 1
                                                        """
                                                    )
                                                )

                                                if (
                                                    geometry_type_result
                                                    and geometry_type_result[
                                                        "geom_type"
                                                    ]
                                                ):
                                                    # Convert PostGIS geometry type to standard format
                                                    geometry_type = (
                                                        geometry_type_result[
                                                            "geom_type"
                                                        ]
                                                        .replace("ST_", "")
                                                        .lower()
                                                    )

                                                    # Calculate bounds with proper SRID handling
                                                    # ST_Extent returns BOX2D with SRID 0, so we need to set the SRID before transforming
                                                    # Treat SRID 0 (unset) as 4326 since most geospatial data without explicit SRID is WGS84
                                                    bounds_result = await pg.fetchrow(
                                                        f"""
                                                        WITH extent_data AS (
                                                            SELECT
                                                                ST_Extent(geom) as extent_geom,
                                                                COALESCE(NULLIF((SELECT ST_SRID(geom) FROM ({query}) AS sub2 WHERE geom IS NOT NULL LIMIT 1), 0), 4326) as original_srid
                                                            FROM ({query}) AS sub
                                                            WHERE geom IS NOT NULL
                                                        )
                                                        SELECT
                                                            CASE
                                                                WHEN original_srid = 4326 THEN
                                                                    ST_XMin(extent_geom)
                                                                ELSE
                                                                    ST_XMin(ST_Transform(ST_SetSRID(extent_geom, original_srid), 4326))
                                                            END as xmin,
                                                            CASE
                                                                WHEN original_srid = 4326 THEN
                                                                    ST_YMin(extent_geom)
                                                                ELSE
                                                                    ST_YMin(ST_Transform(ST_SetSRID(extent_geom, original_srid), 4326))
                                                            END as ymin,
                                                            CASE
                                                                WHEN original_srid = 4326 THEN
                                                                    ST_XMax(extent_geom)
                                                                ELSE
                                                                    ST_XMax(ST_Transform(ST_SetSRID(extent_geom, original_srid), 4326))
                                                            END as xmax,
                                                            CASE
                                                                WHEN original_srid = 4326 THEN
                                                                    ST_YMax(extent_geom)
                                                                ELSE
                                                                    ST_YMax(ST_Transform(ST_SetSRID(extent_geom, original_srid), 4326))
                                                             END as ymax,
                                                             original_srid
                                                         FROM extent_data
                                                        WHERE extent_geom IS NOT NULL
                                                        """
                                                    )

                                                    if bounds_result and all(
                                                        bounds_result[k] is not None
                                                        for k in ("xmin", "ymin", "xmax", "ymax")
                                                    ):
                                                        bounds = [
                                                            float(
                                                                bounds_result["xmin"]
                                                            ),
                                                            float(
                                                                bounds_result["ymin"]
                                                            ),
                                                            float(
                                                                bounds_result["xmax"]
                                                            ),
                                                            float(
                                                                bounds_result["ymax"]
                                                            ),
                                                        ]
                                                        # Capture original SRID into metadata if available
                                                        if (
                                                            "original_srid"
                                                            in bounds_result
                                                            and bounds_result[
                                                                "original_srid"
                                                            ]
                                                            is not None
                                                        ):
                                                            try:
                                                                metadata_dict[
                                                                    "original_srid"
                                                                ] = int(
                                                                    bounds_result[
                                                                        "original_srid"
                                                                    ]
                                                                )
                                                            except (
                                                                ValueError,
                                                                TypeError,
                                                            ):
                                                                pass
                                                else:
                                                    logger.warning("No geometry column found in PostGIS query")

                                                # Check spatial indexes on tables referenced in the query
                                                try:
                                                    # Extract table names from the EXPLAIN plan
                                                    def extract_tables(plan_node):
                                                        tables = set()
                                                        if "Relation Name" in plan_node:
                                                            tables.add(plan_node["Relation Name"])
                                                        if "Plans" in plan_node:
                                                            for subplan in plan_node["Plans"]:
                                                                tables.update(extract_tables(subplan))
                                                        return tables

                                                    referenced_tables = extract_tables(query_plan[0]["Plan"])

                                                    if referenced_tables:
                                                        # Check for GIST indexes on geometry columns in referenced tables
                                                        index_result = await pg.fetchval(
                                                            """
                                                            SELECT COUNT(*) FROM pg_indexes
                                                            WHERE tablename = ANY($1::text[])
                                                            AND indexdef ILIKE '%gist%geom%'
                                                            """,
                                                            list(referenced_tables),
                                                        )
                                                        if index_result and index_result > 0:
                                                            metadata_dict["has_spatial_index"] = True
                                                            logger.info("GIST spatial index detected on geometry column for tables: %s", referenced_tables)
                                                        else:
                                                            metadata_dict["spatial_index_warning"] = (
                                                                f"No GIST index on geometry column for tables: {', '.join(referenced_tables)}. "
                                                                "Tile performance may be degraded. Consider: CREATE INDEX ON <table> USING GIST (geom);"
                                                            )
                                                            logger.warning(
                                                                "No GIST spatial index found on geometry column for tables: %s",
                                                                referenced_tables
                                                            )
                                                except Exception as e:
                                                    logger.warning("Spatial index check failed: %s", e)
                                            finally:
                                                await pg.close()

                                            # Generate a new layer ID
                                            layer_id = generate_id(prefix="L")

                                            # Generate default style if geometry type was detected
                                            maplibre_layers = None
                                            if geometry_type:
                                                try:
                                                    maplibre_layers = generate_maplibre_layers_for_layer_id(
                                                        layer_id, geometry_type
                                                    )
                                                    # PostGIS layers use MVT tiles, so source-layer matches MVT_LAYER_NAME
                                                    # This matches the expectation in the style generation function
                                                    logger.info(
                                                        "Generated default style for PostGIS layer %s with geometry type %s",
                                                        layer_id, geometry_type,
                                                    )
                                                except Exception as e:
                                                    logger.warning(
                                                        "Failed to generate default style for PostGIS layer: %s", e,
                                                    )
                                                    maplibre_layers = None

                                            # Create the layer in the database
                                            await conn.execute(
                                                """
                                                INSERT INTO map_layers
                                                (layer_id, owner_uuid, name, type, postgis_connection_id, postgis_query, metadata, feature_count, bounds, geometry_type, source_map_id, created_on, last_edited, postgis_attribute_column_list)
                                                VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP, $12)
                                                """,
                                                layer_id,
                                                user_id,
                                                layer_name,
                                                "postgis",
                                                postgis_connection_id,
                                                query,
                                                json.dumps(metadata_dict),
                                                feature_count,
                                                bounds,
                                                geometry_type,
                                                map_id,
                                                attribute_names,
                                            )

                                            # Create default style in separate table if we have geometry type
                                            if maplibre_layers:
                                                style_id = generate_id(prefix="S")
                                                await conn.execute(
                                                    """
                                                    INSERT INTO layer_styles
                                                    (style_id, layer_id, style_json, created_by, created_on)
                                                    VALUES ($1, $2, $3, $4, CURRENT_TIMESTAMP)
                                                    """,
                                                    style_id,
                                                    layer_id,
                                                    json.dumps(maplibre_layers),
                                                    user_id,
                                                )

                                                await conn.execute(
                                                    """
                                                    INSERT INTO map_layer_styles
                                                    (map_id, layer_id, style_id)
                                                    VALUES ($1, $2, $3)
                                                    """,
                                                    map_id,
                                                    layer_id,
                                                    style_id,
                                                )

                                            # layers may be NULL, not necessarily initialized to []
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

                                            tool_result = {
                                                "status": "success",
                                                "message": f"PostGIS layer created successfully with ID: {layer_id} and added to map",
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

                                            # Kick off background PMTiles generation
                                            if feature_count and feature_count > 0:
                                                asyncio.create_task(
                                                    _generate_postgis_pmtiles_background(
                                                        layer_id, postgis_connection_id, query,
                                                        feature_count, user_id, current_project_id,
                                                        conversation_id=conversation.id,
                                                    )
                                                )
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

                                    # Auto-zoom to the new PostGIS layer
                                    _postgis_bounds = tool_result.get("bounds")
                                    if _postgis_bounds and len(_postgis_bounds) == 4:
                                        async with kue_ephemeral_action(
                                            conversation.id,
                                            f"Zooming to {layer_name or 'layer'}...",
                                            bounds=_postgis_bounds,
                                        ):
                                            await asyncio.sleep(0.3)

                            await add_chat_completion_message(
                                ChatCompletionToolMessageParam(
                                    role="tool",
                                    tool_call_id=tool_call.id,
                                    content=json.dumps(tool_result),
                                )
                            )
                        elif function_name == "add_layer_to_map":
                            layer_id_to_add = tool_args.get("layer_id")
                            new_name = tool_args.get("new_name")

                            async with kue_ephemeral_action(
                                conversation.id,
                                "Adding layer to map...",
                                update_style_json=True,
                            ):
                                layer_exists = await conn.fetchrow(
                                    """
                                    SELECT layer_id, bounds FROM map_layers
                                    WHERE layer_id = $1 AND owner_uuid = $2
                                    """,
                                    layer_id_to_add,
                                    user_id,
                                )

                                if not layer_exists:
                                    tool_result = {
                                        "status": "error",
                                        "error": f"Layer ID '{layer_id_to_add}' not found or you do not have permission to use it.",
                                    }
                                else:
                                    await conn.execute(
                                        """
                                        UPDATE map_layers SET name = $1 WHERE layer_id = $2
                                        """,
                                        new_name,
                                        layer_id_to_add,
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
                                        layer_id_to_add,
                                        map_id,
                                    )
                                    _layer_bounds = layer_exists["bounds"] if layer_exists else None

                                    tool_result = {
                                        "status": f"Layer '{new_name}' (ID: {layer_id_to_add}) added to map '{map_id}'.",
                                        "layer_id": layer_id_to_add,
                                        "name": new_name,
                                    }
                                    if _layer_bounds and len(_layer_bounds) == 4:
                                        tool_result["bounds"] = list(_layer_bounds)
                                        tool_result["kue_instructions"] = (
                                            f"Layer added. Call zoom_to_bounds with bounds {list(_layer_bounds)} "
                                            "so the user can see it."
                                        )

                                # Auto-zoom to the newly added layer
                                if _layer_bounds and len(_layer_bounds) == 4:
                                    async with kue_ephemeral_action(
                                        conversation.id,
                                        f"Zooming to {new_name}...",
                                        bounds=list(_layer_bounds),
                                    ):
                                        await asyncio.sleep(0.3)

                                await add_chat_completion_message(
                                    ChatCompletionToolMessageParam(
                                        role="tool",
                                        tool_call_id=tool_call.id,
                                        content=json.dumps(tool_result),
                                    )
                                )
                        elif function_name == "query_duckdb_sql":
                            layer_id = tool_args.get("layer_ids", [None])[
                                0
                            ]  # Use first layer or None
                            sql_query = tool_args.get("sql_query")
                            head_n_rows = tool_args.get("head_n_rows", 20)

                            layer_exists = await conn.fetchrow(
                                """
                                SELECT layer_id FROM map_layers
                                WHERE layer_id = $1 AND owner_uuid = $2
                                """,
                                layer_id,
                                user_id,
                            )

                            if not layer_exists:
                                tool_result = {
                                    "status": "error",
                                    "error": f"Layer ID '{layer_id}' not found or you do not have permission to access it.",
                                }
                                await add_chat_completion_message(
                                    ChatCompletionToolMessageParam(
                                        role="tool",
                                        tool_call_id=tool_call.id,
                                        content=json.dumps(tool_result),
                                    )
                                )
                                continue

                            try:
                                # Execute the query using the async function
                                async with kue_ephemeral_action(
                                    conversation.id,
                                    "Querying with SQL...",
                                    layer_id=layer_id,
                                ):
                                    result = await execute_duckdb_query(
                                        sql_query=sql_query,
                                        layer_id=layer_id,
                                        max_n_rows=head_n_rows,
                                        timeout=30,
                                    )

                                # Convert result to CSV format
                                # write header + rows to an in-memory buffer
                                buf = io.StringIO()
                                writer = csv.writer(buf)
                                writer.writerow(result["headers"])
                                writer.writerows(result["result"])

                                result_text = buf.getvalue()

                                if len(result_text) > 25000:
                                    tool_result = {
                                        "status": "error",
                                        "error": f"DuckDB CSV result too large: {len(result_text)} characters exceeds 25,000 character limit, try reducing columns or head_n_rows",
                                    }
                                else:
                                    tool_result = {
                                        "status": "success",
                                        "result": result_text,
                                        "row_count": result["row_count"],
                                        "query": sql_query,
                                    }
                            except HTTPException as e:
                                tool_result = {
                                    "status": "error",
                                    "error": f"DuckDB query error: {e.detail}",
                                }
                            except Exception as e:
                                tool_result = {
                                    "status": "error",
                                    "error": f"Error executing SQL query: {str(e)}",
                                }

                            await add_chat_completion_message(
                                ChatCompletionToolMessageParam(
                                    role="tool",
                                    tool_call_id=tool_call.id,
                                    content=json.dumps(tool_result),
                                )
                            )
                        elif function_name == "set_layer_style":
                            layer_id = tool_args.get("layer_id")
                            maplibre_json_layers_str = tool_args.get(
                                "maplibre_json_layers_str"
                            )

                            if not layer_id or not maplibre_json_layers_str:
                                tool_result = {
                                    "status": "error",
                                    "error": "Missing required parameters (layer_id or maplibre_json_layers_str).",
                                }
                            else:
                                try:
                                    layers = json.loads(maplibre_json_layers_str)

                                    layer_row = await conn.fetchrow(
                                        """
                                        SELECT *
                                        FROM map_layers
                                        WHERE layer_id = $1 AND owner_uuid = $2
                                        """,
                                        layer_id,
                                        user_id,
                                    )
                                    if not layer_row:
                                        raise HTTPException(
                                            404, f"Layer {layer_id} not found"
                                        )
                                    layer = MapLayer(**dict(layer_row))

                                    async with kue_ephemeral_action(
                                        conversation.id,
                                        f"Styling layer {layer.name}...",
                                        update_style_json=True,
                                    ):
                                        style_response = await set_layer_style_route(
                                            request=SetStyleRequest(
                                                maplibre_json_layers=layers,
                                                map_id=map_id,
                                            ),
                                            layer=layer,
                                            user_id=user_id,
                                        )

                                    tool_result = {
                                        "status": "success",
                                        "style_id": style_response.style_id,
                                        "layer_id": style_response.layer_id,
                                        "message": f"Style {style_response.style_id} created and applied to layer {layer_id}",
                                    }

                                except json.JSONDecodeError as e:
                                    tool_result = {
                                        "status": "error",
                                        "error": f"Invalid JSON format: {str(e)}",
                                        "layer_id": layer_id,
                                    }
                                except Exception as e:
                                    tool_result = {
                                        "status": "error",
                                        "error": f"Failed to create and apply style: {str(e)}",
                                        "layer_id": layer_id,
                                    }

                            await add_chat_completion_message(
                                ChatCompletionToolMessageParam(
                                    role="tool",
                                    tool_call_id=tool_call.id,
                                    content=json.dumps(tool_result),
                                ),
                            )
                        elif function_name == "query_postgis_database":
                            postgis_connection_id = tool_args.get(
                                "postgis_connection_id"
                            )
                            sql_query = tool_args.get("sql_query")

                            # Validate query for SQL injection before any execution
                            if sql_query:
                                sql_query = validate_sql_query(sql_query)

                            if not postgis_connection_id or not sql_query:
                                tool_result = {
                                    "status": "error",
                                    "error": "Missing required parameters (postgis_connection_id or sql_query)",
                                }
                            else:
                                # Verify the PostGIS connection exists and user has access.
                                # Fall back to project-level access for internal connections.
                                connection_result = await conn.fetchrow(
                                    """
                                    SELECT connection_uri FROM project_postgres_connections
                                    WHERE id = $1 AND (user_id = $2 OR project_id = $3)
                                    AND soft_deleted_at IS NULL
                                    """,
                                    postgis_connection_id,
                                    user_id,
                                    current_project_id,
                                )

                                if not connection_result:
                                    tool_result = {
                                        "status": "error",
                                        "error": f"PostGIS connection '{postgis_connection_id}' not found or you do not have access to it.",
                                    }
                                else:
                                    try:
                                        # Check if LIMIT is already present and validate it
                                        limited_query = sql_query.strip()
                                        limit_match = re.search(
                                            r"\bLIMIT\s+(\d+)\b",
                                            limited_query,
                                            re.IGNORECASE,
                                        )

                                        if limit_match:
                                            limit_value = int(limit_match.group(1))
                                            if limit_value > 1000:
                                                tool_result = {
                                                    "status": "error",
                                                    "error": f"LIMIT value {limit_value} exceeds maximum allowed limit of 1000",
                                                }
                                                await add_chat_completion_message(
                                                    ChatCompletionToolMessageParam(
                                                        role="tool",
                                                        tool_call_id=tool_call.id,
                                                        content=json.dumps(tool_result),
                                                    ),
                                                )
                                                continue
                                        else:
                                            # No LIMIT found, require explicit LIMIT
                                            tool_result = {
                                                "status": "error",
                                                "error": "Query must include a LIMIT clause with a value less than 1000",
                                            }
                                            await add_chat_completion_message(
                                                ChatCompletionToolMessageParam(
                                                    role="tool",
                                                    tool_call_id=tool_call.id,
                                                    content=json.dumps(tool_result),
                                                ),
                                            )
                                            continue

                                        async with kue_ephemeral_action(
                                            conversation.id,
                                            "Querying PostgreSQL database...",
                                        ):
                                            postgres_conn = await connection_manager.connect_to_postgres(
                                                postgis_connection_id
                                            )
                                            try:
                                                # Execute the query
                                                rows = await postgres_conn.fetch(
                                                    limited_query
                                                )

                                                if not rows:
                                                    tool_result = {
                                                        "status": "success",
                                                        "message": "Query executed successfully but returned no rows",
                                                        "row_count": 0,
                                                        "query": limited_query,
                                                    }
                                                else:
                                                    # Convert rows to list of dicts
                                                    result_data = [
                                                        dict(row) for row in rows
                                                    ]

                                                    # Format the result as a readable string
                                                    if (
                                                        len(result_data) == 1
                                                        and len(result_data[0]) == 1
                                                    ):
                                                        # Single value result
                                                        single_value = list(
                                                            result_data[0].values()
                                                        )[0]
                                                        result_text = f"Query result: {single_value}"
                                                    else:
                                                        # Table format
                                                        if result_data:
                                                            headers = list(
                                                                result_data[0].keys()
                                                            )
                                                            result_lines = [
                                                                "\t".join(headers)
                                                            ]
                                                            for row in result_data:
                                                                result_lines.append(
                                                                    "\t".join(
                                                                        str(
                                                                            row.get(
                                                                                h, ""
                                                                            )
                                                                        )
                                                                        for h in headers
                                                                    )
                                                                )
                                                            result_text = "\n".join(
                                                                result_lines
                                                            )
                                                        else:
                                                            result_text = "No results"

                                                    # Check if result is too large
                                                    if len(result_text) > 25000:
                                                        tool_result = {
                                                            "status": "error",
                                                            "error": f"Query result too large: {len(result_text)} characters exceeds 25,000 character limit. Try reducing the number of columns or rows.",
                                                        }
                                                    else:
                                                        tool_result = {
                                                            "status": "success",
                                                            "result": result_text,
                                                            "row_count": len(
                                                                result_data
                                                            ),
                                                            "query": limited_query,
                                                        }
                                            finally:
                                                await postgres_conn.close()

                                    except HTTPException as e:
                                        tool_result = {
                                            "status": "error",
                                            "error": f"Failed to connect to PostGIS database: {e.detail}",
                                        }
                                    except Exception as e:
                                        tool_result = {
                                            "status": "error",
                                            "error": f"PostgreSQL query error: {str(e)}",
                                            "query": limited_query,
                                        }

                            await add_chat_completion_message(
                                ChatCompletionToolMessageParam(
                                    role="tool",
                                    tool_call_id=tool_call.id,
                                    content=json.dumps(tool_result),
                                ),
                            )

                        elif function_name == "zonal_statistics":
                            raster_layer_id = tool_args.get("raster_layer_id")
                            zones_layer_id = tool_args.get("zones_layer_id")
                            stats = tool_args.get("stats")

                            if not raster_layer_id or not zones_layer_id:
                                tool_result = {
                                    "status": "error",
                                    "error": "Missing required parameters (raster_layer_id or zones_layer_id).",
                                }
                            else:
                                # Verify both layers exist and user has access
                                raster_exists = await conn.fetchrow(
                                    """
                                    SELECT layer_id, type FROM map_layers
                                    WHERE layer_id = $1 AND owner_uuid = $2
                                    """,
                                    raster_layer_id,
                                    user_id,
                                )
                                zones_exists = await conn.fetchrow(
                                    """
                                    SELECT layer_id, type FROM map_layers
                                    WHERE layer_id = $1 AND owner_uuid = $2
                                    """,
                                    zones_layer_id,
                                    user_id,
                                )

                                if not raster_exists:
                                    tool_result = {
                                        "status": "error",
                                        "error": f"Raster layer '{raster_layer_id}' not found or you do not have access to it.",
                                    }
                                elif not zones_exists:
                                    tool_result = {
                                        "status": "error",
                                        "error": f"Zones layer '{zones_layer_id}' not found or you do not have access to it.",
                                    }
                                else:
                                    try:
                                        async with kue_ephemeral_action(
                                            conversation.id,
                                            "Computing zonal statistics...",
                                        ):
                                            from src.geoprocessing.zonal_stats import (
                                                compute_zonal_statistics,
                                            )

                                            tool_result = await compute_zonal_statistics(
                                                raster_layer_id=raster_layer_id,
                                                zones_layer_id=zones_layer_id,
                                                stats=stats,
                                                timeout=30,
                                            )
                                    except HTTPException as e:
                                        tool_result = {
                                            "status": "error",
                                            "error": f"Zonal statistics error: {e.detail}",
                                        }
                                    except Exception as e:
                                        logger.exception(
                                            "Error computing zonal statistics for raster=%s, zones=%s",
                                            raster_layer_id,
                                            zones_layer_id,
                                        )
                                        tool_result = {
                                            "status": "error",
                                            "error": f"Failed to compute zonal statistics: {str(e)}",
                                        }

                            await add_chat_completion_message(
                                ChatCompletionToolMessageParam(
                                    role="tool",
                                    tool_call_id=tool_call.id,
                                    content=json.dumps(tool_result),
                                ),
                            )

                        elif function_name == "query_rwanda_zonal_stats":
                            query_type = tool_args.get("query_type")

                            try:
                                from src.services.rwanda_lakehouse import get_rwanda_lakehouse_manager
                                rwanda_mgr = get_rwanda_lakehouse_manager()

                                if query_type == "district_summary":
                                    province = tool_args.get("province")
                                    week_start = tool_args.get("week_start")
                                    result_data = rwanda_mgr.query_district_summary(
                                        province=province,
                                        week_start=week_start
                                    )
                                    tool_result = {"status": "success", "data": result_data}

                                elif query_type == "ndvi_timeseries":
                                    h3_index = tool_args.get("h3_index")
                                    parcel_id = tool_args.get("parcel_id")
                                    date_from = tool_args.get("date_from")
                                    date_to = tool_args.get("date_to")
                                    result_data = rwanda_mgr.query_ndvi_timeseries(
                                        h3_index=h3_index,
                                        parcel_id=parcel_id,
                                        date_from=date_from,
                                        date_to=date_to
                                    )
                                    tool_result = {"status": "success", "data": result_data}

                                else:
                                    tool_result = {
                                        "status": "error",
                                        "error": f"Unknown query_type: {query_type}. Must be 'district_summary' or 'ndvi_timeseries'."
                                    }

                            except HTTPException as e:
                                tool_result = {
                                    "status": "error",
                                    "error": f"Rwanda lakehouse query error: {e.detail}"
                                }
                            except Exception as e:
                                logger.exception(
                                    "Error querying Rwanda lakehouse: query_type=%s",
                                    query_type
                                )
                                tool_result = {
                                    "status": "error",
                                    "error": f"Failed to query Rwanda lakehouse: {str(e)}"
                                }

                            await add_chat_completion_message(
                                ChatCompletionToolMessageParam(
                                    role="tool",
                                    tool_call_id=tool_call.id,
                                    content=json.dumps(tool_result),
                                )
                            )

                        elif function_name == "search_satellite_imagery":
                            try:
                                from src.services.stac_service import get_stac_service

                                bbox_str = tool_args.get("bbox")
                                parsed_bbox = None
                                if bbox_str:
                                    parsed_bbox = [float(x) for x in bbox_str.split(",")]

                                service = get_stac_service()
                                result_data = await asyncio.get_event_loop().run_in_executor(
                                    None, lambda: service.search_imagery(
                                        bbox=parsed_bbox,
                                        datetime_range=tool_args.get("datetime_range"),
                                        max_cloud_cover=tool_args.get("max_cloud_cover", 20.0),
                                        limit=tool_args.get("limit", 10),
                                    )
                                )

                                if "error" in result_data:
                                    tool_result = {"status": "error", "error": result_data["error"]}
                                else:
                                    # Compute NDVI for the first result if it has B04+B08
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

                                    tool_result = {
                                        "status": "success",
                                        "search_results": result_data,
                                        "ndvi_sample": ndvi_computed,
                                    }
                            except Exception as e:
                                logger.exception("STAC search tool failed")
                                tool_result = {"status": "error", "error": str(e)}

                            await add_chat_completion_message(
                                ChatCompletionToolMessageParam(
                                    role="tool",
                                    tool_call_id=tool_call.id,
                                    content=json.dumps(tool_result),
                                )
                            )

                        elif function_name == "get_field_health":
                            try:
                                from src.services.sentinel_hub_service import get_sentinel_hub_service

                                sh_service = get_sentinel_hub_service()
                                if sh_service is None:
                                    tool_result = {"status": "error", "error": "Sentinel Hub not available"}
                                else:
                                    _fh_geom = tool_args.get("geometry")
                                    # Auto-buffer Point/MultiPoint geometries (500m) so the LLM
                                    # doesn't need to create a buffer first.
                                    if _fh_geom and _fh_geom.get("type") in ("Point", "MultiPoint"):
                                        from shapely.geometry import shape as _shape, mapping as _mapping
                                        from pyproj import Transformer as _Transformer
                                        _pt = _shape(_fh_geom)
                                        _to_utm = _Transformer.from_crs("EPSG:4326", "EPSG:32735", always_xy=True)
                                        _to_wgs = _Transformer.from_crs("EPSG:32735", "EPSG:4326", always_xy=True)
                                        from shapely.ops import transform as _stransform
                                        _pt_utm = _stransform(_to_utm.transform, _pt)
                                        _buf_utm = _pt_utm.buffer(500)  # 500m radius
                                        _buf_wgs = _stransform(_to_wgs.transform, _buf_utm)
                                        _fh_geom = _mapping(_buf_wgs)
                                        logger.info("get_field_health: auto-buffered Point to 500m polygon")

                                    result_data = await asyncio.get_event_loop().run_in_executor(
                                        None, lambda: sh_service.get_field_stats(
                                            geometry=_fh_geom,
                                            date_from=tool_args.get("date_from"),
                                            date_to=tool_args.get("date_to"),
                                            index=tool_args.get("index", "ndvi"),
                                        )
                                    )
                                    if "error" in result_data:
                                        tool_result = {"status": "error", "error": result_data["error"]}
                                    else:
                                        tool_result = {"status": "success", "field_stats": result_data}
                            except Exception as e:
                                logger.exception("get_field_health tool failed")
                                tool_result = {"status": "error", "error": str(e)}

                            await add_chat_completion_message(
                                ChatCompletionToolMessageParam(
                                    role="tool",
                                    tool_call_id=tool_call.id,
                                    content=json.dumps(tool_result),
                                )
                            )

                        elif function_name == "create_management_zones":
                            try:
                                from src.services.precision_ag_service import create_management_zones

                                result_data = await asyncio.get_event_loop().run_in_executor(
                                    None,
                                    lambda: create_management_zones(
                                        geometry=tool_args.get("geometry"),
                                        num_zones=tool_args.get("num_zones", 3),
                                        date_from=tool_args.get("date_from"),
                                        date_to=tool_args.get("date_to"),
                                    ),
                                )
                                if "error" in result_data:
                                    tool_result = {"status": "error", "error": result_data["error"]}
                                else:
                                    tool_result = {"status": "success", "management_zones": result_data}
                            except Exception as e:
                                logger.exception("create_management_zones failed")
                                tool_result = {"status": "error", "error": str(e)}

                            await add_chat_completion_message(
                                ChatCompletionToolMessageParam(
                                    role="tool",
                                    tool_call_id=tool_call.id,
                                    content=json.dumps(tool_result),
                                )
                            )

                        elif function_name == "create_prescription_map":
                            try:
                                from src.services.precision_ag_service import create_prescription_map

                                result_data = await asyncio.get_event_loop().run_in_executor(
                                    None,
                                    lambda: create_prescription_map(
                                        geometry=tool_args.get("geometry"),
                                        crop_type=tool_args.get("crop_type", "maize"),
                                        num_zones=tool_args.get("num_zones", 3),
                                    ),
                                )
                                if "error" in result_data:
                                    tool_result = {"status": "error", "error": result_data["error"]}
                                else:
                                    tool_result = {"status": "success", "prescription_map": result_data}
                            except Exception as e:
                                logger.exception("create_prescription_map failed")
                                tool_result = {"status": "error", "error": str(e)}

                            await add_chat_completion_message(
                                ChatCompletionToolMessageParam(
                                    role="tool",
                                    tool_call_id=tool_call.id,
                                    content=json.dumps(tool_result),
                                )
                            )

                        elif function_name == "create_soil_sampling_plan":
                            try:
                                from src.services.precision_ag_service import create_soil_sampling_plan

                                result_data = await asyncio.get_event_loop().run_in_executor(
                                    None,
                                    lambda: create_soil_sampling_plan(
                                        geometry=tool_args.get("geometry"),
                                        num_zones=tool_args.get("num_zones", 3),
                                    ),
                                )
                                if "error" in result_data:
                                    tool_result = {"status": "error", "error": result_data["error"]}
                                else:
                                    tool_result = {"status": "success", "sampling_plan": result_data}
                            except Exception as e:
                                logger.exception("create_soil_sampling_plan failed")
                                tool_result = {"status": "error", "error": str(e)}

                            await add_chat_completion_message(
                                ChatCompletionToolMessageParam(
                                    role="tool",
                                    tool_call_id=tool_call.id,
                                    content=json.dumps(tool_result),
                                )
                            )

                        elif function_name == "identify_parcel_crop":
                            try:
                                from src.services.sentinel_hub_service import get_sentinel_hub_service
                                from src.services.ml_inference import get_ml_service

                                sh_service = get_sentinel_hub_service()
                                if sh_service is None:
                                    tool_result = {"status": "error", "error": "Sentinel Hub not available"}
                                else:
                                    _ic_geom = tool_args.get("geometry")
                                    if not _ic_geom:
                                        tool_result = {"status": "error", "error": "geometry is required for crop identification"}
                                        break
                                    _ic_months = tool_args.get("months", 6)
                                    if _ic_months < 3:
                                        _ic_months = 3

                                    # Auto-buffer Point geometries
                                    if _ic_geom and _ic_geom.get("type") in ("Point", "MultiPoint"):
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

                                    # Step 1: Get NDVI time-series from Sentinel Hub
                                    ts_result = await asyncio.get_event_loop().run_in_executor(
                                        None, lambda: sh_service.get_field_timeseries(
                                            geometry=_ic_geom,
                                            months=_ic_months,
                                        )
                                    )
                                    if "error" in ts_result:
                                        tool_result = {"status": "error", "error": ts_result["error"]}
                                    else:
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
                                            tool_result = {
                                                "status": "error",
                                                "error": f"Insufficient data: only {len(_ndvi_ts)} cloud-free observations "
                                                         f"in {_ic_months} months. Need at least 4 for crop identification.",
                                            }
                                        else:
                                            # Step 2: Run crop identification
                                            ml_service = get_ml_service()
                                            crop_result = ml_service.identify_crop(_ndvi_ts)
                                            if "error" in crop_result:
                                                tool_result = {"status": "error", "error": crop_result["error"]}
                                            else:
                                                tool_result = {"status": "success", "crop_identification": crop_result}
                            except Exception as e:
                                logger.exception("identify_parcel_crop failed")
                                tool_result = {"status": "error", "error": str(e)}

                            await add_chat_completion_message(
                                ChatCompletionToolMessageParam(
                                    role="tool",
                                    tool_call_id=tool_call.id,
                                    content=json.dumps(tool_result),
                                )
                            )

                        elif function_name == "confirm_crop_prediction":
                            try:
                                from datetime import date as _cdate

                                _predicted = tool_args.get("predicted_crop", "")
                                _actual = tool_args.get("actual_crop", "")
                                _confirmed = tool_args.get("confirmed", False)
                                _season = tool_args.get("season")
                                _geom = tool_args.get("geometry")

                                # Auto-detect season from current date
                                if not _season:
                                    _today = _cdate.today()
                                    _yr = _today.year
                                    # Season A: Sep-Feb, Season B: Feb-Jul
                                    if _today.month >= 9:
                                        _season = f"{_yr + 1}A"
                                    elif _today.month <= 2:
                                        _season = f"{_yr}A"
                                    else:
                                        _season = f"{_yr}B"

                                # Store feedback in PostgreSQL
                                try:
                                    await conn.execute(
                                        """INSERT INTO crop_feedback
                                           (user_id, predicted_crop, actual_crop, confirmed,
                                            season, geometry, created_at)
                                           VALUES ($1, $2, $3, $4, $5, $6, NOW())""",
                                        str(user_id) if user_id else "anonymous",
                                        _predicted,
                                        _actual,
                                        _confirmed,
                                        _season,
                                        json.dumps(_geom) if _geom else None,
                                    )
                                    tool_result = {
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
                                        _predicted, _actual, _confirmed, _season, user_id,
                                    )
                                    tool_result = {
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
                                tool_result = {"status": "error", "error": str(e)}

                            await add_chat_completion_message(
                                ChatCompletionToolMessageParam(
                                    role="tool",
                                    tool_call_id=tool_call.id,
                                    content=json.dumps(tool_result),
                                )
                            )

                        elif function_name == "get_ndvi_stats":
                            try:
                                from datetime import date as _date, datetime as _datetime, timedelta as _td

                                # ── 1. Query PostgreSQL cache (populated by nightly Dagster job) ──
                                _cached_rows: list = []
                                try:
                                    _district = tool_args.get("district")
                                    if _district:
                                        _cached_rows = await conn.fetch(
                                            "SELECT district, week_start, mean_ndvi, std_ndvi, min_ndvi, max_ndvi, valid_pixels "
                                            "FROM ndvi_field_cache WHERE district = $1 "
                                            "ORDER BY week_start DESC LIMIT 50",
                                            _district,
                                        )
                                    else:
                                        _cached_rows = await conn.fetch(
                                            "SELECT district, week_start, mean_ndvi, std_ndvi, min_ndvi, max_ndvi, valid_pixels "
                                            "FROM ndvi_field_cache ORDER BY week_start DESC, district LIMIT 200"
                                        )
                                except Exception:
                                    logger.debug("PostgreSQL NDVI cache not available, will try real-time Sentinel Hub")

                                _ndvi_stats: list = []
                                _source = "postgres_cache"
                                for r in _cached_rows:
                                    _ndvi_stats.append({
                                        "district": r["district"], "week_start": str(r["week_start"]) if r["week_start"] else None,
                                        "mean_ndvi": round(r["mean_ndvi"], 4) if r["mean_ndvi"] else None,
                                        "std_ndvi": round(r["std_ndvi"], 4) if r["std_ndvi"] else None,
                                        "min_ndvi": round(r["min_ndvi"], 4) if r["min_ndvi"] else None,
                                        "max_ndvi": round(r["max_ndvi"], 4) if r["max_ndvi"] else None,
                                        "valid_pixels": r["valid_pixels"],
                                        "source": "sentinel_hub_cache",
                                    })

                                # ── 2. Real-time Sentinel Hub fallback ──
                                # If cache is empty or stale (>7 days old), fetch live from Sentinel Hub
                                _need_realtime = len(_ndvi_stats) == 0
                                if _ndvi_stats:
                                    _latest_week = max(
                                        (s["week_start"] for s in _ndvi_stats if s["week_start"]),
                                        default=None,
                                    )
                                    if _latest_week:
                                        _stale_cutoff = str(_date.today() - _td(days=14))
                                        if _latest_week < _stale_cutoff:
                                            _need_realtime = True

                                _realtime_stats: list = []
                                if _need_realtime:
                                    try:
                                        from src.services.sentinel_hub_service import get_sentinel_hub_service as _get_sh
                                        import numpy as _np

                                        _sh = _get_sh()
                                        if _sh and _sh.is_configured():
                                            # Get district geometries from PostGIS
                                            _district_filter = tool_args.get("district")
                                            _where_clause = "WHERE district = $1" if _district_filter else ""
                                            _query_params = [_district_filter] if _district_filter else []
                                            async with conn.transaction():
                                                _dist_rows = await conn.fetch(
                                                    f"SELECT district, ST_AsGeoJSON(geom) as geom "
                                                    f"FROM rwanda_district_boundaries {_where_clause} "
                                                    f"ORDER BY district",
                                                    *_query_params,
                                                )

                                            _now = _datetime.utcnow()
                                            _rt_from = (_now - _td(days=7)).strftime("%Y-%m-%d")
                                            _rt_to = _now.strftime("%Y-%m-%d")
                                            _rt_week = _rt_from

                                            for _dr in _dist_rows:
                                                try:
                                                    _geom = json.loads(_dr["geom"])
                                                    _stats = _sh.get_field_stats(
                                                        geometry=_geom,
                                                        date_from=_rt_from,
                                                        date_to=_rt_to,
                                                        index="ndvi",
                                                    )
                                                    if "error" in _stats:
                                                        continue
                                                    _intervals = _stats.get("intervals", [])
                                                    if not _intervals:
                                                        continue
                                                    _means = [
                                                        iv["ndvi"]["mean"]
                                                        for iv in _intervals
                                                        if "ndvi" in iv and iv["ndvi"].get("valid_pixels", 0) > 0
                                                    ]
                                                    if not _means:
                                                        continue
                                                    _realtime_stats.append({
                                                        "district": _dr["district"],
                                                        "week_start": _rt_week,
                                                        "mean_ndvi": round(float(_np.mean(_means)), 4),
                                                        "std_ndvi": round(float(_np.std(_means)), 4),
                                                        "min_ndvi": round(float(_np.min(_means)), 4),
                                                        "max_ndvi": round(float(_np.max(_means)), 4),
                                                        "valid_pixels": sum(
                                                            iv["ndvi"].get("valid_pixels", 0)
                                                            for iv in _intervals if "ndvi" in iv
                                                        ),
                                                        "source": "sentinel_hub_realtime",
                                                    })
                                                except Exception as _e:
                                                    logger.debug("SH realtime failed for %s: %s", _dr["district"], _e)
                                            _source = "sentinel_hub_realtime" if not _ndvi_stats else "cache + realtime"
                                    except Exception as _sh_err:
                                        logger.warning("Sentinel Hub real-time NDVI failed: %s", _sh_err)

                                # ── 3. Merge cached + real-time ──
                                _all_stats = _ndvi_stats + _realtime_stats
                                _all_stats.sort(
                                    key=lambda s: (s.get("week_start") or "", s.get("district") or ""),
                                    reverse=True,
                                )

                                if _all_stats:
                                    _sources = set(s.get("source", "cache") for s in _all_stats)
                                    tool_result = {
                                        "status": "success",
                                        "source": " + ".join(sorted(_sources)),
                                        "count": len(_all_stats),
                                        "cached_records": len(_ndvi_stats),
                                        "realtime_records": len(_realtime_stats),
                                        "note": (
                                            "NDVI values: 0.6-0.8 = dense vegetation, 0.3-0.5 = cropland, "
                                            "0.1-0.3 = sparse vegetation, <0.1 = bare soil/cloud contaminated. "
                                            "Negative values indicate heavy cloud cover during the observation period. "
                                            "Each record has a 'source' field: 'sentinel_hub_cache' (nightly batch) "
                                            "or 'sentinel_hub_realtime' (live query)."
                                        ),
                                        "ndvi_stats": _all_stats,
                                    }
                                    _pgc_id = await _ensure_rwanda_postgis_connection(
                                        conn, current_project_id, user_id,
                                    )
                                    if _pgc_id:
                                        tool_result["postgis_connection_id"] = _pgc_id
                                        tool_result["kue_instructions"] = (
                                            "To visualise these NDVI stats on the map, call new_layer_from_postgis with "
                                            f"postgis_connection_id='{_pgc_id}'. IMPORTANT: the query MUST return columns named 'id' and 'geom'. "
                                            "Available tables: rwanda_district_boundaries (district, geom), "
                                            "rwanda_cell_boundaries (cell_id, cell_name, district_name, geom). "
                                            "Example: SELECT ROW_NUMBER() OVER() AS id, district AS district_name, geom FROM rwanda_district_boundaries "
                                            "Then call add_layer_to_map and set_layer_style to colour districts by NDVI. "
                                            "DO NOT reuse an existing layer — always create a NEW layer from PostGIS."
                                        )
                                else:
                                    # ── 4. STAC COG fallback (free, no API key) ──
                                    _stac_stats: list = []
                                    try:
                                        from src.services.stac_service import get_stac_service as _get_stac_ndvi

                                        _stac_ndvi = _get_stac_ndvi()
                                        _stac_district = tool_args.get("district")

                                        if _stac_district:
                                            _stac_bbox_rows = await conn.fetch(
                                                "SELECT district, bbox_west, bbox_south, bbox_east, bbox_north "
                                                "FROM rwanda_district_boundaries WHERE LOWER(district) = LOWER($1)",
                                                _stac_district,
                                            )
                                        else:
                                            _stac_bbox_rows = await conn.fetch(
                                                "SELECT district, bbox_west, bbox_south, bbox_east, bbox_north "
                                                "FROM rwanda_district_boundaries ORDER BY district LIMIT 10"
                                            )

                                        for _sbr in _stac_bbox_rows:
                                            _s_bbox = [float(_sbr["bbox_west"]), float(_sbr["bbox_south"]),
                                                       float(_sbr["bbox_east"]), float(_sbr["bbox_north"])]
                                            _stac_ts = await asyncio.get_event_loop().run_in_executor(
                                                None, lambda bb=_s_bbox: _stac_ndvi.compute_admin_ndvi(bb, days=30, max_scenes=4),
                                            )
                                            if "error" not in _stac_ts:
                                                for _obs in _stac_ts.get("observations", []):
                                                    _stac_stats.append({
                                                        "district": _sbr["district"],
                                                        "week_start": _obs.get("datetime", "")[:10] if _obs.get("datetime") else None,
                                                        "mean_ndvi": _obs.get("mean_ndvi"),
                                                        "std_ndvi": _obs.get("std_ndvi"),
                                                        "min_ndvi": _obs.get("min_ndvi"),
                                                        "max_ndvi": _obs.get("max_ndvi"),
                                                        "valid_pixels": _obs.get("valid_pixel_count"),
                                                        "source": "stac_cog_realtime",
                                                    })
                                    except Exception as _stac_ndvi_err:
                                        logger.warning("STAC NDVI fallback failed: %s", _stac_ndvi_err)

                                    if _stac_stats:
                                        tool_result = {
                                            "status": "success",
                                            "source": "stac_cog_realtime",
                                            "count": len(_stac_stats),
                                            "cached_records": 0,
                                            "realtime_records": len(_stac_stats),
                                            "note": (
                                                "NDVI computed in real-time from Sentinel-2 COGs via STAC (free, no API key). "
                                                "Values: 0.6-0.8 = dense vegetation, 0.3-0.5 = cropland, "
                                                "0.1-0.3 = sparse vegetation, <0.1 = bare soil."
                                            ),
                                            "ndvi_stats": _stac_stats,
                                        }
                                        _pgc_id = await _ensure_rwanda_postgis_connection(
                                            conn, current_project_id, user_id,
                                        )
                                        if _pgc_id:
                                            tool_result["postgis_connection_id"] = _pgc_id
                                            tool_result["kue_instructions"] = (
                                                "To visualise these NDVI stats on the map, call new_layer_from_postgis with "
                                                f"postgis_connection_id='{_pgc_id}'. IMPORTANT: the query MUST return columns named 'id' and 'geom'. "
                                                "Available tables: rwanda_district_boundaries (district, geom), "
                                                "rwanda_cell_boundaries (cell_id, cell_name, district_name, geom). "
                                                "Example: SELECT ROW_NUMBER() OVER() AS id, district AS district_name, geom FROM rwanda_district_boundaries "
                                                "Then call add_layer_to_map and set_layer_style to colour districts by NDVI. "
                                                "DO NOT reuse an existing layer — always create a NEW layer from PostGIS."
                                            )
                                    else:
                                        tool_result = {
                                            "status": "success",
                                            "ndvi_stats": [],
                                            "message": (
                                                "No NDVI data available. Cache is empty, Sentinel Hub unreachable, "
                                                "and STAC COG query found no cloud-free scenes. "
                                                "The nightly Dagster job populates this cache automatically."
                                            ),
                                        }
                            except Exception as e:
                                logger.exception("get_ndvi_stats tool failed")
                                tool_result = {"status": "error", "error": str(e)}

                            await add_chat_completion_message(
                                ChatCompletionToolMessageParam(
                                    role="tool",
                                    tool_call_id=tool_call.id,
                                    content=json.dumps(tool_result),
                                )
                            )

                        elif function_name == "get_cell_ndvi_stats":
                            try:
                                _cell = tool_args.get("cell_name")
                                _district = tool_args.get("district")
                                _where = []
                                _params: list = []
                                _pidx = 1
                                if _cell:
                                    _where.append(f"cell_name ILIKE ${_pidx}")
                                    _params.append(f"%{_cell}%")
                                    _pidx += 1
                                if _district:
                                    _where.append(f"district_name ILIKE ${_pidx}")
                                    _params.append(f"%{_district}%")
                                    _pidx += 1
                                _where_sql = f"WHERE {' AND '.join(_where)}" if _where else ""
                                _rows = await conn.fetch(
                                    f"SELECT cell_name, district_name, week_start, "
                                    f"mean_ndvi, std_ndvi, min_ndvi, max_ndvi, valid_pixels "
                                    f"FROM ndvi_cell_cache {_where_sql} "
                                    f"ORDER BY computed_at DESC LIMIT 100",
                                    *_params,
                                )

                                if _rows:
                                    tool_result = {
                                        "status": "success",
                                        "source": "postgres_cache",
                                        "count": len(_rows),
                                        "cell_ndvi_stats": [
                                            {
                                                "cell_name": r["cell_name"],
                                                "district_name": r["district_name"],
                                                "week_start": str(r["week_start"]) if r["week_start"] else None,
                                                "mean_ndvi": round(r["mean_ndvi"], 4) if r["mean_ndvi"] else None,
                                                "std_ndvi": round(r["std_ndvi"], 4) if r["std_ndvi"] else None,
                                                "min_ndvi": round(r["min_ndvi"], 4) if r["min_ndvi"] else None,
                                                "max_ndvi": round(r["max_ndvi"], 4) if r["max_ndvi"] else None,
                                                "valid_pixels": r["valid_pixels"],
                                            }
                                            for r in _rows
                                        ],
                                    }
                                    # Auto-provision PostGIS connection so Sage can create map layers
                                    _pgc_id = await _ensure_rwanda_postgis_connection(
                                        conn, current_project_id, user_id,
                                    )
                                    if _pgc_id:
                                        tool_result["postgis_connection_id"] = _pgc_id
                                        tool_result["kue_instructions"] = (
                                            "To visualise these cell NDVI stats on the map, call new_layer_from_postgis with "
                                            f"postgis_connection_id='{_pgc_id}'. IMPORTANT: the query MUST return columns named 'id' and 'geom'. "
                                            "Available tables: rwanda_cell_boundaries (cell_id, cell_name, district_name, geom). "
                                            "Example: SELECT cell_id AS id, cell_name, district_name, geom FROM rwanda_cell_boundaries "
                                            "Then call add_layer_to_map and set_layer_style to colour cells by NDVI values. "
                                            "DO NOT reuse an existing layer — always create a NEW layer from PostGIS."
                                        )
                                else:
                                    tool_result = {
                                        "status": "success",
                                        "source": "duckdb_cache",
                                        "cell_ndvi_stats": [],
                                        "message": "No cell NDVI data yet — run rwanda_cell_boundaries asset then nightly_cell_ndvi",
                                    }
                            except Exception as e:
                                logger.exception("get_cell_ndvi_stats tool failed")
                                tool_result = {"status": "error", "error": str(e)}

                            await add_chat_completion_message(
                                ChatCompletionToolMessageParam(
                                    role="tool",
                                    tool_call_id=tool_call.id,
                                    content=json.dumps(tool_result),
                                )
                            )

                        elif function_name == "get_soil_properties":
                            try:
                                from src.services.isdasoil_service import query_soil_point

                                lon = tool_args.get("longitude")
                                lat = tool_args.get("latitude")
                                properties = tool_args.get("properties")
                                depth = tool_args.get("depth", "0-20")

                                result_data = await asyncio.get_event_loop().run_in_executor(
                                    None, lambda: query_soil_point(
                                        lon=lon,
                                        lat=lat,
                                        properties=properties,
                                        depth=depth,
                                    )
                                )

                                if "error" in result_data:
                                    tool_result = {"status": "error", "error": result_data["error"]}
                                else:
                                    tool_result = result_data
                            except Exception as e:
                                logger.exception("get_soil_properties tool failed")
                                tool_result = {"status": "error", "error": str(e)}

                            await add_chat_completion_message(
                                ChatCompletionToolMessageParam(
                                    role="tool",
                                    tool_call_id=tool_call.id,
                                    content=json.dumps(tool_result),
                                )
                            )

                        elif function_name == "get_parcel_ndvi_stats":
                            try:
                                _parcel = tool_args.get("parcel_name")
                                _layer = tool_args.get("layer_id")
                                _where = []
                                _params_p: list = []
                                _pidx = 1
                                if _parcel:
                                    _where.append(f"parcel_name ILIKE ${_pidx}")
                                    _params_p.append(f"%{_parcel}%")
                                    _pidx += 1
                                if _layer:
                                    _where.append(f"layer_id = ${_pidx}")
                                    _params_p.append(_layer)
                                    _pidx += 1
                                _where_sql = f"WHERE {' AND '.join(_where)}" if _where else ""
                                _rows = await conn.fetch(
                                    f"SELECT parcel_id, parcel_name, layer_id, week_start, "
                                    f"mean_ndvi, std_ndvi, min_ndvi, max_ndvi, valid_pixels, area_ha "
                                    f"FROM ndvi_parcel_cache {_where_sql} "
                                    f"ORDER BY computed_at DESC LIMIT 100",
                                    *_params_p,
                                )

                                if _rows:
                                    tool_result = {
                                        "status": "success",
                                        "source": "postgres_cache",
                                        "count": len(_rows),
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
                                            for r in _rows
                                        ],
                                    }
                                else:
                                    tool_result = {
                                        "status": "success",
                                        "source": "postgres_cache",
                                        "parcel_ndvi_stats": [],
                                        "message": (
                                            "No parcel NDVI data yet. Upload field boundaries "
                                            "through Mundi UI and tag with rwanda_parcels=true "
                                            "in layer metadata. The nightly pipeline processes them."
                                        ),
                                    }
                            except Exception as e:
                                logger.exception("get_parcel_ndvi_stats tool failed")
                                tool_result = {"status": "error", "error": str(e)}

                            await add_chat_completion_message(
                                ChatCompletionToolMessageParam(
                                    role="tool",
                                    tool_call_id=tool_call.id,
                                    content=json.dumps(tool_result),
                                )
                            )

                        elif function_name == "get_agri_indices":
                            # Cache-first multi-index query: PostgreSQL cache → Sentinel Hub on miss
                            try:
                                from src.services.sentinel_hub_service import (
                                    get_sentinel_hub_service as _get_sh,
                                    AGRI_INDEX_NAMES as _AGRI_INDICES,
                                )
                                import numpy as _np
                                from datetime import datetime as _datetime, timedelta as _td

                                _CACHE_TTL_DAYS = 7  # Sentinel-2 revisit ~5 days

                                _level = tool_args.get("admin_level", "district")
                                _name_filter = tool_args.get("name")
                                _district_filter = tool_args.get("district")
                                _date_from = tool_args.get("date_from")
                                _date_to = tool_args.get("date_to")

                                if not _date_to:
                                    _date_to = _datetime.utcnow().strftime("%Y-%m-%d")
                                if not _date_from:
                                    _date_from = (_datetime.utcnow() - _td(days=7)).strftime("%Y-%m-%d")

                                # Select the right admin boundary table
                                _table_map = {
                                    "district": ("rwanda_district_boundaries", "district", None),
                                    "sector": ("rwanda_sector_boundaries", "sector_name", "district_name"),
                                    "cell": ("rwanda_cell_boundaries", "cell_name", "district_name"),
                                }
                                _tbl, _name_col, _parent_col = _table_map.get(_level, _table_map["district"])

                                # Build query with optional filters
                                _conditions = []
                                _params: list = []
                                _pidx = 1
                                if _name_filter:
                                    _conditions.append(f"{_name_col} ILIKE ${_pidx}")
                                    _params.append(f"%{_name_filter}%")
                                    _pidx += 1
                                if _district_filter and _parent_col:
                                    _conditions.append(f"{_parent_col} ILIKE ${_pidx}")
                                    _params.append(f"%{_district_filter}%")
                                    _pidx += 1

                                _where = f"WHERE {' AND '.join(_conditions)}" if _conditions else ""

                                # Fetch admin unit names from PostGIS (limit 30)
                                async with conn.transaction():
                                    _admin_rows = await conn.fetch(
                                        f"SELECT {_name_col} AS name, "
                                        f"{_parent_col + ' AS parent,' if _parent_col else ''} "
                                        f"ST_AsGeoJSON(geom) AS geom "
                                        f"FROM {_tbl} {_where} "
                                        f"ORDER BY {_name_col} LIMIT 30",
                                        *_params,
                                    )

                                if not _admin_rows:
                                    tool_result = {
                                        "status": "success",
                                        "agri_indices": [],
                                        "message": f"No {_level} boundaries found matching filters.",
                                    }
                                else:
                                    # ---- Step 1: Check PostgreSQL cache ----
                                    _admin_names = [r["name"] for r in _admin_rows]
                                    _cutoff = (_datetime.utcnow() - _td(days=_CACHE_TTL_DAYS)).strftime("%Y-%m-%d")

                                    # Query cache for fresh rows
                                    _cached_rows = await conn.fetch(
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
                                        _level, _admin_names, _cutoff,
                                    )

                                    # Build set of cached names (dedup: keep most recent per name)
                                    _cached_by_name: dict = {}
                                    for _cr in _cached_rows:
                                        _cname = _cr["admin_name"]
                                        if _cname not in _cached_by_name:
                                            _cached_by_name[_cname] = {
                                                "admin_level": _level,
                                                "name": _cname,
                                                "district": _cr["parent_name"] if _cr["parent_name"] else None,
                                                "date_from": str(_cr["week_start"]),
                                                "date_to": _date_to,
                                                "ndvi_mean": _cr["ndvi_mean"], "ndvi_std": _cr["ndvi_std"],
                                                "evi_mean": _cr["evi_mean"], "evi_std": _cr["evi_std"],
                                                "ndwi_mean": _cr["ndwi_mean"], "ndwi_std": _cr["ndwi_std"],
                                                "savi_mean": _cr["savi_mean"], "savi_std": _cr["savi_std"],
                                                "ndre_mean": _cr["ndre_mean"], "ndre_std": _cr["ndre_std"],
                                                "ndbi_mean": _cr["ndbi_mean"], "ndbi_std": _cr["ndbi_std"],
                                                "valid_pixels": _cr["valid_pixels"],
                                                "source": "cache",
                                            }

                                    # ---- Step 2: Identify cache misses ----
                                    _miss_rows = [r for r in _admin_rows if r["name"] not in _cached_by_name]
                                    _cache_hits = len(_admin_names) - len(_miss_rows)

                                    logger.info(
                                        "agri_indices cache: %d hits, %d misses for %s level",
                                        _cache_hits, len(_miss_rows), _level,
                                    )

                                    # ---- Step 3: Query Sentinel Hub for misses ----
                                    _results: list = list(_cached_by_name.values())
                                    _errors: list = []

                                    if _miss_rows:
                                        _sh = _get_sh()
                                        if not _sh or not _sh.is_configured():
                                            _errors.append("Sentinel Hub not configured — returning cached data only")
                                        else:
                                            for _ar in _miss_rows:
                                                _geom = json.loads(_ar["geom"])
                                                _name = _ar["name"]
                                                _parent = _ar.get("parent")
                                                try:
                                                    _stats = _sh.get_agri_stats(
                                                        geometry=_geom,
                                                        date_from=_date_from,
                                                        date_to=_date_to,
                                                    )
                                                    if "error" in _stats:
                                                        _errors.append(f"{_name}: {_stats['error']}")
                                                        continue
                                                    _intervals = _stats.get("intervals", [])
                                                    if not _intervals:
                                                        continue

                                                    # Aggregate daily intervals into summary
                                                    _row: dict = {
                                                        "admin_level": _level,
                                                        "name": _name,
                                                    }
                                                    if _parent:
                                                        _row["district"] = _parent
                                                    _row["date_from"] = _date_from
                                                    _row["date_to"] = _date_to

                                                    _total_px = 0
                                                    for _idx in _AGRI_INDICES:
                                                        _means = [
                                                            iv[_idx]["mean"]
                                                            for iv in _intervals
                                                            if _idx in iv and iv[_idx].get("valid_pixels", 0) > 0
                                                        ]
                                                        if _means:
                                                            _row[f"{_idx}_mean"] = round(float(_np.mean(_means)), 4)
                                                            _row[f"{_idx}_std"] = round(float(_np.std(_means)), 4)
                                                        else:
                                                            _row[f"{_idx}_mean"] = None
                                                            _row[f"{_idx}_std"] = None
                                                    for iv in _intervals:
                                                        if "ndvi" in iv:
                                                            _total_px += iv["ndvi"].get("valid_pixels", 0)
                                                    _row["valid_pixels"] = _total_px
                                                    _row["source"] = "sentinel_hub_realtime"
                                                    _results.append(_row)

                                                    # ---- Step 4: Write back to PostgreSQL cache ----
                                                    try:
                                                        await conn.execute(
                                                            "INSERT INTO agri_indices_cache "
                                                            "(admin_level, admin_name, parent_name, week_start, "
                                                            "ndvi_mean, ndvi_std, evi_mean, evi_std, "
                                                            "ndwi_mean, ndwi_std, savi_mean, savi_std, "
                                                            "ndre_mean, ndre_std, ndbi_mean, ndbi_std, "
                                                            "valid_pixels) VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12, $13, $14, $15, $16, $17)",
                                                            _level,
                                                            _name,
                                                            _parent,
                                                            _date_from,
                                                            _row.get("ndvi_mean"), _row.get("ndvi_std"),
                                                            _row.get("evi_mean"), _row.get("evi_std"),
                                                            _row.get("ndwi_mean"), _row.get("ndwi_std"),
                                                            _row.get("savi_mean"), _row.get("savi_std"),
                                                            _row.get("ndre_mean"), _row.get("ndre_std"),
                                                            _row.get("ndbi_mean"), _row.get("ndbi_std"),
                                                            _total_px,
                                                        )
                                                    except Exception as _ce:
                                                        logger.warning("Cache write failed for %s: %s", _name, _ce)

                                                except Exception as _e:
                                                    _errors.append(f"{_name}: {str(_e)}")

                                    # Sort results by name for consistent output
                                    _results.sort(key=lambda r: r.get("name", ""))

                                    _source_desc = "cache" if not _miss_rows else (
                                        "sentinel_hub_realtime" if _cache_hits == 0
                                        else f"mixed ({_cache_hits} cached, {len(_miss_rows)} realtime)"
                                    )

                                    tool_result = {
                                        "status": "success",
                                        "source": _source_desc,
                                        "admin_level": _level,
                                        "date_range": f"{_date_from} to {_date_to}",
                                        "count": len(_results),
                                        "cache_hits": _cache_hits,
                                        "cache_misses": len(_miss_rows),
                                        "indices": list(_AGRI_INDICES),
                                        "note": (
                                            "Sentinel-2 L2A data with SCL cloud masking. "
                                            "Cache TTL: 7 days (satellite revisit ~5 days). "
                                            "NDVI 0.6-0.8=dense vegetation, 0.3-0.5=cropland, <0.1=bare. "
                                            "EVI less sensitive to atmosphere. "
                                            "NDWI <0=vegetation, >0=water. "
                                            "SAVI adjusts for soil. NDRE=nitrogen/chlorophyll. "
                                            "NDBI >0=built-up, <0=vegetation."
                                        ),
                                        "agri_indices": _results,
                                    }
                                    if _errors:
                                        tool_result["errors"] = _errors

                                    _pgc_id = await _ensure_rwanda_postgis_connection(
                                        conn, current_project_id, user_id,
                                    )
                                    if _pgc_id and _results:
                                        tool_result["postgis_connection_id"] = _pgc_id

                                        # --- Build PostGIS query with VALUES clause ---
                                        _pg_tbl_map = {
                                            "district": ("rwanda_district_boundaries", "district"),
                                            "sector": ("rwanda_sector_boundaries", "sector_name"),
                                            "cell": ("rwanda_cell_boundaries", "cell_name"),
                                        }
                                        _pg_tbl, _pg_col = _pg_tbl_map.get(_level, _pg_tbl_map["district"])

                                        _val_rows = []
                                        for _r in _results:
                                            _sn = _r["name"].replace("'", "''")
                                            _nv = _r.get("ndvi_mean") or 0
                                            _ev = _r.get("evi_mean") or 0
                                            _wv = _r.get("ndwi_mean") or 0
                                            _sv = _r.get("savi_mean") or 0
                                            _rv = _r.get("ndre_mean") or 0
                                            _bv = _r.get("ndbi_mean") or 0
                                            _val_rows.append(
                                                f"('{_sn}',{_nv},{_ev},{_wv},{_sv},{_rv},{_bv})"
                                            )

                                        _values_sql = ",".join(_val_rows)
                                        _postgis_query = (
                                            f"SELECT ROW_NUMBER() OVER() AS id, "
                                            f"d.{_pg_col} AS name, "
                                            f"v.ndvi, v.evi, v.ndwi, v.savi, v.ndre, v.ndbi, "
                                            f"d.geom "
                                            f"FROM {_pg_tbl} d "
                                            f"JOIN (VALUES {_values_sql}) "
                                            f"AS v(name,ndvi,evi,ndwi,savi,ndre,ndbi) "
                                            f"ON d.{_pg_col} = v.name"
                                        )

                                        # --- Create the layer directly (bypass Sage) ---
                                        _layer_id = generate_id(prefix="L")
                                        _layer_name = f"{_level.title()} Agri Indices"

                                        # Compute NDVI range for color ramp
                                        _nvals = [r.get("ndvi_mean", 0) for r in _results if r.get("ndvi_mean") is not None]
                                        _nmin = round(min(_nvals), 2) if _nvals else 0.0
                                        _nmax = round(max(_nvals), 2) if _nvals else 0.8
                                        _nmid1 = round(_nmin + (_nmax - _nmin) * 0.33, 2)
                                        _nmid2 = round(_nmin + (_nmax - _nmin) * 0.66, 2)

                                        # Metadata with deckgl_3d flag for 3D extrusion
                                        _meta = {"deckgl_3d": True}
                                        _attr_cols = ["name", "ndvi", "evi", "ndwi", "savi", "ndre", "ndbi"]

                                        # Rwanda bounds (approximate)
                                        _bounds = [28.86, -2.84, 30.90, -1.05]

                                        async with kue_ephemeral_action(
                                            conversation.id,
                                            "Creating agri indices layer...",
                                            update_style_json=True,
                                            bounds=_bounds,
                                        ):
                                            await conn.execute(
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
                                                _layer_id, user_id, _layer_name, "postgis",
                                                _pgc_id, _postgis_query, json.dumps(_meta),
                                                len(_results), _bounds, "multipolygon",
                                                map_id, _attr_cols,
                                            )

                                            # Build choropleth style (MapLibre — for 2D fallback + labels)
                                            # source-layer must match MVT_LAYER_NAME = "reprojectedfgb"
                                            _sl = "reprojectedfgb"
                                            _ml_layers = [
                                                {
                                                    "id": f"{_layer_id}-fill",
                                                    "type": "fill",
                                                    "source": _layer_id,
                                                    "source-layer": _sl,
                                                    "paint": {
                                                        "fill-color": [
                                                            "interpolate", ["linear"], ["get", "ndvi"],
                                                            _nmin, "#d73027",
                                                            _nmid1, "#fc8d59",
                                                            _nmid2, "#fee08b",
                                                            _nmax, "#1a9850",
                                                        ],
                                                        "fill-opacity": 0.85,
                                                    },
                                                },
                                                {
                                                    "id": f"{_layer_id}-outline",
                                                    "type": "line",
                                                    "source": _layer_id,
                                                    "source-layer": _sl,
                                                    "paint": {
                                                        "line-color": "#222222",
                                                        "line-width": 1.5,
                                                    },
                                                },
                                                {
                                                    "id": f"{_layer_id}-label",
                                                    "type": "symbol",
                                                    "source": _layer_id,
                                                    "source-layer": _sl,
                                                    "layout": {
                                                        "text-field": [
                                                            "concat",
                                                            ["get", "name"], "\n",
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

                                            _style_id = generate_id(prefix="S")
                                            await conn.execute(
                                                """
                                                INSERT INTO layer_styles
                                                (style_id, layer_id, style_json, created_by, created_on)
                                                VALUES ($1, $2, $3, $4, CURRENT_TIMESTAMP)
                                                """,
                                                _style_id, _layer_id,
                                                json.dumps(_ml_layers), user_id,
                                            )

                                            await conn.execute(
                                                """
                                                INSERT INTO map_layer_styles (map_id, layer_id, style_id)
                                                VALUES ($1, $2, $3)
                                                """,
                                                map_id, _layer_id, _style_id,
                                            )

                                            # Add layer to map
                                            await conn.execute(
                                                """
                                                UPDATE user_mundiai_maps
                                                SET layers = CASE
                                                    WHEN layers IS NULL THEN ARRAY[$1]
                                                    ELSE array_append(layers, $1)
                                                END
                                                WHERE id = $2 AND (layers IS NULL OR NOT ($1 = ANY(layers)))
                                                """,
                                                _layer_id, map_id,
                                            )

                                        tool_result["layer_id"] = _layer_id
                                        tool_result["kue_instructions"] = (
                                            f"The layer '{_layer_name}' (ID: {_layer_id}) has been created and "
                                            f"added to the map with a Red→Green choropleth (NDVI) and 3D extrusion. "
                                            f"Do NOT call new_layer_from_postgis or set_layer_style — it is already done.\n\n"
                                            f"Describe the results to the user: which {_level}s have the highest NDVI "
                                            f"(greenest, healthiest vegetation) and which have the lowest (stressed). "
                                            f"Mention the 3D extrusion where taller = higher NDVI. "
                                            f"Highlight any notable patterns or outliers."
                                        )
                            except Exception as e:
                                logger.exception("get_agri_indices tool failed")
                                tool_result = {"status": "error", "error": str(e)}

                            await add_chat_completion_message(
                                ChatCompletionToolMessageParam(
                                    role="tool",
                                    tool_call_id=tool_call.id,
                                    content=json.dumps(tool_result),
                                )
                            )

                        elif function_name == "query_worldcover_stats":
                            try:
                                _wc_query_type = tool_args.get("query_type", "land_cover")
                                _wc_district = tool_args.get("district")
                                _wc_sector = tool_args.get("sector")
                                _wc_cell = tool_args.get("cell")
                                _wc_bbox = tool_args.get("bbox")  # [west, south, east, north]
                                _wc_lat = tool_args.get("lat")
                                _wc_lon = tool_args.get("lon")
                                _wc_limit = tool_args.get("limit", 10)

                                # Reverse-geocode lat/lon to admin boundary if no
                                # explicit district/sector/cell or bbox was provided
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
                                            # Try cell first (most specific), then sector, then district
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
                                                # Fall back to district lookup
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
                                    # On-the-fly connected-component analysis
                                    # for the SPECIFIC boundary the user asks about.

                                    import asyncpg as _asyncpg
                                    import numpy as _np
                                    from rasterio.merge import merge as _rio_merge
                                    from rasterio.features import geometry_mask as _geo_mask
                                    from scipy.ndimage import label as _scipy_label

                                    _PIXEL_HA = 0.01  # 10m x 10m = 0.01 ha

                                    # Determine admin level: most specific wins
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
                                        tool_result = {
                                            "status": "error",
                                            "error": "Please specify a district, sector, cell, or bbox for cropland analysis.",
                                        }
                                        await add_chat_completion_message(
                                            ChatCompletionToolMessageParam(
                                                role="tool",
                                                tool_call_id=tool_call.id,
                                                content=json.dumps(tool_result),
                                            )
                                        )
                                        continue

                                    # For bbox, build geometry directly; for admin, look up from PostGIS
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
                                        # Look up boundary geometry + bbox from PostGIS
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
                                            tool_result = {
                                                "status": "error",
                                                "error": f"Boundary not found: {_wc_cell or _wc_sector or _wc_district}",
                                            }
                                            await add_chat_completion_message(
                                                ChatCompletionToolMessageParam(
                                                    role="tool",
                                                    tool_call_id=tool_call.id,
                                                    content=json.dumps(tool_result),
                                                )
                                            )
                                            continue

                                        _boundary_name = _brow[0]
                                        _geom = json.loads(_brow[3])
                                        _bbox = (_brow[4], _brow[5], _brow[6], _brow[7])

                                    # Open ESRI LULC COGs via WarpedVRT (UTM -> EPSG:4326)
                                    from src.worldcover import open_rwanda_datasets_warped as _open_warped
                                    from src.worldcover import CROPLAND_CLASS as _CROP_CLS

                                    _wc_pairs = []
                                    _wc_datasets = []
                                    try:
                                        _wc_pairs = _open_warped()
                                        _wc_datasets = [vrt for vrt, _ds in _wc_pairs]

                                        _buf = 0.001
                                        _bounds = (
                                            _bbox[0] - _buf, _bbox[1] - _buf,
                                            _bbox[2] + _buf, _bbox[3] + _buf,
                                        )
                                        _arr, _tfm = _rio_merge(_wc_datasets, bounds=_bounds)
                                        _data = _arr[0]  # single band
                                        _h, _w = _data.shape

                                        # Mask to boundary geometry
                                        _mask = _geo_mask(
                                            [_geom], out_shape=(_h, _w),
                                            transform=_tfm, invert=True,
                                        )

                                        # Extract cropland pixels inside boundary
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

                                        tool_result = {
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
                                else:
                                    # land_cover: area breakdown by class
                                    if _wc_bbox and isinstance(_wc_bbox, list) and len(_wc_bbox) == 4:
                                        # On-the-fly zonal stats for bbox

                                        import numpy as _np
                                        from rasterio.merge import merge as _rio_merge
                                        from rasterio.features import geometry_mask as _geo_mask
                                        from src.worldcover import open_rwanda_datasets_warped as _open_warped
                                        from src.worldcover import CLASS_NAMES as _CLASS_NAMES

                                        _PIXEL_HA = 0.01  # 10m x 10m = 0.01 ha
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

                                            tool_result = {
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
                                        # Pre-computed admin stats from PostgreSQL
                                        _sql = "SELECT admin_level, admin_name, district_name, class_name, area_hectares FROM worldcover_admin_stats"
                                        _where = []
                                        _params = []
                                        _pidx = 1

                                        # Filter by most specific admin level provided
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
                                            # Default: district-level summary
                                            _where.append("admin_level = 'district'")

                                        if _where:
                                            _sql += " WHERE " + " AND ".join(_where)
                                        _sql += " ORDER BY area_hectares DESC"
                                        _rows = await conn.fetch(_sql, *_params)

                                        if _rows:
                                            tool_result = {
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
                                        else:
                                            tool_result = {
                                                "status": "success",
                                                "query_type": "land_cover",
                                                "count": 0,
                                                "data": [],
                                                "note": "No data yet. Run the worldcover_zonal_stats Dagster asset first.",
                                            }

                            except Exception as e:
                                logger.exception("query_worldcover_stats tool failed")
                                tool_result = {"status": "error", "error": str(e)}

                            await add_chat_completion_message(
                                ChatCompletionToolMessageParam(
                                    role="tool",
                                    tool_call_id=tool_call.id,
                                    content=json.dumps(tool_result),
                                )
                            )

                        elif function_name == "get_crop_classifications":
                            try:
                                _district = tool_args.get("district")
                                _cc_lat = tool_args.get("lat")
                                _cc_lon = tool_args.get("lon")

                                # Reverse-geocode lat/lon to district if not explicitly provided
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
                                    _rows = await conn.fetch(
                                        "SELECT district, class_label, area_ha, pixel_count, confidence, job_id "
                                        "FROM crop_classification_cache WHERE district = $1 "
                                        "ORDER BY computed_at DESC LIMIT 50",
                                        _district,
                                    )
                                else:
                                    _rows = await conn.fetch(
                                        "SELECT district, class_label, area_ha, pixel_count, confidence, job_id "
                                        "FROM crop_classification_cache ORDER BY computed_at DESC LIMIT 50"
                                    )

                                if _rows:
                                    tool_result = {
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
                                        conn, current_project_id, user_id,
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
                                else:
                                    tool_result = {
                                        "status": "success",
                                        "source": "postgres_cache",
                                        "classifications": [],
                                        "message": "No classification data yet — Dagster weekly schedule populates this cache",
                                    }
                            except Exception as e:
                                logger.exception("get_crop_classifications tool failed")
                                tool_result = {"status": "error", "error": str(e)}

                            await add_chat_completion_message(
                                ChatCompletionToolMessageParam(
                                    role="tool",
                                    tool_call_id=tool_call.id,
                                    content=json.dumps(tool_result),
                                )
                            )

                        elif function_name == "get_anomaly_alerts":
                            try:
                                _where = []
                                _params = []
                                _pidx = 1
                                if tool_args.get("severity"):
                                    _where.append(f"severity = ${_pidx}")
                                    _params.append(tool_args["severity"])
                                    _pidx += 1
                                if tool_args.get("district"):
                                    _where.append(f"district = ${_pidx}")
                                    _params.append(tool_args["district"])
                                    _pidx += 1
                                _where_sql = f"WHERE {' AND '.join(_where)}" if _where else ""
                                _rows = await conn.fetch(
                                    f"SELECT district, anomaly_date, observed_ndvi, expected_ndvi, "
                                    f"z_score, severity FROM anomaly_alerts_cache {_where_sql} "
                                    f"ORDER BY z_score ASC LIMIT 30",
                                    *_params,
                                )

                                if _rows:
                                    tool_result = {
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
                                    # Auto-provision PostGIS connection so Sage can create map layers
                                    _pgc_id = await _ensure_rwanda_postgis_connection(
                                        conn, current_project_id, user_id,
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
                                else:
                                    tool_result = {
                                        "status": "success",
                                        "source": "postgres_cache",
                                        "alerts": [],
                                        "message": "No anomaly alerts yet — Dagster weekly schedule populates this cache",
                                    }
                            except Exception as e:
                                logger.exception("get_anomaly_alerts tool failed")
                                tool_result = {"status": "error", "error": str(e)}

                            await add_chat_completion_message(
                                ChatCompletionToolMessageParam(
                                    role="tool",
                                    tool_call_id=tool_call.id,
                                    content=json.dumps(tool_result),
                                )
                            )

                        elif function_name == "get_yield_risk":
                            try:
                                _district = tool_args.get("district")
                                _where = "WHERE district = $1" if _district else ""
                                _params = [_district] if _district else []
                                _rows = await conn.fetch(
                                    f"SELECT district, risk_level, risk_description, trend_slope, "
                                    f"kendall_tau, latest_ndvi, mean_ndvi, seasonal_deviation, observations "
                                    f"FROM yield_risk_cache {_where} "
                                    f"ORDER BY computed_at DESC LIMIT 50",
                                    *_params,
                                )

                                if _rows:
                                    tool_result = {
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
                                        conn, current_project_id, user_id,
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
                                else:
                                    tool_result = {
                                        "status": "success",
                                        "source": "postgres_cache",
                                        "assessments": [],
                                        "message": "No yield risk data yet — Dagster weekly schedule populates this cache",
                                    }
                            except Exception as e:
                                logger.exception("get_yield_risk tool failed")
                                tool_result = {"status": "error", "error": str(e)}

                            await add_chat_completion_message(
                                ChatCompletionToolMessageParam(
                                    role="tool",
                                    tool_call_id=tool_call.id,
                                    content=json.dumps(tool_result),
                                )
                            )

                        elif function_name == "get_drought_status":
                            try:
                                _where = []
                                _params = []
                                _pidx = 1
                                if tool_args.get("district"):
                                    _where.append(f"district = ${_pidx}")
                                    _params.append(tool_args["district"])
                                    _pidx += 1
                                if tool_args.get("status"):
                                    _where.append(f"drought_status = ${_pidx}")
                                    _params.append(tool_args["status"])
                                    _pidx += 1
                                _where_sql = f"WHERE {' AND '.join(_where)}" if _where else ""
                                _rows = await conn.fetch(
                                    f"SELECT district, drought_status, current_vci, latest_ndvi, "
                                    f"latest_ndwi, drought_period_count, description "
                                    f"FROM drought_cache {_where_sql} "
                                    f"ORDER BY current_vci ASC LIMIT 50",
                                    *_params,
                                )

                                if _rows:
                                    _districts_out = []
                                    for r in _rows:
                                        _d = {
                                            "district": r["district"],
                                            "drought_status": r["drought_status"],
                                            "vci": r["current_vci"],
                                            "latest_ndvi": r["latest_ndvi"],
                                            "latest_ndwi": r["latest_ndwi"],
                                            "drought_period_count": r["drought_period_count"],
                                            "description": r["description"],
                                        }
                                        # Flag insufficient_data so the LLM doesn't
                                        # fabricate a drought claim
                                        if r["drought_status"] == "insufficient_data":
                                            _d["note"] = (
                                                "Not enough historical data to assess "
                                                "drought for this district yet."
                                            )
                                        _districts_out.append(_d)
                                    tool_result = {
                                        "status": "success",
                                        "source": "postgres_cache",
                                        "count": len(_rows),
                                        "districts": _districts_out,
                                    }
                                    # If ALL districts have insufficient data, add a
                                    # top-level note so the LLM knows not to claim drought
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
                                        conn, current_project_id, user_id,
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
                                else:
                                    # ── STAC COG real-time fallback ──
                                    # Cache is empty — compute drought from Sentinel-2 COGs
                                    try:
                                        from src.services.stac_service import get_stac_service as _get_stac

                                        _stac = _get_stac()
                                        _drought_district = tool_args.get("district")

                                        # Get district bboxes from PostGIS
                                        if _drought_district:
                                            _bbox_rows = await conn.fetch(
                                                "SELECT district, bbox_west, bbox_south, bbox_east, bbox_north "
                                                "FROM rwanda_district_boundaries WHERE LOWER(district) = LOWER($1)",
                                                _drought_district,
                                            )
                                        else:
                                            _bbox_rows = await conn.fetch(
                                                "SELECT district, bbox_west, bbox_south, bbox_east, bbox_north "
                                                "FROM rwanda_district_boundaries ORDER BY district"
                                            )

                                        _stac_districts = []
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
                                            # Limit to 3 districts for real-time (each takes ~60-80s)
                                            if not _drought_district and len(_stac_districts) >= 3:
                                                break

                                        if _stac_districts:
                                            # Check if all STAC results lack sufficient data
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
                                                conn, current_project_id, user_id,
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
                                        else:
                                            tool_result = {
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
                                        tool_result = {
                                            "status": "success",
                                            "source": "postgres_cache",
                                            "districts": [],
                                            "message": "No drought data yet — Dagster weekly schedule populates this cache",
                                        }
                            except Exception as e:
                                logger.exception("get_drought_status tool failed")
                                tool_result = {"status": "error", "error": str(e)}

                            await add_chat_completion_message(
                                ChatCompletionToolMessageParam(
                                    role="tool",
                                    tool_call_id=tool_call.id,
                                    content=json.dumps(tool_result),
                                )
                            )

                        elif function_name == "get_crop_growth_stage":
                            try:
                                _where = []
                                _params = []
                                _pidx = 1
                                if tool_args.get("district"):
                                    _where.append(f"district = ${_pidx}")
                                    _params.append(tool_args["district"])
                                    _pidx += 1
                                if tool_args.get("stage"):
                                    _where.append(f"current_stage = ${_pidx}")
                                    _params.append(tool_args["stage"])
                                    _pidx += 1
                                _where_sql = f"WHERE {' AND '.join(_where)}" if _where else ""
                                _rows = await conn.fetch(
                                    f"SELECT district, current_stage, peak_ndvi, peak_date, "
                                    f"green_up_start, senescence_start, harvest_date, observations "
                                    f"FROM phenology_cache {_where_sql} "
                                    f"ORDER BY computed_at DESC LIMIT 50",
                                    *_params,
                                )

                                if _rows:
                                    tool_result = {
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
                                        conn, current_project_id, user_id,
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
                                else:
                                    tool_result = {
                                        "status": "success",
                                        "source": "postgres_cache",
                                        "districts": [],
                                        "message": "No phenology data yet — Dagster weekly schedule populates this cache",
                                    }
                            except Exception as e:
                                logger.exception("get_crop_growth_stage tool failed")
                                tool_result = {"status": "error", "error": str(e)}

                            await add_chat_completion_message(
                                ChatCompletionToolMessageParam(
                                    role="tool",
                                    tool_call_id=tool_call.id,
                                    content=json.dumps(tool_result),
                                )
                            )

                        elif function_name == "get_weather_stats":
                            try:
                                from datetime import date as _date, timedelta as _td

                                # ── 1. Query AgERA5 cache (PostgreSQL) ──
                                _agera5_rows: list = []
                                try:
                                    _where = []
                                    _params: list = []
                                    _pidx = 1
                                    if tool_args.get("district"):
                                        _where.append(f"district = ${_pidx}")
                                        _params.append(tool_args["district"])
                                        _pidx += 1
                                    if tool_args.get("date_from"):
                                        _where.append(f"observation_date >= ${_pidx}")
                                        _params.append(tool_args["date_from"])
                                        _pidx += 1
                                    if tool_args.get("date_to"):
                                        _where.append(f"observation_date <= ${_pidx}")
                                        _params.append(tool_args["date_to"])
                                        _pidx += 1
                                    if not tool_args.get("date_from") and not tool_args.get("date_to"):
                                        _where.append("observation_date >= CURRENT_DATE - INTERVAL '30 days'")
                                    _where_sql = f"WHERE {' AND '.join(_where)}" if _where else ""
                                    _agera5_rows = await conn.fetch(
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
                                _agera5_dates: set = set()
                                _weather_stats: list = []
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
                                _openmeteo_stats: list = []
                                try:
                                    from src.services.weather_service import get_weather_service as _get_ws

                                    # Get district centroids from PostGIS
                                    _centroids: list = []
                                    async with conn.transaction():
                                        _cent_rows = await conn.fetch(
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
                                            # Filter by user's district/date args and exclude dates we have from AgERA5
                                            _filter_district = tool_args.get("district")
                                            _filter_from = tool_args.get("date_from")
                                            _filter_to = tool_args.get("date_to")
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
                                # Limit total results
                                _all_stats = _all_stats[:300]

                                if _all_stats:
                                    _sources = set(s.get("source", "agera5") for s in _all_stats)
                                    _source_str = " + ".join(sorted(_sources))
                                    tool_result = {
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
                                        conn, current_project_id, user_id,
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
                                else:
                                    tool_result = {
                                        "status": "success",
                                        "weather_stats": [],
                                        "message": (
                                            "No weather data available. DuckDB cache is empty and real-time weather "
                                            "fetch did not return results. Check network connectivity."
                                        ),
                                    }
                            except Exception as e:
                                logger.exception("get_weather_stats tool failed")
                                tool_result = {"status": "error", "error": str(e)}

                            await add_chat_completion_message(
                                ChatCompletionToolMessageParam(
                                    role="tool",
                                    tool_call_id=tool_call.id,
                                    content=json.dumps(tool_result),
                                )
                            )

                        elif function_name == "get_forecast":
                            try:
                                from src.services.forecast_service import get_farm_forecast

                                _fc_lat = tool_args.get("latitude")
                                _fc_lon = tool_args.get("longitude")
                                _fc_district = tool_args.get("district")
                                _fc_days = min(max(1, tool_args.get("forecast_days", 10)), 16)

                                # If district provided but no lat/lon, look up centroid
                                if _fc_district and (_fc_lat is None or _fc_lon is None):
                                    try:
                                        _fc_row = await conn.fetchrow(
                                            "SELECT round(ST_Y(ST_Centroid(geom))::numeric, 4) as lat, "
                                            "round(ST_X(ST_Centroid(geom))::numeric, 4) as lon "
                                            "FROM rwanda_district_boundaries "
                                            "WHERE district ILIKE $1 LIMIT 1",
                                            _fc_district,
                                        )
                                        if _fc_row:
                                            _fc_lat = float(_fc_row["lat"])
                                            _fc_lon = float(_fc_row["lon"])
                                    except Exception:
                                        pass

                                if _fc_lat is None or _fc_lon is None:
                                    # Default to Kigali
                                    _fc_lat = _fc_lat if _fc_lat is not None else -1.9403
                                    _fc_lon = _fc_lon if _fc_lon is not None else 29.8739

                                import asyncio as _aio
                                _fc_result = await _aio.get_event_loop().run_in_executor(
                                    None,
                                    lambda: get_farm_forecast(
                                        _fc_lat, _fc_lon,
                                        forecast_days=_fc_days,
                                    ),
                                )
                                tool_result = {
                                    "status": "success",
                                    **_fc_result,
                                }
                            except Exception as e:
                                logger.exception("get_forecast tool failed")
                                tool_result = {"status": "error", "error": str(e)}

                            await add_chat_completion_message(
                                ChatCompletionToolMessageParam(
                                    role="tool",
                                    tool_call_id=tool_call.id,
                                    content=json.dumps(tool_result),
                                )
                            )

                        elif function_name == "get_forecast_accuracy":
                            try:
                                import asyncio as _aio2
                                from src.services.forecast_service import get_farm_forecast

                                _acc_district = tool_args.get("district")

                                # Get district centroids
                                if _acc_district:
                                    _acc_rows = await conn.fetch(
                                        "SELECT district, "
                                        "round(ST_Y(ST_Centroid(geom))::numeric, 4) as lat, "
                                        "round(ST_X(ST_Centroid(geom))::numeric, 4) as lon "
                                        "FROM rwanda_district_boundaries WHERE district ILIKE $1",
                                        _acc_district,
                                    )
                                else:
                                    _acc_rows = await conn.fetch(
                                        "SELECT district, "
                                        "round(ST_Y(ST_Centroid(geom))::numeric, 4) as lat, "
                                        "round(ST_X(ST_Centroid(geom))::numeric, 4) as lon "
                                        "FROM rwanda_district_boundaries ORDER BY district"
                                    )

                                # Get observed weather from AgERA5 cache (last 5 days)
                                _obs_rows = await conn.fetch(
                                    "SELECT district, observation_date, temperature_mean, "
                                    "temperature_max, temperature_min, precipitation "
                                    "FROM weather_daily_cache "
                                    "WHERE observation_date >= CURRENT_DATE - INTERVAL '5 days' "
                                    "ORDER BY observation_date DESC, district"
                                )

                                # Build observed lookup: {(district, date) -> row}
                                _obs_lookup = {}
                                for r in _obs_rows:
                                    key = (r["district"], str(r["observation_date"]))
                                    _obs_lookup[key] = {
                                        "temp_mean": float(r["temperature_mean"]) if r["temperature_mean"] else None,
                                        "temp_max": float(r["temperature_max"]) if r["temperature_max"] else None,
                                        "temp_min": float(r["temperature_min"]) if r["temperature_min"] else None,
                                        "precip": float(r["precipitation"]) if r["precipitation"] else None,
                                    }

                                _model_errors = {"temp_errors": [], "precip_errors": [], "comparisons": []}

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
                                tool_result = {
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
                                tool_result = {"status": "error", "error": str(e)}

                            await add_chat_completion_message(
                                ChatCompletionToolMessageParam(
                                    role="tool",
                                    tool_call_id=tool_call.id,
                                    content=json.dumps(tool_result),
                                )
                            )

                        elif function_name == "get_emissions_stats":
                            try:
                                # ── Query emissions_annual_cache (PostgreSQL) ──
                                _em_where: list = []
                                _em_params: list = []
                                _em_pidx = 1
                                if tool_args.get("district"):
                                    _em_where.append(f"district = ${_em_pidx}")
                                    _em_params.append(tool_args["district"])
                                    _em_pidx += 1
                                if tool_args.get("year"):
                                    _em_where.append(f"year = ${_em_pidx}")
                                    _em_params.append(int(tool_args["year"]))
                                    _em_pidx += 1
                                if tool_args.get("year_from"):
                                    _em_where.append(f"year >= ${_em_pidx}")
                                    _em_params.append(int(tool_args["year_from"]))
                                    _em_pidx += 1
                                if tool_args.get("year_to"):
                                    _em_where.append(f"year <= ${_em_pidx}")
                                    _em_params.append(int(tool_args["year_to"]))
                                    _em_pidx += 1
                                if tool_args.get("emission_type"):
                                    _em_where.append(f"emission_type = ${_em_pidx}")
                                    _em_params.append(tool_args["emission_type"])
                                    _em_pidx += 1
                                if tool_args.get("sector"):
                                    _em_where.append(f"sector = ${_em_pidx}")
                                    _em_params.append(tool_args["sector"])
                                    _em_pidx += 1
                                if not tool_args.get("year") and not tool_args.get("year_from") and not tool_args.get("year_to"):
                                    _em_where.append("year >= EXTRACT(YEAR FROM CURRENT_DATE)::int - 6")

                                _em_where_sql = f"WHERE {' AND '.join(_em_where)}" if _em_where else ""
                                _em_rows = await conn.fetch(
                                    f"SELECT district, year, emission_type, sector, "
                                    f"sector_label, total_tonnes, grid_cells "
                                    f"FROM emissions_annual_cache {_em_where_sql} "
                                    f"ORDER BY year DESC, district, emission_type, sector "
                                    f"LIMIT 500",
                                    *_em_params,
                                )

                                _emissions_stats: list = []
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
                                    tool_result = {
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
                                        conn, current_project_id, user_id,
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
                                else:
                                    tool_result = {
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
                                tool_result = {"status": "error", "error": str(e)}

                            await add_chat_completion_message(
                                ChatCompletionToolMessageParam(
                                    role="tool",
                                    tool_call_id=tool_call.id,
                                    content=json.dumps(tool_result),
                                )
                            )

                        elif function_name == "add_land_cover_layer":
                            # Add ESRI 10m LULC 2024 land cover as a raster overlay
                            try:
                                _wc_mode = tool_args.get("mode", "all")
                                if _wc_mode not in ("all", "cropland"):
                                    _wc_mode = "all"

                                _layer_id = generate_id(prefix="L")
                                _style_id = generate_id(prefix="S")

                                # Admin boundary clipping
                                _wc_district = tool_args.get("district")
                                _wc_sector = tool_args.get("sector")
                                _wc_cell = tool_args.get("cell")
                                _wc_bbox = tool_args.get("bbox")  # [west, south, east, north]
                                _wc_lat = tool_args.get("lat")
                                _wc_lon = tool_args.get("lon")

                                # Reverse-geocode lat/lon → admin boundary if no
                                # explicit district/sector/cell or bbox was provided
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

                                _wc_meta = {
                                    "worldcover": True,
                                    "worldcover_mode": _wc_mode,
                                }
                                # Store admin clip context so map_service
                                # includes it in tile URLs
                                if _wc_district:
                                    _wc_meta["clip_district"] = _wc_district
                                if _wc_sector:
                                    _wc_meta["clip_sector"] = _wc_sector
                                if _wc_cell:
                                    _wc_meta["clip_cell"] = _wc_cell
                                if _wc_bbox and isinstance(_wc_bbox, list) and len(_wc_bbox) == 4:
                                    _wc_meta["clip_bbox"] = _wc_bbox
                                _meta = json.dumps(_wc_meta)

                                # Use admin bbox, explicit bbox, or fall back to Rwanda
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
                                        pass  # Fall back to Rwanda bounds
                                elif _wc_bbox and isinstance(_wc_bbox, list) and len(_wc_bbox) == 4:
                                    _bounds = _wc_bbox

                                async with kue_ephemeral_action(
                                    conversation.id,
                                    f"Adding {_layer_name} layer...",
                                    update_style_json=True,
                                    bounds=_bounds,
                                ):
                                    # Insert raster layer record
                                    await conn.execute(
                                        """
                                        INSERT INTO map_layers
                                        (layer_id, owner_uuid, name, type,
                                         metadata, bounds, source_map_id,
                                         created_on, last_edited)
                                        VALUES ($1, $2, $3, 'raster',
                                                $4, $5, $6,
                                                CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)
                                        """,
                                        _layer_id, user_id, _layer_name,
                                        _meta, _bounds, map_id,
                                    )

                                    # Empty style — rendering is handled by tile endpoint
                                    await conn.execute(
                                        """
                                        INSERT INTO layer_styles
                                        (style_id, layer_id, style_json, created_by, created_on)
                                        VALUES ($1, $2, $3, $4, CURRENT_TIMESTAMP)
                                        """,
                                        _style_id, _layer_id, "[]", user_id,
                                    )

                                    await conn.execute(
                                        """
                                        INSERT INTO map_layer_styles (map_id, layer_id, style_id)
                                        VALUES ($1, $2, $3)
                                        """,
                                        map_id, _layer_id, _style_id,
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
                                        _layer_id, map_id,
                                    )

                                _class_desc = (
                                    "Cropland highlighted in green, other land cover muted"
                                    if _wc_mode == "cropland"
                                    else "All 9 ESRI land cover classes: water, trees, flooded vegetation, crops, built area, bare ground, snow/ice, clouds, rangeland"
                                )

                                tool_result = {
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
                                tool_result = {"status": "error", "error": str(e)}

                            await add_chat_completion_message(
                                ChatCompletionToolMessageParam(
                                    role="tool",
                                    tool_call_id=tool_call.id,
                                    content=json.dumps(tool_result),
                                )
                            )

                        elif function_name == "get_soil_moisture":
                            try:
                                from datetime import date as _dt_date_sm
                                from src.services.wapor_service import query_soil_moisture

                                _sm_lat = tool_args.get("latitude")
                                _sm_lon = tool_args.get("longitude")
                                _sm_from = None
                                _sm_to = None
                                if tool_args.get("date_from"):
                                    _sm_from = _dt_date_sm.fromisoformat(tool_args["date_from"])
                                if tool_args.get("date_to"):
                                    _sm_to = _dt_date_sm.fromisoformat(tool_args["date_to"])

                                if _sm_lat is None or _sm_lon is None:
                                    tool_result = {"status": "error", "error": "latitude and longitude are required"}
                                else:
                                    import asyncio as _aio_sm
                                    tool_result = await _aio_sm.get_event_loop().run_in_executor(
                                        None,
                                        lambda: query_soil_moisture(
                                            lat=float(_sm_lat),
                                            lon=float(_sm_lon),
                                            date_from=_sm_from,
                                            date_to=_sm_to,
                                        ),
                                    )
                            except Exception as e:
                                logger.exception("get_soil_moisture tool failed")
                                tool_result = {"status": "error", "error": str(e)}

                            await add_chat_completion_message(
                                ChatCompletionToolMessageParam(
                                    role="tool",
                                    tool_call_id=tool_call.id,
                                    content=json.dumps(tool_result),
                                )
                            )

                        elif function_name == "get_evapotranspiration":
                            try:
                                from datetime import date as _dt_date
                                from src.services.wapor_service import query_et

                                _et_lat = tool_args.get("latitude")
                                _et_lon = tool_args.get("longitude")
                                _et_from = None
                                _et_to = None
                                if tool_args.get("date_from"):
                                    _et_from = _dt_date.fromisoformat(tool_args["date_from"])
                                if tool_args.get("date_to"):
                                    _et_to = _dt_date.fromisoformat(tool_args["date_to"])

                                if _et_lat is None or _et_lon is None:
                                    tool_result = {"status": "error", "error": "latitude and longitude are required"}
                                else:
                                    import asyncio as _aio_et
                                    tool_result = await _aio_et.get_event_loop().run_in_executor(
                                        None,
                                        lambda: query_et(
                                            lat=float(_et_lat),
                                            lon=float(_et_lon),
                                            date_from=_et_from,
                                            date_to=_et_to,
                                            include_components=bool(tool_args.get("include_components", False)),
                                        ),
                                    )
                            except Exception as e:
                                logger.exception("get_evapotranspiration tool failed")
                                tool_result = {"status": "error", "error": str(e)}

                            await add_chat_completion_message(
                                ChatCompletionToolMessageParam(
                                    role="tool",
                                    tool_call_id=tool_call.id,
                                    content=json.dumps(tool_result),
                                )
                            )

                        elif function_name == "get_food_security_alerts":
                            try:
                                from src.services.fewsnet_service import get_food_security

                                import asyncio as _aio_fs
                                tool_result = await _aio_fs.get_event_loop().run_in_executor(
                                    None,
                                    lambda: get_food_security(
                                        district=tool_args.get("district"),
                                        period=tool_args.get("period", "current"),
                                    ),
                                )
                            except Exception as e:
                                logger.exception("get_food_security_alerts tool failed")
                                tool_result = {"status": "error", "error": str(e)}

                            await add_chat_completion_message(
                                ChatCompletionToolMessageParam(
                                    role="tool",
                                    tool_call_id=tool_call.id,
                                    content=json.dumps(tool_result),
                                )
                            )

                        elif function_name == "reverse_geocode_coordinates":
                            _rg_lat = tool_args.get("lat")
                            _rg_lon = tool_args.get("lon")

                            if _rg_lat is None or _rg_lon is None:
                                tool_result = {"status": "error", "error": "lat and lon are required"}
                            else:
                                # Province-to-district mapping (stable since 2006)
                                _DISTRICT_TO_PROVINCE = {
                                    "Gasabo": "Kigali City", "Kicukiro": "Kigali City", "Nyarugenge": "Kigali City",
                                    "Burera": "Northern", "Gakenke": "Northern", "Gicumbi": "Northern",
                                    "Musanze": "Northern", "Rulindo": "Northern",
                                    "Gisagara": "Southern", "Huye": "Southern", "Kamonyi": "Southern",
                                    "Muhanga": "Southern", "Nyamagabe": "Southern", "Nyanza": "Southern",
                                    "Nyaruguru": "Southern", "Ruhango": "Southern",
                                    "Bugesera": "Eastern", "Gatsibo": "Eastern", "Kayonza": "Eastern",
                                    "Kirehe": "Eastern", "Ngoma": "Eastern", "Nyagatare": "Eastern",
                                    "Rwamagana": "Eastern",
                                    "Karongi": "Western", "Ngororero": "Western", "Nyabihu": "Western",
                                    "Nyamasheke": "Western", "Rubavu": "Western", "Rusizi": "Western",
                                    "Rutsiro": "Western",
                                }
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
                                        _rg_result = {
                                            "province": None, "district": None,
                                            "sector": None, "cell": None, "village": None,
                                        }

                                        # Village (most specific)
                                        _row = await _pg_conn_rg.fetchrow(
                                            "SELECT village_name, cell_name, sector_name, district_name "
                                            "FROM rwanda_village_boundaries "
                                            "WHERE ST_Contains(geom, ST_SetSRID(ST_Point($1, $2), 4326)) "
                                            "LIMIT 1",
                                            float(_rg_lon), float(_rg_lat),
                                        )
                                        if _row:
                                            _rg_result["village"] = _row["village_name"]
                                            _rg_result["cell"] = _row["cell_name"]
                                            _rg_result["sector"] = _row["sector_name"]
                                            _rg_result["district"] = _row["district_name"]
                                        else:
                                            # Fall back to cell
                                            _row = await _pg_conn_rg.fetchrow(
                                                "SELECT cell_name, sector_name, district_name "
                                                "FROM rwanda_cell_boundaries "
                                                "WHERE ST_Contains(geom, ST_SetSRID(ST_Point($1, $2), 4326)) "
                                                "LIMIT 1",
                                                float(_rg_lon), float(_rg_lat),
                                            )
                                            if _row:
                                                _rg_result["cell"] = _row["cell_name"]
                                                _rg_result["sector"] = _row["sector_name"]
                                                _rg_result["district"] = _row["district_name"]
                                            else:
                                                # Fall back to sector
                                                _row = await _pg_conn_rg.fetchrow(
                                                    "SELECT sector_name, district_name "
                                                    "FROM rwanda_sector_boundaries "
                                                    "WHERE ST_Contains(geom, ST_SetSRID(ST_Point($1, $2), 4326)) "
                                                    "LIMIT 1",
                                                    float(_rg_lon), float(_rg_lat),
                                                )
                                                if _row:
                                                    _rg_result["sector"] = _row["sector_name"]
                                                    _rg_result["district"] = _row["district_name"]
                                                else:
                                                    # Fall back to district
                                                    _row = await _pg_conn_rg.fetchrow(
                                                        "SELECT district FROM rwanda_district_boundaries "
                                                        "WHERE ST_Contains(geom, ST_SetSRID(ST_Point($1, $2), 4326)) "
                                                        "LIMIT 1",
                                                        float(_rg_lon), float(_rg_lat),
                                                    )
                                                    if _row:
                                                        _rg_result["district"] = _row["district"]

                                        # Derive province from district
                                        if _rg_result["district"]:
                                            _rg_result["province"] = _DISTRICT_TO_PROVINCE.get(
                                                _rg_result["district"]
                                            )

                                        if _rg_result["district"]:
                                            tool_result = {
                                                "status": "success",
                                                "coordinates": {"lat": _rg_lat, "lon": _rg_lon},
                                                **_rg_result,
                                            }
                                        else:
                                            tool_result = {
                                                "status": "not_found",
                                                "error": f"Coordinates ({_rg_lat}, {_rg_lon}) are not within Rwanda boundaries.",
                                                "coordinates": {"lat": _rg_lat, "lon": _rg_lon},
                                            }
                                    finally:
                                        await _pg_conn_rg.close()
                                except Exception as _rg_err:
                                    logger.exception("reverse_geocode_coordinates failed")
                                    tool_result = {"status": "error", "error": str(_rg_err)}

                            await add_chat_completion_message(
                                ChatCompletionToolMessageParam(
                                    role="tool",
                                    tool_call_id=tool_call.id,
                                    content=json.dumps(tool_result),
                                )
                            )

                        elif function_name in geoprocessing_function_names:
                            tool_result = await run_geoprocessing_tool(
                                tool_call,
                                conn,
                                user_id,
                                map_id,
                                conversation.id,
                            )
                            await add_chat_completion_message(
                                ChatCompletionToolMessageParam(
                                    role="tool",
                                    tool_call_id=tool_call.id,
                                    content=json.dumps(tool_result),
                                ),
                            )
                        else:
                            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST)

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

    # Unlock the map when processing is complete
    try:
        redis.delete(f"chat_lock:{conversation.id}")
    except Exception:
        logger.debug("Redis unavailable for chat lock cleanup")


class MessageSendRequest(BaseModel):
    message: ChatCompletionUserMessageParam
    selected_feature: SelectedFeature | None


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
    logger.info("send_map_message called: conversation=%s map=%s", conversation.id, map_id)
    user_id = session.get_user_id()

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

    # Get system messages from the provider
    system_messages = await map_state.get_system_messages(
        current_messages, description_text, body.selected_feature
    )

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
        )
    else:
        background_tasks.add_task(
            process_chat_interaction_task,
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
