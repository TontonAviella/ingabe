"""A reader's role per industry: users.report_audience (Agriculture) and users.asset_role (Towers, Power Grid)
(Roger, 2026-10-25)

Revision ID: f2b7d1e5c9a3
Revises: e6a1c4d9b2f7
Create Date: 2026-10-25

The questions on drone and inspection photos are now asked per role (src/services/reader_roles.py). Agriculture
gains cooperative / farm manager, government / district and buyer / processor next to farmer, agronomist, insurer
and scientist (users.report_audience, which reports also read); Telecom Towers and Power Grid have owner, operations
manager and field technician in a column of their own (users.asset_role), so picking a role in one industry never
changes the other.
"""

from typing import Sequence, Union

from alembic import op

revision: str = "f2b7d1e5c9a3"
down_revision: Union[str, None] = "e6a1c4d9b2f7"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_AGRICULTURE = ("farmer", "cooperative", "agronomist", "insurance", "scientist", "government", "buyer")
_OLD = ("farmer", "insurance", "agronomist", "scientist")


def _check(values: tuple[str, ...]) -> str:
    return ", ".join(f"'{v}'" for v in values)


def upgrade() -> None:
    op.execute("ALTER TABLE users DROP CONSTRAINT IF EXISTS users_report_audience_check")
    op.execute(f"ALTER TABLE users ADD CONSTRAINT users_report_audience_check "
               f"CHECK (report_audience IN ({_check(_AGRICULTURE)}))")
    op.execute("ALTER TABLE users ADD COLUMN IF NOT EXISTS asset_role text "
               "CHECK (asset_role IN ('owner', 'manager', 'technician'))")


def downgrade() -> None:
    op.execute("ALTER TABLE users DROP COLUMN IF EXISTS asset_role")
    # The new roles keep the report voice they read in (reader_roles.VOICE).
    op.execute("UPDATE users SET report_audience = CASE report_audience WHEN 'cooperative' THEN 'farmer' "
               "WHEN 'buyer' THEN 'farmer' WHEN 'government' THEN 'insurance' ELSE report_audience END")
    op.execute("ALTER TABLE users DROP CONSTRAINT IF EXISTS users_report_audience_check")
    op.execute(f"ALTER TABLE users ADD CONSTRAINT users_report_audience_check "
               f"CHECK (report_audience IN ({_check(_OLD)}))")
