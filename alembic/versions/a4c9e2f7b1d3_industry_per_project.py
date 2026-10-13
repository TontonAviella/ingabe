"""One industry per project; Brain notes belong to an industry and the database shows only the project's

Revision ID: a4c9e2f7b1d3
Revises: f3b8d1c6a2e4
Create Date: 2026-10-09

- user_mundiai_projects.industry: agriculture | power_grid | telecom. Every project made before this is
  agriculture (that is all Ingabe did); new projects take their creator's users.industry.
- brain_pages.industry: the industry a note belongs to; NULL = general knowledge every industry may read.
  The 37 notes on the server (field notes and insurance reports, 2026-10-09) are all agriculture, so existing
  notes become agriculture. New notes take the app.industry setting of the connection that writes them (set
  for every Sage turn from the project), else NULL.
- Restrictive row-level security, failing closed (audit 2026-10-09):
  - Only a background worker (no app.user_id AND no app.industry) is unrestricted.
  - Any other connection is scoped to app.industry (NULL when unset): it READS its industry's notes plus
    general ones, and WRITES only notes of exactly its scope (a Power Grid turn writes power_grid notes; a
    user request with no industry writes general notes). So no route that forgets to set the industry can
    read another industry, and nobody can slip industry content into a general note or another industry's.
  - Rows hanging off a note (chunks, facts, timeline entries, tags, references, versions, links) are
    readable when their note is, and writable only on a note of exactly the writer's scope.
  Restrictive policies are ANDed with the existing partner and tenant policies, so they can only narrow access.
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "a4c9e2f7b1d3"
down_revision: Union[str, None] = "f3b8d1c6a2e4"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_INDUSTRIES = "('agriculture', 'power_grid', 'telecom')"
_INDUSTRY_CHOICE_LIVE = "2026-10-09 01:08:46+00"  # PR #166 merged: users.industry exists from here on
_SCOPE = "NULLIF(current_setting('app.industry', true), '')"
# Unrestricted only for background workers: no user and no industry on the connection.
_WORKER = f"({_SCOPE} IS NULL AND COALESCE(current_setting('app.user_id', true), '') = '')"
# Every table whose rows hang off a note: (column, the note column it points at). Audit R1-25 added the last three.
_PAGE_CHILDREN = {
    "brain_content_chunks": ("page_id", "id"),
    "brain_facts": ("page_id", "id"),
    "brain_timeline_entries": ("page_id", "id"),
    "brain_tags": ("page_id", "id"),
    "brain_entity_refs": ("page_id", "id"),
    "brain_page_versions": ("page_id", "id"),
    "brain_links": ("from_page_id", "id"),
    "brain_raw_data": ("page_id", "id"),
    "brain_tables": ("page_id", "id"),
    "brain_files": ("page_slug", "slug"),
}


def _has_industry_column(table: str) -> bool:
    return op.get_bind().execute(sa.text(
        "SELECT 1 FROM information_schema.columns WHERE table_name = :t AND column_name = 'industry'"), {"t": table}
    ).scalar() is not None


def upgrade() -> None:
    # Labels survive a downgrade (it keeps the columns), so a later upgrade backfills only columns it creates now;
    # otherwise every Power Grid and Telecom project and note would come back as agriculture (audit R1-32).
    new_projects = not _has_industry_column("user_mundiai_projects")
    new_pages = not _has_industry_column("brain_pages")
    op.execute(
        "ALTER TABLE user_mundiai_projects ADD COLUMN IF NOT EXISTS industry text NOT NULL DEFAULT 'agriculture' "
        f"CHECK (industry IN {_INDUSTRIES})"
    )
    # Since #166 went live (merged 2026-10-09 01:08:46 UTC) people choose an industry at sign-in; a project or note
    # they made after that belongs to their chosen industry, not to agriculture (audit R1-14). Order matters: the
    # agriculture backfill below only fills what is still NULL.
    if new_projects:
        op.execute(
            "UPDATE user_mundiai_projects p SET industry = u.industry FROM users u "
            "WHERE u.internal_uuid = p.owner_uuid::text AND u.industry IN ('power_grid', 'telecom') "
            f"AND p.created_on >= TIMESTAMPTZ '{_INDUSTRY_CHOICE_LIVE}'"
        )
    op.execute(
        "ALTER TABLE brain_pages ADD COLUMN IF NOT EXISTS industry text "
        f"CHECK (industry IN {_INDUSTRIES})"
    )
    if new_pages:
        op.execute(
            "UPDATE brain_pages b SET industry = u.industry FROM users u "
            "WHERE u.internal_uuid = b.owner_uuid::text AND u.industry IN ('power_grid', 'telecom') "
            f"AND b.created_at >= TIMESTAMPTZ '{_INDUSTRY_CHOICE_LIVE}'"
        )
        op.execute("UPDATE brain_pages SET industry = 'agriculture' WHERE industry IS NULL")
    op.execute("ALTER TABLE brain_pages ALTER COLUMN industry SET DEFAULT NULLIF(current_setting('app.industry', true), '')")
    op.execute("CREATE INDEX IF NOT EXISTS brain_pages_industry_idx ON brain_pages (industry)")
    page_read = f"{_WORKER} OR industry IS NULL OR industry = {_SCOPE}"
    page_write = f"{_WORKER} OR industry IS NOT DISTINCT FROM {_SCOPE}"
    op.execute("DROP POLICY IF EXISTS industry_isolation_brain_pages ON brain_pages")
    op.execute(
        "CREATE POLICY industry_isolation_brain_pages ON brain_pages AS RESTRICTIVE FOR ALL "
        f"USING ({page_read}) WITH CHECK ({page_write})"
    )
    for table, (column, note_column) in _PAGE_CHILDREN.items():
        # Readable when the note is; writable only on a note of exactly the writer's scope.
        # A correlated key lookup, not "IN (SELECT ... FROM brain_pages)": that built a scan of every note into each
        # statement's plan, even with app.industry unset, and stopped being hashed past ~670k notes (audit R2-8).
        # brain_pages' own row security still applies inside the EXISTS, so isolation is the same.
        note = f"brain_pages bp WHERE bp.{note_column} = {table}.{column}"
        child_read = f"{_WORKER} OR {table}.{column} IS NULL OR EXISTS (SELECT 1 FROM {note})"
        child_write = (f"{_WORKER} OR EXISTS (SELECT 1 FROM {note} "
                       f"AND bp.industry IS NOT DISTINCT FROM {_SCOPE})")
        op.execute(f"DROP POLICY IF EXISTS industry_isolation_{table} ON {table}")
        op.execute(
            f"CREATE POLICY industry_isolation_{table} ON {table} AS RESTRICTIVE FOR ALL "
            f"USING ({child_read}) WITH CHECK ({child_write})"
        )


def downgrade() -> None:
    # Only the policies go. The industry columns (and their index) stay on purpose, so the labels survive a rollback
    # and a later upgrade does not relabel everything as agriculture (audit R1-32); older code ignores them.
    for table in _PAGE_CHILDREN:
        op.execute(f"DROP POLICY IF EXISTS industry_isolation_{table} ON {table}")
    op.execute("DROP POLICY IF EXISTS industry_isolation_brain_pages ON brain_pages")
