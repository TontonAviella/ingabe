"""A project belongs to one organization (audit 2026-10-09, round 2)

Revision ID: c3e8a1d5f7b2
Revises: b7d2e5a9c3f1
Create Date: 2026-10-09

Sage's turns took their partner (app.partner_id, which decides the Brain's partner-only notes) from whichever
organization the user had active, and a project had no organization. A user in two partners therefore carried
partner B's notes into partner A's project, where the stored memory packet outlived the switch. Projects now record
the organization they act for (src/services/project_partner.py). Existing projects are bound to their owner's
organization when the owner has exactly one; the rest are bound on their next Sage turn.
"""

from typing import Sequence, Union

from alembic import op

revision: str = "c3e8a1d5f7b2"
down_revision: Union[str, None] = "b7d2e5a9c3f1"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute("ALTER TABLE user_mundiai_projects ADD COLUMN IF NOT EXISTS partner_id uuid "
               "REFERENCES organizations(id) ON DELETE SET NULL")
    op.execute(
        """
        UPDATE user_mundiai_projects p SET partner_id = m.org_id
        FROM (SELECT user_id, min(org_id::text)::uuid AS org_id FROM user_organizations
              GROUP BY user_id HAVING count(*) = 1) m
        WHERE m.user_id = p.owner_uuid::text AND p.partner_id IS NULL
        """
    )


def downgrade() -> None:
    op.execute("ALTER TABLE user_mundiai_projects DROP COLUMN IF EXISTS partner_id")
