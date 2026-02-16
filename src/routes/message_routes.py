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
from src.postgis_tiles import MVT_LAYER_NAME

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

# DuckDB cache file populated by Dagster scheduled assets (rwanda_assets.py)
_DUCKDB_CACHE_PATH = "/tmp/ingabe_cache/cache.duckdb"

# Fixed connection ID for the internal Rwanda PostGIS connection
_RWANDA_INTERNAL_CONN_ID = "CRwandaIntDB"


async def _ensure_rwanda_postgis_connection(
    conn, project_id: str, user_id: str,
) -> str | None:
    """Auto-provision an internal PostGIS connection for Rwanda data.

    Creates a project_postgres_connections row pointing to the app's own
    database so Kue can use new_layer_from_postgis to create layers from
    rwanda_district_boundaries, rwanda_cell_boundaries, etc.

    Returns the connection ID, or None on failure.
    """
    try:
        existing = await conn.fetchval(
            "SELECT id FROM project_postgres_connections WHERE id = $1",
            _RWANDA_INTERNAL_CONN_ID,
        )
        if existing:
            return _RWANDA_INTERNAL_CONN_ID

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
    async with async_conn("get_all_conversation_messages") as conn:
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
                    return {
                        "status": "error",
                        "error": f"QGIS processing failed: {response.status_code} - {response.text}",
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
                        return {
                            "status": "error",
                            "error": f"QGIS processing completed but output file {param_name} was not uploaded successfully",
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
            span.set_status(trace.Status(trace.StatusCode.ERROR, str(e)))
            span.set_attribute("error.traceback", traceback.format_exc())
            return {
                "status": "error",
                "error": "Unexpected error running geoprocessing, this is likely a Mundi bug.",
                "algorithm_id": algorithm_id,
            }


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

            openai_messages = []
            for msg in updated_messages_response:
                m = msg.message_json
                if isinstance(m, dict):
                    m = {k: v for k, v in m.items()
                         if k not in _STRIP_NULL_FIELDS or (v is not None and v != [])}
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
            async with kue_ephemeral_action(conversation.id, "Kue is thinking..."):
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

            # If no tool calls, break
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
                                # Verify the PostGIS connection exists and user has access
                                connection_result = await conn.fetchrow(
                                    """
                                    SELECT connection_uri FROM project_postgres_connections
                                    WHERE id = $1 AND user_id = $2
                                    """,
                                    postgis_connection_id,
                                    user_id,
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
                                                    bounds_result = await pg.fetchrow(
                                                        f"""
                                                        WITH extent_data AS (
                                                            SELECT
                                                                ST_Extent(geom) as extent_geom,
                                                                (SELECT ST_SRID(geom) FROM ({query}) AS sub2 WHERE geom IS NOT NULL LIMIT 1) as original_srid
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
                                                        v is not None
                                                        for v in bounds_result
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
                                        except Exception as e:
                                            tool_result = {
                                                "status": "error",
                                                "error": f"Query validation failed: {str(e)}",
                                            }

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
                                    SELECT layer_id FROM map_layers
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
                                    tool_result = {
                                        "status": f"Layer '{new_name}' (ID: {layer_id_to_add}) added to map '{map_id}'.",
                                        "layer_id": layer_id_to_add,
                                        "name": new_name,
                                    }

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

                            if not postgis_connection_id or not sql_query:
                                tool_result = {
                                    "status": "error",
                                    "error": "Missing required parameters (postgis_connection_id or sql_query)",
                                }
                            else:
                                # Verify the PostGIS connection exists and user has access
                                connection_result = await conn.fetchrow(
                                    """
                                    SELECT connection_uri FROM project_postgres_connections
                                    WHERE id = $1 AND user_id = $2
                                    """,
                                    postgis_connection_id,
                                    user_id,
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
                                    result_data = await asyncio.get_event_loop().run_in_executor(
                                        None, lambda: sh_service.get_field_stats(
                                            geometry=tool_args.get("geometry"),
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

                        elif function_name == "get_ndvi_stats":
                            try:
                                import duckdb as _duckdb
                                from datetime import date as _date, timedelta as _td

                                # ── 1. Query DuckDB cache (populated by nightly Dagster job) ──
                                _cached_rows: list = []
                                try:
                                    _conn = _duckdb.connect(database=_DUCKDB_CACHE_PATH, read_only=False)
                                    _conn.execute(
                                        "CREATE TABLE IF NOT EXISTS ndvi_field_cache ("
                                        "district VARCHAR, week_start DATE, mean_ndvi DOUBLE, "
                                        "std_ndvi DOUBLE, min_ndvi DOUBLE, max_ndvi DOUBLE, "
                                        "valid_pixels INTEGER, computed_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP)"
                                    )
                                    _district = tool_args.get("district")
                                    if _district:
                                        _cached_rows = _conn.execute(
                                            "SELECT district, week_start, mean_ndvi, std_ndvi, min_ndvi, max_ndvi, valid_pixels "
                                            "FROM ndvi_field_cache WHERE district = ? "
                                            "ORDER BY week_start DESC LIMIT 50",
                                            [_district],
                                        ).fetchall()
                                    else:
                                        _cached_rows = _conn.execute(
                                            "SELECT district, week_start, mean_ndvi, std_ndvi, min_ndvi, max_ndvi, valid_pixels "
                                            "FROM ndvi_field_cache ORDER BY week_start DESC, district LIMIT 200"
                                        ).fetchall()
                                    _conn.close()
                                except Exception:
                                    logger.debug("DuckDB NDVI cache not available, will try real-time Sentinel Hub")

                                _ndvi_stats: list = []
                                _source = "duckdb_cache"
                                for r in _cached_rows:
                                    _ndvi_stats.append({
                                        "district": r[0], "week_start": str(r[1]) if r[1] else None,
                                        "mean_ndvi": round(r[2], 4) if r[2] else None,
                                        "std_ndvi": round(r[3], 4) if r[3] else None,
                                        "min_ndvi": round(r[4], 4) if r[4] else None,
                                        "max_ndvi": round(r[5], 4) if r[5] else None,
                                        "valid_pixels": r[6],
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

                                            _now = datetime.utcnow()
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
                                    tool_result = {
                                        "status": "success",
                                        "ndvi_stats": [],
                                        "message": (
                                            "No NDVI data available. DuckDB cache is empty and Sentinel Hub "
                                            "real-time query returned no results (possibly due to cloud cover). "
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
                                import duckdb as _duckdb

                                _conn = _duckdb.connect(database=_DUCKDB_CACHE_PATH, read_only=False)
                                _conn.execute(
                                    "CREATE TABLE IF NOT EXISTS ndvi_cell_cache ("
                                    "cell_name VARCHAR, district_name VARCHAR, week_start DATE, "
                                    "mean_ndvi DOUBLE, std_ndvi DOUBLE, min_ndvi DOUBLE, "
                                    "max_ndvi DOUBLE, valid_pixels INTEGER, "
                                    "computed_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP)"
                                )
                                _cell = tool_args.get("cell_name")
                                _district = tool_args.get("district")
                                _where = []
                                _params: list = []
                                if _cell:
                                    _where.append("cell_name ILIKE ?")
                                    _params.append(f"%{_cell}%")
                                if _district:
                                    _where.append("district_name ILIKE ?")
                                    _params.append(f"%{_district}%")
                                _where_sql = f"WHERE {' AND '.join(_where)}" if _where else ""
                                _rows = _conn.execute(
                                    f"SELECT cell_name, district_name, week_start, "
                                    f"mean_ndvi, std_ndvi, min_ndvi, max_ndvi, valid_pixels "
                                    f"FROM ndvi_cell_cache {_where_sql} "
                                    f"ORDER BY computed_at DESC LIMIT 100",
                                    _params,
                                ).fetchall()
                                _conn.close()

                                if _rows:
                                    tool_result = {
                                        "status": "success",
                                        "source": "duckdb_cache",
                                        "count": len(_rows),
                                        "cell_ndvi_stats": [
                                            {
                                                "cell_name": r[0],
                                                "district_name": r[1],
                                                "week_start": str(r[2]) if r[2] else None,
                                                "mean_ndvi": round(r[3], 4) if r[3] else None,
                                                "std_ndvi": round(r[4], 4) if r[4] else None,
                                                "min_ndvi": round(r[5], 4) if r[5] else None,
                                                "max_ndvi": round(r[6], 4) if r[6] else None,
                                                "valid_pixels": r[7],
                                            }
                                            for r in _rows
                                        ],
                                    }
                                    # Auto-provision PostGIS connection so Kue can create map layers
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

                        elif function_name == "get_parcel_ndvi_stats":
                            try:
                                import duckdb as _duckdb

                                _conn = _duckdb.connect(database=_DUCKDB_CACHE_PATH, read_only=False)
                                _conn.execute(
                                    "CREATE TABLE IF NOT EXISTS ndvi_parcel_cache ("
                                    "parcel_id VARCHAR, parcel_name VARCHAR, layer_id VARCHAR, "
                                    "week_start DATE, mean_ndvi DOUBLE, std_ndvi DOUBLE, "
                                    "min_ndvi DOUBLE, max_ndvi DOUBLE, valid_pixels INTEGER, "
                                    "area_ha DOUBLE, computed_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP)"
                                )
                                _parcel = tool_args.get("parcel_name")
                                _layer = tool_args.get("layer_id")
                                _where = []
                                _params_p: list = []
                                if _parcel:
                                    _where.append("parcel_name ILIKE ?")
                                    _params_p.append(f"%{_parcel}%")
                                if _layer:
                                    _where.append("layer_id = ?")
                                    _params_p.append(_layer)
                                _where_sql = f"WHERE {' AND '.join(_where)}" if _where else ""
                                _rows = _conn.execute(
                                    f"SELECT parcel_id, parcel_name, layer_id, week_start, "
                                    f"mean_ndvi, std_ndvi, min_ndvi, max_ndvi, valid_pixels, area_ha "
                                    f"FROM ndvi_parcel_cache {_where_sql} "
                                    f"ORDER BY computed_at DESC LIMIT 100",
                                    _params_p,
                                ).fetchall()
                                _conn.close()

                                if _rows:
                                    tool_result = {
                                        "status": "success",
                                        "source": "duckdb_cache",
                                        "count": len(_rows),
                                        "parcel_ndvi_stats": [
                                            {
                                                "parcel_id": r[0],
                                                "parcel_name": r[1],
                                                "layer_id": r[2],
                                                "week_start": str(r[3]) if r[3] else None,
                                                "mean_ndvi": round(r[4], 4) if r[4] else None,
                                                "std_ndvi": round(r[5], 4) if r[5] else None,
                                                "min_ndvi": round(r[6], 4) if r[6] else None,
                                                "max_ndvi": round(r[7], 4) if r[7] else None,
                                                "valid_pixels": r[8],
                                                "area_ha": r[9],
                                            }
                                            for r in _rows
                                        ],
                                    }
                                else:
                                    tool_result = {
                                        "status": "success",
                                        "source": "duckdb_cache",
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
                            # Cache-first multi-index query: DuckDB cache → Sentinel Hub on miss
                            try:
                                from src.services.sentinel_hub_service import (
                                    get_sentinel_hub_service as _get_sh,
                                    AGRI_INDEX_NAMES as _AGRI_INDICES,
                                )
                                import duckdb as _duckdb
                                import numpy as _np
                                from datetime import timedelta as _td

                                _CACHE_TTL_DAYS = 7  # Sentinel-2 revisit ~5 days

                                _level = tool_args.get("admin_level", "district")
                                _name_filter = tool_args.get("name")
                                _district_filter = tool_args.get("district")
                                _date_from = tool_args.get("date_from")
                                _date_to = tool_args.get("date_to")

                                if not _date_to:
                                    _date_to = datetime.utcnow().strftime("%Y-%m-%d")
                                if not _date_from:
                                    _date_from = (datetime.utcnow() - _td(days=7)).strftime("%Y-%m-%d")

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
                                    # ---- Step 1: Check DuckDB cache ----
                                    _cache_conn = _duckdb.connect(database=_DUCKDB_CACHE_PATH, read_only=False)
                                    _cache_conn.execute(
                                        "CREATE TABLE IF NOT EXISTS agri_indices_cache ("
                                        "admin_level VARCHAR NOT NULL, "
                                        "admin_name VARCHAR NOT NULL, "
                                        "parent_name VARCHAR, "
                                        "week_start DATE NOT NULL, "
                                        "ndvi_mean DOUBLE, ndvi_std DOUBLE, "
                                        "evi_mean DOUBLE, evi_std DOUBLE, "
                                        "ndwi_mean DOUBLE, ndwi_std DOUBLE, "
                                        "savi_mean DOUBLE, savi_std DOUBLE, "
                                        "ndre_mean DOUBLE, ndre_std DOUBLE, "
                                        "ndbi_mean DOUBLE, ndbi_std DOUBLE, "
                                        "valid_pixels INTEGER, "
                                        "computed_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP)"
                                    )

                                    _admin_names = [r["name"] for r in _admin_rows]
                                    _cutoff = (datetime.utcnow() - _td(days=_CACHE_TTL_DAYS)).strftime("%Y-%m-%d")

                                    # Query cache for fresh rows
                                    _placeholders = ", ".join(["?"] * len(_admin_names))
                                    _cached_rows = _cache_conn.execute(
                                        f"SELECT admin_name, parent_name, week_start, "
                                        f"ndvi_mean, ndvi_std, evi_mean, evi_std, "
                                        f"ndwi_mean, ndwi_std, savi_mean, savi_std, "
                                        f"ndre_mean, ndre_std, ndbi_mean, ndbi_std, "
                                        f"valid_pixels, computed_at "
                                        f"FROM agri_indices_cache "
                                        f"WHERE admin_level = ? "
                                        f"AND admin_name IN ({_placeholders}) "
                                        f"AND computed_at >= ? "
                                        f"ORDER BY computed_at DESC",
                                        [_level] + _admin_names + [_cutoff],
                                    ).fetchall()

                                    # Build set of cached names (dedup: keep most recent per name)
                                    _cached_by_name: dict = {}
                                    for _cr in _cached_rows:
                                        _cname = _cr[0]
                                        if _cname not in _cached_by_name:
                                            _cached_by_name[_cname] = {
                                                "admin_level": _level,
                                                "name": _cname,
                                                "district": _cr[1] if _cr[1] else None,
                                                "date_from": str(_cr[2]),
                                                "date_to": _date_to,
                                                "ndvi_mean": _cr[3], "ndvi_std": _cr[4],
                                                "evi_mean": _cr[5], "evi_std": _cr[6],
                                                "ndwi_mean": _cr[7], "ndwi_std": _cr[8],
                                                "savi_mean": _cr[9], "savi_std": _cr[10],
                                                "ndre_mean": _cr[11], "ndre_std": _cr[12],
                                                "ndbi_mean": _cr[13], "ndbi_std": _cr[14],
                                                "valid_pixels": _cr[15],
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

                                                    # ---- Step 4: Write back to DuckDB cache ----
                                                    try:
                                                        _cache_conn.execute(
                                                            "INSERT INTO agri_indices_cache "
                                                            "(admin_level, admin_name, parent_name, week_start, "
                                                            "ndvi_mean, ndvi_std, evi_mean, evi_std, "
                                                            "ndwi_mean, ndwi_std, savi_mean, savi_std, "
                                                            "ndre_mean, ndre_std, ndbi_mean, ndbi_std, "
                                                            "valid_pixels) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                                                            [
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
                                                            ],
                                                        )
                                                    except Exception as _ce:
                                                        logger.warning("Cache write failed for %s: %s", _name, _ce)

                                                except Exception as _e:
                                                    _errors.append(f"{_name}: {str(_e)}")

                                    _cache_conn.close()

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

                                        # --- Create the layer directly (bypass Kue) ---
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

                        elif function_name == "get_crop_classifications":
                            try:
                                import duckdb as _duckdb

                                _conn = _duckdb.connect(database=_DUCKDB_CACHE_PATH, read_only=False)
                                _conn.execute(
                                    "CREATE TABLE IF NOT EXISTS crop_classification_cache ("
                                    "district VARCHAR, class_label VARCHAR, area_ha DOUBLE, "
                                    "pixel_count INTEGER, confidence DOUBLE, job_id VARCHAR, "
                                    "computed_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP)"
                                )
                                _district = tool_args.get("district")
                                if _district:
                                    _rows = _conn.execute(
                                        "SELECT district, class_label, area_ha, pixel_count, confidence, job_id "
                                        "FROM crop_classification_cache WHERE district = ? "
                                        "ORDER BY computed_at DESC LIMIT 50",
                                        [_district],
                                    ).fetchall()
                                else:
                                    _rows = _conn.execute(
                                        "SELECT district, class_label, area_ha, pixel_count, confidence, job_id "
                                        "FROM crop_classification_cache ORDER BY computed_at DESC LIMIT 50"
                                    ).fetchall()
                                _conn.close()

                                if _rows:
                                    tool_result = {
                                        "status": "success",
                                        "source": "duckdb_cache",
                                        "count": len(_rows),
                                        "classifications": [
                                            {"district": r[0], "class_label": r[1], "area_ha": r[2],
                                             "pixel_count": r[3], "confidence": r[4], "job_id": r[5]}
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
                                        "source": "duckdb_cache",
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
                                import duckdb as _duckdb

                                _conn = _duckdb.connect(database=_DUCKDB_CACHE_PATH, read_only=False)
                                _conn.execute(
                                    "CREATE TABLE IF NOT EXISTS anomaly_alerts_cache ("
                                    "district VARCHAR, h3_index VARCHAR, parcel_id VARCHAR, "
                                    "anomaly_date DATE, observed_ndvi DOUBLE, expected_ndvi DOUBLE, "
                                    "z_score DOUBLE, severity VARCHAR, "
                                    "computed_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP)"
                                )
                                _where = []
                                _params = []
                                if tool_args.get("severity"):
                                    _where.append("severity = ?")
                                    _params.append(tool_args["severity"])
                                if tool_args.get("district"):
                                    _where.append("district = ?")
                                    _params.append(tool_args["district"])
                                _where_sql = f"WHERE {' AND '.join(_where)}" if _where else ""
                                _rows = _conn.execute(
                                    f"SELECT district, anomaly_date, observed_ndvi, expected_ndvi, "
                                    f"z_score, severity FROM anomaly_alerts_cache {_where_sql} "
                                    f"ORDER BY z_score ASC LIMIT 30",
                                    _params,
                                ).fetchall()
                                _conn.close()

                                if _rows:
                                    tool_result = {
                                        "status": "success",
                                        "source": "duckdb_cache",
                                        "count": len(_rows),
                                        "alerts": [
                                            {"district": r[0], "date": str(r[1]) if r[1] else None,
                                             "observed_ndvi": r[2], "expected_ndvi": r[3],
                                             "z_score": round(r[4], 3) if r[4] else None, "severity": r[5]}
                                            for r in _rows
                                        ],
                                    }
                                    # Auto-provision PostGIS connection so Kue can create map layers
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
                                        "source": "duckdb_cache",
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
                                import duckdb as _duckdb

                                _conn = _duckdb.connect(database=_DUCKDB_CACHE_PATH, read_only=False)
                                _conn.execute(
                                    "CREATE TABLE IF NOT EXISTS yield_risk_cache ("
                                    "district VARCHAR, risk_level VARCHAR, risk_description VARCHAR, "
                                    "trend_slope DOUBLE, kendall_tau DOUBLE, latest_ndvi DOUBLE, "
                                    "mean_ndvi DOUBLE, seasonal_deviation DOUBLE, observations INTEGER, "
                                    "computed_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP)"
                                )
                                _district = tool_args.get("district")
                                _where = "WHERE district = ?" if _district else ""
                                _params = [_district] if _district else []
                                _rows = _conn.execute(
                                    f"SELECT district, risk_level, risk_description, trend_slope, "
                                    f"kendall_tau, latest_ndvi, mean_ndvi, seasonal_deviation, observations "
                                    f"FROM yield_risk_cache {_where} "
                                    f"ORDER BY computed_at DESC LIMIT 50",
                                    _params,
                                ).fetchall()
                                _conn.close()

                                if _rows:
                                    tool_result = {
                                        "status": "success",
                                        "source": "duckdb_cache",
                                        "count": len(_rows),
                                        "assessments": [
                                            {"district": r[0], "risk_level": r[1], "risk_description": r[2],
                                             "trend_slope": r[3], "kendall_tau": r[4], "latest_ndvi": r[5],
                                             "mean_ndvi": r[6], "seasonal_deviation": r[7], "observations": r[8]}
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
                                        "source": "duckdb_cache",
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
                                import duckdb as _duckdb

                                _conn = _duckdb.connect(database=_DUCKDB_CACHE_PATH, read_only=False)
                                _conn.execute(
                                    "CREATE TABLE IF NOT EXISTS drought_cache ("
                                    "district VARCHAR, drought_status VARCHAR, current_vci DOUBLE, "
                                    "latest_ndvi DOUBLE, latest_ndwi DOUBLE, drought_period_count INTEGER, "
                                    "description VARCHAR, computed_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP)"
                                )
                                _where = []
                                _params = []
                                if tool_args.get("district"):
                                    _where.append("district = ?")
                                    _params.append(tool_args["district"])
                                if tool_args.get("status"):
                                    _where.append("drought_status = ?")
                                    _params.append(tool_args["status"])
                                _where_sql = f"WHERE {' AND '.join(_where)}" if _where else ""
                                _rows = _conn.execute(
                                    f"SELECT district, drought_status, current_vci, latest_ndvi, "
                                    f"latest_ndwi, drought_period_count, description "
                                    f"FROM drought_cache {_where_sql} "
                                    f"ORDER BY current_vci ASC LIMIT 50",
                                    _params,
                                ).fetchall()
                                _conn.close()

                                if _rows:
                                    tool_result = {
                                        "status": "success",
                                        "source": "duckdb_cache",
                                        "count": len(_rows),
                                        "districts": [
                                            {"district": r[0], "drought_status": r[1], "vci": r[2],
                                             "latest_ndvi": r[3], "latest_ndwi": r[4],
                                             "drought_period_count": r[5], "description": r[6]}
                                            for r in _rows
                                        ],
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
                                        "source": "duckdb_cache",
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
                                import duckdb as _duckdb

                                _conn = _duckdb.connect(database=_DUCKDB_CACHE_PATH, read_only=False)
                                _conn.execute(
                                    "CREATE TABLE IF NOT EXISTS phenology_cache ("
                                    "district VARCHAR, current_stage VARCHAR, peak_ndvi DOUBLE, "
                                    "peak_date VARCHAR, green_up_start VARCHAR, senescence_start VARCHAR, "
                                    "harvest_date VARCHAR, observations INTEGER, "
                                    "computed_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP)"
                                )
                                _where = []
                                _params = []
                                if tool_args.get("district"):
                                    _where.append("district = ?")
                                    _params.append(tool_args["district"])
                                if tool_args.get("stage"):
                                    _where.append("current_stage = ?")
                                    _params.append(tool_args["stage"])
                                _where_sql = f"WHERE {' AND '.join(_where)}" if _where else ""
                                _rows = _conn.execute(
                                    f"SELECT district, current_stage, peak_ndvi, peak_date, "
                                    f"green_up_start, senescence_start, harvest_date, observations "
                                    f"FROM phenology_cache {_where_sql} "
                                    f"ORDER BY computed_at DESC LIMIT 50",
                                    _params,
                                ).fetchall()
                                _conn.close()

                                if _rows:
                                    tool_result = {
                                        "status": "success",
                                        "source": "duckdb_cache",
                                        "count": len(_rows),
                                        "districts": [
                                            {"district": r[0], "current_stage": r[1], "peak_ndvi": r[2],
                                             "peak_date": r[3], "green_up_start": r[4],
                                             "senescence_start": r[5], "harvest_date": r[6], "observations": r[7]}
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
                                        "source": "duckdb_cache",
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
                                import duckdb as _duckdb
                                from datetime import date as _date, timedelta as _td

                                # ── 1. Query AgERA5 cache (DuckDB) ──
                                _agera5_rows: list = []
                                try:
                                    _conn = _duckdb.connect(database=_DUCKDB_CACHE_PATH, read_only=False)
                                    _conn.execute(
                                        "CREATE TABLE IF NOT EXISTS weather_daily_cache ("
                                        "district VARCHAR, observation_date DATE, "
                                        "temperature_mean DOUBLE, temperature_max DOUBLE, "
                                        "temperature_min DOUBLE, precipitation DOUBLE, "
                                        "solar_radiation DOUBLE, "
                                        "computed_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP)"
                                    )
                                    _where = []
                                    _params: list = []
                                    if tool_args.get("district"):
                                        _where.append("district = ?")
                                        _params.append(tool_args["district"])
                                    if tool_args.get("date_from"):
                                        _where.append("observation_date >= ?")
                                        _params.append(tool_args["date_from"])
                                    if tool_args.get("date_to"):
                                        _where.append("observation_date <= ?")
                                        _params.append(tool_args["date_to"])
                                    if not tool_args.get("date_from") and not tool_args.get("date_to"):
                                        _where.append("observation_date >= CURRENT_DATE - INTERVAL '30 days'")
                                    _where_sql = f"WHERE {' AND '.join(_where)}" if _where else ""
                                    _agera5_rows = _conn.execute(
                                        f"SELECT district, observation_date, temperature_mean, "
                                        f"temperature_max, temperature_min, precipitation, "
                                        f"solar_radiation "
                                        f"FROM weather_daily_cache {_where_sql} "
                                        f"ORDER BY observation_date DESC, district LIMIT 500",
                                        _params,
                                    ).fetchall()
                                    _conn.close()
                                except Exception:
                                    logger.debug("DuckDB cache not available, will use Open-Meteo only")

                                # Build result list from AgERA5
                                _agera5_dates: set = set()
                                _weather_stats: list = []
                                for r in _agera5_rows:
                                    _dt = str(r[1]) if r[1] else None
                                    if _dt:
                                        _agera5_dates.add(_dt)
                                    _weather_stats.append({
                                        "district": r[0],
                                        "date": _dt,
                                        "temperature_mean_c": r[2],
                                        "temperature_max_c": r[3],
                                        "temperature_min_c": r[4],
                                        "precipitation_mm_day": r[5],
                                        "solar_radiation_mj_m2_day": r[6],
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
                                                    "source": "open-meteo",
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
                                        "count": len(_all_stats),
                                        "agera5_records": len(_weather_stats),
                                        "openmeteo_records": len(_openmeteo_stats),
                                        "note": (
                                            "AgERA5 data (Copernicus reanalysis, high accuracy) covers older dates. "
                                            "Open-Meteo data (real-time forecast model) covers recent days up to today. "
                                            "Each record has a 'source' field indicating its origin."
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
                                            "No weather data available. DuckDB cache is empty and Open-Meteo "
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

                        elif function_name == "add_land_cover_layer":
                            # Add ESA WorldCover 2021 land cover as a raster overlay
                            try:
                                _wc_mode = tool_args.get("mode", "all")
                                if _wc_mode not in ("all", "cropland"):
                                    _wc_mode = "all"

                                _layer_id = generate_id(prefix="L")
                                _style_id = generate_id(prefix="S")

                                _layer_name = (
                                    "ESA WorldCover — Cropland"
                                    if _wc_mode == "cropland"
                                    else "ESA WorldCover 2021"
                                )

                                _meta = json.dumps({
                                    "worldcover": True,
                                    "worldcover_mode": _wc_mode,
                                })

                                # Rwanda bounds
                                _bounds = [28.86, -2.84, 30.90, -1.05]

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
                                    else "All 11 ESA land cover classes: tree cover, shrubland, grassland, cropland, built-up, bare, snow/ice, water, wetland, mangroves, moss/lichen"
                                )

                                tool_result = {
                                    "status": "success",
                                    "layer_id": _layer_id,
                                    "layer_name": _layer_name,
                                    "mode": _wc_mode,
                                    "source": "ESA WorldCover 2021 v200 (10m resolution)",
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

    async with async_conn("send_map_message.update_messages") as conn:
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
