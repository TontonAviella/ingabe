"""Sync→async bridge for Hermes tool handlers.

Hermes invokes tool handlers synchronously from `handle_function_call()`.
Sage's tools (`src/tools/*.py`) are async because they hit asyncpg pools,
async httpx, Qdrant grpc, etc. This module provides the bridge.

Three patterns, in increasing complexity:

1. `run_async_handler(coro_fn, **kw)` — `asyncio.run()` wrapper. Creates a
   fresh event loop per call. Simple, ~1-2ms overhead, fine for tools that
   are dominated by external IO (Nominatim, CHIRPS, Sentinel Hub, etc.).

2. `make_async_tool(name, schema, async_fn, arg_keys)` — factory that wraps
   an async function as a Hermes-compatible sync handler. Most Sage tools
   should use this.

3. **In-process pattern (Phase 2)**: when Hermes runs inside the mundi.ai
   FastAPI process, prefer `asyncio.run_coroutine_threadsafe(coro, app_loop)`
   so we reuse the existing event loop instead of spawning one per call.
   That's the design in `PHASE_2_INPROCESS_DESIGN.md`.

Why not just make Hermes's handler signature async? Hermes core is sync.
Changing that touches `run_agent.py`, `handle_function_call()`, and every
existing tool. Bridging at the plugin boundary is the lower-risk path.
"""
from __future__ import annotations

import asyncio
import functools
import json
import logging
from typing import Any, Callable, Coroutine, Dict, Iterable, Optional

logger = logging.getLogger(__name__)


def run_async_handler(
    coro_fn: Callable[..., Coroutine[Any, Any, Any]],
    /,
    **fn_kwargs: Any,
) -> str:
    """Invoke async `coro_fn(**fn_kwargs)` synchronously, return JSON string.

    Result coercion:
      - Coroutine returns dict-like / Pydantic model → JSON-serialize
      - Coroutine returns str → assumed to be JSON already (passthrough)
      - Coroutine raises → caught and serialized as
        {"status": "error", "error": str(exc), "exc_type": ...}

    Why catch in here vs let it propagate to Hermes: Hermes's handler
    contract is "must return JSON string". An uncaught exception crashes
    the dispatch loop. We turn errors into structured tool results the LLM
    can reason about — same shape Sage's `pydantic_tools` returns on error.
    """
    try:
        result = asyncio.run(coro_fn(**fn_kwargs))
    except Exception as exc:
        logger.exception("Async tool handler failed: %s", coro_fn)
        return json.dumps({
            "status": "error",
            "error": str(exc),
            "exc_type": type(exc).__name__,
        })

    if isinstance(result, str):
        return result
    try:
        return json.dumps(result, default=str)
    except Exception as exc:
        logger.exception("Tool result serialization failed: %s", coro_fn)
        return json.dumps({
            "status": "error",
            "error": f"Result was not JSON-serializable: {exc}",
            "exc_type": type(exc).__name__,
        })


def make_async_tool(
    *,
    async_fn: Callable[..., Coroutine[Any, Any, Any]],
    arg_keys: Iterable[str],
    pass_task_id: bool = False,
    pass_ingabe_context: bool = False,
) -> Callable[..., str]:
    """Wrap an async Sage tool function as a Hermes-compatible sync handler.

    Args:
        async_fn: the async tool function from src/tools/*.py
        arg_keys: which keys from Hermes's `args` dict to pass through.
                  E.g. ("bbox", "index", "date_from") for compute_spectral_index.
        pass_task_id: when True, pass `task_id=kw["task_id"]` to async_fn.
        pass_ingabe_context: when True, pass `ctx=get_ingabe_context()` to
                  async_fn. Use for tools that need user_uuid/map_id.

    Returns a `(args, **kw) -> str` callable suitable for ctx.register_tool().
    """
    arg_keys_tuple = tuple(arg_keys)

    @functools.wraps(async_fn)
    def _hermes_handler(args: Dict[str, Any], **kw: Any) -> str:
        call_kwargs = {k: args.get(k) for k in arg_keys_tuple}
        if pass_task_id:
            call_kwargs["task_id"] = kw.get("task_id")
        if pass_ingabe_context:
            from .context import get_ingabe_context
            call_kwargs["ctx"] = get_ingabe_context(required=False)
        return run_async_handler(async_fn, **call_kwargs)

    return _hermes_handler
