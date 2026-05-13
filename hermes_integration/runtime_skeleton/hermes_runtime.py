"""hermes_runtime — embed Hermes inside mundi.ai's FastAPI process.

SKELETON ONLY. This file represents the SHAPE of the Phase 2 runtime swap.
It does not yet execute — Hermes Agent isn't installed in mundi.ai's venv
yet (blocked on openai 1.78 → 2.36 upgrade; see STREAM_1_OPENAI_UPGRADE_AUDIT.md).

When the upgrade ships and `hermes-agent` is added to pyproject.toml, this
file moves to `src/services/hermes_runtime.py` and the TODO blocks below
are filled with real Hermes API calls.

DESIGN — runtime entry point:

    # In src/routes/message_routes.py, near line 1167 (the existing dispatch loop):

    if _use_hermes_for(partner_id, user_id):
        # NEW: Hermes-powered path (feature-flagged)
        result = await hermes_runtime.run_conversation(
            user_uuid=user_id,
            partner_id=partner_id,
            conversation_id=conversation.id,
            map_id=map_id,
            project_id=current_project_id,
            session=session,
            history=openai_messages,
            tools_payload=tools_payload,
        )
        # Hermes already emitted kue_stream_token events via the WebSocket
        # bridge (Stream 6). We just need to persist the final assistant
        # message + any tool messages.
        for msg in result.messages:
            await add_chat_completion_message(msg)
    else:
        # EXISTING: Sage's for-i-in-range(25) loop (lines 1167-2160)
        for i in range(25):
            ...

DESIGN — Hermes session lifecycle:

    1. mundi.ai builds IngabeContext from request → set_ingabe_context()
    2. hermes_runtime.run_conversation() invokes Hermes's AIAgent
    3. AIAgent.run_conversation() loops: LLM → tool_call → handler → result → ...
    4. Tool handlers (registered via ingabe-sage plugin) read get_ingabe_context()
       and call mundi.ai's existing async tool functions via make_async_tool()
    5. Hermes plugin hooks (Pattern A / Pattern D / partner_skills / etc.) run
       at LLM and tool boundaries — same behaviour as today's Sage hooks
    6. WebSocket events from Hermes JSON-RPC are translated into Sage's
       kue_stream_token / kue_ephemeral_action calls (Stream 6 bridge)
    7. On stream end, return RunResult with persisted messages
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Optional

# TODO Stream 1 (after openai upgrade):
#   from hermes_agent import AIAgent  # noqa
#   from hermes_agent.session import SessionContext

# Pulled from the ingabe-sage plugin once it lives inside mundi.ai's tree.
# For now, the import path is .claude/worktrees/hermes-migration/hermes_integration/plugins/ingabe-sage/
# After Stream 1 cleanup, move the plugin to src/services/hermes_plugin/.
# from src.services.hermes_plugin.context import (
#     IngabeContext, set_ingabe_context, clear_ingabe_context,
# )


@dataclass
class RunResult:
    """Outcome of one Hermes turn. Drop-in replacement for what message_routes
    builds in its current loop (assistant_message + tool messages + telemetry)."""
    messages: list[Any] = field(default_factory=list)
    # Pattern D analytics carried out of Hermes via post_tool_call hook
    tool_calls_total: int = 0
    composition_steps: int = 0
    max_per_step: int = 0
    timeouts: int = 0
    exit_reason: str = "unknown"  # "final_assistant" | "max_iter" | "cancelled" | "error"
    # LLM provider chain that was used (for Pattern D telemetry)
    attempted_models: list[str] = field(default_factory=list)


async def run_conversation(
    *,
    user_uuid: str,
    partner_id: Optional[str],
    conversation_id: int,
    map_id: Optional[str],
    project_id: Optional[str],
    session: Any,  # SQLAlchemy AsyncSession; typed Any to avoid prematurely importing
    history: list[dict],
    tools_payload: list[dict],
    system_prompt: str,
    max_turns: int = 25,
) -> RunResult:
    """Replace the dispatch loop at message_routes.py:1167 with a Hermes invocation.

    Args mirror the locals that loop captures from the FastAPI request. The
    HOOK port (Pattern A Brain injection, partner_skills filter, sage_routing
    small-talk fast-path) happens INSIDE Hermes via plugin hooks — caller
    doesn't need to know about them.

    Returns RunResult shaped to match what message_routes currently constructs
    so the caller doesn't have to be rewritten — just swap the block from
    `for i in range(25):` to `result = await run_conversation(...)`.
    """
    # TODO Stream 1: set the contextvar so tool handlers can read it
    #   set_ingabe_context(IngabeContext(
    #       user_uuid=user_uuid,
    #       conversation_id=conversation_id,
    #       map_id=map_id,
    #       project_id=project_id,
    #       partner_id=partner_id,
    #   ))

    # TODO Stream 1: build a SessionContext for Hermes that carries our state.
    #   The Hermes AIAgent expects a session object; we plug in our own that
    #   forwards to mundi.ai's existing async DB session + Redis client.

    # TODO Stream 1: instantiate AIAgent with our model + plugins enabled.
    #   agent = AIAgent(
    #       model=os.environ["OPENAI_MODEL"],
    #       provider="openrouter",  # via OPENAI_BASE_URL
    #       max_iterations=max_turns,
    #       enabled_plugins=["ingabe-sage", "ingabe-sage-generated"],
    #       hooks_enabled=True,  # so Pattern A / partner_skills / etc. fire
    #   )

    # TODO Stream 1: actually run.
    #   try:
    #       hermes_result = await agent.run_conversation(
    #           messages=[{"role": "system", "content": system_prompt}, *history],
    #           tools=tools_payload,
    #       )
    #   finally:
    #       clear_ingabe_context()

    # TODO Stream 1: translate Hermes RunResult into our RunResult shape.
    raise NotImplementedError(
        "hermes_runtime.run_conversation() is a skeleton. "
        "Fill in after openai 1.78 → 2.36 upgrade lands. "
        "See hermes_integration/STREAM_1_OPENAI_UPGRADE_AUDIT.md."
    )


def is_hermes_enabled_for(
    *,
    partner_id: Optional[str],
    user_uuid: Optional[str],
) -> bool:
    """Feature flag entry point.

    Read from env at first:
      INGABE_USE_HERMES_FOR_PARTNERS=bk-insurance,das
      INGABE_USE_HERMES_FOR_USERS=<comma-separated UUIDs>
      INGABE_USE_HERMES_GLOBAL=1   # cutover toggle

    Future: read from a runtime config table or feature-flag service.
    """
    import os
    if os.environ.get("INGABE_USE_HERMES_GLOBAL") == "1":
        return True
    allow_partners = {
        p.strip() for p in os.environ.get("INGABE_USE_HERMES_FOR_PARTNERS", "").split(",")
        if p.strip()
    }
    if partner_id and partner_id in allow_partners:
        return True
    allow_users = {
        u.strip() for u in os.environ.get("INGABE_USE_HERMES_FOR_USERS", "").split(",")
        if u.strip()
    }
    if user_uuid and user_uuid in allow_users:
        return True
    return False
