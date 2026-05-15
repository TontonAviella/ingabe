"""Hermes Agent runtime entry point for Sage's turn loop.

This module is the cutover seam for Phase 2 of the Sage → Hermes migration.
When `MUNDI_USE_HERMES=1`, the dispatch in
`src/routes/message_routes.py:process_chat_interaction_task` calls
`run_sage_turn_via_hermes(...)` instead of running the hand-rolled chat loop.

## STATE AS OF 2026-05-14 (after introspecting the installed package)

The flag fork is in prod. `hermes-agent` is installed (~30 transitive deps
including anthropic, firecrawl-py, fal-client, parallel-web, exa-py,
edge-tts, prompt_toolkit, fire, croniter, etc.). The plugin scaffolding
(`hermes_integration/plugins/ingabe-sage/`) is on the branch.

But the architecture is NOT what we initially assumed.

## ARCHITECTURAL TRUTH (do not skip this section)

Hermes Agent v2026.5.7 is **a CLI app + a long-running gateway service**,
NOT a Python library you import and instantiate.

What's actually installed:
- `/app/.venv/bin/hermes`          — main CLI binary
- `/app/.venv/bin/hermes-agent`    — alias of the above
- `/app/.venv/bin/hermes-acp`      — Agent Client Protocol adapter
                                     (requires `acp` extra; NOT installed)
- `hermes_cli/` package            — 68 .py files, internal CLI commands
- `hermes_constants.py`, `hermes_logging.py`,
  `hermes_state.py`, `hermes_time.py`  — loose top-level modules
                                          (no namespace package)
- `plugins/` directory             — for `hermes plugins add ...` style

There is NO `import hermes_agent` top-level package. No `Runtime` class.
No `Session` class you can construct. The agent loop lives inside the
gateway process, not exposed as a public Python API.

`hermes gateway` is the long-running service that handles:
- Messaging integrations (Telegram, Discord, WhatsApp, Slack, Matrix,
  DingTalk, Feishu) — yes WhatsApp IS supported, via QR-pair config,
  not a separate library dep
- Plugin loading (`hermes_integration/plugins/ingabe-sage/` would be
  loaded by the gateway, not by mundi-app)
- Agent turn execution

## INTEGRATION OPTIONS (revised, ranked by realism)

### Option A: Hermes Gateway as a sidecar service + ACP protocol

Add `hermes gateway run` (foreground mode) as a second service in
docker-compose-prod.yml. Mount the ingabe-sage plugin into it. Configure
its model via `hermes model select` (OpenRouter+Nemotron). Then
`run_sage_turn_via_hermes(...)` opens an ACP session to the gateway and
forwards the conversation. The gateway streams back; mundi-app proxies
those deltas to its websocket.

Required additions:
- New compose service: `hermes-gateway`
- `acp` Python package (the `[acp]` extra) in requirements.txt
- Volume mount: plugin code into gateway container
- Inter-container networking for mundi-app ↔ hermes-gateway

Effort: ~3-5 days human-team / ~1.5 days CC+gstack.

### Option B: Subprocess `hermes chat -z "..." -m nemotron ...` per turn

Spawn `hermes` CLI per request. Returns full response on stdout. Slow
(no streaming), no concurrent turn support, lose websocket UX.
Realistic only for batch/cron jobs, not interactive Sage.

### Option C: Reuse `hermes_cli` internals directly

Import functions from `hermes_cli.commands`, `hermes_cli.gateway`, etc.
Build our own turn loop on top of Hermes primitives. Fragile — none of
that is public API; would break on every Hermes upgrade.

### Recommended: Option A.

Hermes was DESIGNED to be the orchestration brain across channels. The
gateway model is right for "many people, many companies" (Hermes has
a profile/multi-tenancy model — `hermes gateway list` shows multiple
profiles). The sidecar architecture also means partner-specific config
(WhatsApp accounts, model preferences, etc.) lives in Hermes profiles,
not in mundi-app code.

## CORRECTED WIRING PUNCH LIST (for the next PR)

Status checked off marks what's done by PR #44.

[x] 1. docker-compose-prod.yml: hermes-gateway sidecar service. Same
       image as mundi-app (Hermes is installed there). `hermes gateway
       run` daemon. Named volume `hermes-state`. `profiles: ["hermes"]`
       gate so default `up` doesn't include it.

[x] 2. acp dep: `agent-client-protocol==0.10.0` added to requirements.txt.
       Rebuild triggered 2026-05-15 ~22:40 UTC.

[ ] 3. Plugin config: still TODO. `hermes_integration/plugins/ingabe-sage/`
       lives on `feat/hermes-migration` branch (PR #42, draft). Need to
       either merge that or copy plugin code onto this branch, then add
       the volume mount back to compose service (was deferred because
       empty host dir caused a PermissionError interaction).

[ ] 4. **run_sage_turn_via_hermes** — THE BIG ONE.
       ACP architecture finding (2026-05-15 verified):
       - acp Python SDK's default transport is STDIO (subprocess pipes).
         `connect_to_agent(client_impl, stdin, stdout)` spawns the
         agent as a child process and talks over its stdin/stdout.
       - For our sidecar arch (hermes-gateway is a separate CONTAINER,
         not a child process), we have two paths:
         (a) STDIO-over-docker-exec: mundi-app runs
             `docker exec mundi-hermes-gateway hermes-acp` as a
             subprocess per session, talks ACP over its pipes.
             Requires mundi-app container to have docker socket access.
             Adds privilege scope.
         (b) Network transport: check if acp SDK supports TCP/socket
             transports (the SDK docs hint at "asyncio transports"
             plural — there may be a network option, OR we wire our
             own JSON-RPC-over-WebSocket variant).
             Cleanest if available.
         (c) Embed an ACP server inside mundi-app and have hermes-gateway
             be the client — inverted but reuses the same protocol.
             Probably not what Hermes expects.
       - Decision: investigate acp.transports module in the next session,
         then pick (a) or (b).

       Per-turn flow once transport is chosen:
       - Implement an `IngabeAcpClient(acp.Client)` subclass with
         `session_update()` mapping to `kue_stream_token(...)` deltas.
       - `await conn.new_session(mcp_servers=[], cwd=...)` per chat turn.
       - `await conn.prompt(session_id, [text_block(user_msg)],
         message_id=conversation.id)` sends the user message.
       - Streamed deltas arrive via `session_update()` callback.
       - On `AgentMessageChunk`/`TextContentBlock` chunks, call
         `kue_stream_token(conversation.id, text)`.
       - On final state, persist via `add_chat_completion_message`.

[ ] 5. Tool dispatch back to mundi: when Hermes gateway runs a Sage tool
       (via the ingabe-sage plugin), the handler hits mundi-app's
       /internal/tool-call HTTP endpoint with HMAC. Currently NOT BUILT.
       Scope for PR #46 alongside the inbox endpoint dispatch.

[x] 6. Per-partner profiles: `default` + `bk-insurance` profiles created
       on the live gateway. Both configured with Nemotron 120B free tier
       via OpenRouter. `bk-insurance` profile is dormant (stopped) until
       MUNDI_USE_HERMES_PARTNERS allowlist points at it.

[ ] 7. Cancellation, error path, output capture: comes for free with
       proper ACP wiring — the protocol has cancel + error messages.
       Map them to the existing Redis cancellation key + kue_notify_error.

## ACP transport choice — research needed

The acp SDK's stdio-default is a real friction point for sidecar
architecture. Possible resolutions ranked by preference:

1. **acp.transports has network option** — best case. Single docker
   network call, no subprocess management. Investigate first thing in
   the next session.

2. **hermes-acp accepts --port flag for TCP server mode** — also clean.
   Check `hermes acp --help` after the rebuild (which installs the acp
   module that hermes-acp needs to import).

3. **Subprocess pattern with docker exec** — workable but requires
   mundi-app to have docker socket access (`/var/run/docker.sock`
   volume mount, security implications). Adds startup latency per turn.

4. **Custom JSON-RPC over WebSocket bridge** — implement our own thin
   transport that wraps the ACP message types in WebSocket frames.
   Last resort.

## ROLLBACK (always-available)

`MUNDI_USE_HERMES=0` in `/home/deploy/mundi.ai/.env` and
`docker compose -f docker-compose.yml -f docker-compose.prod.yml restart app`.
Existing chat loop runs. No data or schema changes were made.

## WHAT THIS MODULE STILL DOES (TODAY)

For now, the seam is functional but the runtime invocation is not wired.
`run_sage_turn_via_hermes` raises NotImplementedError with the rollback
recipe. The flag stays at 0 in prod. Nobody is on Hermes after PR #44
merges. The wiring PR (#45) is the sidecar service work above.
"""
from __future__ import annotations

