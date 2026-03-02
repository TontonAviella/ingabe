from fastapi import Path, Depends, HTTPException
import logging
import os

from src.database.models import MundiMap, MundiProject, MapLayer
from src.structures import async_conn, async_read_conn
from src.dependencies.session import (
    UserContext,
    verify_session_required,
)
from src.dag import ForkReason
from src.utils import generate_id

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Authorization helpers
# ---------------------------------------------------------------------------

def _can_access_project(project_row, user_id: str) -> bool:
    """Check if user can access a project (owner, editor, or viewer).

    NOTE: link_accessible is intentionally NOT checked here. That flag
    controls unauthenticated/embed access and must not grant every
    authenticated user blanket access to another user's project.
    """
    if str(project_row["owner_uuid"]) == user_id:
        return True
    editors = project_row.get("editor_uuids") or []
    viewers = project_row.get("viewer_uuids") or []
    return user_id in [str(u) for u in editors + viewers]


def _can_edit_project(project_row, user_id: str) -> bool:
    """Check if user can edit a project (owner or editor)."""
    if str(project_row["owner_uuid"]) == user_id:
        return True
    editors = project_row.get("editor_uuids") or []
    return user_id in [str(u) for u in editors]


async def forked_map(
    original_map_id: str,
    session: UserContext,
    fork_reason: ForkReason,
) -> MundiMap:
    """Fork a map for edit operations and return the new map"""
    user_id = session.get_user_id()

    async with async_conn("forked_map") as conn:
        source_map = await conn.fetchrow(
            """
            SELECT m.id, m.project_id, m.title, m.description, m.layers, m.basemap,
                   p.owner_uuid, p.editor_uuids, p.viewer_uuids
            FROM user_mundiai_maps m
            JOIN user_mundiai_projects p ON p.id = m.project_id
            WHERE m.id = $1 AND m.soft_deleted_at IS NULL
            """,
            original_map_id,
        )
        if not source_map or not _can_edit_project(source_map, user_id):
            raise HTTPException(404, f"Map {original_map_id} not found")

        new_map_id = generate_id(prefix="M")

        # Determine the fork message based on the reason
        fork_message = (
            "Forked by AI agent"
            if fork_reason == ForkReason.AI_EDIT
            else "Forked by user"
        )

        row = await conn.fetchrow(
            """
            INSERT INTO user_mundiai_maps
            (id, project_id, owner_uuid, parent_map_id, title, description, layers, fork_reason, basemap)
            VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9)
            RETURNING *
            """,
            new_map_id,
            source_map["project_id"],
            user_id,
            original_map_id,
            source_map["title"],
            source_map["description"],
            source_map["layers"] or [],
            fork_reason.value,
            source_map["basemap"],
        )
        new_map = MundiMap(**dict(row))

        # Copy over all map_layer_styles to the new map
        await conn.execute(
            """
            INSERT INTO map_layer_styles (map_id, layer_id, style_id)
            SELECT $1, layer_id, style_id
            FROM map_layer_styles
            WHERE map_id = $2
            """,
            new_map_id,
            original_map_id,
        )

        # Update project to include the new map
        await conn.execute(
            """
            UPDATE user_mundiai_projects
            SET maps = array_append(maps, $1),
                map_diff_messages = array_append(map_diff_messages, $2)
            WHERE id = $3
            """,
            new_map_id,
            fork_message,
            source_map["project_id"],
        )

    return new_map


async def forked_map_by_ai(
    original_map_id: str = Path(...),
    session: UserContext = Depends(verify_session_required),
) -> MundiMap:
    return await forked_map(original_map_id, session, ForkReason.AI_EDIT)


async def forked_map_by_user(
    original_map_id: str = Path(...),
    session: UserContext = Depends(verify_session_required),
) -> MundiMap:
    return await forked_map(original_map_id, session, ForkReason.USER_EDIT)


async def get_map(
    map_id: str = Path(...),
    session: UserContext = Depends(verify_session_required),
) -> MundiMap:
    """Get a map the user can access (owner, editor, or viewer)."""
    user_id = session.get_user_id()

    async with async_read_conn("get_map") as conn:
        row = await conn.fetchrow(
            """
            SELECT m.*,
                   p.owner_uuid, p.editor_uuids,
                   p.viewer_uuids, p.link_accessible
            FROM user_mundiai_maps m
            JOIN user_mundiai_projects p ON p.id = m.project_id
            WHERE m.id = $1 AND m.soft_deleted_at IS NULL
            """,
            map_id,
        )
        if not row or not _can_access_project(row, user_id):
            raise HTTPException(404, f"Map {map_id} not found")

        # Build MundiMap from the map columns only
        map_cols = {c.key for c in MundiMap.__table__.columns}
        return MundiMap(**{k: v for k, v in dict(row).items() if k in map_cols})


