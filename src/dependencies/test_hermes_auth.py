"""Tests for the shared Hermes gateway HMAC verifier.

This is the security boundary for /internal/inbox and /internal/tool-call.
A bug here lets anyone on the docker network dispatch tool calls with any
(partner_id, user_id) they want, bypassing application-layer multi-tenant
isolation (RLS still holds at the DB layer, but the GUC would be set
based on attacker-supplied IDs).

These tests are pure unit tests — no FastAPI, no live DB, no env coupling
beyond monkeypatch.
"""
from __future__ import annotations

import hmac
import hashlib

import pytest

from src.dependencies.hermes_auth import (
    HERMES_GATEWAY_SECRET_ENV,
    get_gateway_secret,
    verify_hermes_signature,
)


def _sign(secret: bytes, body: bytes) -> str:
    """Mirror of what the Hermes side will do: HMAC-SHA256, lowercase hex."""
    return hmac.new(secret, body, hashlib.sha256).hexdigest()


# ---------------------------------------------------------------------------
# get_gateway_secret — config-state distinction
# ---------------------------------------------------------------------------


def test_get_gateway_secret_unset_returns_none(monkeypatch):
    """Unset env → None. Caller can distinguish 'misconfigured' (503) from
    'bad signature' (401)."""
    monkeypatch.delenv(HERMES_GATEWAY_SECRET_ENV, raising=False)
    assert get_gateway_secret() is None


def test_get_gateway_secret_empty_string_returns_none(monkeypatch):
    """Empty string == unset. Operators who set HERMES_GATEWAY_SECRET= in
    .env (with nothing after the =) get the same misconfig signal."""
    monkeypatch.setenv(HERMES_GATEWAY_SECRET_ENV, "")
    assert get_gateway_secret() is None


def test_get_gateway_secret_whitespace_only_returns_none(monkeypatch):
    """A trailing newline / accidental whitespace shouldn't count as
    'configured'. We trim before checking."""
    monkeypatch.setenv(HERMES_GATEWAY_SECRET_ENV, "   \n  ")
    assert get_gateway_secret() is None


def test_get_gateway_secret_returns_bytes(monkeypatch):
    """When set, returns UTF-8 encoded bytes (required for hmac.new)."""
    monkeypatch.setenv(HERMES_GATEWAY_SECRET_ENV, "test-secret-value")
    result = get_gateway_secret()
    assert result == b"test-secret-value"


# ---------------------------------------------------------------------------
# verify_hermes_signature — the actual security boundary
# ---------------------------------------------------------------------------


def test_verify_valid_signature_passes(monkeypatch):
    """The happy path: signed body, matching signature → True."""
    secret = "shared-secret-from-prod-dot-env"
    monkeypatch.setenv(HERMES_GATEWAY_SECRET_ENV, secret)
    body = b'{"partner_id":"abc","tool_name":"foo"}'
    sig = _sign(secret.encode(), body)
    assert verify_hermes_signature(body, sig) is True


def test_verify_wrong_signature_fails(monkeypatch):
    """Tampered signature → False. The hex digest is one char off."""
    secret = "shared-secret"
    monkeypatch.setenv(HERMES_GATEWAY_SECRET_ENV, secret)
    body = b'{"partner_id":"abc"}'
    sig = _sign(secret.encode(), body)
    tampered = sig[:-1] + ("0" if sig[-1] != "0" else "1")
    assert verify_hermes_signature(body, tampered) is False


def test_verify_wrong_body_fails(monkeypatch):
    """Same key, signature was for a different body → False.
    Protects against attackers swapping the body and re-using a captured signature
    from a different payload."""
    secret = "shared-secret"
    monkeypatch.setenv(HERMES_GATEWAY_SECRET_ENV, secret)
    body_a = b'{"partner_id":"BK"}'
    body_b = b'{"partner_id":"PARTNER2"}'
    sig_a = _sign(secret.encode(), body_a)
    # Sig from body_a, attempting to authenticate body_b.
    assert verify_hermes_signature(body_b, sig_a) is False


