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
      2. Build an `IngabeAcpClient` that maps streaming session updates
         to `kue_stream_token(conversation.id, ...)`.
      3. `acp.connect_to_agent(client, reader, writer)` → ClientSideConnection.
      4. `await conn.initialize(...)` (protocol handshake).
      5. `await conn.new_session(...)` (per-turn fresh session).
      6. `await conn.prompt(...)` (sends the user message, streams response).
      7. Persist the final assistant message via the same
         `add_chat_completion_message` pattern as the hand-rolled loop.
      8. Close the connection cleanly.

    The first invocation may be slower than steady-state (~300ms subprocess
    spawn). LLM round-trip dominates at 8-60s, so the subprocess cost is
    in the noise.

    SAFETY: this function is only invoked when MUNDI_USE_HERMES=1. Rollback
    is "flip to 0 + restart app" — the existing chat loop takes over
    immediately. No data or schema changes.

    NOT YET ROUTED to users in prod even when the wiring lands — needs
    MUNDI_USE_HERMES_PARTNERS allowlist (TODO, PR #46+).
    """
    import asyncio
    import json
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
        # Defensive: if MUNDI_USE_HERMES=1 was set on a host where the
        # acp package isn't installed (mismatched deploy), surface a
        # clear error instead of silently failing.
        raise RuntimeError(
            "MUNDI_USE_HERMES=1 but agent-client-protocol is not installed. "
            "Either set the flag back to 0 or rebuild the image with the "
            "feat/hermes-acp-wiring branch's requirements.txt."
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

    conn = acp.connect_to_agent(client, reader, writer)

    try:
        # ── 2. Protocol handshake ────────────────────────────────────────
        # Standard ACP initialize; specifies which client capabilities
        # we support. We claim none — Hermes does ALL the heavy lifting,
        # mundi-app is just the user-facing chat surface.
        await conn.initialize(
            acp.InitializeRequest(
                protocol_version=acp.PROTOCOL_VERSION,
                client_capabilities=acp.InitializeRequest.ClientCapabilities(
                    fs=acp.InitializeRequest.ClientCapabilities.Fs(
                        read_text_file=False,
                        write_text_file=False,
                    ),
                    terminal=False,
                ),
            )
        )

        # ── 3. New session for this turn ─────────────────────────────────
        # cwd is required by the protocol; pass /tmp since we don't expose
        # a real working directory to Hermes (filesystem ops are denied).
        session_resp = await conn.new_session(
            acp.NewSessionRequest(
                cwd="/tmp",
                mcp_servers=[],
            )
        )
        session_id = session_resp.session_id

        # ── 4. Send the user prompt + history ────────────────────────────
        # For first cut: just send the last user message. Conversation
        # history wiring lands in a follow-up — see TODO at the bottom.
        # Hermes's session_id is ephemeral per turn; we'll persist mapping
        # to mundi conversation.id in chat_completion_messages if needed.
        last_user_msg = await _extract_last_user_message(conversation, request, session)
        if not last_user_msg:
            logger.warning("No user message to send to Hermes for conv=%s", conversation.id)
            return

        await conn.prompt(
            acp.PromptRequest(
                session_id=session_id,
                prompt=[acp.text_block(last_user_msg)],
                message_id=str(uuid.uuid4()),
            )
        )
        # `prompt` returns once the agent's turn is complete. Streaming
        # chunks reached the user along the way via the client's
        # session_update() callback.

    except Exception:
        logger.exception("Hermes ACP turn failed for conv=%s", conversation.id)
        await kue_notify_error(
            conversation.id,
            "Sage is having trouble responding right now. Please retry. "
            "If this persists, contact your operator."
        )
        raise
    finally:
        # ── 5. Always close cleanly ──────────────────────────────────────
        try:
            writer.close()
            await writer.wait_closed()
        except Exception:
            pass

    # TODO (PR #46+):
    #   - Pass conversation history into the prompt, not just last message
    #   - Persist the final assistant message back to chat_completion_messages
    #     so the next turn sees it in history. Currently Sage's web UI
    #     reconstructs history from chat_completion_messages, so without
    #     this step a Hermes-served turn vanishes from history on reload.
    #   - Bridge cancellation: check redis `messages:{map_id}:cancelled`
    #     periodically and call conn.cancel(session_id, message_id).
    #   - Per-partner profile selection: pass `profile_id=partner_id` so
    #     the gateway routes through the right hermes profile (model, tools).


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
