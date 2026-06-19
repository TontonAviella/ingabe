"""HMAC verification for /internal/* endpoints called by the Hermes gateway.

Two endpoints share this verification:

  - POST /internal/inbox      — inbound channel messages (WhatsApp/Telegram/etc)
  - POST /internal/tool-call  — Hermes calling back to dispatch a Sage tool

Both endpoints accept a `X-Hermes-Signature` header containing the lowercase
hex digest of `HMAC-SHA256(HERMES_GATEWAY_SECRET, raw_request_body)`.

## Canonical payload

The signed payload is the **raw request body bytes**, nothing else. No
header canonicalization, no timestamp inclusion, no nonce. This keeps the
Hermes side trivial (sign the body, send it, done) and keeps mundi-app's
verification side equally trivial.

Trade-off acknowledged: this scheme is replay-vulnerable in theory — an
attacker who intercepts a valid `(body, signature)` pair could replay it.
Mitigations:

  1. Internal docker network only — port 9999 has no `ports:` mapping
     out of the docker-compose service. UFW blocks 8000 from the
     internet (only nginx reaches mundi-app on 8000).
  2. Idempotency keys in the InboxPayload — replays of the same message
     are deduplicated at the handler level (planned for the wiring PR).
  3. Tool-call replays are bounded by mundi-app's own RLS — even if a
     replay reaches the endpoint, GUCs from the replayed signature have
     to match a still-valid (partner_id, user_id) pair.

If we later need replay-proof signatures (e.g. someone exposes the bridge
on a public network), upgrade to body + ISO timestamp + nonce, signed
together, with 5-minute timestamp window. Don't do that today; it adds
clock-skew bugs nobody wants to debug.

## Why a separate module

Both endpoints need this. Putting it here avoids a circular import that
would otherwise happen if `tool_call_routes.py` imported `inbox_routes.py`
just to share the helper. Tests import this module directly, so the
verification logic is unit-tested independent of the route handlers.
"""
from __future__ import annotations

import hmac
import hashlib
import logging
import os
from typing import Optional

logger = logging.getLogger(__name__)


HERMES_GATEWAY_SECRET_ENV = "HERMES_GATEWAY_SECRET"


def get_gateway_secret() -> Optional[bytes]:
    """Return the configured shared secret as bytes, or None if unset.

    Returning None lets the route handler choose between 503 (not
    configured — operator-visible) and 401 (configured but signature
    mismatch — caller-visible). Never confuses the two.
    """
    val = os.environ.get(HERMES_GATEWAY_SECRET_ENV, "").strip()
    if not val:
        return None
    return val.encode("utf-8")


def verify_hermes_signature(raw_body: bytes, signature: Optional[str]) -> bool:
    """Constant-time check: does `signature` match HMAC-SHA256(secret, raw_body)?

    Returns True iff:
      - HERMES_GATEWAY_SECRET is configured (non-empty)
      - `signature` is present
      - `hmac.compare_digest` confirms a byte-exact match

    Returns False otherwise. Never raises. Never logs the signature or
    the body (those could contain partner messages).

    The lowercase hex digest format matches what Hermes will produce
    when it calls back: `hashlib.sha256(...).hexdigest()`.
    """
    secret = get_gateway_secret()
    if secret is None:
        # Operator didn't configure the secret. Log once-per-process
        # so it's visible on startup; don't log per-request (would
        # spam if someone scans the endpoint).
        return False
    if not signature:
        return False

    expected = hmac.new(secret, raw_body, hashlib.sha256).hexdigest()

    # compare_digest is the whole point of using hmac instead of `==`.
    # Constant-time comparison rules out timing-based signature recovery.
    try:
        return hmac.compare_digest(expected, signature.strip().lower())
    except Exception:
        # compare_digest can raise if inputs are not str/bytes-like.
        # Defensive: never let a malformed header crash the route.
        return False
