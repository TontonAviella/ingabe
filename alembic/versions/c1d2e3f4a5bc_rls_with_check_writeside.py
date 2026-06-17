"""add WITH CHECK to tenant_isolation_* policies — close write-side gap

c1d2e3f4a5b9 (maps/layers/styles/chat/pg_connections) and c1d2e3f4a5bb
(brain write-side) created ALL policies with USING only and no WITH CHECK.
That gates read/delete/update-visibility correctly but leaves INSERT and
UPDATE-row-after-write unrestricted:

  A compromised channel worker holding any valid partner GUC could
  INSERT INTO map_layers (owner_uuid, ...) VALUES ('<victim_uuid>', ...)
  and the row would land — RLS would just hide it from the attacker, not
  the victim. The victim then sees a row in their namespace they never
  created. For BK Insurance (regulated payouts off these rows) that is
  not acceptable.

WITH CHECK enforces the same predicate on row data being written. With
both USING and WITH CHECK, INSERTs and UPDATEs can only produce rows the
current partner can also see — write-side spoofing closed.

Policies updated (mirror USING):
  - user_mundiai_maps          (tenant_isolation_maps)
  - map_layers                 (tenant_isolation_map_layers)
  - map_layer_styles           (tenant_isolation_map_layer_styles)
  - chat_completion_messages   (tenant_isolation_chat_messages)
  - project_postgres_connections (tenant_isolation_pg_connections)
  - brain_pending_hooks        (tenant_isolation_brain_pending_hooks)
  - brain_ingest_log           (tenant_isolation_brain_ingest_log)
  - brain_entity_refs          (tenant_isolation_brain_entity_refs, RESTRICTIVE)

Admin/migration bypass (empty GUC → true) is preserved on the WITH CHECK
branch, matching USING, so seeders and alembic upgrades keep working.

Revision ID: c1d2e3f4a5bc
Revises: c1d2e3f4a5bb
Create Date: 2026-05-12 00:00:00.000000

"""
from typing import Sequence, Union

from alembic import op

revision: str = "c1d2e3f4a5bc"
down_revision: str = "c1d2e3f4a5bb"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # ----- user_mundiai_maps -----
    op.execute("""
        ALTER POLICY tenant_isolation_maps ON user_mundiai_maps
        WITH CHECK (
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

    # ----- map_layers -----
    op.execute("""
        ALTER POLICY tenant_isolation_map_layers ON map_layers
        WITH CHECK (
            CASE
                WHEN coalesce(current_setting('app.user_id', true), '') = '' THEN true
                ELSE owner_uuid::text = current_setting('app.user_id', true)
            END
        )
    """)

    # ----- map_layer_styles -----
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

    # ----- chat_completion_messages -----
    op.execute("""
        ALTER POLICY tenant_isolation_chat_messages ON chat_completion_messages
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

    # ----- project_postgres_connections -----
    op.execute("""
        ALTER POLICY tenant_isolation_pg_connections ON project_postgres_connections
        WITH CHECK (
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

    # ----- brain_pending_hooks -----
    # owner_uuid has a DEFAULT from app.user_id GUC, but an explicit INSERT
    # of a spoofed owner_uuid would currently succeed. WITH CHECK blocks it.
    op.execute("""
        ALTER POLICY tenant_isolation_brain_pending_hooks ON brain_pending_hooks
        WITH CHECK (
            CASE
                WHEN coalesce(current_setting('app.user_id', true), '') = '' THEN true
                ELSE owner_uuid::text = current_setting('app.user_id', true)
            END
        )
    """)

    # ----- brain_ingest_log -----
    op.execute("""
        ALTER POLICY tenant_isolation_brain_ingest_log ON brain_ingest_log
        WITH CHECK (
            CASE
                WHEN coalesce(current_setting('app.user_id', true), '') = '' THEN true
                ELSE owner_uuid::text = current_setting('app.user_id', true)
            END
        )
    """)

    # ----- brain_entity_refs (RESTRICTIVE) -----
    # WITH CHECK on RESTRICTIVE blocks cross-page reference spoofing on INSERT.
    op.execute("""
        ALTER POLICY tenant_isolation_brain_entity_refs ON brain_entity_refs
        WITH CHECK (
            CASE
                WHEN coalesce(current_setting('app.user_id', true), '') = '' THEN true
                ELSE page_id IN (
                    SELECT id FROM brain_pages
                    WHERE owner_uuid::text = current_setting('app.user_id', true)
                       OR NULLIF(current_setting('app.user_id', true), '')::uuid = ANY(viewer_uuids)
                       OR NULLIF(current_setting('app.user_id', true), '')::uuid = ANY(editor_uuids)
                )
            END
        )
    """)


def downgrade() -> None:
    # ALTER POLICY ... WITH CHECK (NULL) is not valid syntax; to drop a
    # WITH CHECK clause we have to drop and recreate the policy USING-only.
    # We restore exactly what c1d2e3f4a5b9 / c1d2e3f4a5bb created.

    # user_mundiai_maps
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

    # map_layers
    op.execute("DROP POLICY IF EXISTS tenant_isolation_map_layers ON map_layers")
    op.execute("""
        CREATE POLICY tenant_isolation_map_layers ON map_layers
        USING (
            CASE
                WHEN coalesce(current_setting('app.user_id', true), '') = '' THEN true
                ELSE owner_uuid::text = current_setting('app.user_id', true)
            END
        )
    """)

    # map_layer_styles
    op.execute("DROP POLICY IF EXISTS tenant_isolation_map_layer_styles ON map_layer_styles")
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

    # chat_completion_messages
    op.execute("DROP POLICY IF EXISTS tenant_isolation_chat_messages ON chat_completion_messages")
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

    # project_postgres_connections
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

    # brain_pending_hooks
    op.execute("DROP POLICY IF EXISTS tenant_isolation_brain_pending_hooks ON brain_pending_hooks")
    op.execute("""
        CREATE POLICY tenant_isolation_brain_pending_hooks ON brain_pending_hooks
        USING (
            CASE
                WHEN coalesce(current_setting('app.user_id', true), '') = '' THEN true
                ELSE owner_uuid::text = current_setting('app.user_id', true)
            END
        )
    """)

    # brain_ingest_log
    op.execute("DROP POLICY IF EXISTS tenant_isolation_brain_ingest_log ON brain_ingest_log")
    op.execute("""
        CREATE POLICY tenant_isolation_brain_ingest_log ON brain_ingest_log
        USING (
            CASE
                WHEN coalesce(current_setting('app.user_id', true), '') = '' THEN true
                ELSE owner_uuid::text = current_setting('app.user_id', true)
            END
        )
    """)

    # brain_entity_refs
    op.execute("DROP POLICY IF EXISTS tenant_isolation_brain_entity_refs ON brain_entity_refs")
    op.execute("""
        CREATE POLICY tenant_isolation_brain_entity_refs ON brain_entity_refs
        AS RESTRICTIVE
        USING (
            CASE
                WHEN coalesce(current_setting('app.user_id', true), '') = '' THEN true
                ELSE page_id IN (
                    SELECT id FROM brain_pages
                    WHERE owner_uuid::text = current_setting('app.user_id', true)
                       OR NULLIF(current_setting('app.user_id', true), '')::uuid = ANY(viewer_uuids)
                       OR NULLIF(current_setting('app.user_id', true), '')::uuid = ANY(editor_uuids)
                )
            END
        )
    """)
