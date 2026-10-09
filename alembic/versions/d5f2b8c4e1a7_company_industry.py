"""A company works in one industry (Roger's decision, 2026-10-09)

Revision ID: d5f2b8c4e1a7
Revises: c3e8a1d5f7b2
Create Date: 2026-10-09

Industry was a personal choice, so two people at the same power company could create projects in different
industries, and someone working for two companies had one industry for both. organizations.industry is now the
industry of every project created in that company (src/services/industry.py, industry_for_new_project); a person's
own choice is used only when they act for no company, or for a company that has not chosen yet. Existing companies
take the industry their members chose, when those who chose agree.
"""

from typing import Sequence, Union

from alembic import op

revision: str = "d5f2b8c4e1a7"
down_revision: Union[str, None] = "c3e8a1d5f7b2"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute("ALTER TABLE organizations ADD COLUMN IF NOT EXISTS industry text "
               "CHECK (industry IN ('agriculture', 'power_grid', 'telecom'))")
    op.execute(
        """
        UPDATE organizations o SET industry = m.industry
        FROM (SELECT uo.org_id, min(u.industry) AS industry
              FROM user_organizations uo JOIN users u ON u.internal_uuid = uo.user_id
              WHERE u.industry IN ('agriculture', 'power_grid', 'telecom')
              GROUP BY uo.org_id HAVING count(DISTINCT u.industry) = 1) m
        WHERE m.org_id = o.id AND o.industry IS NULL
        """
    )


def downgrade() -> None:
    op.execute("ALTER TABLE organizations DROP COLUMN IF EXISTS industry")
