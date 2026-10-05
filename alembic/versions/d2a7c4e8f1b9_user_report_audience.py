"""users.report_audience: which view of reports a user gets (farmer, insurance, agronomist, scientist)

Revision ID: d2a7c4e8f1b9
Revises: e4c9b2d7a1f3
Create Date: 2026-10-04

NULL = no choice: the partner's organizations.metadata.default_audience
applies, then insurance_engine.DEFAULT_AUDIENCE.
"""

from typing import Sequence, Union

from alembic import op

revision: str = "d2a7c4e8f1b9"
down_revision: Union[str, None] = "e4c9b2d7a1f3"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute(
        "ALTER TABLE users ADD COLUMN IF NOT EXISTS report_audience text "
        "CHECK (report_audience IN ('farmer', 'insurance', 'agronomist', 'scientist'))"
    )


def downgrade() -> None:
    op.execute("ALTER TABLE users DROP COLUMN IF EXISTS report_audience")
