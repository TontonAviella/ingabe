import pytest

from src.wsgi import _background_workers_enabled


def test_background_workers_flag_is_explicit(monkeypatch):
    monkeypatch.setenv("MUNDI_BACKGROUND_WORKERS_ENABLED", "0")
    assert _background_workers_enabled() is False
    monkeypatch.setenv("MUNDI_BACKGROUND_WORKERS_ENABLED", "true")
    assert _background_workers_enabled() is True


@pytest.mark.anyio
async def test_nonexistent_endpoint(auth_client):
    response = await auth_client.get("/api/foo/bar")
    assert response.status_code == 404
