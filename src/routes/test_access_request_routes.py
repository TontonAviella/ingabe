"""Request access (anyone asks to join) and the Companies page's approve / decline (WorkOS and ntfy mocked)."""

from __future__ import annotations

import uuid
from types import SimpleNamespace

import pytest

from src.database.pool import get_async_db_connection
from src.dependencies import workos_session
from src.dependencies.redis_client import get_async_redis
from src.dependencies.session import verify_session_required
from src.routes import access_request_routes
from src.services import phone_push, workos_auth
from src.wsgi import app

RUN_TAG = uuid.uuid4().hex[:8]


@pytest.fixture(autouse=True)
def fresh_cookies(client):
    client.cookies.clear()
    yield
    client.cookies.clear()


@pytest.fixture
def workos_on(monkeypatch):
    for k, v in {"AUTH_PROVIDER": "workos", "WORKOS_API_KEY": "sk_test", "WORKOS_CLIENT_ID": "client_x",
                 "WORKOS_COOKIE_PASSWORD": "x" * 32, "PLATFORM_ADMIN_EMAILS": "staff@ingabe.rw"}.items():
        monkeypatch.setenv(k, v)
    monkeypatch.delenv("WORKOS_SELF_SIGN_UP", raising=False)
    monkeypatch.setattr(workos_auth, "login_url", lambda redirect_uri, state, org=None, hint=None:
                        f"https://auth.example/authorize?screen_hint={hint}")


@pytest.fixture
def pushes(monkeypatch):
    sent = []

    async def fake_send(title, message, click_path=None, priority=3, tags=None):
        sent.append({"title": title, "message": message, "click_path": click_path})
        return True

    monkeypatch.setattr(phone_push, "send", fake_send)

    async def no_limit(ip):
        return True

    monkeypatch.setattr(access_request_routes, "_within_limit", no_limit)
    return sent


@pytest.fixture
async def cleanup():
    yield
    async with get_async_db_connection() as conn:
        await conn.execute("DELETE FROM access_requests WHERE email LIKE $1", f"%{RUN_TAG}%")


def _email(name: str) -> str:
    return f"{name}-{RUN_TAG}@example.rw"


@pytest.mark.anyio
async def test_sign_up_link_leads_to_request_access_while_invite_only(client, workos_on, monkeypatch):
    r = await client.get("/auth/login", params={"screen_hint": "sign-up"}, follow_redirects=False)
    assert r.status_code == 302 and r.headers["location"] == "/request-access"
    signed_in = await client.get("/auth/login", follow_redirects=False)  # signing in is unchanged
    assert signed_in.headers["location"].startswith("https://auth.example/authorize")
    monkeypatch.setenv("WORKOS_SELF_SIGN_UP", "1")  # sign-up opened in WorkOS: the link goes there again
    opened = await client.get("/auth/login", params={"screen_hint": "sign-up"}, follow_redirects=False)
    assert opened.headers["location"] == "https://auth.example/authorize?screen_hint=sign-up"


@pytest.mark.anyio
async def test_anyone_can_ask_and_staff_are_pushed(client, workos_on, pushes, cleanup):
    email = _email("alain")
    r = await client.post("/api/access-requests",
                          json={"email": email.upper(), "name": "Alain", "organization": "Youth Hub",
                                "reason": "Help promote Ingabe"})
    assert r.status_code == 202 and r.json() == {"received": True}
    assert len(pushes) == 1 and pushes[0]["title"] == "New access request" and email in pushes[0]["message"]
    assert pushes[0]["click_path"] == "/admin/companies"
    again = await client.post("/api/access-requests", json={"email": email, "name": "Alain"})
    assert again.status_code == 202 and len(pushes) == 1  # the same open request: not pushed twice in a day
    async with get_async_db_connection() as conn:
        rows = await conn.fetch("SELECT attempts, source FROM access_requests WHERE email = $1", email)
    assert [(r["attempts"], r["source"]) for r in rows] == [(2, "form")]


@pytest.mark.anyio
async def test_bad_addresses_and_bots_are_not_kept(client, workos_on, pushes, cleanup):
    bad = await client.post("/api/access-requests", json={"email": "not-an-email", "name": "X"})
    assert bad.status_code == 400 and "email" in bad.json()["detail"]
    bot = await client.post("/api/access-requests", json={"email": _email("bot"), "website": "http://spam"})
    assert bot.status_code == 202 and bot.json() == {"received": True} and pushes == []
    async with get_async_db_connection() as conn:
        assert await conn.fetchval("SELECT count(*) FROM access_requests WHERE email = $1", _email("bot")) == 0


