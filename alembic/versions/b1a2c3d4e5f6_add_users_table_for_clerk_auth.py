"""add users table for Clerk auth

Revision ID: b1a2c3d4e5f6
Revises: fad2e5b46554
Create Date: 2026-02-16 00:00:00.000000

"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision: str = "b1a2c3d4e5f6"
down_revision: Union[str, None] = "486828ee76a2"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "users",
        sa.Column("internal_uuid", sa.String(36), primary_key=True),
        sa.Column("clerk_id", sa.String(255), unique=True, nullable=False, index=True),
        sa.Column("email", sa.String(255), nullable=True),
        sa.Column(
            "created_at",
            sa.TIMESTAMP(timezone=True),
            server_default=sa.func.current_timestamp(),
        ),
    )


def downgrade() -> None:
    op.drop_table("users")
