"""Hermes Agent runtime — in-process AIAgent path.

When `MUNDI_USE_HERMES=1`, `process_chat_interaction_task` hands the turn
to `run_sage_turn_via_hermes(...)` which constructs a `hermes_cli` AIAgent
and runs it in-process inside the FastAPI worker.

## Why in-process (and not ACP sidecar / gateway HTTP)

The earlier sidecar attempts (ACP TCP bridge, gateway HTTP) hit two
ergonomic problems:

  1. `acp_adapter` hardcodes `enabled_toolsets=["hermes-acp"]` and
     filters every plugin toolset out before the LLM call. Verified
     2026-05-17 from `acp_adapter/session.py:596-605`.
  2. The gateway api_server path works but introduces an HTTP boundary
     that complicates `IngabeContext` propagation (the
     `(partner_id, user_uuid, conversation_id, map_id)` tuple that
     proxied tool handlers need to issue HMAC-signed `/internal/tool-call`
     POSTs).

In-process avoids both: we construct `AIAgent(platform="api_server", ...)`
directly, and `IngabeContext` is a `ContextVar` set in the same asyncio
task that runs the conversation — so the plugin's `proxy_tool_call`
reads the same context value through normal async-aware propagation.

## Streaming model

`AIAgent.run_conversation()` is a **synchronous** call (it owns its own
LLM-roundtrip blocking loop). We run it in `loop.run_in_executor(None,
...)` and bridge `stream_delta_callback` (which fires from the executor
thread) to `kue_stream_token` (an async coroutine on the event loop) via
a thread-safe `queue.Queue` plus a drainer task.

## Rollback

Same as before: `MUNDI_USE_HERMES=0` in env + restart `mundi-app`. The
hand-rolled loop in `process_chat_interaction_task` takes over.
"""
from __future__ import annotations

import asyncio
import contextvars
import json
import logging
import os
import queue as _q
import threading
import time
import uuid
from typing import Any

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Flag + cache
# ---------------------------------------------------------------------------

def hermes_is_enabled() -> bool:
    """True iff MUNDI_USE_HERMES env var is set to a truthy value.

    Used by `process_chat_interaction_task` to fork dispatch. Default
    behavior (env unset or '0') is the existing hand-rolled chat loop.
    """
    val = os.environ.get("MUNDI_USE_HERMES", "0").strip().lower()
    return val in {"1", "true", "yes"}


# Plugins must be discovered ONCE per process. discover_and_load() walks
# ~/.hermes/plugins/ and the bundled dir, then registers their tool
# schemas + handlers into the global Hermes tool registry. After the
# first call, `hermes_plugins.ingabe_sage.context` is importable.
_PLUGIN_MANAGER = None
_PLUGIN_LOCK = threading.Lock()


def _ensure_plugins_loaded():
    """Load Hermes plugins lazily and cache the manager.

    Hermes's PluginManager scans the user-plugin directory once and
    populates `sys.modules['hermes_plugins.ingabe_sage']` plus the global
    tools registry. We must call this before constructing AIAgent or
    importing the plugin's context module.
    """
    global _PLUGIN_MANAGER
    if _PLUGIN_MANAGER is not None:
        return _PLUGIN_MANAGER
    with _PLUGIN_LOCK:
        if _PLUGIN_MANAGER is not None:
            return _PLUGIN_MANAGER
        from hermes_cli.plugins import PluginManager
        mgr = PluginManager()
        mgr.discover_and_load()
        loaded = [p["name"] for p in mgr.list_plugins()]
        logger.info("Hermes plugins loaded once: %s", loaded)
        _PLUGIN_MANAGER = mgr
    return _PLUGIN_MANAGER


# ---------------------------------------------------------------------------
# IngabeContext propagation
# ---------------------------------------------------------------------------

