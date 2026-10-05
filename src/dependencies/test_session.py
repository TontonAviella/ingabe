"""Tests for authentication error paths in session.py.

Covers legacy-mode rejections, missing MUNDI_AUTH_MODE configuration, and
WorkOS mode failing closed.
"""

import pytest


@pytest.mark.anyio
async def test_unauthenticated_request_rejected(client, env_override):
    """Requests to protected endpoints without auth should return 401."""
    with env_override(MUNDI_AUTH_MODE="view_only"):
        resp = await client.post("/api/maps/create", json={"title": "test"})
        assert resp.status_code == 401


@pytest.mark.anyio
async def test_view_only_cannot_create_map(client, env_override):
    """view_only mode rejects write operations."""
    with env_override(MUNDI_AUTH_MODE="view_only"):
        resp = await client.post("/api/maps/create", json={"title": "test"})
        assert resp.status_code == 401
        assert "Authentication required" in resp.json().get("detail", "")


@pytest.mark.anyio
async def test_edit_mode_allows_request(client, env_override):
    """edit mode allows write operations (legacy single-user)."""
    with env_override(MUNDI_AUTH_MODE="edit"):
        resp = await client.post(
            "/api/maps/create", json={"title": "auth test map"}
        )
        assert resp.status_code == 200


@pytest.mark.anyio
async def test_missing_auth_mode_returns_500(client, env_override):
    """When neither AUTH_PROVIDER=workos nor MUNDI_AUTH_MODE is set, return 500."""
    with env_override(MUNDI_AUTH_MODE=None):
        resp = await client.post("/api/maps/create", json={"title": "test"})
        assert resp.status_code == 500
        assert "AUTH_PROVIDER=workos" in resp.json().get("detail", "")


@pytest.mark.anyio
async def test_get_projects_requires_auth(client, env_override):
    """GET /api/projects requires authentication."""
    with env_override(MUNDI_AUTH_MODE="view_only"):
        resp = await client.get("/api/projects/")
        assert resp.status_code == 401


# ---------------------------------------------------------------------------
# WorkOS mode fails closed (2026-10-05): with AUTH_PROVIDER=workos a Bearer
# token, a ?token= query or missing WorkOS keys used to fall through to Clerk
# or to MUNDI_AUTH_MODE=edit, the shared user with edit rights.
# ---------------------------------------------------------------------------

_WORKOS = dict(
    AUTH_PROVIDER="workos",
    WORKOS_API_KEY="sk_test_x",
    WORKOS_CLIENT_ID="client_x",
    WORKOS_COOKIE_PASSWORD="p" * 40,
    MUNDI_AUTH_MODE="edit",
)


@pytest.mark.anyio
async def test_workos_mode_ignores_bearer_tokens(client, env_override):
    with env_override(**_WORKOS):
        resp = await client.post(
            "/api/maps/create", json={"title": "x"}, headers={"Authorization": "Bearer anything"}
        )
        assert resp.status_code == 401


@pytest.mark.anyio
async def test_workos_selected_without_keys_refuses_instead_of_edit_mode(client, env_override):
    with env_override(**{**_WORKOS, "WORKOS_API_KEY": None}):
        resp = await client.post("/api/maps/create", json={"title": "x"})
        assert resp.status_code == 503


@pytest.mark.anyio
async def test_workos_websocket_ignores_token_query(env_override):
    from types import SimpleNamespace

    from fastapi import WebSocketException

    from src.dependencies.session import verify_websocket

    socket = SimpleNamespace(query_params={"token": "anything"}, cookies={})
    with env_override(**_WORKOS):
        with pytest.raises(WebSocketException) as rejected:
            await verify_websocket(socket)
    assert rejected.value.code == 1008
    with env_override(**{**_WORKOS, "WORKOS_CLIENT_ID": None}):
        with pytest.raises(WebSocketException) as misconfigured:
            await verify_websocket(socket)
    assert misconfigured.value.code == 1011
