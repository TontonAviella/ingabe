"""Ratings of Sage's answers: a person's 👍 or 👎 on one answer, with what was wrong (2026-10-25)

Revision ID: d5f9b2c3e8a4
Revises: c4e8a1f2b7d3
Create Date: 2026-10-25

People had no way to say an answer was wrong, so nothing measured whether Sage's answers were right. One row per
person and answer (rating again changes it): up or down, and an optional note. src/services/sage_answer_ratings.py
saves and reads them; the Ingabe Health page lists the 👎 answers next to that turn's steps (sage_turn_log).
Owned by the app user so the nightly backup includes it.
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "d5f9b2c3e8a4"
down_revision: Union[str, None] = "c4e8a1f2b7d3"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "sage_answer_ratings",
        sa.Column("id", sa.BigInteger, primary_key=True, autoincrement=True),
        sa.Column("message_id", sa.Integer,
                  sa.ForeignKey("chat_completion_messages.id", ondelete="CASCADE"), nullable=False),
        sa.Column("conversation_id", sa.Integer),
        sa.Column("map_id", sa.String(12), nullable=False),
        sa.Column("user_id", sa.String, nullable=False),
        sa.Column("rating", sa.String(4), nullable=False),
        sa.Column("comment", sa.Text),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("NOW()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("NOW()"), nullable=False),
        sa.CheckConstraint("rating IN ('up', 'down')", name="ck_sage_answer_ratings_rating"),
        sa.UniqueConstraint("message_id", "user_id", name="uq_sage_answer_ratings_message_user"),
    )
    op.create_index("ix_sage_answer_ratings_updated_at", "sage_answer_ratings", ["updated_at"])


def downgrade() -> None:
    op.drop_index("ix_sage_answer_ratings_updated_at", table_name="sage_answer_ratings")
    op.drop_table("sage_answer_ratings")
