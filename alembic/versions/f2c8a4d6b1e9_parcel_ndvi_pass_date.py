"""Field NDVI rows say which Sentinel-2 pass they come from (2026-11-24)

Revision ID: f2c8a4d6b1e9
Revises: e7b3c5d9a2f4
Create Date: 2026-11-24

ndvi_parcel_cache now holds the NDVI of each field a person outlined on a drone photo, from the clearest
recent pass (src/services/field_ndvi.py). A figure from a pass weeks old must say so, so each row keeps the
pass's day (`pass_date`) and the share of the field that pass saw clear (`clear_share`). Rows from before have
neither: the table was empty everywhere we know of (the nightly job never found a field).
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "f2c8a4d6b1e9"
down_revision: Union[str, None] = "e7b3c5d9a2f4"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("ndvi_parcel_cache", sa.Column("pass_date", sa.Date, nullable=True))
    op.add_column("ndvi_parcel_cache", sa.Column("clear_share", sa.Float, nullable=True))
    op.create_index("ix_ndvi_parcel_cache_parcel_pass", "ndvi_parcel_cache", ["parcel_id", "pass_date"])


def downgrade() -> None:
    op.drop_index("ix_ndvi_parcel_cache_parcel_pass", table_name="ndvi_parcel_cache")
    op.drop_column("ndvi_parcel_cache", "clear_share")
    op.drop_column("ndvi_parcel_cache", "pass_date")
