"""drop unique project user connection constraint

Revision ID: 37ef2ae77928
Revises: fad2e5b46554
Create Date: 2025-06-27 22:37:58.777352

"""

from typing import Sequence, Union

from alembic import op


# revision identifiers, used by Alembic.
revision: str = "37ef2ae77928"
down_revision: Union[str, None] = "fad2e5b46554"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.drop_constraint(
        "unique_project_user_connection", "project_postgres_connections", type_="unique"
    )


def downgrade() -> None:
    op.create_unique_constraint(
        "unique_project_user_connection",
        "project_postgres_connections",
        ["project_id", "user_id", "connection_uri"],
    )
