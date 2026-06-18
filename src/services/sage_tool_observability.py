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

SAGE_ROUTING_DECISION_EVENT = "backend_sage_routing_decision"
SAGE_TOOL_COMPLETED_EVENT = "backend_sage_tool_call_completed"


def csv_for_values(values: Iterable[Any], *, empty: str = "") -> str:
    strings = sorted({str(value) for value in values if value is not None})
    return ",".join(strings) if strings else empty


def keys_csv(value: Any, *, limit: int = 20) -> str:
    if not isinstance(value, Mapping):
        return ""
    keys = sorted(str(key) for key in value.keys())
    return ",".join(keys[:limit])


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
    }


def summarize_tool_result(result: Any) -> dict[str, Any]:
    if isinstance(result, str):
        try:
            result = json.loads(result)
        except Exception:
            return {
                "tool_status": "text",
                "tool_success": False,
                "tool_has_error": True,
                "result_key_count": 0,
                "result_keys_csv": "",
                "result_size_chars": len(result),
            }

    if not isinstance(result, Mapping):
        return {
            "tool_status": "unknown",
            "tool_success": False,
            "tool_has_error": True,
            "result_key_count": 0,
            "result_keys_csv": "",
            "result_size_chars": 0,
        }

    raw_status = result.get("status")
    has_error = "error" in result or str(raw_status or "").lower() in {
        "error",
        "failed",
        "failure",
    }
    status = str(raw_status or ("error" if has_error else "success"))[:64]
    status_lower = status.lower()
    success = status_lower in {"success", "ok", "completed"} or (
        not has_error and status_lower not in {"not_found", "missing"}
    )

    try:
        result_size_chars = len(json.dumps(result, default=str))
    except Exception:
        result_size_chars = 0

    return {
        "tool_status": status,
        "tool_success": bool(success),
        "tool_has_error": bool(has_error),
        "result_key_count": len(result),
        "result_keys_csv": keys_csv(result),
        "result_size_chars": result_size_chars,
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
) -> bool:
    selected = list(selected_categories)
    return capture_for_session(
        SAGE_ROUTING_DECISION_EVENT,
        session,
        properties={
            "map_id": map_id,
            "project_id": project_id,
            "conversation_id": conversation_id,
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
