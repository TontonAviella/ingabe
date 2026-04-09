"""Tests for OAuth2 token handling and 401 retry in sentinel_hub_tiles.

Covers the stale-token recovery path added after the Sentinel Hub credential
renewal incident: a cached token that the API rejects with 401 must trigger a
forced refresh and exactly one retry, not a hot loop and not a hard failure.
"""

from __future__ import annotations

import time
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest

from src.services import sentinel_hub_tiles as sht


@pytest.fixture(autouse=True)
def _reset_token_cache():
    """Every test starts with a clean token cache."""
    sht._cached_token = None
    sht._token_expires_at = 0.0
    yield
    sht._cached_token = None
    sht._token_expires_at = 0.0


class _FakeResponse:
    """Minimal aiohttp response double supporting async context manager."""

    def __init__(self, status: int, json_data: Any = None, body: str = "", read_bytes: bytes = b""):
        self.status = status
        self._json = json_data
        self._body = body
        self._read = read_bytes

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def json(self):
        return self._json

    async def text(self):
        return self._body

    async def read(self):
        return self._read


class _FakeSession:
    """aiohttp.ClientSession double that returns queued responses per URL."""

    def __init__(self, responses: dict[str, list[_FakeResponse]]):
        self._responses = responses
        self.calls: list[tuple[str, dict]] = []
        self.closed = False

    def post(self, url, **kwargs):
        self.calls.append((url, kwargs))
        queue = self._responses.get(url)
        if not queue:
            raise AssertionError(f"unexpected POST to {url}")
        resp = queue.pop(0)

        class _AwaitableCM:
            def __init__(self, r):
                self._r = r

            def __await__(self):
                async def _inner():
                    return self._r
                return _inner().__await__()

            async def __aenter__(self):
                return self._r

            async def __aexit__(self, *exc):
                return False

        return _AwaitableCM(resp)


def _install_fake_session(responses: dict[str, list[_FakeResponse]]) -> _FakeSession:
    fake = _FakeSession(responses)
    sht._shared_session = fake  # type: ignore[assignment]
    return fake


@pytest.mark.asyncio
async def test_get_access_token_caches_and_returns():
    token_resp = _FakeResponse(200, json_data={"access_token": "tok-1", "expires_in": 3600})
    _install_fake_session({sht._SH_TOKEN_URL: [token_resp]})

    with patch.object(sht, "is_configured", return_value=True), \
         patch.object(sht.settings, "sh_client_id", "id", create=True), \
         patch.object(sht.settings, "sh_client_secret", "secret", create=True):
        t1 = await sht.get_access_token()
        # Second call uses cache — no new POST.
        t2 = await sht.get_access_token()

    assert t1 == "tok-1"
    assert t2 == "tok-1"
    assert sht._token_expires_at > time.monotonic()


@pytest.mark.asyncio
async def test_get_access_token_force_refresh_bypasses_cache():
    sht._cached_token = "stale"
    sht._token_expires_at = time.monotonic() + 10_000  # cache is "valid"

    token_resp = _FakeResponse(200, json_data={"access_token": "fresh", "expires_in": 3600})
    _install_fake_session({sht._SH_TOKEN_URL: [token_resp]})

    with patch.object(sht, "is_configured", return_value=True), \
         patch.object(sht.settings, "sh_client_id", "id", create=True), \
         patch.object(sht.settings, "sh_client_secret", "secret", create=True):
        t = await sht.get_access_token(force_refresh=True)

    assert t == "fresh"


@pytest.mark.asyncio
async def test_get_access_token_bad_credentials_raises():
    err_resp = _FakeResponse(401, body="invalid_client")
    _install_fake_session({sht._SH_TOKEN_URL: [err_resp]})

    with patch.object(sht, "is_configured", return_value=True), \
         patch.object(sht.settings, "sh_client_id", "id", create=True), \
         patch.object(sht.settings, "sh_client_secret", "secret", create=True):
        with pytest.raises(RuntimeError, match="token request failed"):
            await sht.get_access_token()


