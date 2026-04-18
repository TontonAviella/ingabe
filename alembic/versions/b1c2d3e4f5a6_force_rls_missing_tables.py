"""force row level security on tables missing it

Five tables had ENABLE ROW LEVEL SECURITY but not FORCE ROW LEVEL
SECURITY. Without FORCE, the table owner (mundiuser) bypasses RLS
even after losing SUPERUSER/BYPASSRLS. This matters after
a0b1c2d3e4f5 revoked mundiuser's SUPERUSER: the application connects
as mundiuser (the owner), so RLS policies were silently skipped on
these tables.

Revision ID: b1c2d3e4f5a6
Revises: a0b1c2d3e4f5
Create Date: 2026-04-19 00:05:00.000000

"""
from typing import Sequence, Union

from alembic import op

revision: str = "b1c2d3e4f5a6"
down_revision: str = "a0b1c2d3e4f5"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_TABLES = [
    "brain_entities",
    "brain_entity_refs",
    "brain_sources",
    "brain_tables",
    "sage_query_log",
]


def upgrade() -> None:
    for table in _TABLES:
        op.execute(f"ALTER TABLE {table} FORCE ROW LEVEL SECURITY")


def downgrade() -> None:
    for table in _TABLES:
        op.execute(f"ALTER TABLE {table} NO FORCE ROW LEVEL SECURITY")
