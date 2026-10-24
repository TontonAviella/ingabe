"""Land cover per district, sector and cell (ESRI 10 m annual land use/land cover) in Postgres (2026-10-25)

Revision ID: b8d3f1a6c2e4
Revises: a7c4e9b2d1f6
Create Date: 2026-10-25

Sage's query_worldcover_stats read worldcover_admin_stats from Postgres, but the Dagster asset
worldcover_zonal_stats wrote it to a DuckDB file, and that asset was in no job or schedule, so the table existed
nowhere on the live server and the tool could never answer (found by the component self-tests, 2026-10-25).
The table now lives here, filled monthly by the asset (src/pipelines/rwanda_assets.py), owned by the app user so
the nightly backup includes it.
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "b8d3f1a6c2e4"
down_revision: Union[str, None] = "a7c4e9b2d1f6"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "worldcover_admin_stats",
        sa.Column("id", sa.Integer, primary_key=True, autoincrement=True),
        sa.Column("admin_level", sa.String(8), nullable=False),  # district | sector | cell
        sa.Column("admin_name", sa.String, nullable=False),
        sa.Column("district_name", sa.String),
        sa.Column("sector_name", sa.String),
        sa.Column("lulc_year", sa.Integer, nullable=False),
        sa.Column("class_value", sa.Integer, nullable=False),
        sa.Column("class_name", sa.String, nullable=False),
        sa.Column("pixel_count", sa.Integer, nullable=False),
        sa.Column("area_hectares", sa.Float, nullable=False),
        sa.Column("computed_at", sa.DateTime(timezone=True), server_default=sa.text("NOW()"), nullable=False),
    )
    op.create_index("ix_worldcover_admin_stats_level_name", "worldcover_admin_stats",
                    ["admin_level", sa.text("lower(admin_name)")])


def downgrade() -> None:
    op.drop_index("ix_worldcover_admin_stats_level_name", table_name="worldcover_admin_stats")
    op.drop_table("worldcover_admin_stats")
