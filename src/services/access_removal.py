"""Removing access: take someone out of a company, or block them from Ingabe, effective at their next request.

Roger (2026-11-01): after approving someone, staff need a way to cut them off, from a phone. The app checks a
session's access token itself on every request and asks WorkOS only when the token expires, so taking a person out at
WorkOS alone left them working with the token they held. So this module keeps two lists the session check reads
(src/dependencies/workos_session.load_session):

- ended sessions: every active WorkOS session of a person removed or blocked here is ended at WorkOS and listed;
  a listed session is refused on its next request, which signs the person out (pages and WebSockets alike);
- blocked people: refused on every request, whatever their company and however they sign in again, until unblocked.
  Nothing is deleted: their account, maps and memberships stay as they were.

Taking someone out of a company also deletes the app's own membership row (user_organizations), which decides which
of that company's projects a person may open; signing in again gives a session without the company. Removing never
applies to oneself, and blocking never applies to oneself or to Ingabe staff (PLATFORM_ADMIN_EMAILS).

The lists are read into memory and read again every REFRESH_SECONDS, so a change made by another app process reaches
this one within that time; in the process that made it, it applies at once. If the database cannot be read, the last
lists read stay in force (logged), so an outage neither signs everyone out nor lets a blocked person back in early.
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Any, Optional

import asyncpg

from src.database.pool import async_conn
from src.services import workos_auth

logger = logging.getLogger(__name__)

REFRESH_SECONDS = 10
KEEP_ENDED_SESSIONS_DAYS = 60  # longer than any WorkOS session can last

_ended: set[str] = set()
_blocked: set[str] = set()
_read_at = 0.0
_lock = asyncio.Lock()


class NotAllowed(Exception):
    """A removal or block this person may not do (themselves, or Ingabe staff)."""


class SessionsNotEnded(Exception):
    """Taken out of the company, but WorkOS could not list or end their sessions: the open ones last until their
    tokens expire. Blocking does not depend on WorkOS and cuts them off at once."""


async def _read_lists() -> None:
    global _ended, _blocked, _read_at
    async with async_conn("access_removal.read") as conn:
        ended = await conn.fetch("SELECT session_id FROM ended_sessions")
        blocked = await conn.fetch("SELECT workos_user_id FROM blocked_people")
    _ended = {r["session_id"] for r in ended}
    _blocked = {r["workos_user_id"] for r in blocked}
    _read_at = time.monotonic()


async def is_cut_off(workos_user_id: str, session_id: Optional[str]) -> bool:
    """True when this person is blocked or this session was ended here."""
    if time.monotonic() - _read_at > REFRESH_SECONDS:
        async with _lock:
            if time.monotonic() - _read_at > REFRESH_SECONDS:
                try:
                    await _read_lists()
                except Exception:  # noqa: BLE001 - keep the last lists; see the module docstring
                    logger.warning("Access removal lists could not be read; using the last ones read", exc_info=True)
    return workos_user_id in _blocked or (session_id is not None and session_id in _ended)


async def end_sessions(conn: asyncpg.Connection, workos_user_id: str, by: str, why: str) -> int:
    """End every active WorkOS session of this person, at WorkOS and here: how many were ended."""
    session_ids = await asyncio.to_thread(workos_auth.active_session_ids, workos_user_id)
    for session_id in session_ids:
        await conn.execute(
            "INSERT INTO ended_sessions (session_id, workos_user_id, ended_by, why) VALUES ($1, $2, $3, $4) "
            "ON CONFLICT (session_id) DO NOTHING", session_id, workos_user_id, by, why)
        _ended.add(session_id)
        try:
            await asyncio.to_thread(workos_auth.revoke_session, session_id)
        except Exception:  # noqa: BLE001 - refused here already; WorkOS' copy ends when the refresh token is next used
            logger.warning("WorkOS did not end session %s of %s; it is refused here", session_id, workos_user_id,
                           exc_info=True)
    await conn.execute("DELETE FROM ended_sessions WHERE ended_at < NOW() - make_interval(days => $1)",
                       KEEP_ENDED_SESSIONS_DAYS)
    logger.info("Ended %d session(s) of %s (%s, by %s)", len(session_ids), workos_user_id, why, by)
    return len(session_ids)


async def remove_from_company(conn: asyncpg.Connection, organization_id: str, membership_id: str,
                              by_user_id: str, by_email: str) -> dict[str, Any]:
    """Take a member out of a company (WorkOS and the app's membership row) and end their sessions."""
    members = await asyncio.to_thread(workos_auth.organization_members, organization_id)
    target = next((m for m in members if m["id"] == membership_id), None)
    if target is None:
        raise LookupError("Not a member of this company")
    if target["user_id"] == by_user_id:
        raise NotAllowed("You cannot remove yourself")
    removed = await asyncio.to_thread(workos_auth.remove_member, membership_id, organization_id)
    await conn.execute(
        "DELETE FROM user_organizations uo USING users u, organizations o "
        "WHERE uo.user_id = u.internal_uuid AND uo.org_id = o.id AND u.workos_user_id = $1 AND o.workos_org_id = $2",
        removed["user_id"], organization_id)
    try:
        ended = await end_sessions(conn, removed["user_id"], by_email, f"removed from {organization_id}")
    except Exception as e:
        logger.error("Removed %s from %s but could not end their sessions", removed["user_id"], organization_id,
                     exc_info=True)
        raise SessionsNotEnded(
            f"{removed['email'] or 'They'} left the company, but their open sessions could not be ended ({e}). "
            "Block them to sign them out now.") from e
    return {"removed": membership_id, "user_id": removed["user_id"], "email": removed["email"], "sessions_ended": ended}


async def block(conn: asyncpg.Connection, workos_user_id: str, by_user_id: str, by_email: str,
                reason: Optional[str] = None) -> dict[str, Any]:
    """Refuse this person on every request from now on, and end their sessions."""
    if workos_user_id == by_user_id:
        raise NotAllowed("You cannot block yourself")
    email = await asyncio.to_thread(workos_auth.user_email, workos_user_id)
    if workos_auth.is_platform_staff(email):
        raise NotAllowed("Ingabe staff cannot be blocked here; remove them from PLATFORM_ADMIN_EMAILS first")
    await conn.execute(
        "INSERT INTO blocked_people (workos_user_id, email, blocked_by, reason) VALUES ($1, $2, $3, $4) "
        "ON CONFLICT (workos_user_id) DO UPDATE SET email = EXCLUDED.email, blocked_by = EXCLUDED.blocked_by, "
        "reason = EXCLUDED.reason, blocked_at = NOW()",
        workos_user_id, email, by_email, (reason or "").strip()[:500] or None)
    _blocked.add(workos_user_id)
    ended = await end_sessions(conn, workos_user_id, by_email, "blocked")
    return {"user_id": workos_user_id, "email": email, "blocked": True, "sessions_ended": ended}


async def unblock(conn: asyncpg.Connection, workos_user_id: str) -> bool:
    """Let this person in again (they sign in afresh); False if they were not blocked."""
    deleted = await conn.fetchval(
        "DELETE FROM blocked_people WHERE workos_user_id = $1 RETURNING workos_user_id", workos_user_id)
    _blocked.discard(workos_user_id)
    return deleted is not None


async def blocked_people(conn: asyncpg.Connection) -> list[dict[str, Any]]:
    rows = await conn.fetch(
        "SELECT workos_user_id AS user_id, email, blocked_at, blocked_by, reason FROM blocked_people "
        "ORDER BY blocked_at DESC")
    return [{**dict(r), "blocked_at": r["blocked_at"].isoformat()} for r in rows]
