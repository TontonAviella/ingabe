"""insurance_triggers: the NDVI z-score triggers name their real source

a1b2c3d4e5f7 seeded every ndvi_z_score trigger with source 'Sentinel-2/SAR'
(94 rows). The radar half was the Sentinel-1 predictor that stood in for a
missing optical anomaly; it was removed after it was measured not to follow
the real anomaly (docs/SAR_NDVI_SKILL.md), and the z-score now comes only
from Digital Earth Africa's monthly NDVI anomaly. The label is rewritten to
the engine's own name for that dataset (deafrica_stac.NDVI_ANOMALY_SOURCE;
test_insurance_engine keeps the two equal).

Only rows still carrying the seeded label change: a row someone edited keeps
its source. Downgrade restores the seeded label on the rows this migration
changed.

Revision ID: f2b9c4d1a7e3
Revises: d1e7a3c9f5b2
Create Date: 2026-10-07
"""

from alembic import op
import sqlalchemy as sa

revision: str = "f2b9c4d1a7e3"
down_revision: str = "d1e7a3c9f5b2"
branch_labels = None
depends_on = None

SEEDED = "Sentinel-2/SAR"
SOURCE = "Digital Earth Africa NDVI anomaly (Landsat + Sentinel-2 vs 1984-2020)"


def upgrade() -> None:
    op.get_bind().execute(
        sa.text("UPDATE insurance_triggers SET source = :new WHERE signal = 'ndvi_z_score' AND source = :old"),
        {"new": SOURCE, "old": SEEDED},
    )


def downgrade() -> None:
    op.get_bind().execute(
        sa.text("UPDATE insurance_triggers SET source = :old WHERE signal = 'ndvi_z_score' AND source = :new"),
        {"new": SOURCE, "old": SEEDED},
    )