@pytest.mark.asyncio
async def test_fetch_tile_retries_on_401_with_forced_refresh():
    """A 401 on Process API must trigger force_refresh and exactly one retry."""
    call_order: list[str] = []

    async def fake_get_token(force_refresh: bool = False) -> str:
        call_order.append(f"token(force={force_refresh})")
        return "tok-forced" if force_refresh else "tok-stale"

    png = b"\x89PNG\r\n\x1a\nfake"
    fake = _install_fake_session({
        sht._SH_PROCESS_URL: [
            _FakeResponse(401, body="Access token is expired"),
            _FakeResponse(200, read_bytes=png),
        ],
    })

    with patch.object(sht, "get_access_token", side_effect=fake_get_token):
        result = await sht.fetch_tile(
            collection="sentinel-2-l2a",
            layer="TRUE-COLOR",
            bbox=(3215000.0, -350000.0, 3220000.0, -345000.0),
            date_from="2026-01-01",
            date_to="2026-01-31",
        )

    assert result == png
    assert call_order == ["token(force=False)", "token(force=True)"]
    assert len(fake.calls) == 2
    # First call used stale token, second used forced one.
    assert fake.calls[0][1]["headers"]["Authorization"] == "Bearer tok-stale"
    assert fake.calls[1][1]["headers"]["Authorization"] == "Bearer tok-forced"


@pytest.mark.asyncio
async def test_fetch_tile_returns_none_if_retry_also_fails():
    async def fake_get_token(force_refresh: bool = False) -> str:
        return "tok-forced" if force_refresh else "tok-stale"

    _install_fake_session({
        sht._SH_PROCESS_URL: [
            _FakeResponse(401, body="expired"),
            _FakeResponse(500, body="server error"),
        ],
    })

    with patch.object(sht, "get_access_token", side_effect=fake_get_token):
        result = await sht.fetch_tile(
            collection="sentinel-2-l2a",
            layer="TRUE-COLOR",
            bbox=(3215000.0, -350000.0, 3220000.0, -345000.0),
        )

    assert result is None


@pytest.mark.asyncio
async def test_search_catalog_retries_on_401():
    call_order: list[str] = []

    async def fake_get_token(force_refresh: bool = False) -> str:
        call_order.append(f"token(force={force_refresh})")
        return "tok-forced" if force_refresh else "tok-stale"

    features_payload = {
        "features": [
            {"properties": {"datetime": "2026-03-01T08:30:00Z", "eo:cloud_cover": 12.5}},
            {"properties": {"datetime": "2026-03-05T08:30:00Z", "eo:cloud_cover": 3.1}},
        ]
    }
    _install_fake_session({
        sht._SH_CATALOG_URL: [
            _FakeResponse(401, body="expired"),
            _FakeResponse(200, json_data=features_payload),
        ],
    })

    with patch.object(sht, "get_access_token", side_effect=fake_get_token):
        scenes = await sht.search_catalog(
            bbox_wgs84=(28.8, -2.9, 30.9, -1.0),
            collection="sentinel-2-l2a",
            date_from="2026-03-01",
            date_to="2026-03-31",
        )

    assert len(scenes) == 2
    # Sorted by cloud cover ascending.
    assert scenes[0]["cloud_cover"] == 3.1
    assert scenes[1]["cloud_cover"] == 12.5
    assert call_order == ["token(force=False)", "token(force=True)"]


@pytest.mark.asyncio
async def test_search_catalog_returns_empty_on_non_200():
    async def fake_get_token(force_refresh: bool = False) -> str:
        return "tok"

    _install_fake_session({
        sht._SH_CATALOG_URL: [_FakeResponse(400, body="bad request")],
    })

    with patch.object(sht, "get_access_token", side_effect=fake_get_token):
        scenes = await sht.search_catalog(
            bbox_wgs84=(28.8, -2.9, 30.9, -1.0),
            collection="sentinel-2-l2a",
        )

    assert scenes == []


def test_cdse_base_url_selects_cdse_endpoints(monkeypatch):
    """When SH_BASE_URL points to CDSE, token/process/catalog URLs must match."""
    monkeypatch.setenv("SH_BASE_URL", "https://sh.dataspace.copernicus.eu")
    # Force module reload so the top-level URL block re-evaluates.
    import importlib
    reloaded = importlib.reload(sht)
    try:
        assert "dataspace.copernicus.eu" in reloaded._SH_TOKEN_URL
        assert reloaded._SH_PROCESS_URL.startswith("https://sh.dataspace.copernicus.eu")
        assert reloaded._SH_CATALOG_URL.startswith("https://sh.dataspace.copernicus.eu")
        assert "catalog/1.0.0/search" in reloaded._SH_CATALOG_URL
    finally:
        monkeypatch.delenv("SH_BASE_URL", raising=False)
        importlib.reload(sht)
