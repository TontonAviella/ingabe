"""Internal tool-call endpoint — Hermes plugin callback into mundi-app.

This is the reverse direction of the Hermes wiring. /internal/inbox
flows messages INTO mundi-app from channels. /internal/tool-call lets
Hermes (running our Sage tools as plugin functions) call BACK to
mundi-app to actually execute a tool against partner-scoped data.

## Flow

```
1. User in BK chats with Sage; mundi-app routes through Hermes (MUNDI_USE_HERMES=1)
2. Hermes-side ingabe-sage plugin sees the model wants to call `compute_zonal_stats`
3. Plugin sends POST /internal/tool-call to mundi-app with HMAC-signed payload:
     {
       partner_id: "<uuid>",
       user_id: "<uuid>",
       conversation_id: "<int as string>",
       tool_name: "compute_zonal_stats",
       arguments: {"layer_id": "L...", "geometry": {...}}
     }
4. mundi-app verifies HMAC with HERMES_GATEWAY_SECRET (same secret as /inbox)
5. mundi-app sets app.partner_id + app.user_id GUCs on its DB connection
6. mundi-app dispatches to the existing pydantic_tools handler
7. Result returned to Hermes, who feeds it back to the LLM, who streams response
```

## Security boundary

HMAC verification on the BODY (see hermes_auth.py for the full scheme).
Without it, anyone on the docker network can dispatch a tool with any
(partner_id, user_id) they want, bypassing RLS at the application layer
(RLS itself still holds at the DB layer — the GUC would just be set wrong
for whatever Hermes asked).

Apply HMAC check BEFORE setting any GUCs. Order matters: never set GUCs
based on caller-supplied IDs unless we've authenticated the caller.

## Tool whitelisting

`payload.tool_name` is looked up in `get_pydantic_tool_calls()` — a
hard-coded registry. Any name not in that registry returns 404. This
is the key safety property: even a forged signature cannot dispatch
arbitrary code, only the tools mundi-app has chosen to expose.
"""
from __future__ import annotations

import json
import logging
import os
from typing import Any, Optional

from fastapi import APIRouter, Header, HTTPException, Request, status
from pydantic import BaseModel, ValidationError

from src.database.pool import async_conn
from src.dependencies.hermes_auth import (
    get_gateway_secret,
    verify_hermes_signature,
)
from src.dependencies.pydantic_tools import get_pydantic_tool_calls
from src.dependencies.session import ServiceUserContext
from src.routes.websocket import kue_ephemeral_action

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/internal", tags=["internal"])


class ToolCallPayload(BaseModel):
    """Canonical Hermes-to-mundi tool dispatch request.

    Locked-in shape so the gateway side has a stable contract. New
    optional fields are fine; never break existing field semantics.
    """
    partner_id: str        # Clerk org uuid — sets app.partner_id GUC
    user_id: str           # Clerk user uuid — sets app.user_id GUC
    conversation_id: str   # links back to chat_completion_messages
    tool_name: str         # e.g. "compute_zonal_stats", maps to pydantic_tools dispatch
    arguments: dict[str, Any]  # tool-specific argument payload


def tool_call_is_enabled() -> bool:
    """True iff MUNDI_TOOL_CALL_ENABLED env var is set to a truthy value.

    Same flag-gating pattern as MUNDI_INBOX_ENABLED. Even after the
    dispatch wiring lands, ops opens this per-deploy.
    """
    val = os.environ.get("MUNDI_TOOL_CALL_ENABLED", "0").strip().lower()
    return val in {"1", "true", "yes"}


async def _resolve_map_and_project(
    conversation_id: int,
) -> tuple[str, str]:
    """Look up (map_id, project_id) for a conversation.

    Conversation rows have project_id directly. map_id lives on
    chat_completion_messages — a conversation can have messages across
    multiple maps in the DAG. We pick the most-recent message's map_id,
    matching what the active chat UI is most likely showing.

    Runs WITHOUT RLS GUCs set — this is a system-level lookup that
    happens BEFORE we've scoped the connection to a partner. The data
    we read here (conversation.project_id, message.map_id) is then used
    to construct IngabeToolCallMetaArgs; if the (partner_id, user_id)
    from the Hermes payload doesn't actually own that conversation, the
    DOWNSTREAM tool dispatch will fail when its own queries hit RLS.

    Raises HTTPException(404) if the conversation doesn't exist or has
    no messages — in either case there's nothing to dispatch against.
    """
    async with async_conn("tool-call.lookup_conv") as conn:
        row = await conn.fetchrow(
            """
            SELECT c.project_id, m.map_id
            FROM conversations c
            LEFT JOIN chat_completion_messages m
              ON m.conversation_id = c.id
            WHERE c.id = $1 AND c.soft_deleted_at IS NULL
            ORDER BY m.created_at DESC NULLS LAST
            LIMIT 1
            """,
            conversation_id,
        )
    if row is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"conversation {conversation_id} not found",
        )
    if row["map_id"] is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"conversation {conversation_id} has no messages yet — nothing to scope tools against",
        )
    return row["map_id"], row["project_id"]


