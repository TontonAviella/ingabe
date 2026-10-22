"""ndvi_cell_cache rows name their cell by cell_id (2026-10-22)

Revision ID: f3c8a2d6b1e9
Revises: e7a1c4d9b2f3
Create Date: 2026-10-22

Readers joined the cache to rwanda_cell_boundaries on cell_name + district_name, but 2,148 cells have only 2,061
distinct (cell, district) pairs (Gicumbi has three cells named Kabuga), so one cell's NDVI was shown for all of its
namesakes. The weekly cell NDVI job now writes cell_id and the readers join on it. The table was empty until that job
existed, so there are no rows to backfill.
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "f3c8a2d6b1e9"
down_revision: Union[str, None] = "e7a1c4d9b2f3"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("ndvi_cell_cache", sa.Column("cell_id", sa.Integer, nullable=True))
    op.create_index("ix_ndvi_cell_cache_cell_id_computed_at", "ndvi_cell_cache", ["cell_id", "computed_at"])


def downgrade() -> None:
    op.drop_index("ix_ndvi_cell_cache_cell_id_computed_at", table_name="ndvi_cell_cache")
    op.drop_column("ndvi_cell_cache", "cell_id")
