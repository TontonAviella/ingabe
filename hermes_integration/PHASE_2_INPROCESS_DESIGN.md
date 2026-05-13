# Phase 2 Design: Hermes In-Process Inside mundi.ai

**Status:** Design, not implementation. End of Day 6 (Hermes migration).
**Goal:** Replace Sage's dispatch loop in `src/routes/message_routes.py` with Hermes Agent calling real Sage tool handlers — same Postgres pool, same Redis, same WebSocket, same RLS partner isolation.

---

## Problem statement

Days 3–6 proved:
- Hermes runs against OpenRouter Nemotron ✅
- Plugin pattern works for tool registration ✅
- 75 tool schemas visible to LLM (60 GDAL + 15 Pydantic) ✅
- IngabeContext via contextvar + env-var fallback ✅
- LLM correctly reasons over schemas and dispatches with right args ✅

What's still stub: the **handlers** for the 75 generated tools all return "not_yet_wired". Real handlers need:
- An asyncpg connection scoped to the user
- The Redis pub/sub stream for WebSocket updates
- Access to the mundi.ai service layer (brain_service, clay_embedding, insurance_engine, etc.)
- The conversation_id + map_id + project_id + partner_id from the request

All of that lives **inside mundi.ai's FastAPI process**. Hermes needs to run there too.

---

## The architectural choice

| Option | Description | Pros | Cons |
|---|---|---|---|
| **A. Hermes as sidecar (HTTP)** | Hermes runs as a separate process; tools call back to mundi.ai via HTTP | Loose coupling, language-agnostic | Latency per tool call, auth complexity, doubles ops surface |
| **B. Hermes in-process (library)** | Import Hermes as a Python lib inside mundi.ai's FastAPI app; tools call native Sage functions | Zero network hop, shares DB pool / Redis / sessions, RLS GUC stays consistent | Couples upstream Hermes version to mundi.ai deploy |
| **C. Hermes-the-runtime-process (gateway)** | Hermes runs its own gateway process; mundi.ai sends events to it; Hermes calls back over HTTP for tool execution | Matches OpenClaw-style architecture | Same downsides as A, worse — bidirectional HTTP |

**Choice: B (in-process library).** Reasoning:

1. Sage tools already share a Postgres connection and Redis stream with the FastAPI request handler. Network-hopping for tool execution is pure latency tax.
2. RLS partner isolation requires `app.user_id` and `app.partner_id` set on the DB session that tools use. In-process means tools inherit the request's already-scoped connection. Sidecar means re-establishing context per tool call — fragile.
3. Phase 1 hooks (Pattern A Brain injection, Pattern D analytics, partner_skills filter, sage_routing fast-path) all need access to mundi.ai's services. In-process makes these one-line imports.

The trade-off (coupling upstream Hermes version to mundi.ai deploy) is real but manageable: pin to a specific Hermes tag, bump deliberately, test in dev first.

---

## Implementation outline (when we're ready)

### Step 1: Vendor Hermes properly

Currently `vendor/hermes-agent/` has the v2026.5.7 tarball extracted (~80MB). For Phase 2, add it to mundi.ai's deps:

```toml
# pyproject.toml
[tool.uv.sources]
hermes-agent = { path = "vendor/hermes-agent", editable = true }

[project.dependencies]
hermes-agent = "==0.13.0"
```

Or pull the tarball at build time in Dockerfile. The pyproject path keeps version pinned + reviewable.

### Step 2: New module `src/services/hermes_runtime.py`

```python
# src/services/hermes_runtime.py
"""Hermes runtime adapter for Sage tool dispatch.

Replaces the `for i in range(25)` loop in message_routes.py:1167 with
a Hermes Agent invocation. The new flow:

  1. mundi.ai request handler builds an IngabeContext from request
  2. Sets `set_ingabe_context(ctx)` (contextvar — picked up by tool handlers)
  3. Calls hermes_run(prompt, conversation_history, partner_id) — async wrapper
  4. Hermes loop runs: LLM → tool call → real Sage handler → result → LLM → ...
  5. Streams tokens to the existing WebSocket via Pattern A/B/C hooks
  6. Returns final assistant message + tool call log

The HOOKS port (Pattern A, Pattern D, partner_skills filter, sage_routing,
sanitizer, retry chain, cancellation) is done as Hermes plugin hooks
registered in this same module.
"""

from hermes_agent import AIAgent
from hermes_agent.session import SessionDB  # we'll override with Postgres-backed

from src.dependencies.pydantic_tools import get_pydantic_tool_calls
from .ingabe_context import set_ingabe_context, IngabeContext


async def hermes_run(
    *,
    prompt: str,
    history: list,
    user_uuid: str,
    map_id: str | None,
    partner_id: str | None,
    conversation_id: int,
    session: AsyncSession,
):
    set_ingabe_context(IngabeContext(
        user_uuid=user_uuid,
        map_id=map_id,
        partner_id=partner_id,
        conversation_id=conversation_id,
    ))
    agent = AIAgent(
        model=os.environ["OPENAI_MODEL"],
        provider="openrouter",
        max_iterations=25,
        # ... Hermes config matching current Sage settings ...
    )
    return await agent.run(prompt=prompt, history=history)
```

