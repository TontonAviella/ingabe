"""ET normals: usual WaPOR evapotranspiration per H3 res-8 hexagon and dekad of year

Revision ID: c9e5f1a3b4d6
Revises: d2a7c4e8f1b9
Create Date: 2026-10-04

Tables only; src/services/et_normals.build() fills them from WaPOR v3.
"""

from typing import Sequence, Union

from alembic import op

revision: str = "c9e5f1a3b4d6"
down_revision: Union[str, None] = "d2a7c4e8f1b9"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS et_dekad_normals (
            h3_index    text NOT NULL,
            dekad       smallint NOT NULL CHECK (dekad BETWEEN 1 AND 36),
            mean_mm_day real NOT NULL,
            years       smallint NOT NULL,
            PRIMARY KEY (h3_index, dekad)
        )
        """
    )
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS et_dekad_normals_meta (
            id       serial PRIMARY KEY,
            built_at timestamptz NOT NULL,
            summary  jsonb NOT NULL
        )
        """
    )


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS et_dekad_normals_meta")
    op.execute("DROP TABLE IF EXISTS et_dekad_normals")
