"""Tests for authentication error paths in session.py.

Covers legacy-mode rejections, missing tokens, expired/invalid JWTs,
and missing MUNDI_AUTH_MODE configuration.
"""

import pytest
from unittest.mock import patch


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
    """When neither Clerk nor MUNDI_AUTH_MODE is set, return 500."""
    with env_override(MUNDI_AUTH_MODE=None, CLERK_SECRET_KEY=None):
        resp = await client.post("/api/maps/create", json={"title": "test"})
        assert resp.status_code == 500


@pytest.mark.anyio
async def test_invalid_bearer_token_returns_401(client, env_override):
    """An invalid Bearer token should return 401 when Clerk is enabled."""
    with env_override(
        CLERK_SECRET_KEY="test_secret",
        CLERK_ISSUER="https://test.clerk.accounts.dev",
    ):
        with patch("src.dependencies.session._fetch_jwks", return_value={"keys": []}):
            resp = await client.post(
                "/api/maps/create",
                json={"title": "test"},
                headers={"Authorization": "Bearer invalid.token.here"},
            )
            assert resp.status_code == 401


@pytest.mark.anyio
async def test_get_projects_requires_auth(client, env_override):
    """GET /api/projects requires authentication."""
    with env_override(MUNDI_AUTH_MODE="view_only"):
        resp = await client.get("/api/projects/")
        assert resp.status_code == 401
