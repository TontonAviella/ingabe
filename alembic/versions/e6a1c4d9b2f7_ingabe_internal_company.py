"""Ingabe's own company sees every industry; partner companies work in one (2026-10-25)

Revision ID: e6a1c4d9b2f7
Revises: d5f9b2c3e8a4
Create Date: 2026-10-25

Roger, 2026-10-25: a partner company chooses one industry and its people see only that one (no other industries'
tabs, no switch); only Ingabe's own people see all three. organizations.tier already allows 'internal'
(47463555a0f8); Ingabe's company (WorkOS org_01M4ZV3FPK3JKCNR76DHZE3QG6 on the live server) becomes 'internal'.
Where that company does not exist (a laptop, a test database) nothing changes. The rules are in
src/services/industry.py.
"""

from typing import Sequence, Union

from alembic import op

revision: str = "e6a1c4d9b2f7"
down_revision: Union[str, None] = "d5f9b2c3e8a4"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

INGABE_WORKOS_ORG = "org_01M4ZV3FPK3JKCNR76DHZE3QG6"


def upgrade() -> None:
    op.execute(f"UPDATE organizations SET tier = 'internal' WHERE workos_org_id = '{INGABE_WORKOS_ORG}'")


def downgrade() -> None:
    op.execute(f"UPDATE organizations SET tier = 'partner' WHERE workos_org_id = '{INGABE_WORKOS_ORG}'")
