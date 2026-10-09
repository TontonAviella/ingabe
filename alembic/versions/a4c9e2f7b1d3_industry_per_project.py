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
- Restrictive row-level security: with app.industry set, a connection sees and writes only notes of that
  industry or general ones, and only the chunks, facts, timeline entries, tags, references, versions and links
  of notes it can see. With app.industry unset (maintenance, background jobs) nothing changes. Restrictive
  policies are ANDed with the existing partner and tenant policies, so they can only narrow access.
"""

from typing import Sequence, Union

from alembic import op

revision: str = "a4c9e2f7b1d3"
down_revision: Union[str, None] = "f3b8d1c6a2e4"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_INDUSTRIES = "('agriculture', 'power_grid', 'telecom')"
_UNSET = "NULLIF(current_setting('app.industry', true), '') IS NULL"
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
    op.execute(
        "ALTER TABLE brain_pages ADD COLUMN IF NOT EXISTS industry text "
        f"CHECK (industry IN {_INDUSTRIES})"
    )
    op.execute("UPDATE brain_pages SET industry = 'agriculture' WHERE industry IS NULL")
    op.execute("ALTER TABLE brain_pages ALTER COLUMN industry SET DEFAULT NULLIF(current_setting('app.industry', true), '')")
    op.execute("CREATE INDEX IF NOT EXISTS brain_pages_industry_idx ON brain_pages (industry)")
    page_rule = f"{_UNSET} OR industry IS NULL OR industry = current_setting('app.industry', true)"
    op.execute(
        "CREATE POLICY industry_isolation_brain_pages ON brain_pages AS RESTRICTIVE FOR ALL "
        f"USING ({page_rule}) WITH CHECK ({page_rule})"
    )
    for table, column in _PAGE_CHILDREN.items():
        # A row with no note (a free-standing fact) is general knowledge, like a note with no industry.
        rule = f"{_UNSET} OR {column} IS NULL OR {column} IN (SELECT id FROM brain_pages)"
        op.execute(
            f"CREATE POLICY industry_isolation_{table} ON {table} AS RESTRICTIVE FOR ALL "
            f"USING ({rule}) WITH CHECK ({rule})"
        )


def downgrade() -> None:
    for table in _PAGE_CHILDREN:
        op.execute(f"DROP POLICY IF EXISTS industry_isolation_{table} ON {table}")
    op.execute("DROP POLICY IF EXISTS industry_isolation_brain_pages ON brain_pages")
    op.execute("DROP INDEX IF EXISTS brain_pages_industry_idx")
    op.execute("ALTER TABLE brain_pages DROP COLUMN IF EXISTS industry")
    op.execute("ALTER TABLE user_mundiai_projects DROP COLUMN IF EXISTS industry")
