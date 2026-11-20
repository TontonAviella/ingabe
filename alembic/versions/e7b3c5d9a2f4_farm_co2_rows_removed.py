"""Farm carbon dioxide rows are removed: EDGAR's own Rwanda table has no such row (2026-11-20)

Revision ID: e7b3c5d9a2f4
Revises: d4a9e2b6c1f3
Create Date: 2026-11-20

The outside check of the rebuilt rows against EDGAR's country tables (reports/emissions/
prereg-edgar-district-rebuild-2026-11-20.md) matched methane, nitrous oxide and ammonia. Carbon dioxide from
farm soils did not match anything: the grid puts 227 to 437 t a year on squares inside Rwanda's outline, and
EDGAR's CO2 country table gives Rwanda no farm row at all. A figure with no national counterpart is not
served, so the 150 rows go. The reader (src/services/farm_emissions.py) does not read CO2 either.
Downgrade does not bring them back.
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "e7b3c5d9a2f4"
down_revision: Union[str, None] = "d4a9e2b6c1f3"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    bind = op.get_bind()
    if bind.execute(sa.text("SELECT to_regclass('public.emissions_annual_cache')")).scalar() is None:
        return
    bind.execute(sa.text("DELETE FROM emissions_annual_cache WHERE emission_type = 'CO2'"))


def downgrade() -> None:
    pass
