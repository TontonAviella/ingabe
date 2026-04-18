"""brain phase 0: denormalize partner_id/access_scope onto child tables + partner_isolation RLS

The scheduler writes partner_internal data under a shared service-account
owner_uuid (BRAIN_INGEST_OWNER_UUID). Legacy tenant_isolation_* policies
on brain_page_versions / brain_content_chunks / brain_timeline_entries /
brain_tables / brain_entity_refs
gate children through a correlated subquery back to brain_pages keyed on
owner_uuid/viewer_uuids/editor_uuids — which does not check partner_id.
That leaves the child rows without any direct per-tenant gate once
app.user_id is set to something that can reach the parent row by other
means (admin sessions, BYPASSRLS rehearsals, future cache paths).

This migration:
  1. Adds partner_id UUID + access_scope TEXT to the five page_id child
     tables (versions, chunks, timeline, tables, entity_refs), mirroring
     the shape already on brain_pages. brain_entities is excluded because
     d4e5f6a7b8c9 already gave it partner_id, access_scope, and a
     partner_isolation policy.
  2. Backfills existing children from their parent brain_pages row.
  3. Adds a BEFORE INSERT trigger on each child so new inserts inherit
     the parent's partner_id / access_scope automatically — callers do
     not have to thread scope through the child write path.
  4. Adds an AFTER UPDATE trigger on brain_pages that propagates
     partner_id / access_scope changes out to every existing child row.
     This covers normalizer.write_page's ordering: put_page inserts
     children first (children land as public/NULL), then the parent
     UPDATE flips access_scope to partner_internal — the propagation
     trigger runs in the same txn and rewrites the children's tags
     before commit.
  5. Creates partner_isolation_* policies on each child with the same
     CASE WHEN shape as brain_pages. tenant_isolation_* policies are
     kept (both are PERMISSIVE → OR'd), preserving owner/viewer/editor
     access for non-partner data exactly like brain_pages does.

Revision ID: f6a7b8c9d0e1
Revises: e5f6a7b8c9d0
Create Date: 2026-04-18 19:30:00.000000

"""
from typing import Sequence, Union

from alembic import op

revision: str = "f6a7b8c9d0e1"
down_revision: str = "e5f6a7b8c9d0"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


_CHILD_TABLES = [
    "brain_page_versions",
    "brain_content_chunks",
    "brain_timeline_entries",
    "brain_tables",
    "brain_entity_refs",
]


# Same CASE WHEN as _PARTNER_ISOLATION_POLICY in d4e5f6a7b8c9 on brain_pages.
# No correlated subquery — we denormalized partner_id/access_scope onto the
# child row, so the policy reads them directly and avoids the subquery-pullup
# NULLIF trap that e5f6a7b8c9d0 had to work around for tenant_isolation.
_PARTNER_ISOLATION_CHILD = """
    CREATE POLICY partner_isolation_{table} ON {table}
    USING (
        CASE
            WHEN coalesce(current_setting('app.user_id', true), '') = '' THEN true
            WHEN current_setting('app.role', true) = 'admin' THEN true
            WHEN access_scope IS NULL OR access_scope = 'public' THEN true
            WHEN access_scope = 'partner_internal' THEN
                partner_id IS NOT NULL
                AND partner_id::text = coalesce(current_setting('app.partner_id', true), '')
            WHEN access_scope = 'mundi_only' THEN false
            ELSE false
        END
    )
"""


_CHILD_INHERIT_FN = """
    CREATE OR REPLACE FUNCTION brain_child_inherit_partner_scope()
    RETURNS TRIGGER AS $$
    BEGIN
        SELECT p.partner_id, p.access_scope
          INTO NEW.partner_id, NEW.access_scope
          FROM brain_pages p
         WHERE p.id = NEW.page_id;
        RETURN NEW;
    END;
    $$ LANGUAGE plpgsql;
"""


# Propagation on parent UPDATE handles the write ordering in normalizer.write_page:
# put_page inserts children before the parent UPDATE sets access_scope/partner_id,
# so without this trigger the child rows would keep their default (NULL/public)
# tags even though the parent is partner_internal. AFTER UPDATE fires in the same
# transaction as the parent UPDATE, rewriting the child tags before commit.
_PARENT_PROPAGATE_FN = """
    CREATE OR REPLACE FUNCTION brain_pages_propagate_partner_scope()
    RETURNS TRIGGER AS $$
    BEGIN
        IF NEW.partner_id   IS DISTINCT FROM OLD.partner_id
           OR NEW.access_scope IS DISTINCT FROM OLD.access_scope THEN
            UPDATE brain_page_versions
               SET partner_id = NEW.partner_id, access_scope = NEW.access_scope
             WHERE page_id = NEW.id;
            UPDATE brain_content_chunks
               SET partner_id = NEW.partner_id, access_scope = NEW.access_scope
             WHERE page_id = NEW.id;
            UPDATE brain_timeline_entries
               SET partner_id = NEW.partner_id, access_scope = NEW.access_scope
             WHERE page_id = NEW.id;
            UPDATE brain_tables
               SET partner_id = NEW.partner_id, access_scope = NEW.access_scope
             WHERE page_id = NEW.id;
            UPDATE brain_entity_refs
               SET partner_id = NEW.partner_id, access_scope = NEW.access_scope
             WHERE page_id = NEW.id;
        END IF;
        RETURN NEW;
    END;
    $$ LANGUAGE plpgsql;
"""


