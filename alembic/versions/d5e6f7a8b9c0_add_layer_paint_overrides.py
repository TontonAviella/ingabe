"""add layer_paint_overrides table for persisting choropleth/color/opacity overrides

Revision ID: d5e6f7a8b9c0
Revises: c4d5e6f7a8b9
Create Date: 2026-03-03 08:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = "d5e6f7a8b9c0"
down_revision: Union[str, None] = "c4d5e6f7a8b9"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "layer_paint_overrides",
        sa.Column("map_id", sa.String(length=12), nullable=False),
        sa.Column("layer_id", sa.String(length=12), nullable=False),
        sa.Column("overrides_json", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column(
            "updated_at",
            sa.TIMESTAMP(timezone=True),
            server_default=sa.text("CURRENT_TIMESTAMP"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(["map_id"], ["user_mundiai_maps.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["layer_id"], ["map_layers.layer_id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("map_id", "layer_id"),
    )


def downgrade() -> None:
    op.drop_table("layer_paint_overrides")
