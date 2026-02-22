"""Shared helpers for LLM tool handlers.

Reduces boilerplate in ``message_routes.py`` by centralising the common
patterns for building tool results and sending them back to the LLM.
"""

from __future__ import annotations

import json
import logging
from typing import Any, Callable, Dict

from openai.types.chat import ChatCompletionMessageToolCall

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Result builders
# ---------------------------------------------------------------------------

def tool_success(message: str = "Success", **extra: Any) -> Dict[str, Any]:
    """Build a standard success result dict."""
    return {"status": "success", "message": message, **extra}


def tool_error(error: str, **extra: Any) -> Dict[str, Any]:
    """Build a standard error result dict."""
    return {"status": "error", "error": error, **extra}


# ---------------------------------------------------------------------------
# Tool-call argument extraction
# ---------------------------------------------------------------------------

def parse_tool_args(tool_call: ChatCompletionMessageToolCall) -> Dict[str, Any]:
    """Parse tool arguments from a ChatCompletion tool call, with fallback."""
    try:
        return json.loads(tool_call.function.arguments)
    except (json.JSONDecodeError, TypeError):
        return {}


def require_args(
    tool_args: Dict[str, Any],
    *keys: str,
) -> str | None:
    """Return an error string if any *keys* are missing from *tool_args*.

    Returns ``None`` when all keys are present.

    Usage::

        if err := require_args(tool_args, "layer_id", "sql_query"):
            tool_result = tool_error(err)
            ...
    """
    missing = [k for k in keys if not tool_args.get(k)]
    if missing:
        return f"Missing required parameters: {', '.join(missing)}"
    return None


# ---------------------------------------------------------------------------
# Safe tool execution wrapper
# ---------------------------------------------------------------------------

async def execute_tool(
    fn: Callable[..., Any],
    *args: Any,
    tool_name: str = "tool",
    **kwargs: Any,
) -> Dict[str, Any]:
    """Run *fn* and return a result dict, catching any exception.

    On success, *fn* is expected to return a dict (which is passed through).
    On failure, a standard error result is returned and the exception is logged.
    """
    try:
        result = await fn(*args, **kwargs)
        if isinstance(result, dict):
            return result
        return tool_success(message=str(result))
    except Exception as e:
        logger.exception("Tool %s failed", tool_name)
        return tool_error(f"Failed to execute {tool_name}: {e}")
