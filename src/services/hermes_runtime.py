"""Hermes Agent runtime entry point for Sage's turn loop.

This module is the cutover seam for Phase 2 of the Sage → Hermes migration.
When `MUNDI_USE_HERMES=1`, the dispatch in
`src/routes/message_routes.py:process_chat_interaction_task` calls
`run_sage_turn_via_hermes(...)` instead of running the hand-rolled chat loop.

WHY THIS FILE EXISTS RIGHT NOW (2026-05-14)
─────────────────────────────────────────────
The flag fork exists in prod. `hermes-agent` is installed. The plugin
scaffolding (`hermes_integration/plugins/ingabe-sage/`) is in place. The
last unfinished piece is the runtime invocation itself — the call that
hands a turn to Hermes and bridges its streaming hooks back to mundi.ai's
WebSocket emit functions.

The runtime invocation MUST be wired with fresh judgment, not at 3am.
Doing it tired is exactly how a turn-loop replacement breaks prod for
every user simultaneously. So this module DELIBERATELY raises a clear
NotImplementedError when called. The flag is off by default. The error
only surfaces if an operator turns it on prematurely.

WHAT'S MISSING
──────────────
1. Construct Hermes's runtime/session object with the ingabe-sage plugin
   loaded. Hermes's entry point is in `run_agent.py` upstream (750kB file)
   and the session interface is at `gateway/session.py`. The right pattern
   is to import the runtime class, instantiate per-request, NOT module-
   scoped — each chat turn gets its own runtime to isolate context.

2. Pre-call setup:
   - `IngabeContext` contextvar (already scaffolded in
     `hermes_integration/plugins/ingabe-sage/context.py`) must be set
     with user_uuid / partner_id / map_id BEFORE invoking Hermes, so the
     plugin's universal tool shim can read it.
   - The chat history → Hermes input-state translation. Hermes uses its
     own message shape (closer to OpenAI but with extras for tool turns).
     The existing `openai_messages` list in `process_chat_interaction_task`
     is the source.

3. Streaming bridge. Hermes's hook system fires events like `agent:step`,
   `agent:end`. Map those to `kue_stream_token(conversation.id, delta)`
   from `src.routes.websocket`. Token-level streaming requires Hermes's
   per-delta hook (not yet identified in the gateway/hooks.py file).

4. Tool dispatch — see `hermes_integration/plugins/ingabe-sage/tools.py`.
   The plugin has 75 stubbed tool schemas registered. The universal shim
   pattern: every tool's handler reads IngabeContext, looks up the
   matching handler in mundi.ai's existing `pydantic_tool_calls` registry,
   awaits it, returns the JSON-string result. ONE shim function, not 75.

5. Error path. When Hermes raises, surface via `kue_notify_error(
   conversation.id, error_message)` and write a synthetic
   ChatCompletionMessage with role="assistant" to chat_completion_messages
   so the conversation history stays coherent.

6. Output capture. After Hermes finishes the turn, persist the final
   assistant message (and any tool-call messages it produced) via the
   `add_chat_completion_message` closure that the caller already has.
   Cleanest: pass that closure in as a callback.

7. Cancellation. The existing loop checks `redis.get(f"messages:{map_id}:
   cancelled")` per iteration. Hermes doesn't know about Redis. Either:
   (a) bridge with a hook that fires per agent:step and raises asyncio.
   CancelledError when the key is set, or (b) wrap the whole call in a
   `wait_for(..., timeout=...)` with cancellation propagation.

ROLLBACK
────────
Set `MUNDI_USE_HERMES=0` in `/home/deploy/mundi.ai/.env` and restart
`mundi-app` with prod compose files. The fork in
`process_chat_interaction_task` falls back to the existing hand-rolled
chat loop. Zero schema or data changes were made, so no migration
rollback is needed.

CUTOVER ORDER (per partner, not global)
───────────────────────────────────────
Even after wiring is complete, the safe rollout is per-partner:
1. Flip MUNDI_USE_HERMES=1 in staging. Watch p50/p95 latency, error rate
   on PostHog traces tagged with `partner_id`. Compare to the existing
   path for the same partner. Bail if regression.
2. Flip MUNDI_USE_HERMES_PARTNERS=`bk-insurance` (env-var allowlist) in
   prod. Sage routes BK users through Hermes; everyone else stays on the
   old path. Soak for 48h.
3. Expand allowlist by one partner at a time, soaking between each.
4. Once stable for all partners, retire the env-var allowlist and the
   old chat loop in a separate cleanup PR.

(The MUNDI_USE_HERMES_PARTNERS allowlist isn't implemented yet either;
it lives in the same future PR as the runtime invocation.)
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

    NOT YET IMPLEMENTED — see module docstring for the wiring punch list.
    This raises a clear error so operators who flip the flag prematurely
    get an explicit signal, not silent breakage.

    Once implemented, this function takes the same arguments as
    `process_chat_interaction_task` and produces the same observable
    side effects: streamed tokens to the websocket, chat messages
    persisted, tool calls dispatched, conversation history advanced.
    """
    raise NotImplementedError(
        "Hermes runtime invocation is not yet wired. "
        "Set MUNDI_USE_HERMES=0 to use the existing chat loop, or "
        "complete the wiring in src/services/hermes_runtime.py per the "
        "module docstring. Rollback to MUNDI_USE_HERMES=0 + container "
        "restart is the safe state."
    )
