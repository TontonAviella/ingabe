"""Who can open a project, and sharing it with a colleague.

A project is opened by its owner, its editors and its viewers (user_mundiai_projects.owner_uuid, editor_uuids,
viewer_uuids; row-level security enforces the same three). Only the owner shares, and only with people who share an
organization with them (user_organizations): a colleague from the People page. So nobody can use sharing to learn
whether an email address has an Ingabe account, and someone from outside is invited to the organization first.
Until 2026-10-22 there was no way to share from the app: Roger's projects were shared with Arnold by hand in the
database.

Roles: an editor opens the maps, asks Sage and adds layers; a viewer only looks. Neither can delete the project or
share it on.

Guests (Roger, 2026-10-26): the owner can also share one project with anyone by email, for 1, 7 or 30 days. The
invitation is good only for that address: when it signs in (verified), the guest is added to this project's editors
or viewers, and row-level security then shows them this project and nothing else of the organization (Sage acts for
no organization in it, src/services/project_partner.py). When the time runs out (end_expired_guests, every
GUEST_SWEEP_SECONDS) or the owner removes them, they are taken off again. A colleague is shared with the usual way,
never as a guest, so their access never runs out by surprise; sharing with someone the usual way ends their guest
invitation. The answer to an invitation is the same whether or not the address has an account.
"""

from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone
from typing import Any, Literal, Optional

import asyncpg

Role = Literal["editor", "viewer"]
GUEST_DAYS = (1, 7, 30)  # how long the owner can share with a guest
GUEST_SWEEP_SECONDS = 30  # how often expired guests are taken off (the longest a guest outlives their time)
_EMAIL = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


class NotShareable(ValueError):
    """The email is not a colleague of the owner (not in any of their organizations), or is the owner."""


class NotGuest(ValueError):
    """The address cannot be a guest: not an email, the owner's own, or a colleague (shared with the usual way)."""


class NotOwner(PermissionError):
    """Someone other than the project's owner tried to change who has it."""


def owns(owner_uuid: Any, user_id: str) -> bool:
    """The one owner rule: `user_id` is the project's owner (owner_uuid as stored, uuid or text)."""
    return str(owner_uuid) == user_id


async def _require_owner(conn: asyncpg.Connection, project_id: str, user_id: str) -> None:
    owner = await conn.fetchval("SELECT owner_uuid FROM user_mundiai_projects WHERE id = $1", project_id)
    if owner is None:
        raise LookupError(project_id)
    if not owns(owner, user_id):
        raise NotOwner(project_id)


async def colleagues(conn: asyncpg.Connection, owner_id: str) -> list[dict[str, str]]:
    """The people the owner may share with: everyone in one of the owner's organizations, by email."""
    rows = await conn.fetch(
        """
        SELECT DISTINCT u.internal_uuid AS user_id, u.email
          FROM user_organizations mine
          JOIN user_organizations theirs ON theirs.org_id = mine.org_id
          JOIN users u ON u.internal_uuid = theirs.user_id
         WHERE mine.user_id = $1 AND theirs.user_id <> $1 AND u.email IS NOT NULL
         ORDER BY u.email
        """,
        owner_id,
    )
    return [{"user_id": r["user_id"], "email": r["email"]} for r in rows]


async def people(conn: asyncpg.Connection, project_id: str) -> dict[str, Any]:
    """The project's owner and the people it is shared with, each with their email and role."""
    row = await conn.fetchrow(
        "SELECT owner_uuid::text AS owner, coalesce(editor_uuids, '{}')::text[] AS editors, "
        "coalesce(viewer_uuids, '{}')::text[] AS viewers FROM user_mundiai_projects WHERE id = $1",
        project_id,
    )
    if row is None:
        raise LookupError(project_id)
    roles = {u: "viewer" for u in row["viewers"]} | {u: "editor" for u in row["editors"]}
    emails = dict(await conn.fetch(
        "SELECT internal_uuid, email FROM users WHERE internal_uuid = ANY($1::text[])",
        [row["owner"], *roles],
    ))
    guests = await open_guests(conn, project_id)
    guest_users = {g["user_id"] for g in guests if g["user_id"]}
    shared = [
        {"user_id": user_id, "email": emails.get(user_id), "role": role}
        for user_id, role in roles.items() if user_id != row["owner"] and user_id not in guest_users
    ]
    shared.sort(key=lambda p: (p["email"] or "").lower())
    return {"owner": {"user_id": row["owner"], "email": emails.get(row["owner"])}, "people": shared,
            "guests": [{k: g[k] for k in ("id", "email", "role", "status", "expires_at")} for g in guests]}