def _set_ingabe_context(
    *,
    user_uuid: str,
    partner_id: str | None,
    conversation_id: int | str,
    map_id: str | None,
    project_id: str | None,
) -> None:
    """Set the plugin's IngabeContext ContextVar for this request.

    The plugin's `proxy_tool_call` reads from the same ContextVar when it
    runs (in this same asyncio task), so the (partner_id, user_uuid,
    conversation_id) tuple flows into the HMAC-signed POST to mundi-app's
    /internal/tool-call.

    We import the plugin's context module via the
    `hermes_plugins.ingabe_sage.context` path that Hermes's plugin loader
    establishes in sys.modules — that's the SAME module object the plugin
    handlers read from, so the ContextVar is shared.
    """
    _ensure_plugins_loaded()
    from hermes_plugins.ingabe_sage.context import (
        IngabeContext, set_ingabe_context,
    )
    set_ingabe_context(IngabeContext(
        user_uuid=user_uuid,
        partner_id=partner_id,
        conversation_id=conversation_id if isinstance(conversation_id, int)
                          else int(conversation_id),
        map_id=map_id,
        project_id=project_id,
    ))


# ---------------------------------------------------------------------------
# Conversation history → AIAgent format
# ---------------------------------------------------------------------------

async def _build_conversation_history(conversation, request, session) -> list[dict]:
    """Convert chat_completion_messages rows into OpenAI chat format.

    Returns a list of `{"role": ..., "content": ...}` dicts in send order.
    AIAgent.run_conversation accepts this directly as `conversation_history`.

    Excludes the most recent user message (caller passes that as
    `user_message=`).
    """
    try:
        from src.routes.message_routes import get_all_conversation_messages
        rows = await get_all_conversation_messages(conversation.id, session)
    except Exception:
        logger.exception("Failed to load conversation history for conv=%s", conversation.id)
        return []

    out: list[dict] = []
    for row in rows:
        m = row.message_json if hasattr(row, "message_json") else row
        if not isinstance(m, dict):
            continue
        role = m.get("role")
        if role not in ("user", "assistant", "tool", "system"):
            continue
        out.append(m)
    return out


def _extract_last_user_text(history: list[dict]) -> str | None:
    """Walk history backwards and return the most recent user-text message."""
    for m in reversed(history):
        if m.get("role") != "user":
            continue
        content = m.get("content")
        if isinstance(content, str) and content.strip():
            return content
        if isinstance(content, list):
            for part in content:
                if isinstance(part, dict) and part.get("type") == "text":
                    t = part.get("text")
                    if isinstance(t, str) and t.strip():
                        return t
    return None


# ---------------------------------------------------------------------------
# Persistence
# ---------------------------------------------------------------------------

async def _persist_assistant_message(
    map_id: str, user_id: str, conversation_id: str, text: str,
) -> None:
    """Insert the accumulated assistant response into chat_completion_messages.

    Without this, Hermes-served turns vanish from the UI on page reload
    because the frontend reconstructs history from this table.
    """
    import json
    from src.structures import async_conn

    message_dict = {"role": "assistant", "content": text}
    async with async_conn("hermes_persist_assistant") as conn:
        await conn.execute(
            """
            INSERT INTO chat_completion_messages
            (map_id, sender_id, message_json, conversation_id)
            VALUES ($1, $2, $3, $4)
            """,
            map_id,
            user_id,
            json.dumps(message_dict),
            conversation_id,
        )


# ---------------------------------------------------------------------------
# The turn
# ---------------------------------------------------------------------------

