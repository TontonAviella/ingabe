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
       conversation_id: "<uuid>",
       tool_name: "compute_zonal_stats",
       arguments: {"layer_id": "L...", "geometry": {...}}
     }
4. mundi-app verifies HMAC with HERMES_GATEWAY_SECRET (same secret as /inbox)
5. mundi-app sets app.partner_id + app.user_id GUCs on its DB connection
6. mundi-app dispatches to the existing pydantic_tools handler
7. Result returned to Hermes, who feeds it back to the LLM, who streams response
```

## Why this endpoint exists vs. tools-in-Hermes-plugin

Sage's tools (raster interpretation, similarity, insurance engine, etc.)
need partner-scoped data via Postgres RLS. They run inside mundi-app's
process where the DB GUCs are set per-request. Re-implementing them
inside the Hermes plugin would mean duplicating the data-access layer,
the RLS-aware connection pool, the brain ingestion path, etc.

Cheaper: keep tools in mundi-app, let Hermes call back over HTTP. Same
HMAC scheme as /inbox so the gateway side has one signing recipe.

## Security boundary

HMAC verification on the BODY (see hermes_auth.py for the full scheme).
Without it, anyone on the docker network can dispatch a tool with any
(partner_id, user_id) they want, bypassing RLS at the application layer
(RLS itself still holds at the DB layer — the GUC would just be set wrong
for whatever Hermes asked).

Apply HMAC check BEFORE setting any GUCs. Order matters: never set GUCs
based on caller-supplied IDs unless we've authenticated the caller.

## Why 503-scaffold today, not full dispatch

This PR locks in the SECURITY boundary (HMAC). The actual tool dispatch
(routing payload.tool_name → pydantic_tools handler with RLS-bound
connection) is a separate concern that depends on:

  - The ingabe-sage plugin existing (currently empty per CLAUDE.md TODO)
  - GUC-setting connection pool work
  - Result-shape contract aligned with what Hermes expects

Returning 503 after auth means: operator who configures the secret AND
flips MUNDI_USE_HERMES=1 sees a clear "auth works, dispatch wiring
deferred" signal — not a confusing 401 or 500.
"""
from __future__ import annotations

import logging
import os
from typing import Any, Optional

from fastapi import APIRouter, Header, HTTPException, Request, status
from pydantic import BaseModel

from src.dependencies.hermes_auth import (
    get_gateway_secret,
    verify_hermes_signature,
)

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
    - 422: payload shape invalid (FastAPI auto-handles via Pydantic)
    - 503: auth ok but dispatch wiring not yet implemented (current state)
    - 200: tool executed, result returned (future state, after wiring lands)
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

    # Once the wiring lands:
    #   payload = ToolCallPayload.model_validate_json(raw)
    #   async with rls_scoped_conn(partner_id=payload.partner_id,
    #                              user_id=payload.user_id) as conn:
    #       result = await dispatch_tool(payload.tool_name, payload.arguments, conn)
    #   return {"result": result}
    #
    # Three things must hold before flipping that on:
    #   1. ingabe-sage plugin exists on the Hermes side (currently empty)
    #   2. rls_scoped_conn helper sets app.partner_id + app.user_id GUCs
    #      atomically with the SELECT, so a long-lived pool connection
    #      can't leak GUC state across requests
    #   3. dispatch_tool whitelists tool_name against a known set —
    #      never eval-style-dispatch on caller input
    raise HTTPException(
        status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
        detail=(
            "Tool-call endpoint scaffold is live (auth verified) but the "
            "dispatch wiring is not yet implemented. See PR #51+ for "
            "the wiring. Rollback: set MUNDI_TOOL_CALL_ENABLED=0."
        ),
    )
