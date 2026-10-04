"""WorkOS identity columns: users.workos_user_id, organizations.workos_org_id

Revision ID: e4c9b2d7a1f3
Revises: b8d4e0f2a3c5
Create Date: 2026-10-04

Sign-in moves from Clerk to WorkOS. Existing users keep their internal_uuid
(and so their projects): the first WorkOS sign-in links the row by email.
clerk_id becomes optional; it is kept for rollback.
"""

from typing import Sequence, Union

from alembic import op

revision: str = "e4c9b2d7a1f3"
down_revision: Union[str, None] = "b8d4e0f2a3c5"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute("ALTER TABLE users ADD COLUMN IF NOT EXISTS workos_user_id text")
    op.execute("CREATE UNIQUE INDEX IF NOT EXISTS ix_users_workos_user_id ON users (workos_user_id)")
    op.execute("ALTER TABLE users ALTER COLUMN clerk_id DROP NOT NULL")
    op.execute("ALTER TABLE organizations ADD COLUMN IF NOT EXISTS workos_org_id text")
    op.execute("CREATE UNIQUE INDEX IF NOT EXISTS ix_organizations_workos_org_id ON organizations (workos_org_id)")


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS ix_organizations_workos_org_id")
    op.execute("ALTER TABLE organizations DROP COLUMN IF EXISTS workos_org_id")
    op.execute("DROP INDEX IF EXISTS ix_users_workos_user_id")
    op.execute("ALTER TABLE users DROP COLUMN IF EXISTS workos_user_id")
    # clerk_id stays nullable: WorkOS-only rows would violate NOT NULL.
