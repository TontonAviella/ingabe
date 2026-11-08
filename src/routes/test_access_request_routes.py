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


@pytest.mark.parametrize("given, counted_as", [
    ("198.51.100.7", "198.51.100.7"),
    (" 198.51.100.7 ", "198.51.100.7"),
    # One IPv6 client normally holds a whole /64, and one address has many spellings (2026-11-08).
    ("2001:db8:1:2::1", "2001:db8:1:2::/64"),
    ("2001:0db8:0001:0002:0000:0000:0000:0009", "2001:db8:1:2::/64"),
    ("2001:DB8:1:2:FFFF::", "2001:db8:1:2::/64"),
    ("fe80::1%eth0", "fe80::/64"),
    ("::ffff:198.51.100.7", "198.51.100.7"),  # an IPv4 address written as IPv6 is that IPv4 address
    ("testclient", "unknown"), ("", "unknown"), ("198.51.100.7, 203.0.113.9", "unknown"),
])
def test_the_limit_counts_an_ipv4_address_or_an_ipv6_network(given, counted_as):
    assert access_request_routes._limit_subject(given) == counted_as


@pytest.mark.anyio
@pytest.mark.parametrize("case, spellings", [
    (0, ["{net}::1", "{net}::1", "{net}::1"]),  # one address
    (1, ["{net}::1", "{net}::2", "{net}:0:0:0:3"]),  # one client's /64: other addresses, other spellings
])
async def test_one_address_on_the_internet_is_limited(client, workos_on, monkeypatch, cleanup, case, spellings):
    """The limit counts in the shared Redis per address (IPv6: per /64) and hour. This run gets a network of its own
    (an IPv6 documentation /64 from its RUN_TAG) and a fixed hour, and removes its count before and after: it used 1
    of 250 addresses, so runs within the same hour met each other's counts and saw 429 first (2026-11-07)."""
    async def quiet(*a, **k):
        return True

    monkeypatch.setattr(phone_push, "send", quiet)
    monkeypatch.setattr(access_request_routes, "PER_IP_PER_HOUR", 2)
    net = f"2001:db8:{RUN_TAG[:4]}:{RUN_TAG[4:]}"
    addresses = [spelling.format(net=net) for spelling in spellings]
    # One fixed hour for all three requests (a run that crossed the hour would split them over two counts), and
    # one hour per case, so the cases never share a count.
    now = (400_000 + case) * 3600 + 1
    monkeypatch.setattr(access_request_routes, "time", SimpleNamespace(time=lambda: now))
    key = access_request_routes._limit_key(access_request_routes._limit_subject(addresses[0]), now)
    redis = get_async_redis()
    assert redis is not None, "no Redis client in this event loop"

    await redis.delete(key)
    try:
        codes = [(await client.post("/api/access-requests", json={"email": _email(f"ip{case}{i}")},
                                    headers={"Cf-Connecting-Ip": address})).status_code
                 for i, address in enumerate(addresses)]
        expires_in = await redis.ttl(key)
    finally:
        await redis.delete(key)
    assert codes == [202, 202, 429]
    # The count always expires: set with its expiry in one step, so it can never be left counting forever.
    assert 0 < expires_in <= access_request_routes._COUNT_KEPT_SECONDS


@pytest.mark.anyio
async def test_a_mistyped_email_does_not_use_up_a_try(client, workos_on, monkeypatch):
    """The form is checked before the limit counts: five typos must not lock a person out for the hour."""
    counted = []

    async def counting(ip):
        counted.append(ip)
        return True

    monkeypatch.setattr(access_request_routes, "_within_limit", counting)
    r = await client.post("/api/access-requests", json={"email": "not-an-email", "name": "X"})
    assert r.status_code == 400 and counted == []


@pytest.mark.anyio
async def test_a_redis_that_hangs_lets_the_request_through_quickly(monkeypatch):
    """Fail-open must also cover a Redis that never answers, not only one that errors."""
    import asyncio

    class Hanging:
        def pipeline(self, transaction=True):
            return self

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

        def set(self, *a, **k):
            return self

        def incr(self, *a, **k):
            return self

        async def execute(self):
            await asyncio.sleep(60)

    monkeypatch.setattr(access_request_routes, "get_async_redis", lambda: Hanging())
    monkeypatch.setattr(access_request_routes, "_REDIS_TIMEOUT_SECONDS", 0.05)
    assert await asyncio.wait_for(access_request_routes._within_limit("198.51.100.7"), timeout=5) is True


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
