"""users.report_audience holds any industry's role (Roger, 2026-10-25)

Revision ID: f2b7d1e5c9a3
Revises: e6a1c4d9b2f7
Create Date: 2026-10-25

The questions on drone and inspection photos are now asked per role (src/services/reader_roles.py). Agriculture
gains cooperative / farm manager, government / district and buyer / processor next to farmer, agronomist, insurer
and scientist; Telecom Towers and Power Grid have owner, operations manager and field technician. The column keeps
its name; its check lists every role.
"""

from typing import Sequence, Union

from alembic import op

revision: str = "f2b7d1e5c9a3"
down_revision: Union[str, None] = "e6a1c4d9b2f7"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_ROLES = ("farmer", "cooperative", "agronomist", "insurance", "scientist", "government", "buyer",
          "owner", "manager", "technician")
_OLD = ("farmer", "insurance", "agronomist", "scientist")


def _check(values: tuple[str, ...]) -> str:
    return ", ".join(f"'{v}'" for v in values)


def upgrade() -> None:
    op.execute("ALTER TABLE users DROP CONSTRAINT IF EXISTS users_report_audience_check")
    op.execute(f"ALTER TABLE users ADD CONSTRAINT users_report_audience_check "
               f"CHECK (report_audience IN ({_check(_ROLES)}))")


def downgrade() -> None:
    op.execute(f"UPDATE users SET report_audience = NULL WHERE report_audience NOT IN ({_check(_OLD)})")
    op.execute("ALTER TABLE users DROP CONSTRAINT IF EXISTS users_report_audience_check")
    op.execute(f"ALTER TABLE users ADD CONSTRAINT users_report_audience_check "
               f"CHECK (report_audience IN ({_check(_OLD)}))")
