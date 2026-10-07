"""A page's scope change does not mark it as updated; a timeline edit still does.

On 2026-10-07 migration b8d4f0a2c6e1 backfilled access_scope on every page; the
scope was copied to each page's timeline entries, and the timeline trigger
stamped every page with the same updated_at (see d1e7a3c9f5b2).
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
OWNER = str(uuid.uuid4())
SLUG = f"trigger-page-{RUN_TAG}"


@pytest_asyncio.fixture(scope="module", loop_scope="module")
async def page():
    conn = await asyncpg.connect(_build_postgres_url())
    brain = BrainService()
    await conn.execute("SELECT set_config('app.user_id', '', false)")
    await brain.put_page(
        conn, SLUG,
        PageInput(type="field", title="Trigger test field", compiled_truth="A maize field."),
        owner_uuid=OWNER,
    )
    await brain.add_timeline_entry(
        conn, SLUG,
        TimelineInput(date=date(2026, 10, 7), summary="Planted maize", source="field_visit"),
        owner_uuid=OWNER,
    )

    yield {"conn": conn, "brain": brain}

    await conn.execute("DELETE FROM brain_pages WHERE slug = $1", SLUG)
    await conn.close()


async def _updated_at(conn):
    return await conn.fetchval("SELECT updated_at FROM brain_pages WHERE slug = $1", SLUG)


@pytest.mark.postgres
async def test_a_scope_change_leaves_updated_at_alone(page):
    conn = page["conn"]
    before = await _updated_at(conn)

    await conn.execute("UPDATE brain_pages SET access_scope = 'public' WHERE slug = $1", SLUG)

    scopes = await conn.fetch(
        "SELECT e.access_scope FROM brain_timeline_entries e "
        "JOIN brain_pages p ON p.id = e.page_id WHERE p.slug = $1",
        SLUG,
    )
    assert [r["access_scope"] for r in scopes] == ["public"]  # propagation still runs
    assert await _updated_at(conn) == before


@pytest.mark.postgres
async def test_a_timeline_edit_still_refreshes_the_page(page):
    conn = page["conn"]
    before = await _updated_at(conn)

    await conn.execute(
        "UPDATE brain_timeline_entries SET summary = 'Planted cassava' "
        "WHERE page_id = (SELECT id FROM brain_pages WHERE slug = $1)",
        SLUG,
    )

    assert await _updated_at(conn) > before
    results = await page["brain"].search_keyword(conn, "cassava")
    assert SLUG in {r.slug for r in results}
