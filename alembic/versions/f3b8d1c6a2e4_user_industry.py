"""users.industry: the industry a user works in (agriculture, power grid, telecom towers), asked once at sign-in

Revision ID: f3b8d1c6a2e4
Revises: d1e7a3c9f5b2
Create Date: 2026-10-09

NULL = not chosen yet: the app asks before anything else.
"""

from typing import Sequence, Union

from alembic import op

revision: str = "f3b8d1c6a2e4"
down_revision: Union[str, None] = "d1e7a3c9f5b2"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute(
        "ALTER TABLE users ADD COLUMN IF NOT EXISTS industry text "
        "CHECK (industry IN ('agriculture', 'power_grid', 'telecom'))"
    )


def downgrade() -> None:
    op.execute("ALTER TABLE users DROP COLUMN IF EXISTS industry")
