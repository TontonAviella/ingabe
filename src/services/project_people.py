"""Who can open a project, and sharing it with a colleague.

A project is opened by its owner, its editors and its viewers (user_mundiai_projects.owner_uuid, editor_uuids,
viewer_uuids; row-level security enforces the same three). Only the owner shares, and only with people who share an
organization with them (user_organizations): a colleague from the People page. So nobody can use sharing to learn
whether an email address has an Ingabe account, and someone from outside is invited to the organization first.
Until 2026-10-22 there was no way to share from the app: Roger's projects were shared with Arnold by hand in the
database.

Roles: an editor opens the maps, asks Sage and adds layers; a viewer only looks. Neither can delete the project or
share it on.
"""

from __future__ import annotations

from typing import Any, Literal, Optional

import asyncpg

Role = Literal["editor", "viewer"]


class NotShareable(ValueError):
    """The email is not a colleague of the owner (not in any of their organizations), or is the owner."""


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
    shared = [
        {"user_id": user_id, "email": emails.get(user_id), "role": role}
        for user_id, role in roles.items() if user_id != row["owner"]
    ]
    shared.sort(key=lambda p: (p["email"] or "").lower())
    return {"owner": {"user_id": row["owner"], "email": emails.get(row["owner"])}, "people": shared}


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
