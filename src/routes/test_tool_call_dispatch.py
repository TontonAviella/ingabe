"""Tests for /internal/tool-call dispatch wiring (PR #55).

Auth-side tests live in src/dependencies/test_hermes_auth.py — this file
focuses on what happens AFTER signature verification passes:

  - unknown tool_name → 404 (whitelist enforcement, critical security)
  - bad conversation_id → 422
  - tool arg validation failure → 200 with status=error (not 4xx — the
    LLM needs a parseable result to retry from)
  - tool fn raises → 200 with status=error (same reason)
  - happy path → 200 with {"result": <tool output>}

The orchestration is heavy on FastAPI + DB + WebSocket plumbing, so the
tests monkeypatch the surrounding helpers to isolate the dispatch logic.
DB-touching conversation-lookup behaviour belongs in an integration suite.
"""
from __future__ import annotations

import hashlib
import hmac
import json
from contextlib import asynccontextmanager
from typing import Any

import pytest
from fastapi.testclient import TestClient
from pydantic import BaseModel

from src.routes import tool_call_routes
from src.wsgi import app


SECRET = "test-shared-secret-xxxxxxxxxxxxxxxx"


def _sign(body: bytes) -> str:
    return hmac.new(SECRET.encode(), body, hashlib.sha256).hexdigest()


# ---------------------------------------------------------------------------
# Fake tool registry — keeps each test independent of the real Sage surface
# ---------------------------------------------------------------------------


class _StubArgs(BaseModel):
    layer_id: str
    band: int


_LAST_CALL: dict[str, Any] = {}


async def _stub_tool(args: _StubArgs, meta: Any) -> dict[str, Any]:
    """Records the call and returns a deterministic result.

    The meta capture is the only way we can verify partner_id / user_id /
    map_id / project_id / session were threaded through correctly.
    """
    _LAST_CALL.clear()
    _LAST_CALL["args"] = args.model_dump()
    _LAST_CALL["meta"] = {
        "user_uuid": meta.user_uuid,
        "conversation_id": meta.conversation_id,
        "map_id": meta.map_id,
        "project_id": meta.project_id,
        # session is a UserContext; capture its user_id surface only
        "session_user_id": meta.session.get_user_id(),
        "session_org_id": meta.session.get_org_id(),
    }
    return {"status": "ok", "value": 0.62}


async def _raising_tool(args: _StubArgs, meta: Any) -> dict[str, Any]:
    raise RuntimeError("simulated tool failure")


class _MetaStub(BaseModel):
    """Minimal IngabeToolCallMetaArgs replacement to avoid heavy imports."""
    model_config = {"arbitrary_types_allowed": True}
    user_uuid: str
    conversation_id: int
    map_id: str
    project_id: str
    session: Any


