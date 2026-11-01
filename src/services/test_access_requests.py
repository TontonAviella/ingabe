"""Access requests (src/services/access_requests.py): what a person typed, WorkOS' log, one open request per address,
and the phone push."""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone

import pytest
from workos.common.models import AuthenticationMagicAuthFailed, AuthenticationPasswordFailed, WaitlistUserCreated

from src.database.pool import get_async_db_connection
from src.services import access_requests, phone_push, workos_auth

RUN_TAG = uuid.uuid4().hex[:8]


def _failed(kind_cls, kind: str, email: str, code: str, when: str):
    # The shape WorkOS' log gives (recorded 2026-11-01: authentication.magic_auth_failed, code sign_up_not_allowed).
    return kind_cls.from_dict({
        "object": "event", "id": f"event_{uuid.uuid4().hex}", "event": kind, "created_at": when,
        "data": {"type": kind.split(".")[1].removesuffix("_failed"), "status": "failed", "ip_address": "198.51.100.7",
                 "user_agent": "Mozilla/5.0", "user_id": None, "email": email,
                 "error": {"code": code, "message": "Sign up is not allowed."}},
    })


def _waitlist(email: str, when: str):
    return WaitlistUserCreated.from_dict({
        "object": "event", "id": f"event_{uuid.uuid4().hex}", "event": "waitlist_user.created", "created_at": when,
        "data": {"object": "waitlist_user", "id": "waitlist_user_1", "email": email, "state": "pending",
                 "approved_at": None, "created_at": when, "updated_at": when},
    })


def test_clean_submission_checks_what_people_typed():
    sub = access_requests.clean_submission("  Alain@Example.RW ", " Alain ", "", "Promote\x07 Ingabe")
    assert sub == access_requests.Submission("alain@example.rw", "Alain", None, "Promote Ingabe")
    with pytest.raises(ValueError, match="email"):
        access_requests.clean_submission("not-an-email")
    with pytest.raises(ValueError, match="reason"):
        access_requests.clean_submission("a@b.rw", reason="x" * 1001)


def test_only_refused_sign_ups_and_waitlist_entries_are_requests():
    after = datetime(2026, 11, 1, 4, 0, tzinfo=timezone.utc)
    events = [
        _failed(AuthenticationMagicAuthFailed, "authentication.magic_auth_failed", "Ala@Gmail.com",
                "sign_up_not_allowed", "2026-11-01T04:23:17.000Z"),
        # A wrong code from someone who has an account is not a request.
        _failed(AuthenticationMagicAuthFailed, "authentication.magic_auth_failed", "roger@ingabe.rw",
                "invalid_one_time_code", "2026-11-01T04:30:00.000Z"),
        _failed(AuthenticationPasswordFailed, "authentication.password_failed", "pw@x.rw",
                "sign_up_not_allowed", "2026-11-01T04:10:00.000Z"),
        _waitlist("w@x.rw", "2026-11-01T05:00:00.000Z"),
        # Already read last time.
        _failed(AuthenticationMagicAuthFailed, "authentication.magic_auth_failed", "old@x.rw",
                "sign_up_not_allowed", "2026-11-01T03:59:00.000Z"),
    ]
    found = access_requests.attempts_from_events(events, after)
    assert [(e, s) for e, _, s in found] == [
        ("pw@x.rw", "sign_up_attempt"), ("ala@gmail.com", "sign_up_attempt"), ("w@x.rw", "waitlist")]


def test_push_message_in_plain_words():
    first = access_requests.push_message({"email": "a@b.rw", "name": "Alain", "organization": "Youth Hub",
                                          "reason": "Help promote it", "source": "form", "attempts": 1})
    assert first["title"] == "New access request"
    assert first["message"].splitlines() == [
        "Alain asked on the Request access page.", "a@b.rw", "From: Youth Hub", "Why: Help promote it",
        "Approve or decline on the Companies page."]
    again = access_requests.push_message({"email": "a@b.rw", "source": "sign_up_attempt", "attempts": 3})
    assert again["title"] == "Still waiting for access"
    assert "no code was sent" in again["message"] and "3 times" in again["message"]


@pytest.mark.anyio
async def test_one_open_request_per_address_and_pushed_once_a_day():
    async with get_async_db_connection() as conn:
        try:
            await _one_open_request_per_address(conn)
        finally:
            await conn.execute("DELETE FROM access_requests WHERE email LIKE $1", f"%{RUN_TAG}%")


