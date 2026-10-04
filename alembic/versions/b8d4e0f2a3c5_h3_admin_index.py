"""H3 admin index: hexagon <-> province/district/sector/cell/village overlaps

Revision ID: b8d4e0f2a3c5
Revises: a7c3e9d1f2b4
Create Date: 2026-10-04

Tables only; src/services/h3_admin_index.build() fills them from the
rwanda_*_boundaries tables.
"""

from typing import Sequence, Union

from alembic import op

revision: str = "b8d4e0f2a3c5"
down_revision: Union[str, None] = "a7c3e9d1f2b4"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS h3_admin_overlap (
            h3_index      text NOT NULL,
            admin_level   text NOT NULL CHECK (admin_level IN ('province','district','sector','cell','village')),
            unit_id       text NOT NULL,
            unit_name     text NOT NULL,
            overlap_km2   double precision NOT NULL,
            hex_fraction  double precision NOT NULL,
            unit_fraction double precision NOT NULL,
            PRIMARY KEY (admin_level, unit_id, h3_index)
        )
        """
    )
    op.execute("CREATE INDEX IF NOT EXISTS ix_h3_admin_overlap_h3 ON h3_admin_overlap (h3_index)")
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS h3_admin_cells (
            h3_index     text PRIMARY KEY,
            resolution   smallint NOT NULL,
            province     text,
            district     text,
            sector_id    integer,
            sector_name  text,
            cell_id      integer,
            cell_name    text,
            village_id   integer,
            village_name text,
            geom         geometry(Polygon, 4326) NOT NULL
        )
        """
    )
    op.execute("CREATE INDEX IF NOT EXISTS ix_h3_admin_cells_district ON h3_admin_cells (district)")
    op.execute("CREATE INDEX IF NOT EXISTS ix_h3_admin_cells_village ON h3_admin_cells (village_id)")
    op.execute("CREATE INDEX IF NOT EXISTS ix_h3_admin_cells_geom ON h3_admin_cells USING gist (geom)")
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS h3_admin_index_meta (
            id         serial PRIMARY KEY,
            built_at   timestamptz NOT NULL,
            resolution smallint NOT NULL,
            summary    jsonb NOT NULL
        )
        """
    )


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS h3_admin_index_meta")
    op.execute("DROP TABLE IF EXISTS h3_admin_cells")
    op.execute("DROP TABLE IF EXISTS h3_admin_overlap")
