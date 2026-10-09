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
_PAGE_CHILDREN = {
    "brain_content_chunks": "page_id",
    "brain_facts": "page_id",
    "brain_timeline_entries": "page_id",
    "brain_tags": "page_id",
    "brain_entity_refs": "page_id",
    "brain_page_versions": "page_id",
    "brain_links": "from_page_id",
}


def upgrade() -> None:
    op.execute(
        "ALTER TABLE user_mundiai_projects ADD COLUMN IF NOT EXISTS industry text NOT NULL DEFAULT 'agriculture' "
        f"CHECK (industry IN {_INDUSTRIES})"
    )
    # Since #166 went live (merged 2026-10-09 01:08:46 UTC) people choose an industry at sign-in; a project or note
    # they made after that belongs to their chosen industry, not to agriculture (audit R1-14). Order matters: the
    # agriculture backfill below only fills what is still NULL.
    op.execute(
        "UPDATE user_mundiai_projects p SET industry = u.industry FROM users u "
        "WHERE u.internal_uuid = p.owner_uuid::text AND u.industry IN ('power_grid', 'telecom') "
        f"AND p.created_on >= TIMESTAMPTZ '{_INDUSTRY_CHOICE_LIVE}'"
    )
    op.execute(
        "ALTER TABLE brain_pages ADD COLUMN IF NOT EXISTS industry text "
        f"CHECK (industry IN {_INDUSTRIES})"
    )
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
    op.execute(
        "CREATE POLICY industry_isolation_brain_pages ON brain_pages AS RESTRICTIVE FOR ALL "
        f"USING ({page_read}) WITH CHECK ({page_write})"
    )
    for table, column in _PAGE_CHILDREN.items():
        # Readable when the note is; writable only on a note of exactly the writer's scope.
        child_read = f"{_WORKER} OR {column} IS NULL OR {column} IN (SELECT id FROM brain_pages)"
        child_write = (f"{_WORKER} OR {column} IN "
                       f"(SELECT id FROM brain_pages WHERE industry IS NOT DISTINCT FROM {_SCOPE})")
        op.execute(
            f"CREATE POLICY industry_isolation_{table} ON {table} AS RESTRICTIVE FOR ALL "
            f"USING ({child_read}) WITH CHECK ({child_write})"
        )


def downgrade() -> None:
    for table in _PAGE_CHILDREN:
        op.execute(f"DROP POLICY IF EXISTS industry_isolation_{table} ON {table}")
    op.execute("DROP POLICY IF EXISTS industry_isolation_brain_pages ON brain_pages")
    op.execute("DROP INDEX IF EXISTS brain_pages_industry_idx")
    op.execute("ALTER TABLE brain_pages DROP COLUMN IF EXISTS industry")
    op.execute("ALTER TABLE user_mundiai_projects DROP COLUMN IF EXISTS industry")
