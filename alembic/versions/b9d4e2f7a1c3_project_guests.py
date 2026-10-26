"""Guests: one project shared with someone outside the owner's organization, for a limited time (2026-10-26)

Revision ID: b9d4e2f7a1c3
Revises: a3d8f1c6e2b9
Create Date: 2026-10-26

Roger (2026-10-26): share a project with anyone by email, "this project only", and the access "has to have some time
limit and it close". The owner picks 1, 7 or 30 days. A guest is invited by email; when that verified address signs
in, they are added to the project's editors or viewers (so row-level security lets them see that project and nothing
else); when the time runs out, or the owner removes them, they are taken off again. src/services/project_people.py
owns the rules. Owned by the app user so the nightly backup includes it.
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "b9d4e2f7a1c3"
down_revision: Union[str, None] = "a3d8f1c6e2b9"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "project_guests",
        sa.Column("id", sa.BigInteger, primary_key=True, autoincrement=True),
        sa.Column("project_id", sa.String(12),
                  sa.ForeignKey("user_mundiai_projects.id", ondelete="CASCADE"), nullable=False),
        sa.Column("email", sa.Text, nullable=False),  # lower case
        sa.Column("role", sa.String(10), nullable=False),
        sa.Column("invited_by", sa.Text, nullable=False),
        sa.Column("invited_at", sa.DateTime(timezone=True), server_default=sa.text("NOW()"), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("user_id", sa.Text),  # set when the invited address signs in
        sa.Column("accepted_at", sa.DateTime(timezone=True)),
        sa.Column("ended_at", sa.DateTime(timezone=True)),
        sa.Column("ended_why", sa.String(20)),
        sa.Column("invitation_id", sa.Text),  # the sign-in provider's invitation, to revoke it
        sa.CheckConstraint("role IN ('editor', 'viewer')", name="ck_project_guests_role"),
        sa.CheckConstraint("ended_why IS NULL OR ended_why IN ('expired', 'removed', 'colleague')",
                           name="ck_project_guests_ended_why"),
    )
    # One open invitation per project and address.
    op.execute("CREATE UNIQUE INDEX uq_project_guests_open ON project_guests (project_id, email) WHERE ended_at IS NULL")
    op.execute("CREATE INDEX ix_project_guests_due ON project_guests (expires_at) WHERE ended_at IS NULL")


def downgrade() -> None:
    op.drop_table("project_guests")
