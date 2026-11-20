"""Farm emissions by district are rebuilt: each grid square shared by district outline (2026-11-20)

Revision ID: d4a9e2b6c1f3
Revises: c8e1f4a7d2b5
Create Date: 2026-11-20

The old rows gave each district every EDGAR 0.1 degree square inside the rectangle around it: 356 squares
credited where 237 exist, 100 of them counted in two or more districts, so Rwanda's livestock methane for 2022
read 109,195 t where the same grids give 61,338 t. The new rows share each square's tonnes by the part of the
square inside the district's outline (scripts/build_edgar_district_emissions.py; test written first in
reports/emissions/prereg-edgar-district-rebuild-2026-11-20.md: districts add up to Rwanda, no square counted
twice, 1,650 rows).

Every new row's source_version ends in "+outline_share"; Sage's reader (src/services/farm_emissions.py) reads
only those, so an old rectangle row can never be served. grid_cells and mean_flux_kg_m2_s described the old
method and are left empty. The old rows are not kept and downgrade does not bring them back: they were wrong.
"""

import csv
from pathlib import Path
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "d4a9e2b6c1f3"
down_revision: Union[str, None] = "c8e1f4a7d2b5"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

SEED = Path(__file__).resolve().parent.parent / "seeds" / "edgar_farm_emissions_rwanda_2018_2022.csv"
METHOD_MARK = "+outline_share"


def upgrade() -> None:
    bind = op.get_bind()
    if bind.execute(sa.text("SELECT to_regclass('public.emissions_annual_cache')")).scalar() is None:
        return
    with SEED.open(newline="") as fh:
        rows = [
            {
                "district": r["district"],
                "year": int(r["year"]),
                "emission_type": r["emission_type"],
                "sector": r["sector"],
                "sector_label": r["sector_label"],
                "total_tonnes": float(r["total_tonnes"]),
                "source_version": r["source_version"] + METHOD_MARK,
            }
            for r in csv.DictReader(fh)
        ]
    if len(rows) != 1650:
        raise RuntimeError(f"emissions seed has {len(rows)} rows, expected 1650")
    bind.execute(sa.text("DELETE FROM emissions_annual_cache"))
    bind.execute(
        sa.text(
            "INSERT INTO emissions_annual_cache "
            "(district, year, emission_type, sector, sector_label, total_tonnes, source_version) "
            "VALUES (:district, :year, :emission_type, :sector, :sector_label, :total_tonnes, :source_version)"
        ),
        rows,
    )


def downgrade() -> None:
    pass
