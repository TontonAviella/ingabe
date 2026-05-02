"""ndvi_parcel_cache stale column for on-demand recompute

Adds a ``stale`` boolean and ``last_recomputed_at`` timestamp so that boundary
edits can mark a layer's cached NDVI as stale and trigger an on-demand
recompute via the new ``POST /api/parcels/{layer_id}/recompute`` endpoint.

Without this, edits to a parcel boundary only get a fresh NDVI at the next
nightly run (3 AM UTC), which is unacceptable when an insurance worker just
adjusted a field's polygon and needs to see the updated mean immediately.

Revision ID: b8c9d0e1f2a3
Revises: a1b2c3d4e5f7
Create Date: 2026-04-28 00:00:00.000000
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "b8c9d0e1f2a3"
down_revision: Union[str, None] = "a1b2c3d4e5f7"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "ndvi_parcel_cache",
        sa.Column(
            "stale", sa.Boolean, nullable=False, server_default=sa.text("false"),
        ),
    )
    op.add_column(
        "ndvi_parcel_cache",
        sa.Column("last_recomputed_at", sa.DateTime, nullable=True),
    )
    op.create_index(
        "ix_ndvi_parcel_cache_layer_stale",
        "ndvi_parcel_cache",
        ["layer_id", "stale"],
    )


def downgrade() -> None:
    op.drop_index("ix_ndvi_parcel_cache_layer_stale", table_name="ndvi_parcel_cache")
    op.drop_column("ndvi_parcel_cache", "last_recomputed_at")
    op.drop_column("ndvi_parcel_cache", "stale")
