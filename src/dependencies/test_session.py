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
    with env_override(MUNDI_AUTH_MODE="edit", CLERK_SECRET_KEY=None):
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


# ---------------------------------------------------------------------------
# Clerk fallback blocking tests
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_clerk_no_token_blocked_by_default(client, env_override):
    """When Clerk is enabled but no Bearer token is sent, return 401 (not legacy fallback)."""
    with env_override(
        CLERK_SECRET_KEY="test_secret",
        MUNDI_AUTH_MODE="edit",
        CLERK_ALLOW_LEGACY_FALLBACK=None,
    ):
        resp = await client.post("/api/maps/create", json={"title": "test"})
        assert resp.status_code == 401
        assert "Bearer token missing" in resp.json().get("detail", "")


@pytest.mark.anyio
async def test_clerk_legacy_fallback_allowed_with_env(client, env_override):
    """CLERK_ALLOW_LEGACY_FALLBACK=true restores old behavior during migration."""
    with env_override(
        CLERK_SECRET_KEY="test_secret",
        MUNDI_AUTH_MODE="edit",
        CLERK_ALLOW_LEGACY_FALLBACK="true",
    ):
        resp = await client.post(
            "/api/maps/create", json={"title": "fallback test"}
        )
        assert resp.status_code == 200


@pytest.mark.anyio
async def test_clerk_not_enabled_still_works(client, env_override):
    """When Clerk is NOT configured, legacy mode works normally (no regression)."""
    with env_override(
        CLERK_SECRET_KEY=None,
        MUNDI_AUTH_MODE="edit",
        CLERK_ALLOW_LEGACY_FALLBACK=None,
    ):
        resp = await client.post(
            "/api/maps/create", json={"title": "legacy test"}
        )
        assert resp.status_code == 200
