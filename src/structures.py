from __future__ import annotations
import datetime
import json
import os
import sys
import asyncpg
from typing import Literal, Optional
from opentelemetry import trace
import asyncio
from pydantic import BaseModel
from src.database.models import MundiChatCompletionMessage
from src.geoprocessing.dispatch import get_tools
from openai.types.chat import ChatCompletionMessageToolCallParam

IS_RUNNING_PYTEST = "pytest" in sys.modules or "PYTEST_CURRENT_TEST" in os.environ

_async_connection_pool = None
_async_pool_lock = asyncio.Lock()

# Read-replica pool (populated only when POSTGRES_READ_HOST is set)
_async_read_pool = None
_async_read_pool_lock = asyncio.Lock()

# Get tracer for this module
tracer = trace.get_tracer(__name__)


def _build_postgres_url(host: Optional[str] = None, port: Optional[str] = None) -> str:
    """Build a PostgreSQL DSN from environment variables.

    ``host`` and ``port`` override the env defaults so the same helper
    works for both the primary and read-replica connections.
    """
    user = os.environ["POSTGRES_USER"]
    password = os.environ["POSTGRES_PASSWORD"]
    h = host or os.environ["POSTGRES_HOST"]
    p = port or os.environ.get("POSTGRES_PORT", "5432")
    db = os.environ["POSTGRES_DB"]
    return f"postgresql://{user}:{password}@{h}:{p}/{db}"


async def _get_async_connection_pool():
    global _async_connection_pool
    if _async_connection_pool is None:
        async with _async_pool_lock:
            if _async_connection_pool is None:
                _async_connection_pool = await asyncpg.create_pool(
                    dsn=_build_postgres_url(),
                    min_size=1,
                    max_size=10,
                )
    return _async_connection_pool


async def _get_async_read_pool():
    """Return the read-replica pool, or the primary pool when no replica is configured."""
    read_host = os.environ.get("POSTGRES_READ_HOST")
    if not read_host:
        # No replica configured — fall back to primary
        return await _get_async_connection_pool()

    global _async_read_pool
    if _async_read_pool is None:
        async with _async_read_pool_lock:
            if _async_read_pool is None:
                read_port = os.environ.get(
                    "POSTGRES_READ_PORT",
                    os.environ.get("POSTGRES_PORT", "5432"),
                )
                _async_read_pool = await asyncpg.create_pool(
                    dsn=_build_postgres_url(host=read_host, port=read_port),
                    min_size=1,
                    max_size=10,
                )
    return _async_read_pool


class AsyncDatabaseConnection:
    """Context-manager that yields an *exclusive* connection.

    Using a per-request dedicated connection completely avoids the
    "another operation is in progress" race that can occur when the same
    connection object is shared between overlapping coroutines.  The
    overhead of opening a new connection is negligible for the test
    suite and greatly simplifies correctness.

    Set ``readonly=True`` to route the connection to the read replica
    (when ``POSTGRES_READ_HOST`` is configured).  Without a replica the
    connection falls back to the primary transparently.
    """

    def __init__(self, span_name: Optional[str] = None, readonly: bool = False):
        self.conn: Optional[asyncpg.Connection] = None
        self.span: Optional[trace.Span] = None
        self.span_name: Optional[str] = span_name
        self.readonly: bool = readonly

    async def __aenter__(self) -> asyncpg.Connection:
        # only create a span if we're in a recording context
        current_span = trace.get_current_span()
        if current_span.is_recording():
            self.span = tracer.start_span(self.span_name or "asyncpg")

        # In pytest, connect directly instead of using pool.
        # NOTE: this bypasses read/write pool routing — the readonly flag
        # is only effective in production where connections come from pools.
        if IS_RUNNING_PYTEST:
            self.conn = await asyncpg.connect(_build_postgres_url())
        else:
            # Choose pool based on readonly flag
            if self.readonly:
                pool = await _get_async_read_pool()
            else:
                pool = await _get_async_connection_pool()
            self.conn = await pool.acquire()
        return self.conn

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        if self.conn is not None:
            if IS_RUNNING_PYTEST:
                await self.conn.close()
            else:
                # Release back to the correct pool
                if self.readonly:
                    pool = await _get_async_read_pool()
                else:
                    pool = await _get_async_connection_pool()
                await pool.release(self.conn)
        if self.span:
            self.span.end()


def get_async_db_connection():
    """Return a write connection to the primary database."""
    return AsyncDatabaseConnection()


def get_async_read_connection():
    """Return a read-only connection routed to the replica (or primary if no replica)."""
    return AsyncDatabaseConnection(readonly=True)


def async_conn(span_name: Optional[str] = None):
    """Write connection with OpenTelemetry span."""
    return AsyncDatabaseConnection(f"pg {span_name}")


def async_read_conn(span_name: Optional[str] = None):
    """Read-only connection with OpenTelemetry span, routed to the replica."""
    return AsyncDatabaseConnection(f"pg:ro {span_name}", readonly=True)


