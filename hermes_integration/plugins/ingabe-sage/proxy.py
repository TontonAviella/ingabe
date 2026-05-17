"""HMAC-signed HTTP proxy from the Hermes plugin to mundi-app.

This is the bridge that lets Hermes tool handlers (running inside the
hermes-gateway container) execute Sage tools by calling back into
mundi-app's `/internal/tool-call` endpoint with an authenticated payload.

## Why proxy instead of in-process

Sage's real tool handlers live in mundi-app:
  - They need partner-scoped DB connections via PostgreSQL RLS (GUCs)
  - They depend on Qdrant, MinIO, the brain service, etc.
  - They are async (asyncpg, async httpx) — Hermes plugin handlers are sync

Re-implementing them inside the plugin would mean duplicating the entire
data-access layer. Cheaper: keep handlers in mundi-app, let Hermes call
back over HTTP. mundi-app's /internal/tool-call sets the RLS GUCs from
the HMAC-verified (partner_id, user_id) and dispatches the named tool.

## Auth model

Identical to /internal/inbox:

  signature = HMAC-SHA256(HERMES_GATEWAY_SECRET, raw_body).hexdigest()
  POST /internal/tool-call
  X-Hermes-Signature: <hex digest>
  body: ToolCallPayload JSON

See src/dependencies/hermes_auth.py for the verification side.

## Failure modes (returned to the LLM as JSON, never raised)

  - HERMES_GATEWAY_SECRET unset in the gateway env  → config_error
  - No IngabeContext set (no partner/user/conversation) → context_missing
  - mundi-app returns 503 (route disabled or dispatch unwired)  → upstream_unavailable
  - mundi-app returns 401 (signature mismatch)  → auth_failed
  - mundi-app returns 4xx/5xx for any other reason  → upstream_error
  - Network error / timeout  → network_error
  - mundi-app returns 200 OK  → result is unwrapped and returned

All failure shapes are JSON-stringified per Hermes's tool-result contract
so the LLM can read them and decide whether to retry or apologize.

## Timeouts

Default 90s — most Sage tools complete in under 5s, but raster operations
(zonal stats over a large polygon, NDVI snapshots) can take 30-60s. The
ceiling is bounded by mundi-app's own request timeout, not by us.

PRs after this one (especially #55, which wires dispatch on the mundi-app
side) MUST keep this timeout in mind — long-running tools should chunk
results or move to background dispatch before this becomes a problem.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import logging
import os
from typing import Any, Callable, Dict, Optional

import httpx

from .context import get_ingabe_context

logger = logging.getLogger(__name__)

# Env vars. Both must be set in the hermes-gateway container.
MUNDI_APP_URL_ENV = "MUNDI_APP_URL"
HERMES_GATEWAY_SECRET_ENV = "HERMES_GATEWAY_SECRET"

# In the prod docker-compose network, mundi-app is reachable by service name.
DEFAULT_MUNDI_APP_URL = "http://app:8000"

# 90s upper bound — see module docstring rationale.
TOOL_CALL_TIMEOUT_SECONDS = 90.0


def _get_mundi_app_url() -> str:
    """Read MUNDI_APP_URL from env, falling back to the compose service DNS."""
    return os.environ.get(MUNDI_APP_URL_ENV, "").strip() or DEFAULT_MUNDI_APP_URL


def _get_gateway_secret() -> Optional[bytes]:
    """Read HERMES_GATEWAY_SECRET from env. Returns None if unset.

    Same convention as src/dependencies/hermes_auth.py:get_gateway_secret —
    None means "operator hasn't configured this side yet", which we surface
    to the LLM as a config_error (not a network or auth failure).
    """
    val = os.environ.get(HERMES_GATEWAY_SECRET_ENV, "").strip()
    if not val:
        return None
    return val.encode("utf-8")


def _sign_body(secret: bytes, raw_body: bytes) -> str:
    """Return the lowercase hex HMAC-SHA256 digest of `raw_body`.

    Matches verify_hermes_signature on the receiving side. Centralized here
    so test_proxy.py can call the same primitive that production uses.
    """
    return hmac.new(secret, raw_body, hashlib.sha256).hexdigest()


def _error(kind: str, tool_name: str, message: str, **extra: Any) -> str:
    """Build a stable JSON error payload that the LLM can pattern-match.

    Stable `status` taxonomy:
      - config_error: mundi-app not configured properly
      - context_missing: no IngabeContext (partner/user/conversation)
      - upstream_unavailable: /internal/tool-call returned 503
      - auth_failed: /internal/tool-call returned 401 (signature mismatch)
      - upstream_error: any other 4xx/5xx
      - network_error: connection refused, timeout, DNS failure, etc.
    """
    payload: Dict[str, Any] = {
        "status": kind,
        "tool_name": tool_name,
        "message": message,
    }
    payload.update(extra)
    return json.dumps(payload)


def proxy_tool_call(
    tool_name: str,
    arguments: Dict[str, Any],
    task_id: Optional[str] = None,
) -> str:
    """Dispatch a Sage tool by POSTing to mundi-app's /internal/tool-call.

    Sync function — Hermes invokes tool handlers synchronously. If the
    underlying mundi-app handler is async, mundi-app handles that on its
    side. This proxy just speaks HTTP.

    Returns a JSON string in all cases (success, error, anything). Never
    raises — Hermes treats raised exceptions as crashes that abort the
    whole turn, which is wrong for "this one tool didn't work".
    """
    secret = _get_gateway_secret()
    if secret is None:
        logger.error("proxy_tool_call: %s set but no HERMES_GATEWAY_SECRET in env (tool=%s)", HERMES_GATEWAY_SECRET_ENV, tool_name)
        return _error(
            "config_error",
            tool_name,
            (
                f"{HERMES_GATEWAY_SECRET_ENV} is not set in the hermes-gateway "
                "container environment. Tool dispatch cannot be authenticated."
            ),
        )

    ctx = get_ingabe_context(required=False)
    if ctx is None or ctx.partner_id is None or ctx.conversation_id is None:
        # We can't dispatch without (partner_id, user_id, conversation_id) —
        # mundi-app needs those to set RLS GUCs and link results to the chat.
        # Surface this clearly so the LLM can apologize rather than retry.
        return _error(
            "context_missing",
            tool_name,
            (
                "No IngabeContext available — partner_id, user_id, and "
                "conversation_id are required to dispatch tools but were not "
                "set by the caller."
            ),
            have_user_uuid=(ctx.user_uuid if ctx else None),
            have_partner_id=(ctx.partner_id if ctx else None),
            have_conversation_id=(ctx.conversation_id if ctx else None),
        )

    payload = {
        "partner_id": ctx.partner_id,
        "user_id": ctx.user_uuid,
        "conversation_id": str(ctx.conversation_id),
        "tool_name": tool_name,
        "arguments": arguments,
    }
    # Canonicalize so the signature matches what we send on the wire.
    raw_body = json.dumps(payload, separators=(",", ":"), sort_keys=True).encode("utf-8")
    signature = _sign_body(secret, raw_body)

    url = _get_mundi_app_url().rstrip("/") + "/internal/tool-call"
    headers = {
        "Content-Type": "application/json",
        "X-Hermes-Signature": signature,
    }

    try:
        with httpx.Client(timeout=TOOL_CALL_TIMEOUT_SECONDS) as client:
            response = client.post(url, content=raw_body, headers=headers)
    except (httpx.ConnectError, httpx.ReadTimeout, httpx.RemoteProtocolError) as exc:
        # Network/transport problem. Don't leak the URL contents to the
        # LLM — keep the error message generic.
        logger.warning(
            "proxy_tool_call: network error reaching mundi-app (tool=%s task=%s): %s",
            tool_name, task_id, exc,
        )
        return _error(
            "network_error",
            tool_name,
            "Could not reach mundi-app to dispatch the tool. The service may be restarting.",
        )

    if response.status_code == 503:
        # /internal/tool-call returns 503 in two cases:
        #   1. MUNDI_TOOL_CALL_ENABLED=0 (operator hasn't opened the route)
        #   2. dispatch wiring not yet implemented (PR #54 ships before #55
        #      so this is the EXPECTED initial response from a fresh deploy)
        # Both are operator-visible problems, not LLM problems.
        return _error(
            "upstream_unavailable",
            tool_name,
            "mundi-app's /internal/tool-call endpoint is not yet wired to dispatch this tool.",
            upstream_status=503,
        )

    if response.status_code == 401:
        return _error(
            "auth_failed",
            tool_name,
            "HMAC signature was rejected by mundi-app. Check HERMES_GATEWAY_SECRET on both sides.",
        )

    if response.status_code >= 400:
        # Don't dump full body to logs — could contain partner data. Truncate.
        body_excerpt = response.text[:200]
        logger.warning(
            "proxy_tool_call: upstream %d (tool=%s task=%s body=%r)",
            response.status_code, tool_name, task_id, body_excerpt,
        )
        return _error(
            "upstream_error",
            tool_name,
            f"mundi-app returned HTTP {response.status_code}.",
            upstream_status=response.status_code,
        )

    # 2xx — the body should be JSON. Pass it through as a string.
    body = response.text
    # Validate it's parseable JSON; if mundi-app sent garbage, surface that
    # clearly rather than letting the LLM choke on it downstream.
    try:
        json.loads(body)
    except ValueError:
        return _error(
            "upstream_error",
            tool_name,
            "mundi-app returned non-JSON content on a 2xx response.",
            upstream_status=response.status_code,
        )
    return body


def make_proxy_handler(tool_name: str) -> Callable[..., str]:
    """Return a Hermes-compatible sync handler that proxies via HTTP.

    Closes over `tool_name` so every registered tool gets a handler that
    routes to that specific endpoint without per-call lookups.
    """
    def _handler(args: Dict[str, Any], **kw: Any) -> str:
        return proxy_tool_call(
            tool_name=tool_name,
            arguments=args,
            task_id=kw.get("task_id"),
        )
    _handler.__name__ = f"_proxy_handler_{tool_name}"
    _handler.__doc__ = f"Auto-generated HTTP proxy handler for Sage tool {tool_name!r}."
    return _handler
