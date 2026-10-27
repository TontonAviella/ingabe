"""Remove the Power Grid industry (Roger, 2026-10-27: not worth the energy; Ingabe keeps Agriculture and Telecom).

Revision ID: d8e1f4a7c2b6
Revises: c5e8a2d7f4b1
Create Date: 2026-10-27

- Projects, users and companies set to power_grid move to agriculture, the default industry. On the server on
  2026-10-27 that was two empty projects ("New Map", no layers) of Roger's own accounts, and no user or company.
- Questions learned or asked in Power Grid (common_questions, asked_questions) are about pylons and lines and mean
  nothing elsewhere: they are deleted (none existed).
- A Brain note of the Power Grid industry is visible only to Power Grid; moving it would show it to another industry,
  and deleting it would lose someone's notes. So the migration stops if any exists (none did) for a person to decide.
- Every industry check then allows only agriculture and telecom. Downgrade allows power_grid again (the rows moved
  stay where they are).
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "d8e1f4a7c2b6"
down_revision: Union[str, Sequence[str], None] = "c5e8a2d7f4b1"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

# (table, constraint, column cast): the checks as the earlier migrations made them.
_CHECKS = (
    ("users", "users_industry_check", "industry"),
    ("user_mundiai_projects", "user_mundiai_projects_industry_check", "industry"),
    ("brain_pages", "brain_pages_industry_check", "industry"),
    ("organizations", "organizations_industry_check", "industry"),
    ("common_questions", "ck_common_questions_industry", "industry"),
    ("asked_questions", "ck_asked_questions_industry", "industry"),
)


def _recheck(industries: str) -> None:
    for table, name, column in _CHECKS:
        op.execute(f"ALTER TABLE {table} DROP CONSTRAINT IF EXISTS {name}")
        op.execute(f"ALTER TABLE {table} ADD CONSTRAINT {name} CHECK ({column} IN ({industries}))")


def upgrade() -> None:
    conn = op.get_bind()
    notes = conn.execute(sa.text("SELECT count(*) FROM brain_pages WHERE industry = 'power_grid'")).scalar()
    if notes:
        raise RuntimeError(f"{notes} Brain notes belong to Power Grid: decide where they go before removing it")
    for table in ("user_mundiai_projects", "users", "organizations"):
        op.execute(f"UPDATE {table} SET industry = 'agriculture' WHERE industry = 'power_grid'")
    for table in ("common_questions", "asked_questions"):
        op.execute(f"DELETE FROM {table} WHERE industry = 'power_grid'")
    _recheck("'agriculture', 'telecom'")


def downgrade() -> None:
    _recheck("'agriculture', 'power_grid', 'telecom'")
