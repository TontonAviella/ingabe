"""District ground temperature (MODIS 8-day land surface temperature) for the heat condition (2026-10-24)

Revision ID: a7c4e9b2d1f6
Revises: f3c8a2d6b1e9
Create Date: 2026-10-24

One row per 8-day composite, satellite (MOD = Terra, MYD = Aqua) and district: mean day and night land surface
temperature (K) over the district's clear 1 km pixels and the share that was clear. Filled weekly by the Dagster
asset weekly_land_surface_temperature (history since 2000 loaded once); read by
src/services/land_surface_temperature.py for the insurance report and Sage's drought answer.
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "a7c4e9b2d1f6"
down_revision: Union[str, None] = "f3c8a2d6b1e9"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "lst_district_8day",
        sa.Column("id", sa.Integer, primary_key=True, autoincrement=True),
        sa.Column("district", sa.String, nullable=False),
        sa.Column("composite_start", sa.Date, nullable=False),
        sa.Column("product", sa.String(3), nullable=False),
        sa.Column("lst_day_k", sa.Float),
        sa.Column("day_clear_share", sa.Float),
        sa.Column("lst_night_k", sa.Float),
        sa.Column("night_clear_share", sa.Float),
        sa.Column("computed_at", sa.DateTime(timezone=True), server_default=sa.text("NOW()"), nullable=False),
        sa.UniqueConstraint("district", "composite_start", "product", name="uq_lst_district_8day"),
    )
    op.create_index("ix_lst_district_8day_district_start", "lst_district_8day", ["district", "composite_start"])


def downgrade() -> None:
    op.drop_index("ix_lst_district_8day_district_start", table_name="lst_district_8day")
    op.drop_table("lst_district_8day")