async def _one_open_request_per_address(conn):
    email = f"alain-{RUN_TAG}@example.rw"
    first = await access_requests.record(conn, access_requests.Submission(email), "sign_up_attempt",
                                         seen_at=datetime.now(timezone.utc))
    assert first["notify"] and first["attempts"] == 1 and first["source"] == "sign_up_attempt"
    await access_requests.mark_notified(conn, first["id"])
    second = await access_requests.record(conn, access_requests.Submission(email, "Alain", "Youth Hub"), "form")
    # The form says more than the attempt: same request, now from the form, and nobody is pushed twice in a day.
    assert second["id"] == first["id"] and second["attempts"] == 2 and second["source"] == "form"
    assert second["name"] == "Alain" and not second["notify"]
    await conn.execute("UPDATE access_requests SET notified_at = NOW() - $2::interval WHERE id = $1",
                       first["id"], access_requests.RENOTIFY_AFTER + timedelta(minutes=1))
    third = await access_requests.record(conn, access_requests.Submission(email), "sign_up_attempt")
    assert third["notify"] and third["name"] == "Alain"  # still waiting a day later: pushed again
    decided = await access_requests.decide(conn, first["id"], "declined", "staff@ingabe.rw", "Declined")
    assert decided is not None and decided["status"] == "declined"
    assert await access_requests.decide(conn, first["id"], "approved", "staff@ingabe.rw", "x") is None
    fresh = await access_requests.record(conn, access_requests.Submission(email), "form")
    assert fresh["id"] != first["id"] and fresh["notify"]  # asking again after a no is a new request


@pytest.mark.anyio
async def test_reading_the_workos_log_pushes_each_new_request_once(monkeypatch):
    async with get_async_db_connection() as conn:
        try:
            await _reading_the_workos_log(conn, monkeypatch)
        finally:
            await conn.execute("DELETE FROM access_requests WHERE email LIKE $1", f"%{RUN_TAG}%")


async def _reading_the_workos_log(conn, monkeypatch):
    email = f"ala-{RUN_TAG}@gmail.com"
    after = await access_requests.events_read_from(conn)
    when = (after + timedelta(seconds=1)).isoformat().replace("+00:00", "Z")
    event = _failed(AuthenticationMagicAuthFailed, "authentication.magic_auth_failed", email,
                    "sign_up_not_allowed", when)
    monkeypatch.setattr(workos_auth, "list_events", lambda kinds, since: [event] if since < event.created_at else [])
    pushed = []

    async def fake_send(title, message, click_path=None, priority=3, tags=None):
        pushed.append((title, message, click_path))
        return True

    monkeypatch.setattr(phone_push, "send", fake_send)
    assert await access_requests.read_workos_log(conn) == 1
    assert len(pushed) == 1 and email in pushed[0][1] and pushed[0][2] == "/admin/companies"
    # The next read starts after that event: nothing new, nobody pushed again.
    assert await access_requests.read_workos_log(conn) == 0 and len(pushed) == 1
    row = await conn.fetchrow("SELECT source, notified_at FROM access_requests WHERE email = $1", email)
    assert row["source"] == "sign_up_attempt" and row["notified_at"] is not None


@pytest.mark.anyio
async def test_the_log_is_never_read_further_back_than_the_lookback():
    async with get_async_db_connection() as conn:
        try:
            old = datetime.now(timezone.utc) - timedelta(days=40)
            await access_requests.record(conn, access_requests.Submission(f"old-{RUN_TAG}@x.rw"), "sign_up_attempt",
                                         seen_at=old)
            start = await access_requests.events_read_from(conn)
            assert start >= datetime.now(timezone.utc) - access_requests.EVENT_LOOKBACK - timedelta(seconds=5)
        finally:
            await conn.execute("DELETE FROM access_requests WHERE email LIKE $1", f"%{RUN_TAG}%")


@pytest.mark.anyio
async def test_a_flood_is_kept_but_not_pushed(monkeypatch):
    pushed = []

    async def fake_send(title, message, click_path=None, priority=3, tags=None):
        pushed.append(title)
        return True

    monkeypatch.setattr(phone_push, "send", fake_send)
    monkeypatch.setattr(access_requests, "PUSHES_PER_DAY", 0)
    async with get_async_db_connection() as conn:
        try:
            kept = await access_requests.record(conn, access_requests.Submission(f"flood-{RUN_TAG}@x.rw"), "form")
            await access_requests.notify(conn, kept)
            assert pushed == [] and await conn.fetchval(
                "SELECT count(*) FROM access_requests WHERE email = $1", f"flood-{RUN_TAG}@x.rw") == 1
        finally:
            await conn.execute("DELETE FROM access_requests WHERE email LIKE $1", f"%{RUN_TAG}%")