@pytest.fixture
def stub_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Enable the route, configure the secret, no-op the DB/WS side effects."""
    monkeypatch.setenv("MUNDI_TOOL_CALL_ENABLED", "1")
    monkeypatch.setenv("HERMES_GATEWAY_SECRET", SECRET)

    # No real DB: bypass conversation lookup. Signature mirrors the real
    # _resolve_map_and_project — accepts user_id so the RLS-scoped lookup
    # path is exercised at the call site.
    async def _fake_lookup(conversation_id: int, user_id: str) -> tuple[str, str]:
        return ("M00000000001", "P00000000001")
    monkeypatch.setattr(
        tool_call_routes,
        "_resolve_map_and_project",
        _fake_lookup,
    )

    # No real WebSocket: stub kue_ephemeral_action
    @asynccontextmanager
    async def _fake_ephemeral(*args, **kwargs):
        yield None
    monkeypatch.setattr(tool_call_routes, "kue_ephemeral_action", _fake_ephemeral)


@pytest.fixture
def stub_registry(monkeypatch: pytest.MonkeyPatch) -> dict:
    """Replace the tool registry with a tiny one we control."""
    registry = {
        "stub_tool": (_stub_tool, _StubArgs, _MetaStub),
        "stub_raising_tool": (_raising_tool, _StubArgs, _MetaStub),
    }
    monkeypatch.setattr(
        tool_call_routes,
        "get_pydantic_tool_calls",
        lambda: registry,
    )
    return registry


# ---------------------------------------------------------------------------
# Gate / config / auth tests
# ---------------------------------------------------------------------------


def test_returns_503_when_route_disabled(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("MUNDI_TOOL_CALL_ENABLED", raising=False)
    with TestClient(app) as c:
        r = c.post("/internal/tool-call", content=b"{}")
    assert r.status_code == 503
    assert "disabled" in r.json()["detail"].lower()


def test_returns_503_when_secret_unset(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MUNDI_TOOL_CALL_ENABLED", "1")
    monkeypatch.delenv("HERMES_GATEWAY_SECRET", raising=False)
    with TestClient(app) as c:
        r = c.post("/internal/tool-call", content=b"{}")
    assert r.status_code == 503
    assert "HERMES_GATEWAY_SECRET" in r.json()["detail"]


def test_returns_401_when_signature_missing(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MUNDI_TOOL_CALL_ENABLED", "1")
    monkeypatch.setenv("HERMES_GATEWAY_SECRET", SECRET)
    with TestClient(app) as c:
        r = c.post("/internal/tool-call", content=b"{}")
    assert r.status_code == 401


def test_returns_401_when_signature_wrong(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MUNDI_TOOL_CALL_ENABLED", "1")
    monkeypatch.setenv("HERMES_GATEWAY_SECRET", SECRET)
    body = b'{"x":1}'
    with TestClient(app) as c:
        r = c.post(
            "/internal/tool-call",
            content=body,
            headers={"X-Hermes-Signature": "deadbeef" * 8},
        )
    assert r.status_code == 401


# ---------------------------------------------------------------------------
# Payload validation
# ---------------------------------------------------------------------------


def test_returns_422_on_malformed_payload(stub_env: None) -> None:
    """Missing required fields trips Pydantic — surface as 422."""
    body = json.dumps({"partner_id": "p"}).encode()  # missing the rest
    with TestClient(app) as c:
        r = c.post(
            "/internal/tool-call",
            content=body,
            headers={"X-Hermes-Signature": _sign(body)},
        )
    assert r.status_code == 422


def test_returns_404_on_unknown_tool_name(
    stub_env: None, stub_registry: dict,
) -> None:
    """Whitelist enforcement — anything not in the registry is dispatchable.
    Critical: even a forged signature can only run curated tools."""
    body = json.dumps({
        "partner_id": "p", "user_id": "u",
        "conversation_id": "1", "tool_name": "rm_rf_slash",
        "arguments": {},
    }).encode()
    with TestClient(app) as c:
        r = c.post(
            "/internal/tool-call",
            content=body,
            headers={"X-Hermes-Signature": _sign(body)},
        )
    assert r.status_code == 404
    assert "rm_rf_slash" in r.json()["detail"]


def test_returns_422_on_non_integer_conversation_id(
    stub_env: None, stub_registry: dict,
) -> None:
    body = json.dumps({
        "partner_id": "p", "user_id": "u",
        "conversation_id": "not-an-int", "tool_name": "stub_tool",
        "arguments": {"layer_id": "L", "band": 1},
    }).encode()
    with TestClient(app) as c:
        r = c.post(
            "/internal/tool-call",
            content=body,
            headers={"X-Hermes-Signature": _sign(body)},
        )
    assert r.status_code == 422


# ---------------------------------------------------------------------------
# Dispatch outcomes
# ---------------------------------------------------------------------------


def test_bad_tool_arguments_return_200_with_status_error(
    stub_env: None, stub_registry: dict,
) -> None:
    """Pydantic arg-model failures must NOT 4xx — the LLM needs a parseable
    string result so it can retry with corrected arguments."""
    body = json.dumps({
        "partner_id": "p", "user_id": "u",
        "conversation_id": "1", "tool_name": "stub_tool",
        "arguments": {"layer_id": "L"},  # missing `band`
    }).encode()
    with TestClient(app) as c:
        r = c.post(
            "/internal/tool-call",
            content=body,
            headers={"X-Hermes-Signature": _sign(body)},
        )
    assert r.status_code == 200
    payload = r.json()["result"]
    assert payload["status"] == "error"
    assert "Invalid arguments" in payload["error"]


def test_tool_raising_returns_200_with_status_error(
    stub_env: None, stub_registry: dict,
) -> None:
    """A raised exception inside the tool fn must become a result-shape
    error, not a 500. The Hermes turn loop apologizes from the result;
    a 500 would crash the turn."""
    body = json.dumps({
        "partner_id": "p", "user_id": "u",
        "conversation_id": "1", "tool_name": "stub_raising_tool",
        "arguments": {"layer_id": "L", "band": 1},
    }).encode()
    with TestClient(app) as c:
        r = c.post(
            "/internal/tool-call",
            content=body,
            headers={"X-Hermes-Signature": _sign(body)},
        )
    assert r.status_code == 200
    payload = r.json()["result"]
    assert payload["status"] == "error"
    assert "stub_raising_tool failed" in payload["error"]
    assert "simulated tool failure" in payload["error"]


def test_happy_path_returns_result_and_threads_meta_args_through(
    stub_env: None, stub_registry: dict,
) -> None:
    """The dispatch must (a) return the tool's return value verbatim in
    `result`, and (b) construct IngabeToolCallMetaArgs with the correct
    user_uuid, partner_id (via session.org_id), conversation_id, and
    resolved map_id/project_id from the conversation lookup."""
    body = json.dumps({
        "partner_id": "partner-bk",
        "user_id": "user-aaaa",
        "conversation_id": "42",
        "tool_name": "stub_tool",
        "arguments": {"layer_id": "L1", "band": 2},
    }).encode()
    with TestClient(app) as c:
        r = c.post(
            "/internal/tool-call",
            content=body,
            headers={"X-Hermes-Signature": _sign(body)},
        )

    assert r.status_code == 200, r.text
    assert r.json() == {"result": {"status": "ok", "value": 0.62}}

    # Verify meta_args were threaded correctly into the tool fn
    assert _LAST_CALL["args"] == {"layer_id": "L1", "band": 2}
    assert _LAST_CALL["meta"]["user_uuid"] == "user-aaaa"
    assert _LAST_CALL["meta"]["conversation_id"] == 42
    assert _LAST_CALL["meta"]["map_id"] == "M00000000001"
    assert _LAST_CALL["meta"]["project_id"] == "P00000000001"
    # ServiceUserContext.get_user_id / get_org_id surface the IDs
    assert _LAST_CALL["meta"]["session_user_id"] == "user-aaaa"
    assert _LAST_CALL["meta"]["session_org_id"] == "partner-bk"
