"""revoke SUPERUSER + BYPASSRLS on mundiuser

mundiuser currently has rolsuper + BYPASSRLS in production, which means
every RLS policy (tenant_isolation, partner_isolation) is a no-op. This
migration revokes both privileges so RLS actually enforces data isolation.

After this migration:
  - mundiuser still owns all existing objects (full CRUD on own tables)
  - RLS policies are enforced for mundiuser sessions
  - DDL operations (CREATE TABLE, ALTER TABLE, CREATE INDEX) still work
    because mundiuser owns the schema objects

Future alembic migrations that create new tables must either:
  a) Run under the postgres superuser (separate DSN in alembic.ini), or
  b) GRANT CREATE on schema public to mundiuser (already true if owner)

Rollback procedure (run as postgres superuser):
  ALTER ROLE mundiuser SUPERUSER BYPASSRLS;

Revision ID: a0b1c2d3e4f5
Revises: f6a7b8c9d0e1
Create Date: 2026-04-18 20:30:00.000000

"""
from typing import Sequence, Union

from alembic import op

revision: str = "a0b1c2d3e4f5"
down_revision: str = "f6a7b8c9d0e1"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute("ALTER ROLE mundiuser NOSUPERUSER NOBYPASSRLS")

    op.execute(
        "GRANT ALL ON ALL TABLES IN SCHEMA public TO mundiuser"
    )
    op.execute(
        "GRANT ALL ON ALL SEQUENCES IN SCHEMA public TO mundiuser"
    )
    op.execute(
        "GRANT EXECUTE ON ALL FUNCTIONS IN SCHEMA public TO mundiuser"
    )
    op.execute(
        "ALTER DEFAULT PRIVILEGES IN SCHEMA public "
        "GRANT ALL ON TABLES TO mundiuser"
    )
    op.execute(
        "ALTER DEFAULT PRIVILEGES IN SCHEMA public "
        "GRANT ALL ON SEQUENCES TO mundiuser"
    )


def downgrade() -> None:
    op.execute("ALTER ROLE mundiuser SUPERUSER BYPASSRLS")
