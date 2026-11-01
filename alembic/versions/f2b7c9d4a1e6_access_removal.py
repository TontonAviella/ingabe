"""Removing access: sessions ended here and people blocked from Ingabe, refused at once (2026-11-01)

Revision ID: f2b7c9d4a1e6
Revises: e5a9c3f1b7d2
Create Date: 2026-11-01

Roger (2026-11-01): after approving someone he needs a way to cut them off, from his phone. The app checks a
session's access token itself and asks WorkOS only when it expires, so a person removed at WorkOS kept working until
then. ended_sessions lists sessions ended here; blocked_people lists people refused whatever their company.
src/services/access_removal.py owns the rules. Owned by the app user so the nightly backup includes them.
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "f2b7c9d4a1e6"
down_revision: Union[str, None] = "e5a9c3f1b7d2"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "ended_sessions",
        sa.Column("session_id", sa.Text, primary_key=True),  # WorkOS session id (the access token's sid)
        sa.Column("workos_user_id", sa.Text, nullable=False),
        sa.Column("ended_at", sa.DateTime(timezone=True), server_default=sa.text("NOW()"), nullable=False),
        sa.Column("ended_by", sa.Text, nullable=False),
        sa.Column("why", sa.Text, nullable=False),
    )
    op.create_table(
        "blocked_people",
        sa.Column("workos_user_id", sa.Text, primary_key=True),
        sa.Column("email", sa.Text),
        sa.Column("blocked_at", sa.DateTime(timezone=True), server_default=sa.text("NOW()"), nullable=False),
        sa.Column("blocked_by", sa.Text, nullable=False),
        sa.Column("reason", sa.Text),
    )


def downgrade() -> None:
    op.drop_table("blocked_people")
    op.drop_table("ended_sessions")
