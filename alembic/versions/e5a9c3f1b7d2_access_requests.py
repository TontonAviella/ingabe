"""Access requests: people without an invitation ask to join, and Ingabe staff approve or decline (2026-11-01)

Revision ID: e5a9c3f1b7d2
Revises: d8e1f4a7c2b6
Create Date: 2026-11-01

Roger (2026-11-01): someone tried to sign up, never got a code (sign-up is invite-only, so WorkOS refused and sent
nothing) and nobody was told. A request now comes from the app's "Request access" form, or from WorkOS' log of a
refused sign-up or a waitlist entry; staff are pushed a phone notification and approve (an invitation into a company)
or decline on the Companies page. src/services/access_requests.py owns the rules. Owned by the app user so the
nightly backup includes it.
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "e5a9c3f1b7d2"
down_revision: Union[str, None] = "d8e1f4a7c2b6"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "access_requests",
        sa.Column("id", sa.BigInteger, primary_key=True, autoincrement=True),
        sa.Column("email", sa.Text, nullable=False),  # lower case
        sa.Column("name", sa.Text),
        sa.Column("organization", sa.Text),
        sa.Column("reason", sa.Text),
        sa.Column("source", sa.String(20), nullable=False),
        sa.Column("attempts", sa.Integer, server_default="1", nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("NOW()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("NOW()"), nullable=False),
        sa.Column("last_event_at", sa.DateTime(timezone=True)),  # newest WorkOS event folded in: the reader's cursor
        sa.Column("notified_at", sa.DateTime(timezone=True)),
        sa.Column("status", sa.String(10), server_default="open", nullable=False),
        sa.Column("decided_by", sa.Text),
        sa.Column("decided_at", sa.DateTime(timezone=True)),
        sa.Column("decided_how", sa.Text),
        sa.CheckConstraint("source IN ('form', 'sign_up_attempt', 'waitlist')", name="ck_access_requests_source"),
        sa.CheckConstraint("status IN ('open', 'approved', 'declined')", name="ck_access_requests_status"),
    )
    # One open request per address: a second ask (or attempt) updates it.
    op.execute("CREATE UNIQUE INDEX uq_access_requests_open ON access_requests (email) WHERE status = 'open'")
    op.execute("CREATE INDEX ix_access_requests_recent ON access_requests (updated_at DESC)")


def downgrade() -> None:
    op.drop_table("access_requests")
