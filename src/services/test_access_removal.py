"""Removing access (WorkOS mocked, database real): a removed or blocked person is refused on their next request."""

from __future__ import annotations

import uuid

import pytest

from src.database.pool import get_async_db_connection
from src.dependencies import workos_session
from src.services import access_removal, workos_auth

RUN_TAG = uuid.uuid4().hex[:8]
STAFF = f"staff-{RUN_TAG}@ingabe.rw"


def _session(user_id: str, session_id: str) -> workos_auth.WorkOSSession:
    return workos_auth.WorkOSSession(
        user_id=user_id, email=f"{user_id}@x.rw", first_name=None, last_name=None, profile_picture_url=None,
        session_id=session_id, organization_id=None, role=None)


@pytest.fixture
def workos(monkeypatch):
    """WorkOS as the real functions answer (shapes from src/services/workos_auth.py); lists re-read on each check."""
    monkeypatch.setenv("PLATFORM_ADMIN_EMAILS", STAFF)
    ended: list[str] = []
    sessions = {f"user_{RUN_TAG}_a": [f"session_{RUN_TAG}_a1", f"session_{RUN_TAG}_a2"]}
    monkeypatch.setattr(workos_auth, "active_session_ids", lambda user_id: list(sessions.get(user_id, [])))
    monkeypatch.setattr(workos_auth, "revoke_session", lambda session_id: ended.append(session_id))
    monkeypatch.setattr(workos_auth, "user_email",
                        lambda user_id: STAFF if user_id.endswith("_staff") else f"{user_id}@x.rw")
    monkeypatch.setattr(access_removal, "_read_at", 0.0)
    monkeypatch.setattr(access_removal, "REFRESH_SECONDS", -1)  # read the lists on every check
    yield ended


async def _cleanup():
    async with get_async_db_connection() as conn:
        await conn.execute("DELETE FROM blocked_people WHERE workos_user_id LIKE $1", f"user_{RUN_TAG}_%")
        await conn.execute("DELETE FROM ended_sessions WHERE workos_user_id LIKE $1", f"user_{RUN_TAG}_%")


async def _signed_in_as(monkeypatch, user_id: str, session_id: str):
    async def check(cookie, refresh=True):
        return _session(user_id, session_id)
    monkeypatch.setattr(workos_session, "_check_cookie", check)
    return await workos_session.load_session("cookie")


@pytest.mark.anyio
async def test_a_blocked_person_is_refused_until_unblocked(monkeypatch, workos):
    user = f"user_{RUN_TAG}_a"
    try:
        assert await _signed_in_as(monkeypatch, user, f"session_{RUN_TAG}_new") is not None
        async with get_async_db_connection() as conn:
            blocked = await access_removal.block(conn, user, f"user_{RUN_TAG}_staff", STAFF, "left the company")
        assert blocked["sessions_ended"] == 2 and sorted(workos) == [f"session_{RUN_TAG}_a1", f"session_{RUN_TAG}_a2"]
        # A session started after the block (signing in again) is refused too: the person is blocked, not the session.
        assert await _signed_in_as(monkeypatch, user, f"session_{RUN_TAG}_new") is None
        async with get_async_db_connection() as conn:
            assert await access_removal.unblock(conn, user) is True
            assert await access_removal.unblock(conn, user) is False
        assert await _signed_in_as(monkeypatch, user, f"session_{RUN_TAG}_new") is not None
        # The sessions ended by the block stay ended after unblocking: the person signs in afresh.
        assert await _signed_in_as(monkeypatch, user, f"session_{RUN_TAG}_a1") is None
    finally:
        await _cleanup()


@pytest.mark.anyio
async def test_staff_and_oneself_cannot_be_blocked(monkeypatch, workos):
    async with get_async_db_connection() as conn:
        with pytest.raises(access_removal.NotAllowed):
            await access_removal.block(conn, f"user_{RUN_TAG}_me", f"user_{RUN_TAG}_me", STAFF)
        with pytest.raises(access_removal.NotAllowed):
            await access_removal.block(conn, f"user_{RUN_TAG}_staff", f"user_{RUN_TAG}_me", STAFF)
    assert workos == []