@router.post("/tool-call")
async def tool_call(
    request: Request,
    x_hermes_signature: Optional[str] = Header(default=None),
):
    """Receive a tool dispatch request from Hermes. Auth via HMAC.

    States this endpoint can return:
    - 503: route is disabled (default; flip MUNDI_TOOL_CALL_ENABLED=1 to open)
    - 503: HERMES_GATEWAY_SECRET not configured on mundi-app
    - 401: HMAC signature missing or mismatched
    - 422: payload shape invalid (Pydantic-driven)
    - 404: tool_name not in the dispatch registry OR conversation not found
    - 200: tool executed, result returned as `{"result": <tool output>}`
    - 200: tool raised — returned as `{"result": {"status": "error", ...}}`
      (caller-visible failure, not endpoint failure: we still authed and
      dispatched correctly, the tool just couldn't do its job)
    """
    if not tool_call_is_enabled():
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=(
                "Tool-call route is disabled. Set MUNDI_TOOL_CALL_ENABLED=1 "
                "after Hermes gateway sidecar is configured and the "
                "ingabe-sage plugin is loaded."
            ),
        )

    # Same operator-vs-caller distinction as /inbox. 503 = your fault, 401 = mine.
    if get_gateway_secret() is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="HERMES_GATEWAY_SECRET not set in mundi-app environment",
        )

    raw = await request.body()
    if not verify_hermes_signature(raw, x_hermes_signature):
        # CRITICAL: this check MUST fire before we read partner_id/user_id
        # from the body. Setting GUCs based on unverified caller-supplied
        # IDs would let a docker-network attacker pick any partner they
        # wanted and trick RLS into showing them another partner's data.
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="signature_required",
        )

    try:
        payload = ToolCallPayload.model_validate_json(raw)
    except ValidationError as e:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"invalid tool-call payload: {e.errors()}",
        )

    # Whitelist check — payload.tool_name must be a known Sage tool. Without
    # this guard, a forged signature could dispatch arbitrary code in the
    # process. Even with auth, only the curated registry is dispatchable.
    registry = get_pydantic_tool_calls()
    if payload.tool_name not in registry:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"unknown tool: {payload.tool_name!r}",
        )
    fn, ArgModel, MetaModel = registry[payload.tool_name]

    # conversation_id arrives as str (Hermes plugin stringifies); the DB
    # column is int. Coerce explicitly so we get a clean 422 on garbage.
    try:
        conversation_id_int = int(payload.conversation_id)
    except (TypeError, ValueError):
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"conversation_id is not an integer: {payload.conversation_id!r}",
        )

    # Resolve map + project from the conversation. Runs without GUCs —
    # see _resolve_map_and_project's docstring for the security note on
    # why that's acceptable.
    map_id, project_id = await _resolve_map_and_project(conversation_id_int)

    # Parse tool-specific arguments. Validation failure is a CALLER error
    # (200 with status=error in the result body), not a 4xx — Hermes still
    # wants to feed an error string back to the LLM so it can retry.
    try:
        parsed_args = ArgModel(**(payload.arguments or {}))
    except (ValidationError, TypeError, ValueError) as e:
        return {
            "result": {
                "status": "error",
                "error": f"Invalid arguments for {payload.tool_name}: {e}",
            },
        }

    # Now open a partner-scoped connection. From here until __aexit__ all
    # DB work runs with RLS bound to (partner_id, user_id) from the
    # signed payload.
    ephemeral_label = f"Sage is running {payload.tool_name}…"
    async with async_conn(
        "tool-call.dispatch",
        user_id=payload.user_id,
        partner_id=payload.partner_id,
    ):
        async with kue_ephemeral_action(
            conversation_id_int,
            ephemeral_label,
        ):
            try:
                meta_args = MetaModel(
                    user_uuid=payload.user_id,
                    conversation_id=conversation_id_int,
                    map_id=map_id,
                    project_id=project_id,
                    session=ServiceUserContext(
                        user_uuid=payload.user_id,
                        partner_id=payload.partner_id,
                    ),
                )
                tool_result = await fn(parsed_args, meta_args)
            except Exception as e:
                # Bubbled tool failures: do NOT 500. Hermes turn loop must
                # see a parseable result so it can apologize. Same shape as
                # the in-process chat-loop uses (src/routes/message_routes.py).
                logger.exception(
                    "tool-call dispatch failed (tool=%s conv=%s partner=%s)",
                    payload.tool_name, conversation_id_int, payload.partner_id,
                )
                tool_result = {
                    "status": "error",
                    "error": f"{payload.tool_name} failed: {e}",
                }

    # Confirm the result is JSON-serializable before returning — FastAPI
    # will otherwise emit a confusing 500. Round-tripping catches any
    # non-JSON tool outputs (e.g. raw numpy arrays) early.
    try:
        json.dumps(tool_result)
    except (TypeError, ValueError) as e:
        logger.error(
            "tool-call result not JSON-serializable (tool=%s): %s",
            payload.tool_name, e,
        )
        tool_result = {
            "status": "error",
            "error": f"{payload.tool_name} returned non-serializable result",
        }

    return {"result": tool_result}
