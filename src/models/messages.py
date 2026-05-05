"""Pydantic models and conversion helpers for chat messages and tool calls.

These models are used to transform internal database records into
API-safe response objects displayed in the chat UI.
"""

from __future__ import annotations

import datetime
import json
import logging
from typing import Literal, Optional

from openai.types.chat import ChatCompletionMessageToolCallParam
from pydantic import BaseModel

from src.database.models import MundiChatCompletionMessage
from src.geoprocessing.dispatch import get_tools

logger = logging.getLogger(__name__)


def _parse_tool_args(raw: str) -> dict:
    # gemma4:31b occasionally emits arguments with trailing tokens or two
    # concatenated JSON objects. Strict json.loads crashes the WS handler
    # ("Error connecting to LLM"). Fall back to raw_decode to extract the
    # first valid object; on total failure, return {} so the chat survives.
    # Also enforce dict output: downstream args.get(...) breaks on lists/scalars.
    try:
        parsed = json.loads(raw)
        if isinstance(parsed, dict):
            return parsed
    except json.JSONDecodeError:
        pass
    try:
        obj, end = json.JSONDecoder().raw_decode(raw.lstrip())
        if isinstance(obj, dict):
            if end < len(raw.lstrip()):
                logger.warning(
                    "tool_call arguments had trailing data after %d chars; discarded: %r",
                    end,
                    raw[end : end + 64],
                )
            return obj
    except json.JSONDecodeError:
        pass
    logger.error("tool_call arguments unparseable, using empty dict: %r", raw[:200])
    return {}


# ---------------------------------------------------------------------------
# Pydantic models
# ---------------------------------------------------------------------------

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
        "satellite",
        "map-pin",
    ]
    code: CodeBlock | None
    table: dict | None


class SanitizedToolResponse(BaseModel):
    id: str
    status: Literal["success", "error"]


class SanitizedMessage(BaseModel):
    role: str
    content: Optional[str] = None
    has_tool_calls: bool
    tool_calls: list[SanitizedToolCall]
    map_id: str
    created_at: datetime.datetime
    conversation_id: int
    tool_response: Optional[SanitizedToolResponse] = None


# ---------------------------------------------------------------------------
# Tool-call UI metadata
# ---------------------------------------------------------------------------

TC_ICON_MAP = {
    "query_duckdb_sql": "text-search",
    "query_postgis_database": "text-search",
    "new_layer_from_postgis": "text-search",
    "set_layer_style": "brush",
    "add_layer_to_map": "map-plus",
    "zoom_to_bounds": "zoom-in",
    "download_from_openstreetmap": "cloud-download",
    "execute_shell_in_vm": "square-terminal",
    "search_location": "map-pin",
    "display_satellite_layer": "satellite",
    "compute_spectral_index": "satellite",
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
    "search_location": "Searching for location...",
    "display_satellite_layer": "Loading satellite imagery...",
    "compute_spectral_index": "Computing spectral index...",
}


# ---------------------------------------------------------------------------
# Conversion helpers
# ---------------------------------------------------------------------------

def sanitized_fc_table_from_args(args: dict) -> dict:
    return args


def convert_openai_tool_call_to_sanitized_tool_call(
    tool_call: ChatCompletionMessageToolCallParam,
) -> SanitizedToolCall:
    args = _parse_tool_args(tool_call["function"]["arguments"])
    function_name = tool_call["function"]["name"]

    all_tools = get_tools()
    geoprocessing_function_names = [tool["function"]["name"] for tool in all_tools]
    is_geoprocessing_tool = function_name in geoprocessing_function_names

    code_block: CodeBlock | None = None
    if function_name == "query_duckdb_sql":
        code_block = CodeBlock(language="sql", code=args.get("sql_query", ""))
    elif function_name == "query_postgis_database":
        code_block = CodeBlock(language="sql", code=args.get("sql_query", ""))
    elif function_name == "new_layer_from_postgis":
        code_block = CodeBlock(language="sql", code=args.get("query", ""))

    table: dict | None = None
    if function_name == "download_from_openstreetmap":
        tags = args.get("tags")
        bbox = args.get("bbox")
        if tags is not None and bbox is not None:
            table = sanitized_fc_table_from_args(
                {
                    "tags": tags,
                    "bbox": ", ".join(map(str, bbox)),
                }
            )
    elif is_geoprocessing_tool:
        table = sanitized_fc_table_from_args(args)

    if function_name in TC_TAGLINE_MAP:
        tagline = TC_TAGLINE_MAP[function_name]
    elif is_geoprocessing_tool:
        tagline = function_name.replace("_", ":")
    else:
        tagline = function_name

    if function_name in TC_ICON_MAP:
        icon = TC_ICON_MAP[function_name]
    elif is_geoprocessing_tool:
        icon = "qgis"
    else:
        icon = "wrench"

    return SanitizedToolCall(
        id=tool_call["id"],
        tagline=tagline,
        icon=icon,
        code=code_block,
        table=table,
    )


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
