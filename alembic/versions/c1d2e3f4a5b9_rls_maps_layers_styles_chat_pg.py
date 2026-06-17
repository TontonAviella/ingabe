"""add RLS policies to user_mundiai_maps, map_layers, map_layer_styles,
chat_completion_messages, project_postgres_connections

Closes the partner-isolation gap on the non-brain tables. Before this
migration these 5 tables had no RLS at all — a misconfigured channel
worker could read or write across user boundaries unchecked.

Pattern matches existing tenant_isolation_projects/conversations:
- app.user_id GUC = empty → migration/admin bypass (CASE WHEN short-circuit)
- app.user_id GUC = set  → enforce owner_uuid match (direct or via join)

CASE WHEN is required because Postgres does not short-circuit OR in policy
expressions; a plain ::uuid cast on '' would 500 the query at plan time
even when the bypass branch is true. See f3a4b5c6d7e8 for the history.

For tables without a direct owner column we join through user_mundiai_maps:
- map_layer_styles → user_mundiai_maps.owner_uuid via map_id
- chat_completion_messages → user_mundiai_maps.owner_uuid via map_id

map_layers and project_postgres_connections have direct uuid columns
(owner_uuid and user_id respectively).

Revision ID: c1d2e3f4a5b9
Revises: b1c2d3e4f5a8
Create Date: 2026-05-12 00:00:00.000000

"""
from typing import Sequence, Union

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "c1d2e3f4a5b9"
down_revision: str = "b1c2d3e4f5a8"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # -- user_mundiai_maps -------------------------------------------------
    op.execute("ALTER TABLE user_mundiai_maps ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE user_mundiai_maps FORCE ROW LEVEL SECURITY")
    op.execute("""
        CREATE POLICY tenant_isolation_maps ON user_mundiai_maps
        USING (
            CASE
                WHEN coalesce(current_setting('app.user_id', true), '') = '' THEN true
                ELSE
                    owner_uuid::text = current_setting('app.user_id', true)
                    OR project_id IN (
                        SELECT id FROM user_mundiai_projects
                        WHERE owner_uuid::text = current_setting('app.user_id', true)
                           OR NULLIF(current_setting('app.user_id', true), '')::uuid = ANY(editor_uuids)
                           OR NULLIF(current_setting('app.user_id', true), '')::uuid = ANY(viewer_uuids)
                    )
            END
        )
    """)

    # -- map_layers --------------------------------------------------------
    op.execute("ALTER TABLE map_layers ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE map_layers FORCE ROW LEVEL SECURITY")
    op.execute("""
        CREATE POLICY tenant_isolation_map_layers ON map_layers
        USING (
            CASE
                WHEN coalesce(current_setting('app.user_id', true), '') = '' THEN true
                ELSE owner_uuid::text = current_setting('app.user_id', true)
            END
        )
    """)

    # -- map_layer_styles --------------------------------------------------
    # No direct owner column. Join through user_mundiai_maps.
    op.execute("ALTER TABLE map_layer_styles ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE map_layer_styles FORCE ROW LEVEL SECURITY")
    op.execute("""
        CREATE POLICY tenant_isolation_map_layer_styles ON map_layer_styles
        USING (
            CASE
                WHEN coalesce(current_setting('app.user_id', true), '') = '' THEN true
                ELSE map_id IN (
                    SELECT id FROM user_mundiai_maps
                    WHERE owner_uuid::text = current_setting('app.user_id', true)
                )
            END
        )
    """)

    # -- chat_completion_messages -----------------------------------------
    # Authorization is via the map's owner. sender_id is the LLM/system
    # identity, not the row owner — using it for isolation would be wrong.
    op.execute("ALTER TABLE chat_completion_messages ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE chat_completion_messages FORCE ROW LEVEL SECURITY")
    op.execute("""
        CREATE POLICY tenant_isolation_chat_messages ON chat_completion_messages
        USING (
            CASE
                WHEN coalesce(current_setting('app.user_id', true), '') = '' THEN true
                ELSE map_id IN (
                    SELECT id FROM user_mundiai_maps
                    WHERE owner_uuid::text = current_setting('app.user_id', true)
                )
            END
        )
    """)

    # -- project_postgres_connections -------------------------------------
    # Connection URIs include partner DB credentials — strict isolation.
    op.execute("ALTER TABLE project_postgres_connections ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE project_postgres_connections FORCE ROW LEVEL SECURITY")
    op.execute("""
        CREATE POLICY tenant_isolation_pg_connections ON project_postgres_connections
        USING (
            CASE
                WHEN coalesce(current_setting('app.user_id', true), '') = '' THEN true
                ELSE
                    user_id::text = current_setting('app.user_id', true)
                    OR project_id IN (
                        SELECT id FROM user_mundiai_projects
                        WHERE owner_uuid::text = current_setting('app.user_id', true)
                           OR NULLIF(current_setting('app.user_id', true), '')::uuid = ANY(editor_uuids)
                    )
            END
        )
    """)


def downgrade() -> None:
    op.execute("DROP POLICY IF EXISTS tenant_isolation_pg_connections ON project_postgres_connections")
    op.execute("ALTER TABLE project_postgres_connections DISABLE ROW LEVEL SECURITY")

    op.execute("DROP POLICY IF EXISTS tenant_isolation_chat_messages ON chat_completion_messages")
    op.execute("ALTER TABLE chat_completion_messages DISABLE ROW LEVEL SECURITY")

    op.execute("DROP POLICY IF EXISTS tenant_isolation_map_layer_styles ON map_layer_styles")
    op.execute("ALTER TABLE map_layer_styles DISABLE ROW LEVEL SECURITY")

    op.execute("DROP POLICY IF EXISTS tenant_isolation_map_layers ON map_layers")
    op.execute("ALTER TABLE map_layers DISABLE ROW LEVEL SECURITY")

    op.execute("DROP POLICY IF EXISTS tenant_isolation_maps ON user_mundiai_maps")
    op.execute("ALTER TABLE user_mundiai_maps DISABLE ROW LEVEL SECURITY")
