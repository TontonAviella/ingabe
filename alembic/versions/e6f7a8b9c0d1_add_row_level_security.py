"""add row-level security policies to user_mundiai_projects and conversations

Revision ID: e6f7a8b9c0d1
Revises: d5e6f7a8b9c0
Create Date: 2026-03-04 10:00:00.000000

"""
from typing import Sequence, Union

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "e6f7a8b9c0d1"
down_revision: str = "d5e6f7a8b9c0"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # -- user_mundiai_projects --
    op.execute("ALTER TABLE user_mundiai_projects ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE user_mundiai_projects FORCE ROW LEVEL SECURITY")
    op.execute("""
        CREATE POLICY tenant_isolation_projects ON user_mundiai_projects
        USING (
            current_setting('app.user_id', true) IS NULL
            OR owner_uuid::text = current_setting('app.user_id', true)
            OR current_setting('app.user_id', true)::uuid = ANY(editor_uuids)
            OR current_setting('app.user_id', true)::uuid = ANY(viewer_uuids)
        )
    """)

    # -- conversations --
    op.execute("ALTER TABLE conversations ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE conversations FORCE ROW LEVEL SECURITY")
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


def downgrade() -> None:
    op.execute("DROP POLICY IF EXISTS tenant_isolation_conversations ON conversations")
    op.execute("ALTER TABLE conversations DISABLE ROW LEVEL SECURITY")

    op.execute("DROP POLICY IF EXISTS tenant_isolation_projects ON user_mundiai_projects")
    op.execute("ALTER TABLE user_mundiai_projects DISABLE ROW LEVEL SECURITY")