async def share(conn: asyncpg.Connection, project_id: str, owner_id: str, email: str, role: Role) -> dict[str, Any]:
    """Gives a colleague of the owner `role` on the project (replacing any role they had); returns people().
    NotOwner unless `owner_id` owns the project; NotShareable unless the email is one of their colleagues."""
    await _require_owner(conn, project_id, owner_id)
    wanted = email.strip().lower()
    match: Optional[dict[str, str]] = next(
        (c for c in await colleagues(conn, owner_id) if c["email"].lower() == wanted), None
    )
    if match is None:
        raise NotShareable(email)
    await conn.execute(
        """
        UPDATE user_mundiai_projects
           SET editor_uuids = array_remove(coalesce(editor_uuids, '{}'), $2::uuid)
                              || CASE WHEN $3 = 'editor' THEN ARRAY[$2::uuid] ELSE '{}'::uuid[] END,
               viewer_uuids = array_remove(coalesce(viewer_uuids, '{}'), $2::uuid)
                              || CASE WHEN $3 = 'viewer' THEN ARRAY[$2::uuid] ELSE '{}'::uuid[] END
         WHERE id = $1
        """,
        project_id, match["user_id"], role,
    )
    await conn.execute(
        "UPDATE project_guests SET ended_at = now(), ended_why = 'colleague' "
        "WHERE project_id = $1 AND email = $2 AND ended_at IS NULL",
        project_id, wanted,
    )
    return await people(conn, project_id)


async def unshare(conn: asyncpg.Connection, project_id: str, owner_id: str, user_id: str) -> dict[str, Any]:
    """Takes the project away from someone it was shared with (the owner keeps it); returns people().
    NotOwner unless `owner_id` owns the project."""
    await _require_owner(conn, project_id, owner_id)
    await conn.execute(
        "UPDATE user_mundiai_projects SET editor_uuids = array_remove(coalesce(editor_uuids, '{}'), $2::uuid), "
        "viewer_uuids = array_remove(coalesce(viewer_uuids, '{}'), $2::uuid) WHERE id = $1",
        project_id, user_id,
    )
    return await people(conn, project_id)


# --- Guests: one project, anyone by email, for a limited time -------------------------------------------------------

_SET_ROLE = """
UPDATE user_mundiai_projects
   SET editor_uuids = array_remove(coalesce(editor_uuids, '{}'), $2::uuid)
                      || CASE WHEN $3 = 'editor' THEN ARRAY[$2::uuid] ELSE '{}'::uuid[] END,
       viewer_uuids = array_remove(coalesce(viewer_uuids, '{}'), $2::uuid)
                      || CASE WHEN $3 = 'viewer' THEN ARRAY[$2::uuid] ELSE '{}'::uuid[] END
 WHERE id = $1
"""
_TAKE_OFF = ("UPDATE user_mundiai_projects SET editor_uuids = array_remove(coalesce(editor_uuids, '{}'), $2::uuid), "
             "viewer_uuids = array_remove(coalesce(viewer_uuids, '{}'), $2::uuid) WHERE id = $1")


async def open_guests(conn: asyncpg.Connection, project_id: str) -> list[dict[str, Any]]:
    """The project's guests whose time has not run out: invited (not signed in yet) or active."""
    rows = await conn.fetch(
        "SELECT id, email, role, expires_at, user_id FROM project_guests "
        "WHERE project_id = $1 AND ended_at IS NULL AND expires_at > now() ORDER BY email",
        project_id,
    )
    return [{"id": r["id"], "email": r["email"], "role": r["role"], "user_id": r["user_id"],
             "expires_at": r["expires_at"].isoformat(), "status": "active" if r["user_id"] else "invited"}
            for r in rows]


