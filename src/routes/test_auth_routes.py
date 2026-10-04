"""WorkOS sign-in routes and session middleware (WorkOS itself is mocked)."""

from __future__ import annotations

import base64
from urllib.parse import parse_qs, urlparse

import pytest

from src.dependencies import workos_session
from src.routes import auth_routes
from src.services import workos_auth


@pytest.fixture
def workos_on(monkeypatch):
    for k, v in {"AUTH_PROVIDER": "workos", "WORKOS_API_KEY": "sk_test", "WORKOS_CLIENT_ID": "client_x",
                 "WORKOS_COOKIE_PASSWORD": "x" * 32}.items():
        monkeypatch.setenv(k, v)
    monkeypatch.setattr(workos_auth, "login_url", lambda redirect_uri, state, org=None, hint=None:
                        f"https://auth.example/authorize?state={state}&redirect_uri={redirect_uri}")


@pytest.mark.anyio
async def test_login_redirects_to_workos_with_a_state_cookie(client, workos_on):
    r = await client.get("/auth/login", params={"return_to": "/project/abc"}, follow_redirects=False)
    assert r.status_code == 302
    state = parse_qs(urlparse(r.headers["location"]).query)["state"][0]
    nonce, _, target = state.partition(".")
    assert base64.urlsafe_b64decode(target).decode() == "/project/abc"
    assert f"wos_state={nonce}" in r.headers["set-cookie"]


@pytest.mark.anyio
async def test_callback_with_a_wrong_state_does_not_sign_in(client, workos_on):
    r = await client.get("/auth/callback", params={"code": "c", "state": "forged.Lw=="}, follow_redirects=False)
    assert r.status_code == 302 and r.headers["location"] == "/?sign_in_error=1"
    assert "wos_session=" not in r.headers.get("set-cookie", "")


@pytest.mark.anyio
async def test_callback_sets_the_session_cookie(client, workos_on, monkeypatch):
    linked = []
    monkeypatch.setattr(workos_auth, "complete_login", lambda code: ("sealed-cookie", {"id": "user_9", "email": "x@y.rw"}, None))

    async def fake_provision(uid, email):
        linked.append((uid, email))
        return "uuid-9"

    monkeypatch.setattr(auth_routes, "provision_workos_user", fake_provision)
    target = base64.urlsafe_b64encode(b"/project/abc").decode()
    client.cookies.set("wos_state", "n1", path="/auth/")
    r = await client.get("/auth/callback", params={"code": "c", "state": f"n1.{target}"}, follow_redirects=False)
    client.cookies.delete("wos_state", path="/auth/")
    assert r.status_code == 302 and r.headers["location"] == "/project/abc"
    assert "wos_session=sealed-cookie" in r.headers["set-cookie"] and "HttpOnly" in r.headers["set-cookie"]
    assert linked == [("user_9", "x@y.rw")]


@pytest.mark.anyio
async def test_api_needs_a_session_when_workos_is_on(client, workos_on):
    r = await client.get("/api/auth/me")
    assert r.status_code == 401


@pytest.mark.anyio
async def test_middleware_sends_back_a_refreshed_cookie(client, workos_on, monkeypatch):
    refreshed = workos_auth.WorkOSSession(
        user_id="user_9", email="x@y.rw", first_name=None, last_name=None, profile_picture_url=None,
        session_id="sess_9", organization_id=None, role=None, refreshed_cookie="fresh")

    async def fake_load(cookie):
        return refreshed

    monkeypatch.setattr(workos_session, "load_session", fake_load)
    client.cookies.set("wos_session", "stale")
    r = await client.get("/auth/login", follow_redirects=False)
    client.cookies.delete("wos_session")
    assert any("wos_session=fresh" in v for v in r.headers.get_list("set-cookie"))
