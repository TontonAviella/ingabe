"""add title column to user_mundiai_projects table

Revision ID: 158ce8e20754
Revises: a01d56f3eead
Create Date: 2025-08-02 21:49:09.440712

"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = "158ce8e20754"
down_revision: Union[str, None] = "a01d56f3eead"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "user_mundiai_projects",
        sa.Column("title", sa.String(), nullable=True, server_default="Untitled Map"),
    )
    op.execute(
        "UPDATE user_mundiai_projects SET title = 'Untitled Map' WHERE title IS NULL"
    )


def downgrade() -> None:
    op.drop_column("user_mundiai_projects", "title")
