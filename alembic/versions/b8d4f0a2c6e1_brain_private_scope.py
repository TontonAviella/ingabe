"""brain: a page with no access_scope is private to its owner, not public

d4e5f6a7b8c9 let RLS treat access_scope IS NULL as public ("legacy rows
pre-backfill"); the backfill never came, and BrainService.put_page left the
scope NULL unless told otherwise. So every raster, layer, project-document
and insurance page a user created was readable by every other user and
partner through search_brain, get_page, list_pages and get_pages_in_bbox:
two PERMISSIVE policies are OR'ed, and partner_isolation said yes to NULL.
Found 2026-10-07 (33 real pages of one user, readable by any session).

This migration:
  1. Adds the scope 'private' (the owner, viewers and editors only) to the
     CHECK constraints of brain_pages and its five scoped children.
  2. Backfills brain_pages.access_scope IS NULL: 'partner_internal' when the
     page names a partner (the owner's partner sees it), otherwise
     'private'. The propagate trigger (f6a7b8c9d0e1) copies the scope to
     the page's children. updated_at is left alone, so nothing re-embeds.
  3. Makes brain_pages.access_scope NOT NULL DEFAULT 'private', so a missing
     scope can never again mean anything.
  4. Rewrites partner_isolation_* on brain_pages and the five children to
     grant public rows and the session partner's partner_internal rows
     only. private, mundi_only and NULL fall through to false, so only
     tenant_isolation_* (owner, viewers, editors) can grant them.
  5. Makes tenant_isolation_brain_entity_refs PERMISSIVE again (same USING
     and WITH CHECK). c1d2e3f4a5bb made it RESTRICTIVE only to cancel the
     NULL-is-public grant; left RESTRICTIVE, an owner would lose the refs
     of their own private pages, since partner_isolation no longer grants
     those. All five children now share one structure.

Both policies stay PERMISSIVE. Making partner_isolation RESTRICTIVE would
AND it with the owner-only tenant policy and hide public knowledge from
everyone but the service account that ingested it.

The empty app.user_id branch (migrations, the hook processor, the ingestion
scheduler) still sees every row, and the NULLIF guard on the tenant
policies' uuid casts is untouched (memory feedback_rls_nullif_uuid_cast).

brain_entities keeps its own policy: it has no owner column, so "private"
means nothing there, and no application code reads or writes it.

Revision ID: b8d4f0a2c6e1
Revises: c9e5f1a3b4d6
Create Date: 2026-10-07

"""

from typing import Sequence, Union

from alembic import op

revision: str = "b8d4f0a2c6e1"
down_revision: Union[str, None] = "c9e5f1a3b4d6"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


_TABLES = [
    "brain_pages",
    "brain_page_versions",
    "brain_content_chunks",
    "brain_timeline_entries",
    "brain_tables",
    "brain_entity_refs",
]

_SCOPES_WITH_PRIVATE = "'public', 'partner_internal', 'mundi_only', 'private'"
_SCOPES_BEFORE = "'public', 'partner_internal', 'mundi_only'"

# Grants only what is shared beyond the page's own members. The page's owner,
# viewers and editors are granted by tenant_isolation_{table}; the two
# PERMISSIVE policies are OR'ed.
_PARTNER_ISOLATION = """
    CREATE POLICY partner_isolation_{table} ON {table}
    USING (
        CASE
            WHEN coalesce(current_setting('app.user_id', true), '') = '' THEN true
            WHEN current_setting('app.role', true) = 'admin' THEN true
            WHEN access_scope = 'public' THEN true
            WHEN access_scope = 'partner_internal' THEN
                partner_id IS NOT NULL
                AND partner_id::text = coalesce(current_setting('app.partner_id', true), '')
            ELSE false
        END
    )
"""

# d4e5f6a7b8c9 / f6a7b8c9d0e1, for downgrade.
_PARTNER_ISOLATION_NULL_IS_PUBLIC = """
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


# The page's owner, viewers and editors (c1d2e3f4a5bb / c1d2e3f4a5bc).
_ENTITY_REFS_MEMBERS = """
    CASE
        WHEN coalesce(current_setting('app.user_id', true), '') = '' THEN true
        ELSE page_id IN (
            SELECT id FROM brain_pages
            WHERE owner_uuid::text = current_setting('app.user_id', true)
               OR NULLIF(current_setting('app.user_id', true), '')::uuid = ANY(viewer_uuids)
               OR NULLIF(current_setting('app.user_id', true), '')::uuid = ANY(editor_uuids)
        )
    END
"""


def _entity_refs_tenant_policy(kind: str) -> None:
    op.execute("DROP POLICY IF EXISTS tenant_isolation_brain_entity_refs ON brain_entity_refs")
    op.execute(f"""
        CREATE POLICY tenant_isolation_brain_entity_refs ON brain_entity_refs
        AS {kind}
        USING ({_ENTITY_REFS_MEMBERS})
        WITH CHECK ({_ENTITY_REFS_MEMBERS})
    """)


def _set_scope_check(scopes: str) -> None:
    for table in _TABLES:
        op.execute(f"ALTER TABLE {table} DROP CONSTRAINT IF EXISTS {table}_access_scope_chk")
        op.execute(
            f"ALTER TABLE {table} ADD CONSTRAINT {table}_access_scope_chk "
            f"CHECK (access_scope IS NULL OR access_scope IN ({scopes}))"
        )


def _replace_partner_policies(policy: str) -> None:
    for table in _TABLES:
        op.execute(f"DROP POLICY IF EXISTS partner_isolation_{table} ON {table}")
        op.execute(policy.format(table=table))


def upgrade() -> None:
    # Runs in the migration's empty-app.user_id context, so RLS lets the
    # backfill (and the trigger's child updates) see every row.
    _set_scope_check(_SCOPES_WITH_PRIVATE)
    op.execute("""
        UPDATE brain_pages
           SET access_scope = CASE WHEN partner_id IS NOT NULL
                                   THEN 'partner_internal' ELSE 'private' END
         WHERE access_scope IS NULL
    """)
    op.execute("ALTER TABLE brain_pages ALTER COLUMN access_scope SET DEFAULT 'private'")
    op.execute("ALTER TABLE brain_pages ALTER COLUMN access_scope SET NOT NULL")
    _replace_partner_policies(_PARTNER_ISOLATION)
    _entity_refs_tenant_policy("PERMISSIVE")


def downgrade() -> None:
    # Pages backfilled to partner_internal cannot be told apart from pages
    # that were partner_internal before; they stay partner_internal.
    _entity_refs_tenant_policy("RESTRICTIVE")
    _replace_partner_policies(_PARTNER_ISOLATION_NULL_IS_PUBLIC)
    op.execute("ALTER TABLE brain_pages ALTER COLUMN access_scope DROP NOT NULL")
    op.execute("ALTER TABLE brain_pages ALTER COLUMN access_scope DROP DEFAULT")
    op.execute("UPDATE brain_pages SET access_scope = NULL WHERE access_scope = 'private'")
    _set_scope_check(_SCOPES_BEFORE)
