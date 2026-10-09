"""Which organization a project's Sage turns act for.

A project belongs to the organization it was created in (user_mundiai_projects.partner_id). Brain's partner-only
notes are read and written under app.partner_id, so a turn acts for the project's organization, not for whichever
organization the user has active: before this, a user in two partners carried partner B's notes into partner A's
project, and the stored memory packet outlived the switch (audit 2026-10-09, round 2). A project with no
organization yet (made before this, or in a personal workspace) is bound by its first turn in an organization.
"""

from __future__ import annotations

from typing import Optional

import asyncpg


async def partner_for_project(conn: asyncpg.Connection, project_id: Optional[str], user_id: str,
                              session_partner: Optional[str]) -> Optional[str]:
    """The organization this user's turn in this project acts for; None (no partner notes) if they are not in it."""
    if not project_id:
        return None
    bound = await conn.fetchval(
        "UPDATE user_mundiai_projects "
        "SET partner_id = COALESCE(partner_id, (SELECT id FROM organizations WHERE id::text = $2)) "
        "WHERE id = $1 RETURNING partner_id::text",
        project_id, session_partner,
    )
    if bound is None:  # only a real organization binds; no organization, or Hermes' local placeholder, passes as is
        return session_partner
    if bound == session_partner:
        return bound
    member = await conn.fetchval("SELECT 1 FROM user_organizations WHERE user_id = $1 AND org_id = $2::uuid",
                                 user_id, bound)
    return bound if member else None
