"""add basemap column to user_mundiai_maps

Revision ID: 486828ee76a2
Revises: 1e7729123b46
Create Date: 2025-08-27 01:53:49.075003

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '486828ee76a2'
down_revision: Union[str, None] = '1e7729123b46'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column('user_mundiai_maps', sa.Column('basemap', sa.String(length=50), nullable=True))


def downgrade() -> None:
    op.drop_column('user_mundiai_maps', 'basemap')