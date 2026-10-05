"""Organization members API (WorkOS mocked): admins manage, members only read."""

from __future__ import annotations

import uuid

import pytest

from src.dependencies import workos_session
from src.dependencies.session import verify_session_required
from src.services import workos_auth
from src.wsgi import app

RUN_TAG = uuid.uuid4().hex[:8]


def _session(role: str) -> workos_auth.WorkOSSession:
    return workos_auth.WorkOSSession(
        user_id=f"user_{RUN_TAG}_{role}", email=f"{role}-{RUN_TAG}@example.rw", first_name="A", last_name="B",
        profile_picture_url=None, session_id="sess_x", organization_id=f"org_{RUN_TAG}", role=role)


@pytest.fixture
def signed_in(monkeypatch, client):
    for k, v in {"AUTH_PROVIDER": "workos", "WORKOS_API_KEY": "sk_test", "WORKOS_CLIENT_ID": "client_x",
                 "WORKOS_COOKIE_PASSWORD": "x" * 32}.items():
        monkeypatch.setenv(k, v)
    monkeypatch.setattr(workos_auth, "organization_name", lambda org_id: f"Org {RUN_TAG}")
    calls = []
    monkeypatch.setattr(workos_auth, "organization_members", lambda org: [
        {"id": "om_me", "user_id": f"user_{RUN_TAG}_admin", "email": "me@x.rw", "name": None, "picture": None,
         "role": "admin", "status": "active"},
        {"id": "om_2", "user_id": "user_other", "email": "o@x.rw", "name": None, "picture": None,
         "role": "member", "status": "active"}])
    monkeypatch.setattr(workos_auth, "pending_invitations", lambda org: [{"id": "inv_1", "email": "n@x.rw",
                                                                         "role": "member", "expires_at": "x"}])
    monkeypatch.setattr(workos_auth, "invite", lambda org, email, role, inviter: calls.append(("invite", email, role)) or {"id": "inv_2"})
    monkeypatch.setattr(workos_auth, "remove_member", lambda mid, org: calls.append(("remove", mid)))

    def as_role(role):
        async def fake_load(cookie):
            return _session(role)
        monkeypatch.setattr(workos_session, "load_session", fake_load)
        client.cookies.set("wos_session", "x")

    # The DB-side user/org provisioning is covered elsewhere; here only the WorkOS role matters.
    app.dependency_overrides[verify_session_required] = lambda: object()
    yield as_role, calls
    app.dependency_overrides.pop(verify_session_required, None)
    client.cookies.delete("wos_session")


@pytest.mark.anyio
async def test_admin_sees_members_and_invitations(client, signed_in):
    as_role, _ = signed_in
    as_role("admin")
    body = (await client.get("/api/auth/organization/members")).json()
    assert body["can_manage"] is True and len(body["members"]) == 2 and body["invitations"][0]["email"] == "n@x.rw"


@pytest.mark.anyio
async def test_member_reads_but_cannot_manage(client, signed_in):
    as_role, calls = signed_in
    as_role("member")
    body = (await client.get("/api/auth/organization/members")).json()
    assert body["can_manage"] is False and body["invitations"] == []
    r = await client.post("/api/auth/organization/invitations", json={"email": "new@x.rw"})
    assert r.status_code == 403 and calls == []


@pytest.mark.anyio
async def test_admin_invites_and_removes_but_not_themselves(client, signed_in):
    as_role, calls = signed_in
    as_role("admin")
    assert (await client.post("/api/auth/organization/invitations", json={"email": "new@x.rw", "role": "member"})).status_code == 200
    assert (await client.delete("/api/auth/organization/members/om_2")).status_code == 200
    assert (await client.delete("/api/auth/organization/members/om_me")).status_code == 400
    assert (await client.delete("/api/auth/organization/members/om_unknown")).status_code == 404
    assert calls == [("invite", "new@x.rw", "member"), ("remove", "om_2")]
