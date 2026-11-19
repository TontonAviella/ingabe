"""The internal Rwanda reader no longer sees crop_classification_cache (2026-11-19)

Revision ID: c8e1f4a7d2b5
Revises: f2b7c9d4a1e6
Create Date: 2026-11-19

The weekly crop classification job was removed (it was KMeans clusters, not crop identity) and the table has had
no rows since. Sage is shown every table its reader login may read, so "show the crop classification results for
Bugesera" sent it querying an empty table step after step and never answering. Nothing reads the table. The
table itself is kept; only the readers' permission goes (src/database/rwanda_reader.py no longer lists it).
"""

from typing import Sequence, Union

from alembic import op

from src.database.rwanda_reader import READER_ROLES

revision: str = "c8e1f4a7d2b5"
down_revision: Union[str, None] = "f2b7c9d4a1e6"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    for role in READER_ROLES:
        op.execute(
            f"DO $$ BEGIN IF to_regclass('public.crop_classification_cache') IS NOT NULL "
            f"AND EXISTS (SELECT FROM pg_roles WHERE rolname = '{role}') THEN "
            f"REVOKE ALL ON public.crop_classification_cache FROM {role}; END IF; END $$;"
        )


def downgrade() -> None:
    # Not granted back: the table has no rows and no reader, and the role migration's own downgrade
    # (b7d2e5a9c3f1) revokes only what rwanda_reader lists before it drops the roles.
    pass
