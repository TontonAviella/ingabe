"""Internal inbox endpoint — receives messages from Hermes gateway sidecar.

This is the seam where channels (WhatsApp, Telegram, Slack, etc.) handed
off by Hermes gateway re-enter mundi-app's identity + data plane.

## Flow (per project_unified_account_design.md)

```
1. Hermes gateway receives WhatsApp message at BK's number
2. Hermes calls POST /internal/inbox with HMAC-signed payload:
     {
       partner_id: "<uuid>",
       channel: "whatsapp",
       external_id: "+250-78x-xxxxxx",
       message: "What's the rainfall forecast?",
       hermes_session_id: "sess_...",
       received_at: "2026-05-14T22:00:00Z",
       idempotency_key: "<channel>:<message_id>"
     }
3. mundi-app verifies HMAC with HERMES_GATEWAY_SECRET shared secret.
4. Looks up user_channel_bindings WHERE (channel, external_id, partner_id)
   AND revoked_at IS NULL.
5a. Match → route to Sage with that user_uuid + partner_id, stream
    response back to gateway via ACP for delivery to channel.
5b. No match → check if message looks like "VERIFY <code>" → consume
    channel_bind_codes row → INSERT binding → confirm to user.
5c. Otherwise → ignore + send help instructions back via gateway.
```

## Security

- HMAC-SHA256 over the canonical payload, header `X-Hermes-Signature`.
  Shared secret in env `HERMES_GATEWAY_SECRET`. Without it, the route
  responds 503 (not configured). Without a matching signature, 401.
- Idempotency: `idempotency_key` is `{channel}:{provider_message_id}`.
  Replays return the cached response within a TTL window. Prevents
  double-processing on gateway retries.
- Phone is partner-scoped: same number can bind different users in
  different partners. The unique constraint on user_channel_bindings
  enforces this at the DB layer.
- No verbose error responses to the caller. Errors return generic 401/
  503/422. Avoid leaking partner existence to an unsigned caller.

## State as of 2026-05-14

This module is scaffolding. The PR #46 wiring (HMAC + binding lookup +
verify-code consumption + Sage dispatch) lands in a follow-up. Right
now the endpoint returns 503 with a clear "not yet wired" message so
operators who configure the gateway prematurely get an explicit signal.

The `MUNDI_INBOX_ENABLED=0` env default ensures even after the wiring
lands, the route is opt-in per-deploy. Flip to 1 after Hermes gateway
is configured and the binding tables have at least one verified row.
"""
from __future__ import annotations

import os
import logging
from typing import Optional

from fastapi import APIRouter, HTTPException, Request, Header, status
from pydantic import BaseModel

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/internal", tags=["internal"])


class InboxPayload(BaseModel):
    """Canonical inbound message payload from Hermes gateway.

    Field shape is locked in by project_unified_account_design.md. Add
    new fields by extending — never break existing field semantics.
    """
    partner_id: str
    channel: str  # 'whatsapp', 'telegram', 'slack', etc.
    external_id: str  # E.164 phone, telegram handle, slack user id
    message: str
    hermes_session_id: str
    received_at: str  # ISO 8601
    idempotency_key: str  # e.g. "whatsapp:wamid.xxx"


def inbox_is_enabled() -> bool:
    """True iff MUNDI_INBOX_ENABLED env var is set to a truthy value.

    Even after the wiring is implemented in a follow-up PR, this flag
    keeps the route opt-in per deploy. Operators turn it on only after
    Hermes gateway is configured + bindings are seeded.
    """
    val = os.environ.get("MUNDI_INBOX_ENABLED", "0").strip().lower()
    return val in {"1", "true", "yes"}


def _verify_hmac(raw_body: bytes, signature: Optional[str]) -> bool:
    """Constant-time HMAC verification.

    Returns True iff `signature` matches HMAC-SHA256(raw_body,
    HERMES_GATEWAY_SECRET). False if either is missing.

    Currently a placeholder — proper implementation lands in the follow-
    up PR alongside Hermes gateway sidecar wiring. Returns False today.
    """
    secret = os.environ.get("HERMES_GATEWAY_SECRET")
    if not secret or not signature:
        return False
    # TODO: implement hmac.compare_digest(hmac.new(secret, raw_body,
    # 'sha256').hexdigest(), signature) when the gateway is signing
    # for real. Default to False until verified end-to-end.
    return False


@router.post("/inbox")
async def inbox(
    request: Request,
    x_hermes_signature: Optional[str] = Header(default=None),
):
    """Receive a message from Hermes gateway. Auth via HMAC.

    States this endpoint can return:
    - 503: route is disabled (default; flip MUNDI_INBOX_ENABLED=1 to open)
    - 401: HMAC signature missing or mismatched
    - 422: payload shape invalid
    - 200: accepted (also when binding is missing — we send a help message
           via the gateway, never reject the caller mid-flight)

    Per project_unified_account_design.md, after binding lookup:
    - matched → dispatch into Sage with that user_uuid + partner_id
    - unmatched + "VERIFY <code>" pattern → consume code → create binding
    - unmatched + anything else → send onboarding instructions via gateway
    """
    if not inbox_is_enabled():
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=(
                "Inbox route is disabled. Set MUNDI_INBOX_ENABLED=1 after "
                "Hermes gateway sidecar is configured and HERMES_GATEWAY_SECRET "
                "is set in the environment."
            ),
        )

    raw = await request.body()
    if not _verify_hmac(raw, x_hermes_signature):
        # Generic 401 — don't leak whether the secret is unset vs sig
        # mismatch. Either way the caller is not authorized.
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="signature_required",
        )

    # Once the wiring PR lands, parse + dispatch happens here:
    #   payload = InboxPayload.model_validate_json(raw)
    #   binding = await _lookup_binding(payload)
    #   if binding: await _dispatch_to_sage(binding, payload)
    #   elif _is_verify_pattern(payload): await _consume_bind_code(payload)
    #   else: await _send_help_message(payload)
    #
    # Until then, return a clear error so operators who flip the flag
    # before the wiring exists see exactly what's missing.
    raise HTTPException(
        status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
        detail=(
            "Inbox endpoint scaffold is live but the dispatch wiring is "
            "not yet implemented. See PR #46 for the wiring. Rollback: "
            "set MUNDI_INBOX_ENABLED=0."
        ),
    )
