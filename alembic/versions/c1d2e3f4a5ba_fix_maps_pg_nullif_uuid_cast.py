"""fix NULLIF uuid cast on tenant_isolation_maps and tenant_isolation_pg_connections

c1d2e3f4a5b9 added RLS to 5 unprotected tables. Two of those policies join
through user_mundiai_projects with `current_setting('app.user_id', true)::uuid
= ANY(editor_uuids|viewer_uuids)`. Postgres' optimizer pulls the subquery up
and evaluates `''::uuid` even when the outer CASE WHEN bypass branch should
have short-circuited — same eager-eval bug e5f6a7b8c9d0 fixed for brain_*.

Symptom: any INSERT/UPDATE on user_mundiai_maps or project_postgres_connections
in admin context (empty GUC) fails with
`invalid input syntax for type uuid: ""`. That blocks migrations, seeders, and
any background job that runs without an app.user_id GUC set.

Fix: wrap with `NULLIF(current_setting('app.user_id', true), '')::uuid` so the
empty-string case yields NULL (and `NULL = ANY(arr)` is NULL, which is falsy
under WHERE — the outer CASE bypass still wins). Identical pattern to
e5f6a7b8c9d0.

This migration only rewrites the two affected policies. The other three from
c1d2e3f4a5b9 (map_layers, map_layer_styles, chat_completion_messages) don't
have the ANY-on-array branch and are unaffected.

Revision ID: c1d2e3f4a5ba
Revises: c1d2e3f4a5b9
Create Date: 2026-05-12 00:00:00.000000

"""
from typing import Sequence, Union

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "c1d2e3f4a5ba"
down_revision: str = "c1d2e3f4a5b9"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # -- user_mundiai_maps -------------------------------------------------
    op.execute("DROP POLICY IF EXISTS tenant_isolation_maps ON user_mundiai_maps")
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

    # -- project_postgres_connections -------------------------------------
    op.execute("DROP POLICY IF EXISTS tenant_isolation_pg_connections ON project_postgres_connections")
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
                           OR current_setting('app.user_id', true)::uuid = ANY(editor_uuids)
                    )
            END
        )
    """)

    op.execute("DROP POLICY IF EXISTS tenant_isolation_maps ON user_mundiai_maps")
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
                           OR current_setting('app.user_id', true)::uuid = ANY(editor_uuids)
                           OR current_setting('app.user_id', true)::uuid = ANY(viewer_uuids)
                    )
            END
        )
    """)
