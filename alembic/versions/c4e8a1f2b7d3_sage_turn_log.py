"""Sage turn log: what Sage did on each turn, kept 90 days, for the Ingabe Health page (2026-10-25)

Revision ID: c4e8a1f2b7d3
Revises: b8d3f1a6c2e4
Create Date: 2026-10-25

The flight recorder (src/services/sage_flight_recorder.py) traced turns only to Langfuse, which is not running, so
nothing said which tool failed on which turn, how long each step took, or which of Sage's rules stepped in. One row
per turn now holds that (src/services/sage_turn_log.py): the steps in order (model calls and tool calls, with time,
status and the error), the rules that fired, and the outcome. No message text. Owned by the app user so the nightly
backup includes it.
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "c4e8a1f2b7d3"
down_revision: Union[str, None] = "b8d3f1a6c2e4"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "sage_turn_log",
        sa.Column("id", sa.BigInteger, primary_key=True, autoincrement=True),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("seconds", sa.Float, nullable=False),
        sa.Column("outcome", sa.String(16), nullable=False),  # completed | failed | cancelled
        sa.Column("map_id", sa.String),
        sa.Column("conversation_id", sa.String),
        sa.Column("partner_id", sa.String),
        sa.Column("rules", postgresql.ARRAY(sa.String), nullable=False, server_default="{}"),
        sa.Column("record", postgresql.JSONB, nullable=False),
    )
    op.create_index("ix_sage_turn_log_started_at", "sage_turn_log", ["started_at"])


def downgrade() -> None:
    op.drop_index("ix_sage_turn_log_started_at", table_name="sage_turn_log")
    op.drop_table("sage_turn_log")
