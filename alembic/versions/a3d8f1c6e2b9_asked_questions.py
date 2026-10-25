"""Questions people ask Sage about their photos, and the questions learned from them (2026-10-26)

Revision ID: a3d8f1c6e2b9
Revises: f2b7d1e5c9a3
Create Date: 2026-10-26

People can ask Sage their own question on a photo. asked_questions keeps each one (who, company, industry, role,
photo layer and the words, dropped after 90 days) and how Sage's model filed it: the same as a card, the same as a
learned question, a new learned question, or skipped. common_questions holds the learned questions, in general words,
per industry; one asked by 3 or more people is offered to everyone in that industry. src/services/asked_questions.py
owns the rules. Owned by the app user so the nightly backup includes them.
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "a3d8f1c6e2b9"
down_revision: Union[str, None] = "f2b7d1e5c9a3"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_INDUSTRIES = "industry IN ('agriculture', 'telecom', 'power_grid')"


def upgrade() -> None:
    op.create_table(
        "common_questions",
        sa.Column("id", sa.BigInteger, primary_key=True, autoincrement=True),
        sa.Column("industry", sa.String(20), nullable=False),
        sa.Column("question", sa.Text, nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("NOW()"), nullable=False),
        sa.CheckConstraint(_INDUSTRIES, name="ck_common_questions_industry"),
    )
    op.execute("CREATE UNIQUE INDEX uq_common_questions_industry_question ON common_questions (industry, lower(question))")
    op.create_table(
        "asked_questions",
        sa.Column("id", sa.BigInteger, primary_key=True, autoincrement=True),
        sa.Column("asked_at", sa.DateTime(timezone=True), server_default=sa.text("NOW()"), nullable=False),
        sa.Column("user_id", sa.String, nullable=False),
        sa.Column("org_id", sa.String),
        sa.Column("industry", sa.String(20), nullable=False),
        sa.Column("role", sa.String(20)),
        sa.Column("layer_id", sa.String(12), nullable=False),
        sa.Column("question", sa.Text),  # the person's own words; NULL after 90 days
        sa.Column("status", sa.String(10), server_default="pending", nullable=False),
        sa.Column("card_id", sa.String(40)),
        sa.Column("common_id", sa.BigInteger, sa.ForeignKey("common_questions.id", ondelete="SET NULL")),
        sa.CheckConstraint(_INDUSTRIES, name="ck_asked_questions_industry"),
        sa.CheckConstraint("status IN ('pending', 'card', 'learned', 'new', 'skipped', 'failed')",
                           name="ck_asked_questions_status"),
    )
    op.create_index("ix_asked_questions_common_id", "asked_questions", ["common_id"])
    op.create_index("ix_asked_questions_asked_at", "asked_questions", ["asked_at"])


def downgrade() -> None:
    op.drop_table("asked_questions")
    op.drop_table("common_questions")
