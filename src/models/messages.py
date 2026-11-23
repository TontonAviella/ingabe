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
    id: Optional[int] = None  # chat_completion_messages.id: what a 👍/👎 on an answer points at
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
    "new_layer_from_postgis": "text-search",
    "set_layer_style": "brush",
    "add_layer_to_map": "map-plus",
    "zoom_to_bounds": "zoom-in",
    "search_location": "map-pin",
    "display_satellite_layer": "satellite",
    "compute_spectral_index": "satellite",
}

# One plain line for each step Sage takes, read in the conversation under the question (the chats panel). A step is
# never shown by its tool's name: a name such as "reverse_geocode_coordinates" tells a reader nothing (Roger,
# 2026-11-23). Every tool Sage can be offered has a line (src/models/test_tool_step_lines.py); a tool with none
# reads STEP_LINE_OTHER.
STEP_LINE_OTHER = "Took a step"

TC_TAGLINE_MAP = {
    "add_land_cover_layer": "Added the land cover map",
    "add_layer_to_map": "Added a layer to the map",
    "add_observation": "Saved a note",
    "analyze_raster_object_candidates": "Looked for objects in the photo",
    "analyze_rgb_field": "Read the photo's colours",
    "brain_graph_query": "Searched the notes",
    "brain_trajectory": "Read the notes over time",
    "compare_rasters": "Compared two pictures",
    "compute_spectral_index": "Worked out a satellite index",
    "compute_zonal_stats": "Measured the picture inside an area",
    "count_plants_in_plot": "Counted the plants",
    "create_point_layer": "Added a point to the map",
    "create_raster_h3_context_layer": "Screened the photo in hexagons",
    "describe_user_raster": "Read what the picture holds",
    "detect_dry_spells": "Looked for dry spells",
    "detect_flood_extent": "Looked for flooding",
    "display_geojson_layer": "Drew a shape on the map",
    "display_layer": "Showed a layer on the map",
    "display_satellite_layer": "Loaded satellite imagery",
    "evaluate_insurance_trigger": "Checked the insurance trigger",
    "find_stress_zones": "Looked for stressed patches",
    "get_agri_indices": "Read the crop indices",
    "get_anomaly_alerts": "Checked for unusual readings",
    "get_cell_ndvi_stats": "Read greenness (NDVI) by cell",
    "get_crop_growth_stage": "Read the crop's growth stage",
    "get_drone_photo_findings": "Read what was found on the photo",
    "get_drought_status": "Checked for drought",
    "get_entity": "Read a note",
    "get_evapotranspiration": "Read the water plants used",
    "get_farm_emissions": "Read the farm emission estimates",
    "get_field_health": "Read the field's health from the satellite",
    "get_forecast": "Read the weather forecast",
    "get_forecast_accuracy": "Checked how good forecasts have been",
    "get_insurance_accuracy": "Checked how accurate the insurance figures are",
    "get_insurance_intelligence": "Read the season's insurance report",
    "get_ndvi_stats": "Read greenness (NDVI) by district",
    "get_parcel_ndvi_stats": "Read greenness (NDVI) by parcel",
    "get_soil_moisture": "Read soil moisture",
    "get_soil_properties": "Read the soil's properties",
    "get_tower_inspection_findings": "Read the tower inspection",
    "get_value_distribution": "Read how the picture's values spread",
    "get_weather_stats": "Read the rain and weather record",
    "get_yield_risk": "Read the yield risk",
    "interpret_raster_health": "Judged crop health from the picture",
    "keep_answer_in_words": "Answered in words",
    "list_admin_units": "Listed the places",
    "new_layer_from_postgis": "Made a layer from the database",
    "predict_ndvi_from_sar": "Estimated greenness from radar",
    "query_worldcover_stats": "Read the land cover",
    "read_pixel_at": "Read the value at a point",
    "reverse_geocode_coordinates": "Found which place this is",
    "search_brain": "Searched the notes",
    "search_location": "Looked up the place",
    "search_satellite_imagery": "Searched for satellite images",
    "set_layer_style": "Changed how a layer looks",
    "show_admin_boundary": "Showed the place's outline",
    "show_plants_and_soil_from_satellite": "Read plants and soil from the satellite",
    "tower_photos_needing_review": "Listed the tower photos still to check",
    "zoom_to_bounds": "Moved the map",
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

    # Tools declared in tools.json show their arguments as a table in the chat.
    is_catalog_tool = function_name in {tool["function"]["name"] for tool in get_tools()}

    code_block: CodeBlock | None = None
    if function_name == "new_layer_from_postgis":
        code_block = CodeBlock(language="sql", code=args.get("query", ""))

    table: dict | None = sanitized_fc_table_from_args(args) if is_catalog_tool else None
    tagline = TC_TAGLINE_MAP.get(function_name, STEP_LINE_OTHER)
    icon = TC_ICON_MAP.get(function_name, "wrench")

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
        id=getattr(cc_message, "id", None),
        role=role,
        content=cc_message.message_json["content"] if role != "tool" else None,
        has_tool_calls=bool(cc_message.message_json.get("tool_calls")),
        tool_calls=tool_calls,
        map_id=cc_message.map_id,
        created_at=cc_message.created_at,
        conversation_id=cc_message.conversation_id,
        tool_response=tool_response,
    )