### Step 3: Port the 11 KEEP-items as Hermes plugin hooks

| Sage feature | Hermes hook | Notes |
|---|---|---|
| partner_skills allowlist filter | `pre_tool_call` returning `{"action": "block", "message": "..."}` | Reads partner_id from IngabeContext, queries `partner_skills` table |
| Pattern A Brain injection | `pre_llm_call` returning prompt addition | Inject Brain block into system prompt |
| Pattern D analytics | `post_tool_call` writing OTel spans | Pure observability |
| sage_routing small-talk fast-path | `pre_llm_call` returning model override | Use 7B local model on small-talk |
| LLM-stack sanitizer | Custom OpenAI client wrapper (NOT a hook) | Replaces Hermes's default OpenAI calls |
| Retry chain (5xx + payload 400 → fallback) | Custom client wrapper | Same — wraps openai.AsyncOpenAI |
| Redis cancellation | Periodic check in tool handler wrapper | OR Hermes `_interrupt_requested` flag set by listener thread |
| 60 Pydantic tools | `register(ctx)` reads `get_pydantic_tool_calls()`, registers each | Each handler uses `make_async_tool(async_fn=..., arg_keys=..., pass_ingabe_context=True)` |
| IngabeToolCallMetaArgs | contextvar set in `hermes_run()` before invoking agent | Tool handlers call `get_ingabe_context()` |
| 4-shape WS contract | `transform_terminal_output` + `pre_llm_call` + custom tool wrapper | Maps Hermes JSON-RPC events to Sage's existing kue_* functions |
| RLS GUC plumbing | `on_session_start` hook sets `app.user_id`, `app.partner_id` on DB pool | Per-request scoping |

### Step 4: Replace message_routes.py:1167 loop

Cut over per-route, not all at once:
1. New endpoint `/api/messages/send-hermes` runs Hermes
2. Existing `/api/messages/send` keeps Sage loop
3. Feature flag in frontend routes 5% → 50% → 100% over weeks
4. Compare WS event quality, tool call accuracy, latency, user feedback
5. Remove Sage loop once Hermes is at parity for 30 days

### Step 5: Memory provider — Honcho-backed brain_pages

Hermes ships an Honcho `MemoryProvider`. We write a custom one that stores dialectic user profiles in `brain_pages` (with RLS partner isolation preserved):

```python
class IngabeBrainMemoryProvider(MemoryProvider):
    async def sync_turn(self, turn_messages):
        # Update brain_pages with dialectic user model
        ...
    async def prefetch(self, query):
        # Return Brain context block (Pattern A)
        ...
```

This is the **single highest-leverage Phase 3 deliverable** — turns Sage into a self-improving per-user agent.

---

## Cost + risk

**Engineering cost**: 4-6 weeks for one engineer to do Phases 2+3 cleanly:
- Week 1: Vendor Hermes, write `hermes_runtime.py`, get one full conversation flowing
- Week 2: Port 11 KEEP hooks one at a time, verify each
- Week 3: Wire 75 real tool handlers (mostly mechanical — `make_async_tool` factory)
- Week 4: Honcho-backed brain_pages memory provider
- Week 5: A/B route, 5%→100% rollout, telemetry
- Week 6: Decommission old Sage loop, write document-release

**Risk register**:
1. Upstream Hermes API breakage during the 6 weeks → pin to v2026.5.7, vendor in tree
2. Tool handler async signatures don't all match Hermes's sync handler contract → `make_async_tool` factory handles this
3. WebSocket event format drift → emit BOTH old `kue_*` events AND new Hermes JSON-RPC for 30 days, watch for client errors
4. RLS GUC scoping bug → comprehensive test: simulate two partner sessions interleaved, assert no cross-partner data
5. Latency regression → A/B test will catch this; can fall back if Hermes is 2x slower

---

## What's needed BEFORE starting Phase 2

| Prerequisite | Status |
|---|---|
| Hermes installed + verified | ✅ |
| Plugin registration pattern proven | ✅ |
| Context-injection pattern designed | ✅ |
| Codegen pipeline for tool schemas | ✅ |
| Async handler bridge helper | ✅ (`async_bridge.py`) |
| Verified Hermes runs Nemotron at scale | ⏳ stress-tested only on simple prompts |
| BK Insurance pilot timeline clear | ⏳ pending commercial decisions |
| Decision: do BK pilot on existing Sage in parallel | ⏳ (see master plan) |

**Recommendation:** Do Phase 2 in parallel with BK pilot on existing Sage. Phase 2 ships when ready; cutover is a feature flag flip.
