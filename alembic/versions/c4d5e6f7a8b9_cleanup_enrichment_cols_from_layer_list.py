"""remove enrichment metric keys from postgis_attribute_column_list

A bug in the initial enrich endpoint added metric keys (temp_mean,
rainfall_mm, etc.) to postgis_attribute_column_list.  These columns
don't exist in the PostGIS source tables and cause tile queries to fail
with "column does not exist".  This migration strips them out.

Revision ID: c4d5e6f7a8b9
Revises: b3c4d5e6f7a8
Create Date: 2026-03-01 00:00:00.000000

"""

from typing import Sequence, Union

from alembic import op
from sqlalchemy import text

# revision identifiers, used by Alembic.
revision: str = "c4d5e6f7a8b9"
down_revision: Union[str, None] = "b3c4d5e6f7a8"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

# All metric keys that may have been erroneously added
_METRIC_KEYS = [
    "cropland_pct",
    "forest_pct",
    "built_pct",
    "rangeland_pct",
    "ndvi_mean",
    "rainfall_mm",
    "temp_mean",
]


def upgrade() -> None:
    conn = op.get_bind()
    for key in _METRIC_KEYS:
        conn.execute(
            text(
                "UPDATE map_layers "
                "SET postgis_attribute_column_list = array_remove(postgis_attribute_column_list, CAST(:key AS varchar)) "
                "WHERE postgis_attribute_column_list @> ARRAY[CAST(:key AS varchar)]"
            ),
            {"key": key},
        )


def downgrade() -> None:
    # No-op: we can't know which layers previously had these keys
    pass
