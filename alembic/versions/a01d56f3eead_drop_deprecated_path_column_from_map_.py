"""drop deprecated path column from map_layers

Revision ID: a01d56f3eead
Revises: 920b5ccbca48
Create Date: 2025-07-31 01:57:47.752733

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'a01d56f3eead'
down_revision: Union[str, None] = '920b5ccbca48'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Drop the deprecated path column from map_layers table
    op.drop_column('map_layers', 'path')


def downgrade() -> None:
    # Re-add the path column if we need to rollback
    op.add_column('map_layers', sa.Column('path', sa.String(), nullable=False, server_default=''))