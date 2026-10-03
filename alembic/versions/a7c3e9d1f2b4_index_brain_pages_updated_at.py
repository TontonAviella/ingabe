"""Index brain_pages.updated_at.

The Brain hook loop asks "has any page changed since the last stale-embedding
scan?" every 30 s, and the scan itself orders pages by updated_at. Without an
index both read every page: max(updated_at) took 4.9 s and the scan 6.7 s on
431,670 pages (measured 2026-10-03), several times a minute. Built
CONCURRENTLY so writes to brain_pages are not blocked while it builds.

Revision ID: a7c3e9d1f2b4
Revises: 57f54ece3e6d
"""
from __future__ import annotations

from alembic import op

revision: str = "a7c3e9d1f2b4"
down_revision: str = "57f54ece3e6d"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.get_context().autocommit_block():
        op.execute(
            "CREATE INDEX CONCURRENTLY IF NOT EXISTS ix_brain_pages_updated_at "
            "ON brain_pages (updated_at)"
        )


def downgrade() -> None:
    with op.get_context().autocommit_block():
        op.execute("DROP INDEX CONCURRENTLY IF EXISTS ix_brain_pages_updated_at")
