"""Companies page API (WorkOS mocked): only Ingabe staff add companies and invite their admins."""

from __future__ import annotations

import pytest

from src.dependencies import workos_session
from src.dependencies.session import verify_session_required
from src.services import workos_auth
from src.wsgi import app


def _session(email: str) -> workos_auth.WorkOSSession:
    return workos_auth.WorkOSSession(
        user_id="user_x", email=email, first_name=None, last_name=None, profile_picture_url=None,
        session_id="sess_x", organization_id=None, role=None)


@pytest.fixture
def as_user(monkeypatch, client):
    for k, v in {"AUTH_PROVIDER": "workos", "WORKOS_API_KEY": "sk_test", "WORKOS_CLIENT_ID": "client_x",
                 "WORKOS_COOKIE_PASSWORD": "x" * 32, "PLATFORM_ADMIN_EMAILS": "staff@ingabe.rw, other@ingabe.rw"}.items():
        monkeypatch.setenv(k, v)
    calls = []
    monkeypatch.setattr(workos_auth, "companies", lambda: [{"id": "org_1", "name": "BK Insurance"}])
    monkeypatch.setattr(workos_auth, "create_partner",
                        lambda name, email: calls.append((name, email)) or {"organization_id": "org_2", "created": True})

    def login(email):
        async def fake_load(cookie):
            return _session(email)
        monkeypatch.setattr(workos_session, "load_session", fake_load)
        client.cookies.set("wos_session", "x")

    app.dependency_overrides[verify_session_required] = lambda: object()
    yield login, calls
    app.dependency_overrides.pop(verify_session_required, None)
    client.cookies.clear()


@pytest.mark.anyio
async def test_staff_list_and_add_companies(client, as_user):
    login, calls = as_user
    login("Staff@Ingabe.rw")
    assert (await client.get("/api/admin/companies")).json()["companies"][0]["name"] == "BK Insurance"
    r = await client.post("/api/admin/companies", json={"name": "Radiant", "admin_email": "it@radiant.rw"})
    assert r.status_code == 200 and calls == [("Radiant", "it@radiant.rw")]


@pytest.mark.anyio
async def test_company_admins_are_not_platform_staff(client, as_user):
    login, calls = as_user
    login("admin@bk.rw")
    assert (await client.get("/api/admin/companies")).status_code == 403
    assert (await client.post("/api/admin/companies", json={"name": "X", "admin_email": "a@x.rw"})).status_code == 403
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
