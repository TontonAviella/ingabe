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

1. **docker-compose-prod.yml**: add a `hermes-gateway` service. Image
   = `mundi-public:local` (same image — Hermes is installed there).
   Command = `hermes gateway run`. Mount plugin dir read-only. Expose
   ACP socket/port to mundi-app on the docker network.

2. **acp dep**: add `agent-client-protocol>=0.9.0,<1.0` to
   requirements.txt. Rebuild image.

3. **Plugin config**: `hermes_integration/plugins/ingabe-sage/plugin.yaml`
   already declares the tool surface. Mount it via volume so the gateway
   loads it on startup.

4. **`run_sage_turn_via_hermes`**: open an ACP session to the gateway
   (via `acp` Python client), forward user message + history, stream
   response back to mundi-app's websocket via `kue_stream_token(...)`.
   Persist final assistant message via the `add_chat_completion_message`
   closure.

5. **Tool dispatch back to mundi**: the ingabe-sage plugin's tool handlers
   currently are stubs. When the gateway runs a tool, the plugin handler
   must somehow reach back to mundi-app's existing dispatch. Options:
   (a) HTTP callback from gateway to mundi-app at a `/internal/tool-call`
   endpoint, (b) shared filesystem state, (c) PostGIS-driven tool execution
   in-gateway. (a) is simplest.

6. **Per-partner profiles**: `hermes profile <partner-id>` creates a
   profile. Profile-scoped model config, WhatsApp credentials, tool
   allowlists. The dispatch passes `--profile <partner_id>` per turn.

7. **Cancellation, error path, output capture**: same as before but
   bridged through ACP events instead of in-process hooks.

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
