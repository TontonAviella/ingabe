"""fix RLS policies: use CASE WHEN to prevent ''::uuid cast error

PostgreSQL does NOT short-circuit OR in policy expressions — the query
planner may evaluate ''::uuid even when the coalesce bypass is TRUE,
causing "invalid input syntax for type uuid" errors (production 500s).

Fix: wrap in CASE WHEN so the uuid casts are never evaluated when
app.user_id is NULL or '' (migrations/background/pool-reuse).

Revision ID: f3a4b5c6d7e8
Revises: e6f7a8b9c0d1
Create Date: 2026-03-04 12:00:00.000000

"""
from typing import Sequence, Union

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "f3a4b5c6d7e8"
down_revision: str = "e6f7a8b9c0d1"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Drop and recreate the projects policy with CASE WHEN fix
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

    # Drop and recreate the conversations policy with CASE WHEN fix
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
    # Revert to the original IS NULL policies
    op.execute("DROP POLICY IF EXISTS tenant_isolation_conversations ON conversations")
    op.execute("""
        CREATE POLICY tenant_isolation_conversations ON conversations
        USING (
            current_setting('app.user_id', true) IS NULL
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
            current_setting('app.user_id', true) IS NULL
            OR owner_uuid::text = current_setting('app.user_id', true)
            OR current_setting('app.user_id', true)::uuid = ANY(editor_uuids)
            OR current_setting('app.user_id', true)::uuid = ANY(viewer_uuids)
        )
    """)