import logging
import os

logger = logging.getLogger(__name__)


def hermes_is_enabled() -> bool:
    """True iff MUNDI_USE_HERMES env var is set to a truthy value.

    Used by `process_chat_interaction_task` to fork dispatch. Default
    behavior (env unset or '0') is the existing hand-rolled chat loop.

    Recognized truthy values: '1', 'true', 'yes' (case-insensitive). Any
    other value is treated as off, including unset.
    """
    val = os.environ.get("MUNDI_USE_HERMES", "0").strip().lower()
    return val in {"1", "true", "yes"}


SESSION_REDIS_KEY = "hermes:session:{conversation_id}"
SESSION_TTL_SECONDS = 24 * 60 * 60  # 24 hours — session is per-conversation, not per-day
CANCEL_POLL_INTERVAL_SECONDS = 1.0


async def run_sage_turn_via_hermes(
    request,
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
    """Run one Sage turn through Hermes Agent's runtime via ACP.

    Architecture:
      mundi-app  ─TCP→  hermes-acp-bridge  ─stdio→  hermes-acp subprocess
                                                     └─ Hermes gateway state

    Per chat turn:
      1. Open async TCP connection to the bridge.
      2. Build an `IngabeAcpClient` that streams chunks to the WebSocket
         AND accumulates them for post-turn persistence.
      3. `acp.connect_to_agent(client, reader, writer)` → ClientSideConnection.
      4. `await conn.initialize(...)` (protocol handshake).
      5. Session resume: if Redis has `hermes:session:{conversation_id}`,
         call `load_session(...)` to restore Hermes-side context. Else
         `new_session(...)` and cache the new id under the same key.
         Hermes preserves chat history internally per session (verified
         via `agentCapabilities.loadSession=true` on prod 2026-05-15).
      6. Spawn a cancellation watchdog task polling Redis
         `messages:{map_id}:cancelled` every 1s. If the key fires, call
         `conn.cancel(session_id, message_id)` to interrupt the turn.
      7. `await conn.prompt(...)` (sends the user message, streams response).
      8. After prompt returns, cancel the watchdog, then persist the
         accumulated assistant text to chat_completion_messages so the
         next page reload sees the full turn.
      9. Close the connection cleanly.

    The first invocation may be slower than steady-state (~300ms subprocess
    spawn). LLM round-trip dominates at 8-60s, so the subprocess cost is
    in the noise.

    SAFETY: this function is only invoked when MUNDI_USE_HERMES=1. Rollback
    is "flip to 0 + restart app" — the existing chat loop takes over
    immediately. No data or schema changes.

    NOT YET ROUTED to users in prod even when the wiring lands — needs
    MUNDI_USE_HERMES_PARTNERS allowlist (still TODO).
    """
    import asyncio
    import os
    import uuid

    bridge_host = os.environ.get("HERMES_ACP_BRIDGE_HOST", "hermes-acp-bridge")
    bridge_port = int(os.environ.get("HERMES_ACP_BRIDGE_PORT", "9999"))

    logger.info(
        "Sage→Hermes turn: map=%s user=%s conv=%s bridge=%s:%d",
        map_id, user_id, conversation.id, bridge_host, bridge_port,
    )

    # Lazy imports — keep top-level src.services.* import lightweight
    # for environments without acp installed (CI without Hermes deps).
    try:
        import acp
    except ImportError as e:
        raise RuntimeError(
            "MUNDI_USE_HERMES=1 but agent-client-protocol is not installed. "
            "Either set the flag back to 0 or rebuild the image with main's "
            "requirements.txt."
        ) from e

    from src.routes.websocket import kue_stream_token, kue_notify_error
    from src.services.hermes_acp_client import build_ingabe_acp_client

    # ── 1. Open TCP connection to the bridge ─────────────────────────────
    try:
        reader, writer = await asyncio.open_connection(bridge_host, bridge_port)
    except OSError as e:
        await kue_notify_error(
            conversation.id,
            f"Hermes gateway unreachable ({bridge_host}:{bridge_port}). "
            f"Sage is temporarily unavailable on this profile. Operator: "
            f"check `docker ps | grep hermes-acp-bridge` on the host."
        )
        raise RuntimeError(f"ACP bridge connection failed: {e}") from e

    client = build_ingabe_acp_client(
        stream_token=kue_stream_token,
        notify_error=kue_notify_error,
        conversation_id=conversation.id,
    )

    # acp.connect_to_agent(client, input_stream, output_stream) where:
    #   - input_stream is the stream we WRITE TO the agent  → StreamWriter
    #   - output_stream is the stream we READ FROM the agent → StreamReader
    # Verified against acp v0.10.0 ClientSideConnection.__init__ which
    # asserts isinstance(input_stream, StreamWriter) AND
    # isinstance(output_stream, StreamReader). Previous code had these
    # reversed (`acp.connect_to_agent(client, reader, writer)`) and crashed
    # immediately with `TypeError: ClientSideConnection requires asyncio
    # StreamWriter/StreamReader` on every MUNDI_USE_HERMES=1 invocation.
    conn = acp.connect_to_agent(client, writer, reader)

    cancel_watchdog: asyncio.Task | None = None
    message_id = str(uuid.uuid4())

    try:
        # ── 2. Protocol handshake ────────────────────────────────────────
        # The SDK methods take direct kwargs, NOT Request objects (verified
        # against acp v0.10.0 via inspect.signature on 2026-05-15 — the
        # *Request types exist but are JSON wire models, not call args).
        from acp.schema import ClientCapabilities, FileSystemCapabilities
        await conn.initialize(
            protocol_version=acp.PROTOCOL_VERSION,
            client_capabilities=ClientCapabilities(
                fs=FileSystemCapabilities(
                    readTextFile=False,
                    writeTextFile=False,
                ),
                terminal=False,
            ),
        )

        # ── 3. Resume or create session ──────────────────────────────────
        session_id = await _resume_or_create_session(conn, acp, conversation.id)

        # ── 4. Extract the user message ──────────────────────────────────
        # Only need the latest user message — Hermes has the rest of the
        # conversation history server-side (resumed in step 3).
        last_user_msg = await _extract_last_user_message(conversation, request, session)
        if not last_user_msg:
            logger.warning("No user message to send to Hermes for conv=%s", conversation.id)
            return

        # ── 5. Spawn cancellation watchdog ───────────────────────────────
        cancel_watchdog = asyncio.create_task(
            _cancel_watchdog(conn, session_id, message_id, map_id, conversation.id)
        )

        # ── 6. Send the prompt, stream response back via client ──────────
        await conn.prompt(
            prompt=[acp.text_block(last_user_msg)],
            session_id=session_id,
            message_id=message_id,
        )
        # `prompt` returns once the agent's turn is complete. Streaming
        # chunks reached the user along the way via the client's
        # session_update() callback, and were accumulated in
        # `client.accumulated_text`.

        # ── 7. Persist the assistant response ────────────────────────────
        # Wrap in its own try/except: the streamed response already
        # reached the user via WebSocket, so a persistence failure
        # shouldn't trigger the "Sage is having trouble" notification.
        # Worst case here: next page reload doesn't show this turn
        # (we'd see a gap in chat_completion_messages, surfaced in logs).
        assistant_text = "".join(client.accumulated_text)
        if assistant_text.strip():
            try:
                await _persist_assistant_message(
                    map_id, user_id, conversation.id, assistant_text,
                )
            except Exception:
                logger.exception(
                    "Failed to persist Hermes response for conv=%s "
                    "(user saw streamed response, but it'll be missing on reload)",
                    conversation.id,
                )
        else:
            logger.warning(
                "Hermes returned empty response for conv=%s — nothing to persist",
                conversation.id,
            )

    except Exception:
        logger.exception("Hermes ACP turn failed for conv=%s", conversation.id)
        await kue_notify_error(
            conversation.id,
            "Sage is having trouble responding right now. Please retry. "
            "If this persists, contact your operator."
        )
        raise
    finally:
        # ── 8. Cancel watchdog + close connection ────────────────────────
        if cancel_watchdog is not None and not cancel_watchdog.done():
            cancel_watchdog.cancel()
            try:
                await cancel_watchdog
            except (asyncio.CancelledError, Exception):
                pass  # watchdog cleanup errors don't affect the turn
        try:
            writer.close()
            await writer.wait_closed()
        except Exception:
            pass

    # TODO (still deferred):
    #   - Per-partner profile selection: pass `profile_id=partner_id` so
    #     the gateway routes through the right hermes profile (model, tools).
    #   - /internal/tool-call reverse callback for Sage tools that live in
    #     mundi-app (Phase 1 raster, Phase 2 similarity, insurance engine).
    #   - MUNDI_USE_HERMES_PARTNERS allowlist gate.


async def _resume_or_create_session(conn, acp, conversation_id: str) -> str:
    """Resume the Hermes session for this conversation, else create one.

    Returns the session_id to use for `prompt(...)`. Session lifecycle:
    - First turn: `new_session` → cache id in Redis with 24h TTL
    - Subsequent turn: read cached id → `load_session` → bump TTL
    - If `load_session` fails (Hermes restarted, id expired): fall back
      to `new_session` so the user still gets a response (loses context,
      acceptable trade-off — better than 500).

    Per agent capabilities verified on prod 2026-05-15:
      agentCapabilities.loadSession = true
      agentCapabilities.sessionCapabilities = {fork, list, resume}
    """
    from src.dependencies.redis_client import get_redis_client

    redis_key = SESSION_REDIS_KEY.format(conversation_id=conversation_id)

    try:
        redis = get_redis_client()
        cached_id = redis.get(redis_key)
    except Exception:
        logger.exception("Redis read failed for %s; falling back to new_session", redis_key)
        cached_id = None

    if cached_id:
        try:
            # SDK takes direct kwargs (cwd, session_id), not a Request object
            await conn.load_session(
                cwd="/tmp",
                session_id=cached_id,
                mcp_servers=[],
            )
            logger.info("Hermes session resumed: conv=%s session=%s", conversation_id, cached_id)
            # Refresh TTL — active conversations stay loaded
            try:
                redis.expire(redis_key, SESSION_TTL_SECONDS)
            except Exception:
                pass
            return cached_id
        except Exception:
            logger.warning(
                "load_session failed for cached id=%s (Hermes restart? expiry?); "
                "creating fresh session for conv=%s",
                cached_id, conversation_id,
            )
            # Fall through to new_session

    # First turn (or recovery from failed resume)
    session_resp = await conn.new_session(
        cwd="/tmp",
        mcp_servers=[],
    )
    new_id = session_resp.session_id
    logger.info("Hermes session created: conv=%s session=%s", conversation_id, new_id)

    try:
        redis = get_redis_client()
        redis.set(redis_key, new_id, ex=SESSION_TTL_SECONDS)
    except Exception:
        logger.exception("Redis write failed for %s; session not cached", redis_key)

    return new_id


async def _cancel_watchdog(
    conn, session_id: str, message_id: str, map_id: str, conversation_id: str,
) -> None:
    """Poll Redis cancellation key; call conn.cancel(...) if set.

    Mirrors the hand-rolled chat loop's cancellation pattern
    (`if redis.get(f"messages:{map_id}:cancelled"): break`) but for
    Hermes's session/message_id model. Cancelled key consumed on detect
    so the next turn doesn't see stale state.
    """
    import asyncio
    from src.dependencies.redis_client import get_redis_client

    cancel_key = f"messages:{map_id}:cancelled"
    try:
        while True:
            await asyncio.sleep(CANCEL_POLL_INTERVAL_SECONDS)
            try:
                redis = get_redis_client()
                if redis.get(cancel_key):
                    redis.delete(cancel_key)
                    logger.info(
                        "Cancellation triggered for conv=%s session=%s msg=%s",
                        conversation_id, session_id, message_id,
                    )
                    try:
                        # conn.cancel sends CancelNotification(session_id=...)
                        # which the agent maps to its currently-running prompt.
                        # message_id isn't part of the wire payload (verified
                        # against acp v0.10.0 source), but we log it for trace.
                        await conn.cancel(session_id=session_id)
                    except Exception:
                        logger.exception("conn.cancel failed")
                    return
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.debug("Redis cancellation poll failed", exc_info=True)
    except asyncio.CancelledError:
        return


async def _persist_assistant_message(
    map_id: str, user_id: str, conversation_id: str, text: str,
) -> None:
    """Insert the accumulated Hermes response into chat_completion_messages.

    Without this, Hermes-served turns vanish from the UI on page reload
    because the frontend reconstructs history from this table. Schema
    matches the hand-rolled loop's `add_chat_completion_message` closure
    in `process_chat_interaction_task` — same columns, same JSON shape.
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


async def _extract_last_user_message(conversation, request, session) -> str | None:
    """Pull the most recent user message out of the conversation.

    The hand-rolled chat loop reconstructs history via
    `get_all_conversation_messages(conversation.id, session)`. We reuse
    that same query to find the last user-role message and forward it
    to Hermes. Full-history support is the follow-up.
    """
    try:
        from src.routes.postgres_routes import get_all_conversation_messages
        messages = await get_all_conversation_messages(conversation.id, session)
        for msg in reversed(messages):
            m = msg.message_json if hasattr(msg, "message_json") else {}
            if isinstance(m, dict) and m.get("role") == "user":
                content = m.get("content")
                if isinstance(content, str) and content.strip():
                    return content
                if isinstance(content, list):
                    # OpenAI content blocks: pick the first text part
                    for part in content:
                        if isinstance(part, dict) and part.get("type") == "text":
                            t = part.get("text")
                            if t:
                                return t
    except Exception:
        logger.exception("Failed to extract last user message")
    return None
