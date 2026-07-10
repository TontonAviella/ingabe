"""Regression coverage for transient Clerk JWKS failures."""

import json
import time
from unittest.mock import Mock, patch

import pytest
import requests
from fastapi import HTTPException

from src.dependencies import session


def _reset_jwks_state(monkeypatch, cache_path) -> None:
    monkeypatch.setattr(session, "_jwks_cache", None)
    monkeypatch.setattr(session, "_jwks_fetched_at", 0.0)
    monkeypatch.setattr(session, "_jwks_refresh_retry_at", 0.0)
    monkeypatch.setenv("CLERK_ISSUER", "https://test.clerk.accounts.dev")
    monkeypatch.setenv("CLERK_JWKS_CACHE_PATH", str(cache_path))


# Regression: ISSUE-007 - transient Clerk JWKS timeout produced HTTP 500 on map load.
# Found by /qa on 2026-07-10
# Report: .gstack/qa-reports/qa-report-localhost-2026-07-09.md
def test_jwks_uses_shared_fresh_disk_cache_without_network(tmp_path, monkeypatch) -> None:
    cache_path = tmp_path / "clerk_jwks.json"
    payload = {"keys": [{"kid": "shared-worker-key"}]}
    cache_path.write_text(json.dumps(payload), encoding="utf-8")
    _reset_jwks_state(monkeypatch, cache_path)

    with patch("requests.get") as get:
        assert session._fetch_jwks() == payload

    get.assert_not_called()


def test_jwks_uses_bounded_memory_cache_when_refresh_times_out(tmp_path, monkeypatch) -> None:
    _reset_jwks_state(monkeypatch, tmp_path / "missing.json")
    payload = {"keys": [{"kid": "known-key"}]}
    monkeypatch.setattr(session, "_jwks_cache", payload)
    monkeypatch.setattr(session, "_jwks_fetched_at", 0.0)

    with patch("requests.get", side_effect=requests.ReadTimeout("temporary")):
        assert session._fetch_jwks() == payload

    assert session._jwks_refresh_retry_at > time.time()


def test_jwks_without_any_cache_reports_service_unavailable(tmp_path, monkeypatch) -> None:
    _reset_jwks_state(monkeypatch, tmp_path / "missing.json")

    with patch("requests.get", side_effect=requests.ReadTimeout("temporary")):
        with pytest.raises(session.ClerkJWKSUnavailableError):
            session._fetch_jwks()


@pytest.mark.anyio
async def test_http_auth_translates_jwks_outage_to_503(monkeypatch) -> None:
    monkeypatch.setattr(
        session,
        "_decode_clerk_jwt",
        Mock(side_effect=session.ClerkJWKSUnavailableError("offline")),
    )

    with pytest.raises(HTTPException) as exc_info:
        await session._authenticate_clerk("token")

    assert exc_info.value.status_code == 503
