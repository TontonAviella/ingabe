"""A layer follows the roles of the projects it is in (Roger's decision, 2026-10-12)

Revision ID: e7a1c4d9b2f3
Revises: d5f2b8c4e1a7
Create Date: 2026-10-12

map_layers' row-level security showed a layer only to the person who uploaded it, so a project's editors and viewers
got 404 on its layers (get_layer never saw the row, before its own project check ran) and a shared map opened with
the owner's layers missing. Now, as for the project's maps:
- read: the uploader, or anyone with a role (owner, editor, viewer) in a live project whose live map lists the layer;
- change: the uploader, or an owner or editor of such a project;
- add and delete: the uploader only (a new row is always the writer's own; nobody deletes another person's upload).
An empty app.user_id (migrations, seeders, the background pipelines) still sees everything, as before.
"""

from typing import Sequence, Union

from alembic import op

revision: str = "e7a1c4d9b2f3"
down_revision: Union[str, None] = "d5f2b8c4e1a7"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_ME = "current_setting('app.user_id', true)"
_ME_UUID = f"NULLIF({_ME}, '')::uuid"
_NO_USER = f"coalesce({_ME}, '') = ''"
_OWN = f"owner_uuid::text = {_ME}"


def _in_projects(roles: str) -> str:
    """The layer is on a live map of a live project where the caller has one of these roles."""
    return f"""layer_id IN (
        SELECT unnest(m.layers) FROM user_mundiai_maps m
         WHERE m.soft_deleted_at IS NULL
           AND m.project_id IN (
               SELECT p.id FROM user_mundiai_projects p
                WHERE p.soft_deleted_at IS NULL AND ({roles})))"""


_EDITORS = f"p.owner_uuid::text = {_ME} OR {_ME_UUID} = ANY(p.editor_uuids)"
_MEMBERS = f"{_EDITORS} OR {_ME_UUID} = ANY(p.viewer_uuids)"


def _when_user(rule: str) -> str:
    return f"CASE WHEN {_NO_USER} THEN true ELSE ({rule}) END"


def upgrade() -> None:
    op.execute("DROP POLICY IF EXISTS tenant_isolation_map_layers ON map_layers")
    read = _when_user(f"{_OWN} OR {_in_projects(_MEMBERS)}")
    change = _when_user(f"{_OWN} OR {_in_projects(_EDITORS)}")
    own = _when_user(_OWN)
    op.execute(f"CREATE POLICY map_layers_read ON map_layers FOR SELECT USING ({read})")
    op.execute(f"CREATE POLICY map_layers_change ON map_layers FOR UPDATE USING ({change}) WITH CHECK ({change})")
    op.execute(f"CREATE POLICY map_layers_add ON map_layers FOR INSERT WITH CHECK ({own})")
    op.execute(f"CREATE POLICY map_layers_delete ON map_layers FOR DELETE USING ({own})")


def downgrade() -> None:
    for name in ("map_layers_read", "map_layers_change", "map_layers_add", "map_layers_delete"):
        op.execute(f"DROP POLICY IF EXISTS {name} ON map_layers")
    own = _when_user(_OWN)
    op.execute(f"CREATE POLICY tenant_isolation_map_layers ON map_layers USING ({own}) WITH CHECK ({own})")
