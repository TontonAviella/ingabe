"""add chat message notification trigger

Revision ID: 395e7734f751
Revises: 932975d39fb8
Create Date: 2025-06-20 05:03:40.532965

"""

from typing import Sequence, Union

from alembic import op


# revision identifiers, used by Alembic.
revision: str = "395e7734f751"
down_revision: Union[str, None] = "932975d39fb8"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Create the notification function
    op.execute("""
        CREATE OR REPLACE FUNCTION notify_chat_message() RETURNS trigger AS $$
        DECLARE
          payload JSON;
        BEGIN
          payload := json_build_object('id', NEW.id, 'map_id', NEW.map_id);
          PERFORM pg_notify('chat_completion_messages_notify', payload::text);
          RETURN NEW;
        END;
        $$ LANGUAGE plpgsql;
    """)

    # Create the trigger
    op.execute("""
        CREATE TRIGGER chat_message_notify
          AFTER INSERT ON chat_completion_messages
          FOR EACH ROW EXECUTE PROCEDURE notify_chat_message();
    """)


def downgrade() -> None:
    # Drop the trigger
    op.execute(
        "DROP TRIGGER IF EXISTS chat_message_notify ON chat_completion_messages;"
    )

    # Drop the function
    op.execute("DROP FUNCTION IF EXISTS notify_chat_message();")
