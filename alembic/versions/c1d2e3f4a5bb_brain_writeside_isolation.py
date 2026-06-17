"""tighten RLS on brain_pending_hooks, brain_ingest_log, brain_entity_refs

Audit of brain_service write paths surfaced three real exposures:

1. brain_pending_hooks had `USING true` (no-op). Payloads carry user-uploaded
   layer metadata (paths, layer names, map_ids) — cross-partner read is a leak.
2. brain_ingest_log had `USING true` (no-op). Reads from user context could
   leak which partners are ingesting what data.
3. brain_entity_refs.tenant_isolation was `page_id IN (SELECT id FROM brain_pages)`
   with no owner filter — any authenticated user could see references to any
   user's pages.

All three are fixed in one migration. brain_pending_hooks and brain_ingest_log
gain an `owner_uuid` column populated automatically from the `app.user_id` GUC
at INSERT time (see brain_service.py for the column default expression). Legacy
rows have NULL owner — only the empty-GUC admin context sees them.

brain_entity_refs gets the standard owner_uuid join through brain_pages with
NULLIF-guarded uuid casts (see feedback_rls_nullif_uuid_cast for context).

Revision ID: c1d2e3f4a5bb
Revises: c1d2e3f4a5ba
Create Date: 2026-05-12 00:00:00.000000

"""
from typing import Sequence, Union

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "c1d2e3f4a5bb"
down_revision: str = "c1d2e3f4a5ba"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # ----- brain_pending_hooks: add owner column + tighten policy -----
    op.execute("""
        ALTER TABLE brain_pending_hooks
        ADD COLUMN IF NOT EXISTS owner_uuid uuid
    """)
    # Default future inserts to the calling user's uuid (NULL under admin/empty GUC).
    op.execute("""
        ALTER TABLE brain_pending_hooks
        ALTER COLUMN owner_uuid SET DEFAULT NULLIF(current_setting('app.user_id', true), '')::uuid
    """)
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

    # ----- brain_ingest_log: same pattern -----
    op.execute("""
        ALTER TABLE brain_ingest_log
        ADD COLUMN IF NOT EXISTS owner_uuid uuid
    """)
    op.execute("""
        ALTER TABLE brain_ingest_log
        ALTER COLUMN owner_uuid SET DEFAULT NULLIF(current_setting('app.user_id', true), '')::uuid
    """)
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

    # ----- brain_entity_refs: replace weak policy with owner-aware join -----
    # RESTRICTIVE so this AND's with partner_isolation (which grants public/NULL
    # access_scope to everyone). Without RESTRICTIVE the two PERMISSIVE policies
    # OR together and partner_isolation's public branch defeats owner-scoping.
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


def downgrade() -> None:
    op.execute("DROP POLICY IF EXISTS tenant_isolation_brain_entity_refs ON brain_entity_refs")
    op.execute("""
        CREATE POLICY tenant_isolation_brain_entity_refs ON brain_entity_refs
        AS PERMISSIVE
        USING (
            CASE
                WHEN coalesce(current_setting('app.user_id', true), '') = '' THEN true
                ELSE page_id IN (SELECT id FROM brain_pages)
            END
        )
    """)

    op.execute("DROP POLICY IF EXISTS tenant_isolation_brain_ingest_log ON brain_ingest_log")
    op.execute("CREATE POLICY tenant_isolation_brain_ingest_log ON brain_ingest_log USING (true)")
    op.execute("ALTER TABLE brain_ingest_log ALTER COLUMN owner_uuid DROP DEFAULT")
    op.execute("ALTER TABLE brain_ingest_log DROP COLUMN IF EXISTS owner_uuid")

    op.execute("DROP POLICY IF EXISTS tenant_isolation_brain_pending_hooks ON brain_pending_hooks")
    op.execute("CREATE POLICY tenant_isolation_brain_pending_hooks ON brain_pending_hooks USING (true)")
    op.execute("ALTER TABLE brain_pending_hooks ALTER COLUMN owner_uuid DROP DEFAULT")
    op.execute("ALTER TABLE brain_pending_hooks DROP COLUMN IF EXISTS owner_uuid")
