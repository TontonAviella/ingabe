"""PostHog observability helpers for Sage/Hermes tool routing.

The raw tool arguments and tool outputs can contain user data, SQL, filenames,
or private map details. This module only emits low-cardinality metadata:
tool name, router category, argument/result keys, status, and latency.
"""

from __future__ import annotations

import json
import time
from collections.abc import Iterable, Mapping, MutableMapping
from typing import Any

from src.dependencies.sage_routing import (
    routing_alignment_for_tool,
    tool_category_for_name,
)
from src.services.posthog_analytics import capture_for_session
from src.services.sage_flight_recorder import keys_csv, summarize_tool_result

SAGE_ROUTING_DECISION_EVENT = "backend_sage_routing_decision"
SAGE_TOOL_COMPLETED_EVENT = "backend_sage_tool_call_completed"


def csv_for_values(values: Iterable[Any], *, empty: str = "") -> str:
    strings = sorted({str(value) for value in values if value is not None})
    return ",".join(strings) if strings else empty




def build_sage_tool_context(
    *,
    tool_name: str,
    tool_args: Any,
    routing_reason: str,
    selected_categories: Iterable[str],
    tool_registry: str,
    map_id: str,
    project_id: str,
    conversation_id: int,
    client_turn_id: str | None = None,
    message_id: str | None = None,
) -> dict[str, Any]:
    selected = set(selected_categories)
    return {
        "started_at": time.monotonic(),
        "tool_name": tool_name,
        "tool_category": tool_category_for_name(tool_name),
        "tool_registry": tool_registry,
        "routing_reason": routing_reason,
        "routing_selected_categories_csv": csv_for_values(selected, empty="all_tools"),
        "routing_alignment": routing_alignment_for_tool(tool_name, selected),
        "tool_arg_key_count": len(tool_args) if isinstance(tool_args, Mapping) else 0,
        "tool_arg_keys_csv": keys_csv(tool_args),
        "map_id": map_id,
        "project_id": project_id,
        "conversation_id": conversation_id,
        "client_turn_id": client_turn_id,
        "message_id": message_id,
    }




def capture_sage_routing_decision(
    *,
    session: Any,
    map_id: str,
    project_id: str | None,
    conversation_id: int,
    routing_reason: str,
    selected_categories: Iterable[str],
    is_small_talk: bool,
    model: str | None,
    tool_count: int,
    user_message_length: int,
    tool_payload_bytes: int,
    client_turn_id: str | None = None,
    message_id: str | None = None,
) -> bool:
    selected = list(selected_categories)
    return capture_for_session(
        SAGE_ROUTING_DECISION_EVENT,
        session,
        properties={
            "map_id": map_id,
            "project_id": project_id,
            "conversation_id": conversation_id,
            "client_turn_id": client_turn_id,
            "message_id": message_id,
            "routing_reason": routing_reason,
            "selected_categories_csv": csv_for_values(selected, empty="all_tools"),
            "is_small_talk": is_small_talk,
            "model": model,
            "tool_count": tool_count,
            "tool_filter_applied": bool(selected),
            "user_message_length": user_message_length,
            "tool_payload_bytes": tool_payload_bytes,
        },
    )


def capture_sage_tool_result_message(
    *,
    message: Mapping[str, Any],
    context_by_tool_call_id: MutableMapping[str, dict[str, Any]],
    session: Any,
) -> bool:
    if message.get("role") != "tool":
        return False
    tool_call_id = str(message.get("tool_call_id") or "")
    if not tool_call_id:
        return False

    context = context_by_tool_call_id.pop(tool_call_id, None)
    if not context:
        return False

    started_at = float(context.pop("started_at", time.monotonic()))
    result_summary = summarize_tool_result(message.get("content"))
    properties = {
        **context,
        **result_summary,
        "elapsed_ms": int((time.monotonic() - started_at) * 1000),
    }
    return capture_for_session(
        SAGE_TOOL_COMPLETED_EVENT,
        session,
        properties=properties,
    )
