"""Companies page API (WorkOS mocked): only Ingabe staff add companies and invite their admins."""

from __future__ import annotations

import uuid

import pytest

from src.database.pool import get_async_db_connection
from src.dependencies import workos_session
from src.dependencies.session import verify_session_required
from src.services import workos_auth
from src.wsgi import app


def _session(email: str) -> workos_auth.WorkOSSession:
    return workos_auth.WorkOSSession(
        user_id="user_x", email=email, first_name=None, last_name=None, profile_picture_url=None,
        session_id="sess_x", organization_id=None, role=None)


@pytest.fixture
async def as_user(monkeypatch, client):
    for k, v in {"AUTH_PROVIDER": "workos", "WORKOS_API_KEY": "sk_test", "WORKOS_CLIENT_ID": "client_x",
                 "WORKOS_COOKIE_PASSWORD": "x" * 32, "PLATFORM_ADMIN_EMAILS": "staff@ingabe.rw, other@ingabe.rw"}.items():
        monkeypatch.setenv(k, v)
    calls = []
    new_org = f"org_test_{uuid.uuid4().hex[:12]}"  # a company row is made for it: unique per run and worker
    monkeypatch.setattr(workos_auth, "companies", lambda: [{"id": "org_1", "name": "BK Insurance"}])
    monkeypatch.setattr(workos_auth, "create_partner", lambda name, email: calls.append((name, email)) or
                        {"organization_id": new_org, "name": name, "created": True, "invitation": {}})
    monkeypatch.setattr(workos_auth, "organization_name", lambda org_id: "Radiant")

    def login(email):
        async def fake_load(cookie):
            return _session(email)
        monkeypatch.setattr(workos_session, "load_session", fake_load)
        client.cookies.set("wos_session", "x")

    app.dependency_overrides[verify_session_required] = lambda: object()
    yield login, calls, new_org
    app.dependency_overrides.pop(verify_session_required, None)
    client.cookies.clear()
    async with get_async_db_connection() as conn:
        await conn.execute("DELETE FROM organizations WHERE workos_org_id = $1", new_org)


@pytest.mark.anyio
async def test_staff_list_and_add_companies(client, as_user):
    login, calls, new_org = as_user
    login("Staff@Ingabe.rw")
    listed = (await client.get("/api/admin/companies")).json()
    assert listed["companies"][0]["name"] == "BK Insurance" and listed["companies"][0]["industry"] is None
    assert [o["key"] for o in listed["industries"]] == ["agriculture", "power_grid", "telecom"]
    no_industry = await client.post("/api/admin/companies", json={"name": "Radiant", "admin_email": "it@radiant.rw"})
    assert no_industry.status_code in (400, 422) and calls == []  # a partner company needs its industry
    r = await client.post("/api/admin/companies",
                          json={"name": "Radiant", "admin_email": "it@radiant.rw", "industry": "power_grid"})
    assert r.status_code == 200 and calls == [("Radiant", "it@radiant.rw")]
    async with get_async_db_connection() as conn:  # set before anyone from the company signs in
        assert await conn.fetchval("SELECT industry FROM organizations WHERE workos_org_id = $1", new_org) == "power_grid"
    moved = await client.put(f"/api/admin/companies/{new_org}/industry", json={"industry": "telecom"})
    assert moved.status_code == 200
    async with get_async_db_connection() as conn:
        assert await conn.fetchval("SELECT industry FROM organizations WHERE workos_org_id = $1", new_org) == "telecom"


@pytest.mark.anyio
async def test_company_admins_are_not_platform_staff(client, as_user):
    login, calls, _new_org = as_user
    login("admin@bk.rw")
    assert (await client.get("/api/admin/companies")).status_code == 403
    assert (await client.post("/api/admin/companies",
                              json={"name": "X", "admin_email": "a@x.rw", "industry": "telecom"})).status_code == 403
    assert (await client.put("/api/admin/companies/org_1/industry", json={"industry": "telecom"})).status_code == 403
    assert calls == []


def test_each_company_gets_one_plain_status_line():
    pending = [{"email": "it@bk.rw", "state": "pending"}]
    assert workos_auth._company_status(3, pending)["text"] == "Active: 3 people"
    assert workos_auth._company_status(0, pending)["code"] == "invited"
    assert workos_auth._company_status(0, [{"email": "a", "state": "expired"}])["code"] == "expired"
    assert workos_auth._company_status(0, [])["code"] == "no_admin"


def test_the_workos_sample_company_is_not_listed():
    from types import SimpleNamespace

    sample = SimpleNamespace(domains=[SimpleNamespace(domain="example.com")])
    real = SimpleNamespace(domains=[SimpleNamespace(domain="bk.rw")])
    assert workos_auth._is_workos_sample(sample) and not workos_auth._is_workos_sample(real)
    assert not workos_auth._is_workos_sample(SimpleNamespace(domains=None))


def test_only_the_first_staff_email_is_the_owner(monkeypatch):
    monkeypatch.setenv("PLATFORM_ADMIN_EMAILS", "Owner@ingabe.rw, staff@ingabe.rw")
    assert workos_auth.is_platform_owner("owner@ingabe.rw")
    assert not workos_auth.is_platform_owner("staff@ingabe.rw") and workos_auth.is_platform_staff("staff@ingabe.rw")
    assert not workos_auth.is_platform_owner(None)