def test_verify_wrong_secret_fails(monkeypatch):
    """Signature was made with a different secret → False. The attacker
    knew the body but didn't know the secret."""
    monkeypatch.setenv(HERMES_GATEWAY_SECRET_ENV, "real-secret")
    body = b'{"partner_id":"abc"}'
    forged = _sign(b"attackers-guess", body)
    assert verify_hermes_signature(body, forged) is False


def test_verify_no_signature_fails(monkeypatch):
    """Missing X-Hermes-Signature header → False. The route handler turns
    this into 401 signature_required."""
    monkeypatch.setenv(HERMES_GATEWAY_SECRET_ENV, "real-secret")
    body = b'{"partner_id":"abc"}'
    assert verify_hermes_signature(body, None) is False
    assert verify_hermes_signature(body, "") is False


def test_verify_no_secret_fails(monkeypatch):
    """Even with a 'matching' signature, no configured secret → False.
    The route handler turns this into 503 (misconfig), not 401."""
    monkeypatch.delenv(HERMES_GATEWAY_SECRET_ENV, raising=False)
    body = b'{"x":1}'
    sig = _sign(b"any-secret", body)
    assert verify_hermes_signature(body, sig) is False


def test_verify_case_insensitive_hex(monkeypatch):
    """Hex digest can arrive in upper or lower case. We normalize to
    lowercase before constant-time compare. Hermes sends lowercase but
    if a future client capitalizes, we still accept."""
    secret = "shared-secret"
    monkeypatch.setenv(HERMES_GATEWAY_SECRET_ENV, secret)
    body = b'{"x":1}'
    sig = _sign(secret.encode(), body)
    assert verify_hermes_signature(body, sig.upper()) is True


def test_verify_strips_whitespace_in_signature(monkeypatch):
    """A trailing newline from a copy-paste curl shouldn't fail auth."""
    secret = "shared-secret"
    monkeypatch.setenv(HERMES_GATEWAY_SECRET_ENV, secret)
    body = b'{"x":1}'
    sig = _sign(secret.encode(), body)
    assert verify_hermes_signature(body, f"  {sig}\n") is True


def test_verify_empty_body(monkeypatch):
    """An empty body (b'') is a valid input to hmac. Sig for empty body
    must still verify correctly."""
    secret = "shared-secret"
    monkeypatch.setenv(HERMES_GATEWAY_SECRET_ENV, secret)
    sig = _sign(secret.encode(), b"")
    assert verify_hermes_signature(b"", sig) is True


@pytest.mark.parametrize("body", [
    b"",
    b"\x00\x01\x02\x03",
    b'{"unicode":"\xe6\x97\xa5\xe6\x9c\xac"}',  # UTF-8 Japanese chars
    b"x" * 10000,  # 10KB payload — guards against length-based bugs
])
def test_verify_arbitrary_bytes(body, monkeypatch):
    """Verifier handles binary bodies, large bodies, and unicode without
    blowing up. The body is bytes; encoding is the caller's problem."""
    secret = "shared-secret"
    monkeypatch.setenv(HERMES_GATEWAY_SECRET_ENV, secret)
    sig = _sign(secret.encode(), body)
    assert verify_hermes_signature(body, sig) is True


def test_verify_never_raises_on_garbage_input(monkeypatch):
    """A malformed header (non-hex chars, wrong length) returns False,
    doesn't raise. compare_digest can ValueError on certain inputs;
    we wrap it defensively."""
    monkeypatch.setenv(HERMES_GATEWAY_SECRET_ENV, "secret")
    body = b'{"x":1}'
    # Garbage inputs that aren't valid hex / not the right length:
    for garbage in ["xyz", "!!!not-hex!!!", "deadbeef", "0" * 100]:
        # Should return False, not raise.
        result = verify_hermes_signature(body, garbage)
        assert result is False, f"expected False for {garbage!r}, got {result!r}"
