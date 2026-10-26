"""Projects and conversations RLS: an empty app.user_id no longer breaks a query (2026-10-26)

Revision ID: c5e8a2d7f4b1
Revises: b9d4e2f7a1c3
Create Date: 2026-10-26

A pooled connection that served a signed-in user is handed back with app.user_id = '' (not unset). The projects and
conversations policies cast the setting to uuid inside their ELSE branch, and Postgres may evaluate that cast even
when the CASE takes the "no user" branch, so every query on these tables from a connection without a user (Sage
naming chats, the guest expiry loop, adding a guest at sign-in) failed at random with
'invalid input syntax for type uuid: ""'. NULLIF turns '' into NULL before the cast, as the maps policy already does
(c1d2e3f4a5ba, memory lesson "RLS policies with ANY(uuid[]) must wrap current_setting::uuid in NULLIF"). The rules
themselves do not change.
"""

from typing import Sequence, Union

from alembic import op

revision: str = "c5e8a2d7f4b1"
down_revision: Union[str, None] = "b9d4e2f7a1c3"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_ME = "NULLIF(current_setting('app.user_id', true), '')::uuid"
_ME_OLD = "current_setting('app.user_id', true)::uuid"


def _projects(me: str) -> str:
    return f"""
        CASE
            WHEN coalesce(current_setting('app.user_id', true), '') = '' THEN true
            ELSE owner_uuid::text = current_setting('app.user_id', true)
                 OR {me} = ANY(editor_uuids)
                 OR {me} = ANY(viewer_uuids)
        END"""


def _conversations(me: str) -> str:
    return f"""
        CASE
            WHEN coalesce(current_setting('app.user_id', true), '') = '' THEN true
            ELSE owner_uuid::text = current_setting('app.user_id', true)
                 OR project_id IN (
                     SELECT id FROM user_mundiai_projects
                     WHERE owner_uuid::text = current_setting('app.user_id', true)
                        OR {me} = ANY(editor_uuids)
                        OR {me} = ANY(viewer_uuids))
        END"""


def upgrade() -> None:
    op.execute(f"ALTER POLICY tenant_isolation_projects ON user_mundiai_projects USING ({_projects(_ME)})")
    op.execute(f"ALTER POLICY tenant_isolation_conversations ON conversations USING ({_conversations(_ME)})")


def downgrade() -> None:
    op.execute(f"ALTER POLICY tenant_isolation_projects ON user_mundiai_projects USING ({_projects(_ME_OLD)})")
    op.execute(f"ALTER POLICY tenant_isolation_conversations ON conversations USING ({_conversations(_ME_OLD)})")
