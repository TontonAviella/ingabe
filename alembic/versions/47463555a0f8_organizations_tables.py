"""organizations and user_organizations tables

Partner onboarding infrastructure: links Clerk Organizations to internal
UUIDs, maps users to orgs with roles.

Revision ID: 47463555a0f8
Revises: b1c2d3e4f5a6
Create Date: 2026-04-19 12:00:00.000000

"""
from typing import Sequence, Union

from alembic import op

revision: str = "47463555a0f8"
down_revision: str = "b1c2d3e4f5a6"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute("""
        CREATE TABLE organizations (
            id           UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            name         TEXT NOT NULL,
            slug         TEXT NOT NULL UNIQUE,
            tier         TEXT NOT NULL DEFAULT 'partner'
                         CHECK (tier IN ('partner', 'internal', 'trial')),
            clerk_org_id TEXT UNIQUE,
            metadata     JSONB NOT NULL DEFAULT '{}',
            created_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
            updated_at   TIMESTAMPTZ NOT NULL DEFAULT now()
        )
    """)

    op.execute("""
        CREATE TABLE user_organizations (
            user_id    TEXT NOT NULL REFERENCES users(internal_uuid) ON DELETE CASCADE,
            org_id     UUID NOT NULL REFERENCES organizations(id) ON DELETE CASCADE,
            role       TEXT NOT NULL DEFAULT 'member'
                       CHECK (role IN ('owner', 'admin', 'member')),
            created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            PRIMARY KEY (user_id, org_id)
        )
    """)

    op.execute(
        "CREATE INDEX idx_user_organizations_org ON user_organizations(org_id)"
    )
    op.execute(
        "CREATE INDEX idx_organizations_clerk ON organizations(clerk_org_id) "
        "WHERE clerk_org_id IS NOT NULL"
    )


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS user_organizations")
    op.execute("DROP TABLE IF EXISTS organizations")
