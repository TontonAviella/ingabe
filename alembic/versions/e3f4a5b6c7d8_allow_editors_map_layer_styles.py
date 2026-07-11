"""Allow project editors to attach styles to editable maps.

Revision ID: e3f4a5b6c7d8
Revises: e2f3a4b5c6d7
"""

from alembic import op


revision: str = "e3f4a5b6c7d8"
down_revision: str = "e2f3a4b5c6d7"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""
        ALTER POLICY tenant_isolation_map_layer_styles ON map_layer_styles
        WITH CHECK (
            CASE
                WHEN coalesce(current_setting('app.user_id', true), '') = '' THEN true
                ELSE EXISTS (
                    SELECT 1
                    FROM user_mundiai_maps m
                    JOIN user_mundiai_projects p ON p.id = m.project_id
                    WHERE m.id = map_layer_styles.map_id
                      AND p.soft_deleted_at IS NULL
                      AND (
                          m.owner_uuid::text = current_setting('app.user_id', true)
                          OR p.owner_uuid::text = current_setting('app.user_id', true)
                          OR NULLIF(current_setting('app.user_id', true), '')::uuid
                             = ANY(COALESCE(p.editor_uuids, ARRAY[]::uuid[]))
                      )
                )
            END
        )
    """)


def downgrade() -> None:
    op.execute("""
        ALTER POLICY tenant_isolation_map_layer_styles ON map_layer_styles
        WITH CHECK (
            CASE
                WHEN coalesce(current_setting('app.user_id', true), '') = '' THEN true
                ELSE map_id IN (
                    SELECT id FROM user_mundiai_maps
                    WHERE owner_uuid::text = current_setting('app.user_id', true)
                )
            END
        )
    """)
