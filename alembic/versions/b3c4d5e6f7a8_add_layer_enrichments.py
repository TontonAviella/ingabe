"""add layer_enrichments table for on-the-fly choropleth metrics

Revision ID: b3c4d5e6f7a8
Revises: a7b8c9d0e1f2
Create Date: 2026-02-28 12:00:00.000000

"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "b3c4d5e6f7a8"
down_revision: str = "a7b8c9d0e1f2"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "layer_enrichments",
        sa.Column("id", sa.Integer, primary_key=True, autoincrement=True),
        sa.Column(
            "layer_id",
            sa.String(12),
            sa.ForeignKey("map_layers.layer_id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("feature_id", sa.Integer, nullable=False),
        sa.Column("column_name", sa.String(63), nullable=False),
        sa.Column("value", sa.Float),
        sa.Column(
            "computed_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("NOW()"),
        ),
        sa.UniqueConstraint(
            "layer_id", "feature_id", "column_name", name="uq_enrichment_layer_feat_col"
        ),
    )
    op.create_index(
        "ix_enrich_layer",
        "layer_enrichments",
        ["layer_id", "column_name"],
    )


def downgrade() -> None:
    op.drop_index("ix_enrich_layer", "layer_enrichments")
    op.drop_table("layer_enrichments")
