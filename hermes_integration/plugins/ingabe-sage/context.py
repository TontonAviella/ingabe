"""IngabeContext — per-call context that Sage tools need.

Plumbing for mundi.ai's IngabeToolCallMetaArgs (user_uuid, conversation_id,
map_id, project_id, partner_id, session). When Hermes runs inside the mundi.ai
FastAPI process (Phase 2 of master plan), tools will read this via a contextvar
that the request handler set right before invoking Hermes.

For now (Day 4), we read from env vars so the PoC can demonstrate the wiring
without requiring in-process integration:
    INGABE_USER_UUID, INGABE_MAP_ID, INGABE_PROJECT_ID, INGABE_CONVERSATION_ID, INGABE_PARTNER_ID
"""
from __future__ import annotations

import os
from contextvars import ContextVar
from dataclasses import dataclass
from typing import Optional


@dataclass(frozen=True)
class IngabeContext:
    """Mirrors mundi.ai's IngabeToolCallMetaArgs.

    `session` is intentionally not carried here — Hermes tools that need an
    asyncpg connection should open one via brain_service / dependency-injected
    helpers when running in-process, or use a mundi.ai HTTP shim when running
    out-of-process.
    """
    user_uuid: str
    conversation_id: Optional[int] = None
    map_id: Optional[str] = None
    project_id: Optional[str] = None
    partner_id: Optional[str] = None


# Async-safe, thread-safe per-call slot. Set by the in-process caller right
# before invoking Hermes; read by tool handlers.
_current_context: ContextVar[Optional[IngabeContext]] = ContextVar(
    "ingabe_context", default=None
)


def set_ingabe_context(ctx: IngabeContext) -> None:
    """Mundi.ai sets this once per Hermes invocation."""
    _current_context.set(ctx)


def get_ingabe_context(required: bool = False) -> Optional[IngabeContext]:
    """Tool handlers read this. Returns None if not set unless required=True.

    Fallback: when no contextvar is set, try env vars. Lets us drive Hermes
    from the CLI for PoC purposes before mundi.ai integration lands.
    """
    ctx = _current_context.get()
    if ctx is not None:
        return ctx

    user_uuid = os.environ.get("INGABE_USER_UUID")
    if not user_uuid:
        if required:
            raise RuntimeError(
                "IngabeContext required but not set. Either call "
                "set_ingabe_context() before invoking Hermes, or set "
                "INGABE_USER_UUID env var (plus INGABE_MAP_ID etc.) for "
                "CLI testing."
            )
        return None

    conv = os.environ.get("INGABE_CONVERSATION_ID")
    return IngabeContext(
        user_uuid=user_uuid,
        conversation_id=int(conv) if conv else None,
        map_id=os.environ.get("INGABE_MAP_ID"),
        project_id=os.environ.get("INGABE_PROJECT_ID"),
        partner_id=os.environ.get("INGABE_PARTNER_ID"),
    )
