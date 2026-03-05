"""fix RLS policies: use CASE WHEN to prevent ''::uuid cast error

PostgreSQL does NOT short-circuit OR in policy expressions. The query
planner may evaluate ''::uuid even when coalesce bypass is TRUE,
causing 'invalid input syntax for type uuid' errors (production 500s).

CASE WHEN guarantees the uuid casts are never reached when
app.user_id is NULL or '' (migrations, background, pool reuse).

Revision ID: a1b2c3d4e5f6
Revises: f3a4b5c6d7e8
Create Date: 2026-03-05 09:00:00.000000

"""
from typing import Sequence, Union

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "a1b2c3d4e5f6"
down_revision: str = "f3a4b5c6d7e8"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute("DROP POLICY IF EXISTS tenant_isolation_projects ON user_mundiai_projects")
    op.execute("""
        CREATE POLICY tenant_isolation_projects ON user_mundiai_projects
        USING (
            CASE
                WHEN coalesce(current_setting('app.user_id', true), '') = '' THEN true
                ELSE
                    owner_uuid::text = current_setting('app.user_id', true)
                    OR current_setting('app.user_id', true)::uuid = ANY(editor_uuids)
                    OR current_setting('app.user_id', true)::uuid = ANY(viewer_uuids)
            END
        )
    """)

    op.execute("DROP POLICY IF EXISTS tenant_isolation_conversations ON conversations")
    op.execute("""
        CREATE POLICY tenant_isolation_conversations ON conversations
        USING (
            CASE
                WHEN coalesce(current_setting('app.user_id', true), '') = '' THEN true
                ELSE
                    owner_uuid::text = current_setting('app.user_id', true)
                    OR project_id IN (
                        SELECT id FROM user_mundiai_projects
                        WHERE owner_uuid::text = current_setting('app.user_id', true)
                           OR current_setting('app.user_id', true)::uuid = ANY(editor_uuids)
                           OR current_setting('app.user_id', true)::uuid = ANY(viewer_uuids)
                    )
            END
        )
    """)


def downgrade() -> None:
    op.execute("DROP POLICY IF EXISTS tenant_isolation_conversations ON conversations")
    op.execute("""
        CREATE POLICY tenant_isolation_conversations ON conversations
        USING (
            coalesce(current_setting('app.user_id', true), '') = ''
            OR owner_uuid::text = current_setting('app.user_id', true)
            OR project_id IN (
                SELECT id FROM user_mundiai_projects
                WHERE owner_uuid::text = current_setting('app.user_id', true)
                   OR current_setting('app.user_id', true)::uuid = ANY(editor_uuids)
                   OR current_setting('app.user_id', true)::uuid = ANY(viewer_uuids)
            )
        )
    """)

    op.execute("DROP POLICY IF EXISTS tenant_isolation_projects ON user_mundiai_projects")
    op.execute("""
        CREATE POLICY tenant_isolation_projects ON user_mundiai_projects
        USING (
            coalesce(current_setting('app.user_id', true), '') = ''
            OR owner_uuid::text = current_setting('app.user_id', true)
            OR current_setting('app.user_id', true)::uuid = ANY(editor_uuids)
            OR current_setting('app.user_id', true)::uuid = ANY(viewer_uuids)
        )
    """)
