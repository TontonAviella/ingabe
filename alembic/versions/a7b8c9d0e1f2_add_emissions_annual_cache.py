"""add emissions_annual_cache table for EDGAR data

Revision ID: a7b8c9d0e1f2
Revises: c2d3e4f5a6b7, f2a3b4c5d6e7
Create Date: 2026-02-28 00:00:00.000000

"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "a7b8c9d0e1f2"
down_revision = ("c2d3e4f5a6b7", "f2a3b4c5d6e7")
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "emissions_annual_cache",
        sa.Column("id", sa.Integer, primary_key=True, autoincrement=True),
        sa.Column("district", sa.String, nullable=False, index=True),
        sa.Column("year", sa.Integer, nullable=False),
        sa.Column("emission_type", sa.String, nullable=False),
        sa.Column("sector", sa.String, nullable=False),
        sa.Column("sector_label", sa.String),
        sa.Column("total_tonnes", sa.Float),
        sa.Column("mean_flux_kg_m2_s", sa.Float),
        sa.Column("grid_cells", sa.Integer),
        sa.Column("source_version", sa.String),
        sa.Column(
            "computed_at", sa.DateTime, server_default=sa.text("NOW()"),
        ),
    )
    op.create_index(
        "ix_emissions_annual_cache_lookup",
        "emissions_annual_cache",
        ["district", "year", "emission_type", "sector"],
        unique=True,
    )


def downgrade() -> None:
    op.drop_index("ix_emissions_annual_cache_lookup", "emissions_annual_cache")
    op.drop_table("emissions_annual_cache")
