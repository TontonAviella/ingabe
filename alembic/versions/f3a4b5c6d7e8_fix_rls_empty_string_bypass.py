"""fix RLS policies to handle empty-string after connection pool RESET

After RESET app.user_id on a pooled connection, current_setting returns ''
(empty string) instead of NULL. The original IS NULL check failed to bypass
RLS for unauthenticated/background queries on reused connections.

Fix: use coalesce(current_setting(...), '') = '' which handles both NULL
and empty string.

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
    # Drop and recreate the projects policy with coalesce fix
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

    # Drop and recreate the conversations policy with coalesce fix
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
