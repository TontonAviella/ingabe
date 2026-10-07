"""brain: a timeline entry's scope change no longer marks its page as updated

trg_brain_timeline_search_vector (c3d4e5f6a7b8) runs `UPDATE brain_pages SET
updated_at = now()` after any update of a timeline entry, so the page's
search_vector picks up the entry's text. It also fired when only the entry's
access columns changed: brain_pages_propagate_partner_scope (f6a7b8c9d0e1)
copies a page's partner_id and access_scope to its timeline entries, so every
scope change re-stamped the page. On 2026-10-07 b8d4f0a2c6e1's backfill gave
all 34 live pages the same updated_at (its docstring said it would not). The
Brain packet's viewport query orders by updated_at, so the tie let copies of a
photo on other maps crowd out the copy on the user's map (fixed separately in
#145; the timestamps were restored by hand).

The trigger now fires on INSERT, DELETE and on UPDATE of the columns a page's
content depends on (page_id, date, source, summary, detail), not on
partner_id, access_scope, owner_uuid or created_at. The function is unchanged.

Revision ID: d1e7a3c9f5b2
Revises: b8d4f0a2c6e1
Create Date: 2026-10-07

"""

from typing import Sequence, Union

from alembic import op

revision: str = "d1e7a3c9f5b2"
down_revision: Union[str, None] = "b8d4f0a2c6e1"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _create_trigger(events: str) -> None:
    op.execute("DROP TRIGGER IF EXISTS trg_brain_timeline_search_vector ON brain_timeline_entries")
    op.execute(f"""
        CREATE TRIGGER trg_brain_timeline_search_vector
        AFTER {events} ON brain_timeline_entries
        FOR EACH ROW
        EXECUTE FUNCTION update_brain_page_search_from_timeline()
    """)


def upgrade() -> None:
    _create_trigger("INSERT OR DELETE OR UPDATE OF page_id, date, source, summary, detail")


def downgrade() -> None:
    _create_trigger("INSERT OR UPDATE OR DELETE")