def upgrade() -> None:
    # ── columns + constraints + indexes on each child ─────────────────
    for table in _CHILD_TABLES:
        op.execute(f"""
            ALTER TABLE {table}
                ADD COLUMN IF NOT EXISTS partner_id    UUID,
                ADD COLUMN IF NOT EXISTS access_scope  TEXT
        """)
        op.execute(f"""
            ALTER TABLE {table}
                ADD CONSTRAINT {table}_access_scope_chk
                CHECK (access_scope IS NULL
                       OR access_scope IN ('public', 'partner_internal', 'mundi_only'))
        """)
        op.execute(f"""
            ALTER TABLE {table}
                ADD CONSTRAINT {table}_partner_scope_chk
                CHECK (access_scope <> 'partner_internal' OR partner_id IS NOT NULL)
        """)
        op.execute(
            f"CREATE INDEX IF NOT EXISTS idx_{table}_scope_partner "
            f"ON {table}(access_scope, partner_id)"
        )

    # ── backfill existing children from their parent ─────────────────
    # Safe because partner_internal writes are currently gated off at both
    # the scheduler and write_page layers (BRAIN_PARTNER_INTERNAL_ENABLED),
    # so all pre-existing brain_pages rows have NULL or public scope; the
    # backfill lands as NULL/NULL or NULL/public on children. The WHERE
    # c.access_scope IS NULL guard makes this idempotent if the migration
    # is ever re-run against a partially populated schema.
    for table in _CHILD_TABLES:
        op.execute(f"""
            UPDATE {table} c
               SET partner_id    = p.partner_id,
                   access_scope  = p.access_scope
              FROM brain_pages p
             WHERE c.page_id = p.id
               AND c.access_scope IS NULL
               AND p.access_scope IS NOT NULL
        """)

    # ── child-side inheritance on INSERT ──────────────────────────────
    op.execute(_CHILD_INHERIT_FN)
    for table in _CHILD_TABLES:
        op.execute(
            f"DROP TRIGGER IF EXISTS trg_{table}_inherit_partner_scope ON {table}"
        )
        op.execute(f"""
            CREATE TRIGGER trg_{table}_inherit_partner_scope
                BEFORE INSERT ON {table}
                FOR EACH ROW
                EXECUTE FUNCTION brain_child_inherit_partner_scope()
        """)

    # ── parent-side propagation on UPDATE ─────────────────────────────
    op.execute(_PARENT_PROPAGATE_FN)
    op.execute(
        "DROP TRIGGER IF EXISTS trg_brain_pages_propagate_partner_scope ON brain_pages"
    )
    op.execute("""
        CREATE TRIGGER trg_brain_pages_propagate_partner_scope
            AFTER UPDATE OF partner_id, access_scope ON brain_pages
            FOR EACH ROW
            EXECUTE FUNCTION brain_pages_propagate_partner_scope()
    """)

    # ── partner_isolation policy on each child (additive, OR'd with
    #     legacy tenant_isolation) ──────────────────────────────────────
    for table in _CHILD_TABLES:
        op.execute(
            f"DROP POLICY IF EXISTS partner_isolation_{table} ON {table}"
        )
        op.execute(_PARTNER_ISOLATION_CHILD.format(table=table))


def downgrade() -> None:
    for table in _CHILD_TABLES:
        op.execute(
            f"DROP POLICY IF EXISTS partner_isolation_{table} ON {table}"
        )

    op.execute(
        "DROP TRIGGER IF EXISTS trg_brain_pages_propagate_partner_scope ON brain_pages"
    )
    op.execute("DROP FUNCTION IF EXISTS brain_pages_propagate_partner_scope()")

    for table in _CHILD_TABLES:
        op.execute(
            f"DROP TRIGGER IF EXISTS trg_{table}_inherit_partner_scope ON {table}"
        )
    op.execute("DROP FUNCTION IF EXISTS brain_child_inherit_partner_scope()")

    for table in _CHILD_TABLES:
        op.execute(f"DROP INDEX IF EXISTS idx_{table}_scope_partner")
        op.execute(
            f"ALTER TABLE {table} DROP CONSTRAINT IF EXISTS {table}_partner_scope_chk"
        )
        op.execute(
            f"ALTER TABLE {table} DROP CONSTRAINT IF EXISTS {table}_access_scope_chk"
        )
        op.execute(f"""
            ALTER TABLE {table}
                DROP COLUMN IF EXISTS access_scope,
                DROP COLUMN IF EXISTS partner_id
        """)
