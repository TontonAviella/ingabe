"""drop anomaly_alerts_cache: nothing writes or reads it any more

weekly_anomaly_scan, its only writer, was deleted on 2026-10-07: its z-score
compared a district with its own last 8 weeks and could not fire on the data
it had. Its readers moved to Digital Earth Africa's monthly NDVI anomaly: the
insurance engine (PR #150) and Sage's get_anomaly_alerts
(src/services/district_ndvi_anomaly.py). The table held 0 rows when this was
written. Downgrade recreates it empty, as e1f2a3b4c5d6 made it.
"""

from typing import Sequence, Union

import sqlalchemy as sa

from alembic import op

revision: str = "d2a7c4e9f1b3"
down_revision: Union[str, None] = "d1e7a3c9f5b2"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.drop_table("anomaly_alerts_cache")


def downgrade() -> None:
    op.create_table(
        "anomaly_alerts_cache",
        sa.Column("id", sa.Integer, primary_key=True, autoincrement=True),
        sa.Column("district", sa.String, nullable=False, index=True),
        sa.Column("h3_index", sa.String),
        sa.Column("parcel_id", sa.String),
        sa.Column("anomaly_date", sa.Date),
        sa.Column("observed_ndvi", sa.Float),
        sa.Column("expected_ndvi", sa.Float),
        sa.Column("z_score", sa.Float),
        sa.Column("severity", sa.String),
        sa.Column(
            "computed_at", sa.DateTime, server_default=sa.text("NOW()"),
        ),
    )
