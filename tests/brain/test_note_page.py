"""Where a person's note goes when they do not name a page (BrainService.note_page).

On 2026-11-19 the routing eval showed Sage searching instead of saving ("note that the Rulindo coop reported pests
today"): add_observation needed the slug of a page that already existed, so a note about anything new had nowhere
to go. A note now goes on the page with that title, else on a page of the person's own made for it. These tests
run the real SQL under row-level security.
"""

import uuid
from datetime import date

import asyncpg
import pytest
import pytest_asyncio

from src.database.pool import _build_postgres_url
from src.services.brain_service import BrainService, PageInput, TimelineInput

pytestmark = pytest.mark.asyncio(loop_scope="module")

RUN_TAG = uuid.uuid4().hex[:8]
USER = str(uuid.uuid4())
STRANGER = str(uuid.uuid4())
PARTNER = str(uuid.uuid4())
OTHER_PARTNER = str(uuid.uuid4())
COOP = f"Rulindo coop {RUN_TAG}"
HAIL = f"Nyarugenge hail {RUN_TAG}"


async def _as(conn: asyncpg.Connection, user_id: str, partner_id: str) -> None:
    await conn.execute("SELECT set_config('app.user_id', $1, false)", user_id)
    await conn.execute("SELECT set_config('app.partner_id', $1, false)", partner_id)
    await conn.execute("SELECT set_config('app.role', '', false)")


@pytest_asyncio.fixture(scope="module", loop_scope="module")
async def conn():
    connection = await asyncpg.connect(_build_postgres_url())
    try:
        yield connection
    finally:
        await connection.execute("SELECT set_config('app.role', 'admin', false)")
        await connection.execute("DELETE FROM brain_pages WHERE slug LIKE $1 OR title LIKE $1", f"%{RUN_TAG}%")
        await connection.close()


async def test_a_note_goes_on_the_page_with_that_title_or_on_a_new_page_of_the_persons_own(conn):
    brain = BrainService()
    await _as(conn, USER, PARTNER)
    existing = f"coop-{RUN_TAG}"
    await brain.put_page(conn, existing, PageInput(type="company", title=COOP, compiled_truth="A cooperative."),
                         owner_uuid=USER)

    # the title, in any case: the page that is there
    assert await brain.note_page(conn, COOP.upper(), USER) == (existing, "found")
    # a slug the person can read wins, whatever `about` says
    assert await brain.note_page(conn, "something else", USER, slug=existing) == (existing, "given")

    # nothing with that title: a page of the person's own is made, and found again for the next note
    slug, how = await brain.note_page(conn, HAIL, USER)
    assert how == "made" and slug.startswith("note-nyarugenge-hail-")
    assert await brain.note_page(conn, HAIL, USER) == (slug, "found")
    entry = await brain.add_timeline_entry(
        conn, slug, TimelineInput(date=date(2026, 11, 18), summary="It hailed.", detail="", source="user_report"),
        owner_uuid=USER)
    assert entry > 0
    page = await brain.get_page(conn, slug)
    assert page is not None and page.title == HAIL

    # a slug that names nothing readable falls back to what the note is about, never to an error
    assert await brain.note_page(conn, HAIL, USER, slug=f"no-such-page-{RUN_TAG}") == (slug, "found")
    with pytest.raises(ValueError):
        await brain.note_page(conn, "   ", USER)


async def test_another_persons_note_page_is_not_found_or_written_to(conn):
    brain = BrainService()
    await _as(conn, USER, PARTNER)
    mine, _ = await brain.note_page(conn, HAIL, USER)

    # a stranger at another partner notes the same thing: their own page, not the first person's
    await _as(conn, STRANGER, OTHER_PARTNER)
    assert await brain.get_page(conn, mine) is None
    theirs, how = await brain.note_page(conn, HAIL, STRANGER)
    assert how == "made" and theirs != mine
    # and the first person's slug, handed over as if known, is not written to
    assert await brain.note_page(conn, HAIL, STRANGER, slug=mine) == (theirs, "found")