@pytest.mark.anyio
async def test_one_address_on_the_internet_is_limited(client, workos_on, monkeypatch, cleanup):
    """The limit counts in the shared Redis per address and hour. This run gets an address of its own (an IPv6
    documentation address from its RUN_TAG) and a fixed hour, and removes its count before and after: it used 1 of
    250 addresses, so runs within the same hour met each other's counts and saw 429 first (2026-11-07)."""
    async def quiet(*a, **k):
        return True

    monkeypatch.setattr(phone_push, "send", quiet)
    monkeypatch.setattr(access_request_routes, "PER_IP_PER_HOUR", 2)
    address = f"2001:db8::{RUN_TAG[:4]}:{RUN_TAG[4:]}"
    # One fixed hour for all three requests: a run that crossed the hour would split them over two counts.
    monkeypatch.setattr(access_request_routes, "time", SimpleNamespace(time=lambda: 400_000 * 3600 + 1))
    redis = get_async_redis()
    assert redis is not None, "this test needs Redis: the limit is not checked without it"

    async def forget_count():
        async for key in redis.scan_iter(match=f"access_request:{address}:*"):
            await redis.delete(key)

    await forget_count()
    try:
        codes = [(await client.post("/api/access-requests", json={"email": _email(f"ip{i}")},
                                    headers={"Cf-Connecting-Ip": address})).status_code
                 for i in range(3)]
    finally:
        await forget_count()
    assert codes == [202, 202, 429]


@pytest.fixture
def staff(monkeypatch):
    invited = []
    monkeypatch.setattr(workos_auth, "organization_name", lambda org_id: "BK Insurance")
    monkeypatch.setattr(workos_auth, "invite", lambda org, email, role, inviter: invited.append((org, email, role))
                        or {"id": "inv_1"})

    def login(email):
        async def fake_load(cookie):
            return workos_auth.WorkOSSession(
                user_id="user_staff", email=email, first_name=None, last_name=None, profile_picture_url=None,
                session_id="sess_x", organization_id=None, role=None)
        monkeypatch.setattr(workos_session, "load_session", fake_load)
        workos_session_cookie = "x"
        return workos_session_cookie

    app.dependency_overrides[verify_session_required] = lambda: object()
    yield login, invited
    app.dependency_overrides.pop(verify_session_required, None)


@pytest.mark.anyio
async def test_staff_approve_with_an_invitation_or_decline(client, workos_on, pushes, staff, cleanup):
    login, invited = staff
    for who in ("approve", "decline"):
        await client.post("/api/access-requests", json={"email": _email(who), "name": who.title()})
    client.cookies.set("wos_session", login("someone@partner.rw"))
    assert (await client.get("/api/admin/companies/access-requests")).status_code == 403
    client.cookies.set("wos_session", login("staff@ingabe.rw"))
    listed = (await client.get("/api/admin/companies/access-requests")).json()["requests"]
    mine = {r["email"]: r for r in listed if RUN_TAG in r["email"]}
    assert set(mine) == {_email("approve"), _email("decline")} and all(r["status"] == "open" for r in mine.values())

    ok = await client.post(f"/api/admin/companies/access-requests/{mine[_email('approve')]['id']}/approve",
                           json={"organization_id": "org_bk"})
    assert ok.status_code == 200 and ok.json()["decided_how"] == "Invited to BK Insurance as member"
    assert invited == [("org_bk", _email("approve"), "member")]
    twice = await client.post(f"/api/admin/companies/access-requests/{mine[_email('approve')]['id']}/approve",
                              json={"organization_id": "org_bk"})
    assert twice.status_code == 404 and len(invited) == 1  # decided once: no second invitation

    no = await client.post(f"/api/admin/companies/access-requests/{mine[_email('decline')]['id']}/decline")
    assert no.status_code == 200 and no.json()["status"] == "declined" and len(invited) == 1

    first_admin = await client.post("/api/access-requests", json={"email": _email("newco")})
    assert first_admin.status_code == 202
    listed = (await client.get("/api/admin/companies/access-requests")).json()["requests"]
    newco = next(r for r in listed if r["email"] == _email("newco"))
    # The Add company form already invited them as its first admin: approving sends no second email.
    done = await client.post(f"/api/admin/companies/access-requests/{newco['id']}/approve",
                             json={"organization_id": "org_new", "role": "admin", "already_invited": True})
    assert done.status_code == 200 and done.json()["decided_how"] == "Invited to BK Insurance as admin"
    assert len(invited) == 1
