"""convert_chat_completion_message_json_to_jsonb

Revision ID: 07c7ae795a24
Revises: 71c52d9a8344
Create Date: 2025-07-11 20:17:32.506979

"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


# revision identifiers, used by Alembic.
revision: str = "07c7ae795a24"
down_revision: Union[str, None] = "71c52d9a8344"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Convert message_json from TEXT to JSONB
    # First, add a new temporary column of type JSONB
    op.add_column(
        "chat_completion_messages",
        sa.Column(
            "message_json_temp", postgresql.JSONB(astext_type=sa.Text()), nullable=True
        ),
    )

    # Copy data from TEXT column to JSONB column, parsing JSON
    op.execute("""
        UPDATE chat_completion_messages
        SET message_json_temp = message_json::jsonb
        WHERE message_json IS NOT NULL
    """)

    # Drop the old TEXT column
    op.drop_column("chat_completion_messages", "message_json")

    # Rename the temporary column to the original name
    op.alter_column(
        "chat_completion_messages", "message_json_temp", new_column_name="message_json"
    )

    # Make the column NOT NULL
    op.alter_column("chat_completion_messages", "message_json", nullable=False)


def downgrade() -> None:
    # Convert message_json from JSONB back to TEXT
    # Add temporary TEXT column
    op.add_column(
        "chat_completion_messages",
        sa.Column("message_json_temp", sa.Text(), nullable=True),
    )

    # Copy data from JSONB column to TEXT column, converting to JSON string
    op.execute("""
        UPDATE chat_completion_messages
        SET message_json_temp = message_json::text
        WHERE message_json IS NOT NULL
    """)

    # Drop the JSONB column
    op.drop_column("chat_completion_messages", "message_json")

    # Rename the temporary column to the original name
    op.alter_column(
        "chat_completion_messages", "message_json_temp", new_column_name="message_json"
    )

    # Make the column NOT NULL
    op.alter_column("chat_completion_messages", "message_json", nullable=False)