class SanitizedMessage(BaseModel):
    role: str
    content: Optional[str] = None
    has_tool_calls: bool
    tool_calls: list[SanitizedToolCall]
    map_id: str
    created_at: datetime.datetime
    conversation_id: int
    tool_response: Optional[SanitizedToolResponse] = None


def convert_mundi_message_to_sanitized(
    cc_message: MundiChatCompletionMessage,
) -> SanitizedMessage:
    role = cc_message.message_json["role"]
    assert role in ["user", "assistant", "tool"]

    tool_calls = []
    if cc_message.message_json.get("tool_calls"):
        for tool_call in cc_message.message_json["tool_calls"]:
            tool_call: ChatCompletionMessageToolCallParam = tool_call
            tool_calls.append(
                convert_openai_tool_call_to_sanitized_tool_call(tool_call)
            )

    tool_response = None
    if role == "tool":
        try:
            content = json.loads(cc_message.message_json.get("content"))
            # delicately detect errors... by assuming success
            tool_response = SanitizedToolResponse(
                id=cc_message.message_json["tool_call_id"],
                status="error" if content["status"] == "error" else "success",
            )
        except (json.JSONDecodeError, KeyError):
            pass

    return SanitizedMessage(
        role=role,
        content=cc_message.message_json["content"] if role != "tool" else None,
        has_tool_calls=bool(cc_message.message_json.get("tool_calls")),
        tool_calls=tool_calls,
        map_id=cc_message.map_id,
        created_at=cc_message.created_at,
        conversation_id=cc_message.conversation_id,
        tool_response=tool_response,
    )


class CodeBlock(BaseModel):
    language: str
    code: str


class SanitizedToolCall(BaseModel):
    id: str
    tagline: str
    icon: Literal[
        "text-search",
        "brush",
        "wrench",
        "map-plus",
        "cloud-download",
        "zoom-in",
        "qgis",
        "square-terminal",
    ]
    code: CodeBlock | None
    table: dict | None


class SanitizedToolResponse(BaseModel):
    id: str
    status: Literal["success", "error"]


TC_ICON_MAP = {
    "query_duckdb_sql": "text-search",
    "query_postgis_database": "text-search",
    "new_layer_from_postgis": "text-search",
    "set_layer_style": "brush",
    "add_layer_to_map": "map-plus",
    "zoom_to_bounds": "zoom-in",
    "download_from_openstreetmap": "cloud-download",
    "execute_shell_in_vm": "square-terminal",
}
TC_TAGLINE_MAP = {
    "query_duckdb_sql": "Querying layer in DuckDB...",
    "query_postgis_database": "Querying PostGIS layer...",
    "new_layer_from_postgis": "Creating layer from PostGIS...",
    "set_layer_style": "Setting layer style...",
    "add_layer_to_map": "Adding layer to map...",
    "zoom_to_bounds": "Zooming to bounds...",
    "download_from_openstreetmap": "Downloading from OpenStreetMap...",
    "execute_shell_in_vm": "Running analysis...",
}


def sanitized_fc_table_from_args(args: dict) -> dict:
    return args


def convert_openai_tool_call_to_sanitized_tool_call(
    tool_call: ChatCompletionMessageToolCallParam,
) -> SanitizedToolCall:
    args = json.loads(tool_call["function"]["arguments"])
    function_name = tool_call["function"]["name"]

    # Check if this is a geoprocessing tool
    all_tools = get_tools()
    geoprocessing_function_names = [tool["function"]["name"] for tool in all_tools]

    is_geoprocessing_tool = function_name in geoprocessing_function_names

    code_block: CodeBlock | None = None
    if tool_call["function"]["name"] == "query_duckdb_sql":
        code_block = CodeBlock(language="sql", code=args["sql_query"])
    elif tool_call["function"]["name"] == "query_postgis_database":
        code_block = CodeBlock(language="sql", code=args["sql_query"])
    elif tool_call["function"]["name"] == "new_layer_from_postgis":
        code_block = CodeBlock(language="sql", code=args["query"])

    table: dict | None = None
    if tool_call["function"]["name"] == "download_from_openstreetmap":
        table = sanitized_fc_table_from_args(
            {
                "tags": args["tags"],
                "bbox": ", ".join(map(str, args["bbox"])),
            }
        )
    elif is_geoprocessing_tool:
        # For geoprocessing tools, put all arguments in a table
        table = sanitized_fc_table_from_args(args)

    # Determine tagline
    if is_geoprocessing_tool:
        # Replace underscores with colons for geoprocessing tools
        tagline = function_name.replace("_", ":")
    else:
        tagline = TC_TAGLINE_MAP.get(function_name, function_name)

    icon = TC_ICON_MAP.get(tool_call["function"]["name"], "wrench")
    if is_geoprocessing_tool:
        icon = "qgis"

    return SanitizedToolCall(
        id=tool_call["id"],
        tagline=tagline,
        icon=icon,
        code=code_block,
        table=table,
    )
