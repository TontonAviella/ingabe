"""Access notes: why someone was let in or removed, visible to the person who wrote it (2026-11-19)

Revision ID: a7d3f9c2e5b8
Revises: f2b7c9d4a1e6
Create Date: 2026-11-19
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "a7d3f9c2e5b8"
down_revision: Union[str, None] = "f2b7c9d4a1e6"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "access_notes",
        sa.Column("id", sa.BigInteger, primary_key=True),
        sa.Column("workos_user_id", sa.Text, nullable=False),
        sa.Column("note", sa.Text, nullable=False),
        sa.Column("written_by", postgresql.UUID, nullable=False),
        sa.Column("reader_uuids", postgresql.ARRAY(postgresql.UUID), server_default="{}", nullable=False),
        sa.Column("written_at", sa.DateTime(timezone=True), server_default=sa.text("NOW()"), nullable=False),
    )
    op.execute("ALTER TABLE access_notes ENABLE ROW LEVEL SECURITY")
    op.execute("""
        CREATE POLICY access_notes_readers ON access_notes USING (
            written_by = current_setting('app.user_id', true)::uuid
            OR current_setting('app.user_id', true)::uuid = ANY(reader_uuids))""")


def downgrade() -> None:
    op.drop_table("access_notes")