async def run_sage_turn_via_hermes(
    request,  # FastAPI Request — only needed to satisfy the seam signature
    map_id: str,
    session,
    user_id: str,
    chat_args,
    map_state,
    conversation,
    system_prompt_provider,
    connection_manager,
    pydantic_tool_calls,
) -> None:
    """Run one Sage turn through an in-process Hermes AIAgent.

    Flow:
      1. Lazy-load Hermes plugins (once per process).
      2. Set IngabeContext ContextVar for this request.
      3. Pull conversation history from DB; extract the last user message.
      4. Construct AIAgent(platform="api_server", ...) with our plugin
         toolsets activated (`ingabe-sage`, `ingabe-sage-proxied`).
      5. Stream tokens out via stream_delta_callback → kue_stream_token.
      6. Run agent.run_conversation in an executor (it's sync).
      7. Persist the final assistant text. Emit WS done=True.

    Cancellation: a watchdog coroutine polls the Redis cancel key every
    second. On cancel it calls `agent.interrupt(...)`, which the api_server
    pattern uses to stop in-flight LLM calls cleanly.
    """
    from src.routes.websocket import kue_stream_token, kue_notify_error

    _ensure_plugins_loaded()

    # --- 1. Build IngabeContext from the request ---------------------------
    partner_id = session.get_org_id() if hasattr(session, "get_org_id") else None

    # Dev-mode fallback: LegacyUserContext (Clerk off / MUNDI_AUTH_MODE=edit)
    # has no org and returns partner_id=None. The hand-rolled path tolerates
    # this because its tool handlers don't actually read app.partner_id; our
    # HMAC proxy + receiver both require a non-None partner_id by design.
    # Synthesize the same dev UUID LegacyUserContext uses for user_id so the
    # local dev experience matches the hand-rolled path. In prod with Clerk,
    # session.get_org_id() returns a real org UUID and this branch never fires.
    if partner_id is None:
        from src.dependencies.session import LegacyUserContext
        if isinstance(session, LegacyUserContext):
            partner_id = "00000000-0000-0000-0000-000000000000"
            logger.info(
                "Dev-mode LegacyUserContext detected; using dev partner_id "
                "for IngabeContext (conv=%s)", conversation.id,
            )

    project_id = getattr(conversation, "project_id", None) or getattr(map_state, "project_id", None)

    _set_ingabe_context(
        user_uuid=str(user_id),
        partner_id=partner_id,
        conversation_id=conversation.id,
        map_id=map_id,
        project_id=project_id,
    )

    # --- 2. Pull history + last user message -------------------------------
    history = await _build_conversation_history(conversation, request, session)
    last_user_text = _extract_last_user_text(history)
    if not last_user_text:
        logger.warning("No user message to send to Hermes (conv=%s)", conversation.id)
        return

    # AIAgent expects the latest user message via `user_message=`; pass the
    # rest as `conversation_history` (excluding the trailing user turn).
    prior_history: list[dict] = []
    saw_target = False
    for m in reversed(history):
        if not saw_target and m.get("role") == "user" and m.get("content") == last_user_text:
            saw_target = True
            continue
        if saw_target:
            prior_history.append(m)
    prior_history.reverse()

    # --- 3. System prompt --------------------------------------------------
    try:
        system_message = system_prompt_provider.get_system_prompt()
    except Exception:
        logger.exception("system_prompt_provider failed; falling back to None")
        system_message = None

    # --- 4. Resolve Hermes runtime + toolsets -----------------------------
    try:
        from run_agent import AIAgent
        from gateway.run import (
            _resolve_runtime_agent_kwargs,
            _resolve_gateway_model,
            _load_gateway_config,
            GatewayRunner,
        )
        from hermes_cli.tools_config import _get_platform_tools
    except ImportError as e:
        await kue_notify_error(
            conversation.id,
            "Hermes runtime is not installed. Set MUNDI_USE_HERMES=0 and restart, "
            "or rebuild the image with hermes-agent in requirements.txt.",
        )
        raise RuntimeError(f"Hermes import failed: {e}") from e

    runtime_kwargs = _resolve_runtime_agent_kwargs()
    model = _resolve_gateway_model()
    cfg = _load_gateway_config()
    enabled_toolsets = sorted(_get_platform_tools(cfg, "api_server"))
    try:
        fallback_model = GatewayRunner._load_fallback_model()
    except Exception:
        fallback_model = None

    logger.info(
        "Sage→Hermes in-process turn: conv=%s map=%s user=%s partner=%s "
        "model=%s toolsets=%d",
        conversation.id, map_id, user_id, partner_id, model, len(enabled_toolsets),
    )

    # --- 5. Streaming bridge: sync delta cb → asyncio queue → kue WS ------
    turn_id = f"hermes-{conversation.id}-{int(time.time() * 1000)}"
    delta_queue: _q.Queue = _q.Queue()
    persist_queue: _q.Queue = _q.Queue()  # per-tool {assistant+tool} message pairs
    accumulated: list[str] = []

    def _on_delta(delta):
        # AIAgent fires `stream_delta_callback(None)` as a CLI-display
        # sentinel before tool execution — drop it (matches api_server
        # behavior at line 1080).
        if delta is None or not isinstance(delta, str):
            return
        if not delta:
            return
        delta_queue.put(delta)

    def _on_tool_complete(tool_call_id, function_name, function_args, function_result):
        """Capture a completed tool round for DB persistence.

        Hand-rolled `process_chat_interaction_task` persists every tool round
        as two rows in chat_completion_messages: (a) an assistant message
        carrying the tool_calls array, (b) a tool message carrying the
        result string keyed by tool_call_id. Without this, the in-process
        Hermes path only persists the final assistant text and history is
        lost across reloads. We mirror the hand-rolled shape one tool at a
        time (OpenAI's chat format permits an assistant message with a
        single tool_call).

        Fires from the AIAgent executor thread — defer the DB write to the
        main loop via persist_queue.
        """
        try:
            args_str = (
                json.dumps(function_args) if not isinstance(function_args, str)
                else function_args
            )
            result_str = (
                function_result if isinstance(function_result, str)
                else json.dumps(function_result)
            )
        except Exception:
            return  # never raise back into the agent loop
        persist_queue.put({
            "tool_call_id": tool_call_id,
            "function_name": function_name,
            "args_str": args_str,
            "result_str": result_str,
        })

    async def _drain_to_websocket():
        loop = asyncio.get_running_loop()
        while True:
            try:
                delta = await loop.run_in_executor(None, delta_queue.get)
            except asyncio.CancelledError:
                return
            if delta is _SENTINEL_DONE:
                return
            accumulated.append(delta)
            try:
                await kue_stream_token(conversation.id, delta, turn_id=turn_id)
            except Exception:
                logger.debug("kue_stream_token push failed", exc_info=True)

    async def _drain_to_db():
        """Persist tool rounds queued by _on_tool_complete to chat_completion_messages."""
        import json as _json
        from src.structures import async_conn
        loop = asyncio.get_running_loop()
        while True:
            try:
                item = await loop.run_in_executor(None, persist_queue.get)
            except asyncio.CancelledError:
                return
            if item is _SENTINEL_DONE:
                return
            try:
                assistant_msg = {
                    "role": "assistant",
                    "content": None,
                    "tool_calls": [{
                        "id": item["tool_call_id"],
                        "type": "function",
                        "function": {
                            "name": item["function_name"],
                            "arguments": item["args_str"],
                        },
                    }],
                }
                tool_msg = {
                    "role": "tool",
                    "tool_call_id": item["tool_call_id"],
                    "content": item["result_str"],
                }
                async with async_conn("hermes_persist_tool_round") as c:
                    for m in (assistant_msg, tool_msg):
                        await c.execute(
                            """
                            INSERT INTO chat_completion_messages
                            (map_id, sender_id, message_json, conversation_id)
                            VALUES ($1, $2, $3, $4)
                            """,
                            map_id, user_id, _json.dumps(m), conversation.id,
                        )
            except Exception:
                logger.exception(
                    "Failed to persist tool round (conv=%s tool=%s)",
                    conversation.id, item.get("function_name"),
                )

    # --- 6. Build the AIAgent ---------------------------------------------
    agent_ref: list[Any] = [None]

    def _build_agent():
        return AIAgent(
            model=model,
            **runtime_kwargs,
            max_iterations=int(os.environ.get("HERMES_MAX_ITERATIONS", "30")),
            quiet_mode=True,
            verbose_logging=False,
            enabled_toolsets=enabled_toolsets,
            session_id=f"conv-{conversation.id}",
            platform="api_server",
            stream_delta_callback=_on_delta,
            tool_complete_callback=_on_tool_complete,
            fallback_model=fallback_model,
            ephemeral_system_prompt=system_message,
        )

    # --- 7. Cancellation watchdog -----------------------------------------
    async def _cancel_watchdog():
        from src.dependencies.redis_client import get_redis_client
        cancel_key = f"messages:{map_id}:cancelled"
        while True:
            await asyncio.sleep(CANCEL_POLL_INTERVAL_SECONDS)
            try:
                redis = get_redis_client()
                if redis.get(cancel_key):
                    redis.delete(cancel_key)
                    agent = agent_ref[0]
                    if agent is not None:
                        try:
                            agent.interrupt("user cancellation")
                            logger.info("Hermes turn interrupted by user (conv=%s)", conversation.id)
                        except Exception:
                            logger.exception("agent.interrupt failed")
                    return
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.debug("cancel watchdog poll failed", exc_info=True)

    # --- 8. Run --------------------------------------------------------
    drainer_task: asyncio.Task | None = None
    persist_task: asyncio.Task | None = None
    cancel_task: asyncio.Task | None = None
    try:
        loop = asyncio.get_running_loop()
        drainer_task = asyncio.create_task(_drain_to_websocket())
        persist_task = asyncio.create_task(_drain_to_db())
        cancel_task = asyncio.create_task(_cancel_watchdog())

        def _run_sync() -> dict:
            agent = _build_agent()
            agent_ref[0] = agent
            return agent.run_conversation(
                user_message=last_user_text,
                conversation_history=prior_history or None,
                task_id=str(conversation.id),
            )

        # Critical: ContextVars (including IngabeContext) do NOT propagate
        # into executor threads by default. Snapshot the current context and
        # have the executor run inside it, so the plugin's proxy_tool_call
        # sees the (partner_id, user_uuid, conversation_id) we just set.
        ctx_snapshot = contextvars.copy_context()
        result = await loop.run_in_executor(None, ctx_snapshot.run, _run_sync)
        # Final flush: signal the drainers to stop after pulling any
        # remaining deltas / tool rounds. We can't push None directly because
        # it's also the sentinel _on_delta drops; use a private object.
        delta_queue.put(_SENTINEL_DONE)
        persist_queue.put(_SENTINEL_DONE)
        await asyncio.wait_for(drainer_task, timeout=2.0)
        await asyncio.wait_for(persist_task, timeout=5.0)  # DB writes are slower

        # Persist the assistant text + WS done signal
        assistant_text = "".join(accumulated).strip()
        if not assistant_text:
            # Some result shapes carry the final text in result["content"]
            # or result["message"]; try to recover.
            if isinstance(result, dict):
                for key in ("content", "message", "text", "output"):
                    val = result.get(key)
                    if isinstance(val, str) and val.strip():
                        assistant_text = val.strip()
                        break

        if assistant_text:
            try:
                await _persist_assistant_message(
                    map_id, user_id, conversation.id, assistant_text,
                )
            except Exception:
                logger.exception(
                    "Failed to persist Hermes response (conv=%s, user got streamed text)",
                    conversation.id,
                )
        else:
            logger.warning(
                "Hermes returned empty response for conv=%s — nothing to persist; result=%r",
                conversation.id, (str(result)[:200] if result else None),
            )

        try:
            await kue_stream_token(conversation.id, "", done=True, turn_id=turn_id)
        except Exception:
            logger.debug("kue_stream_token done=True failed", exc_info=True)

    except Exception:
        logger.exception("Hermes in-process turn failed for conv=%s", conversation.id)
        try:
            await kue_notify_error(
                conversation.id,
                "Sage is having trouble responding right now. Please retry."
            )
        except Exception:
            pass
        try:
            await kue_stream_token(conversation.id, "", done=True, turn_id=turn_id)
        except Exception:
            pass
        raise
    finally:
        for t in (drainer_task, persist_task, cancel_task):
            if t and not t.done():
                t.cancel()
                try:
                    await t
                except (asyncio.CancelledError, Exception):
                    pass


# Module-private sentinel used to signal the drainer to exit cleanly.
_SENTINEL_DONE = object()

CANCEL_POLL_INTERVAL_SECONDS = 1.0