@pytest.mark.anyio
async def test_removal_from_a_company_ends_sessions_and_deletes_the_app_membership(monkeypatch, workos):
    user, org = f"user_{RUN_TAG}_a", f"org_{RUN_TAG}"
    member = {"id": f"om_{RUN_TAG}", "user_id": user, "email": f"{user}@x.rw", "name": None, "picture": None,
              "role": "member", "status": "active", "last_sign_in_at": None}
    monkeypatch.setattr(workos_auth, "organization_members", lambda organization_id: [member])
    removed_at_workos = []
    monkeypatch.setattr(workos_auth, "remove_member",
                        lambda membership_id, organization_id: removed_at_workos.append(membership_id) or member)
    internal = str(uuid.uuid4())
    async with get_async_db_connection() as conn:
        await conn.execute("INSERT INTO users (internal_uuid, workos_user_id, email) VALUES ($1, $2, $3)",
                           internal, user, member["email"])
        org_row = await conn.fetchval(
            "INSERT INTO organizations (name, slug, workos_org_id) VALUES ($1, $1, $2) RETURNING id", f"Co {RUN_TAG}", org)
        await conn.execute("INSERT INTO user_organizations (user_id, org_id) VALUES ($1, $2)", internal, org_row)
    try:
        async with get_async_db_connection() as conn:
            with pytest.raises(access_removal.NotAllowed):
                await access_removal.remove_from_company(conn, org, member["id"], user, STAFF)
            with pytest.raises(LookupError):
                await access_removal.remove_from_company(conn, org, "om_unknown", f"user_{RUN_TAG}_staff", STAFF)
            out = await access_removal.remove_from_company(conn, org, member["id"], f"user_{RUN_TAG}_staff", STAFF)
            left = await conn.fetchval("SELECT count(*) FROM user_organizations WHERE user_id = $1", internal)
        assert out["sessions_ended"] == 2 and removed_at_workos == [member["id"]] and left == 0
        assert await _signed_in_as(monkeypatch, user, f"session_{RUN_TAG}_a2") is None
        assert await _signed_in_as(monkeypatch, user, f"session_{RUN_TAG}_later") is not None  # not blocked
    finally:
        await _cleanup()
        async with get_async_db_connection() as conn:
            await conn.execute("DELETE FROM user_organizations WHERE user_id = $1", internal)
            await conn.execute("DELETE FROM organizations WHERE workos_org_id = $1", org)
            await conn.execute("DELETE FROM users WHERE internal_uuid = $1", internal)


@pytest.mark.anyio
async def test_the_last_lists_stay_in_force_when_the_database_cannot_be_read(monkeypatch):
    monkeypatch.setattr(access_removal, "_blocked", {f"user_{RUN_TAG}_b"})
    monkeypatch.setattr(access_removal, "_ended", set())
    monkeypatch.setattr(access_removal, "_read_at", 0.0)

    async def down():
        raise ConnectionError("database down")
    monkeypatch.setattr(access_removal, "_read_lists", down)
    assert await access_removal.is_cut_off(f"user_{RUN_TAG}_b", "s") is True
    assert await access_removal.is_cut_off(f"user_{RUN_TAG}_c", "s") is False


@pytest.mark.anyio
async def test_a_removal_whose_sessions_cannot_be_ended_says_so(monkeypatch, workos):
    user, org = f"user_{RUN_TAG}_a", f"org_{RUN_TAG}_x"
    member = {"id": f"om_{RUN_TAG}_x", "user_id": user, "email": f"{user}@x.rw", "name": None, "picture": None,
              "role": "member", "status": "active", "last_sign_in_at": None}
    monkeypatch.setattr(workos_auth, "organization_members", lambda organization_id: [member])
    monkeypatch.setattr(workos_auth, "remove_member", lambda membership_id, organization_id: member)

    def workos_down(user_id):
        raise ConnectionError("WorkOS unreachable")
    monkeypatch.setattr(workos_auth, "active_session_ids", workos_down)
    async with get_async_db_connection() as conn:
        with pytest.raises(access_removal.SessionsNotEnded, match="Block them"):
            await access_removal.remove_from_company(conn, org, member["id"], f"user_{RUN_TAG}_staff", STAFF)
        # Blocking does not depend on WorkOS listing sessions for the cut-off itself.
        monkeypatch.setattr(workos_auth, "active_session_ids", lambda user_id: [])
        await access_removal.block(conn, user, f"user_{RUN_TAG}_staff", STAFF)
    try:
        assert await _signed_in_as(monkeypatch, user, f"session_{RUN_TAG}_open") is None
    finally:
        await _cleanup()