async def get_layer(
    layer_id: str = Path(...),
    session: UserContext = Depends(verify_session_required),
) -> MapLayer:
    """Get a layer, verifying the caller owns it or has project access."""
    user_id = session.get_user_id()

    async with async_read_conn("get_layer") as conn:
        layer_row = await conn.fetchrow(
            """
            SELECT *
            FROM map_layers
            WHERE layer_id = $1
            """,
            layer_id,
        )
        if not layer_row:
            raise HTTPException(404, f"Layer {layer_id} not found")

        # Owner can always access their own layer
        if str(layer_row["owner_uuid"]) != user_id:
            # Check if the layer is on a map in a project the user can access
            project_row = await conn.fetchrow(
                """
                SELECT p.owner_uuid, p.editor_uuids, p.viewer_uuids, p.link_accessible
                FROM user_mundiai_projects p
                JOIN user_mundiai_maps m ON m.project_id = p.id
                WHERE $1 = ANY(m.layers)
                  AND p.soft_deleted_at IS NULL
                  AND m.soft_deleted_at IS NULL
                LIMIT 1
                """,
                layer_id,
            )
            if not project_row or not _can_access_project(project_row, user_id):
                raise HTTPException(404, f"Layer {layer_id} not found")

        return MapLayer(**dict(layer_row))


async def get_project(
    project_id: str = Path(...),
    session: UserContext = Depends(verify_session_required),
) -> MundiProject:
    """Get a project the user can access (owner, editor, viewer, or link-accessible)."""
    user_id = session.get_user_id()

    async with async_read_conn("get_project") as conn:
        project_row = await conn.fetchrow(
            """
            SELECT *
            FROM user_mundiai_projects
            WHERE id = $1 AND soft_deleted_at IS NULL
            """,
            project_id,
        )
        if not project_row or not _can_access_project(project_row, user_id):
            raise HTTPException(404, f"Project {project_id} not found")

        return MundiProject(**dict(project_row))


# Edit guards — Clerk-authenticated users can always edit; legacy mode checks MUNDI_AUTH_MODE
def _editing_allowed() -> bool:
    if os.environ.get("CLERK_SECRET_KEY"):
        return True  # Clerk auth: user is authenticated, editing allowed
    return (os.environ.get("MUNDI_AUTH_MODE") or "edit").lower() == "edit"


async def edit_project(
    project_id: str = Path(...),
    session: UserContext = Depends(verify_session_required),
) -> MundiProject:
    """Get a project the user can *edit* (owner or editor)."""
    if not _editing_allowed():
        raise HTTPException(status_code=403, detail="Editing disabled in view_only mode")

    user_id = session.get_user_id()
    async with async_read_conn("edit_project") as conn:
        project_row = await conn.fetchrow(
            "SELECT * FROM user_mundiai_projects WHERE id = $1 AND soft_deleted_at IS NULL",
            project_id,
        )
        if not project_row or not _can_edit_project(project_row, user_id):
            raise HTTPException(403, "You do not have edit access to this project")
        return MundiProject(**dict(project_row))


async def edit_map(
    map_id: str = Path(...),
    session: UserContext = Depends(verify_session_required),
) -> MundiMap:
    """Get a map the user can *edit* (owner or editor of parent project)."""
    if not _editing_allowed():
        raise HTTPException(status_code=403, detail="Editing disabled in view_only mode")

    user_id = session.get_user_id()
    async with async_read_conn("edit_map") as conn:
        row = await conn.fetchrow(
            """
            SELECT m.*,
                   p.owner_uuid, p.editor_uuids
            FROM user_mundiai_maps m
            JOIN user_mundiai_projects p ON p.id = m.project_id
            WHERE m.id = $1 AND m.soft_deleted_at IS NULL
            """,
            map_id,
        )
        if not row or not _can_edit_project(row, user_id):
            raise HTTPException(403, "You do not have edit access to this map")
        map_cols = {c.key for c in MundiMap.__table__.columns}
        return MundiMap(**{k: v for k, v in dict(row).items() if k in map_cols})
