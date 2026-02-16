"""add source_map_id to map_layers

Revision ID: 71c52d9a8344
Revises: fad2e5b46554
Create Date: 2025-07-01 09:35:00.000000

"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision: str = "71c52d9a8344"
down_revision: Union[str, None] = "37ef2ae77928"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Add source_map_id column as optional string to map_layers table
    op.add_column("map_layers", sa.Column("source_map_id", sa.String(), nullable=True))


def downgrade() -> None:
    # Remove source_map_id column from map_layers table
    op.drop_column("map_layers", "source_map_id")
