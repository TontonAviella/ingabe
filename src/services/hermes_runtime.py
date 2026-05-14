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


async def run_sage_turn_via_hermes(*args, **kwargs) -> None:
    """Run one Sage turn through Hermes Agent's runtime.

    NOT YET IMPLEMENTED — see module docstring for the CORRECTED wiring
    punch list (sidecar gateway service + ACP protocol, NOT a library
    import as initially assumed).

    Raises a clear error so operators who flip the flag prematurely get
    an explicit signal, not silent breakage.
    """
    raise NotImplementedError(
        "Hermes runtime invocation is not yet wired. The correct "
        "integration is a sidecar `hermes gateway run` container + the "
        "ACP (Agent Client Protocol) — NOT a Python library import "
        "(Hermes does not expose a library API). See module docstring "
        "for the punch list. Rollback to MUNDI_USE_HERMES=0 + container "
        "restart is the safe state."
    )