async def invite_guest(conn: asyncpg.Connection, project_id: str, owner_id: str, email: str, role: Role,
                       days: int) -> dict[str, Any]:
    """Invites `email` to this one project as `role` for `days` (or renews and changes an open invitation):
    {"id", "email", "expires_at", "signed_in"}. A guest already signed in gets the new role at once.
    NotOwner unless `owner_id` owns the project; NotGuest for a bad address, the owner's own or a colleague's;
    ValueError for days outside GUEST_DAYS."""
    await _require_owner(conn, project_id, owner_id)
    if days not in GUEST_DAYS:
        raise ValueError(f"days must be one of {', '.join(map(str, GUEST_DAYS))}")
    address = email.strip().lower()
    if not _EMAIL.match(address):
        raise NotGuest(f"{email} is not an email address")
    owner_email = await conn.fetchval("SELECT lower(email) FROM users WHERE internal_uuid = $1", owner_id)
    if address == owner_email:
        raise NotGuest("That is your own address")
    if any(c["email"].lower() == address for c in await colleagues(conn, owner_id)):
        raise NotGuest(f"{email} is in your organization: choose them from your colleagues instead")
    expires = datetime.now(timezone.utc) + timedelta(days=days)
    row = await conn.fetchrow(
        """
        INSERT INTO project_guests (project_id, email, role, invited_by, expires_at)
        VALUES ($1, $2, $3, $4, $5)
        ON CONFLICT (project_id, email) WHERE ended_at IS NULL
        DO UPDATE SET role = EXCLUDED.role, expires_at = EXCLUDED.expires_at, invited_by = EXCLUDED.invited_by
        RETURNING id, user_id, expires_at
        """,
        project_id, address, role, owner_id, expires,
    )
    if row["user_id"]:
        await conn.execute(_SET_ROLE, project_id, row["user_id"], role)
    return {"id": row["id"], "email": address, "expires_at": row["expires_at"].isoformat(),
            "signed_in": bool(row["user_id"])}


async def note_invitation(conn: asyncpg.Connection, guest_id: int, invitation_id: Optional[str]) -> None:
    await conn.execute("UPDATE project_guests SET invitation_id = $2 WHERE id = $1", guest_id, invitation_id)


async def claim_guest_invitations(conn: asyncpg.Connection, user_id: str, email: Optional[str],
                                  verified: bool) -> list[str]:
    """Gives a person who signed in with a verified, invited address the projects they were invited to (the
    invitation works only for that address); the project ids. Run on a connection without a user (it changes
    projects the person cannot see yet)."""
    if not email or not verified:
        return []
    rows = await conn.fetch(
        "UPDATE project_guests SET user_id = $1, accepted_at = now() "
        "WHERE email = $2 AND user_id IS NULL AND ended_at IS NULL AND expires_at > now() "
        "RETURNING project_id, role",
        user_id, email.strip().lower(),
    )
    for r in rows:
        await conn.execute(_SET_ROLE, r["project_id"], user_id, r["role"])
    return [r["project_id"] for r in rows]


async def end_expired_guests(conn: asyncpg.Connection) -> int:
    """Takes guests whose time ran out off their project; how many. Run every GUEST_SWEEP_SECONDS on a connection
    without a user."""
    rows = await conn.fetch(
        "UPDATE project_guests SET ended_at = now(), ended_why = 'expired' "
        "WHERE ended_at IS NULL AND expires_at <= now() RETURNING project_id, user_id",
    )
    for r in rows:
        if r["user_id"]:
            await conn.execute(_TAKE_OFF, r["project_id"], r["user_id"])
    return len(rows)


async def remove_guest(conn: asyncpg.Connection, project_id: str, owner_id: str, guest_id: int) -> Optional[str]:
    """Ends a guest's access to the project now; the sign-in provider's invitation to revoke (None if none).
    NotOwner unless `owner_id` owns the project; LookupError for no such open guest."""
    await _require_owner(conn, project_id, owner_id)
    row = await conn.fetchrow(
        "UPDATE project_guests SET ended_at = now(), ended_why = 'removed' "
        "WHERE id = $1 AND project_id = $2 AND ended_at IS NULL RETURNING user_id, invitation_id",
        guest_id, project_id,
    )
    if row is None:
        raise LookupError(guest_id)
    if row["user_id"]:
        await conn.execute(_TAKE_OFF, project_id, row["user_id"])
    return row["invitation_id"]
