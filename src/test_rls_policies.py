"""Tests for PostgreSQL Row-Level Security policies.

Verifies that RLS on user_mundiai_projects correctly filters rows based
on the app.user_id session variable set via set_config().
"""

import uuid
import pytest
import asyncpg

from src.database.pool import _build_postgres_url


@pytest.fixture
async def rls_conn():
    """Provide a raw asyncpg connection for direct RLS testing."""
    conn = await asyncpg.connect(_build_postgres_url())
    tr = conn.transaction()
    await tr.start()
    try:
        yield conn
    finally:
        await tr.rollback()
        await conn.close()


@pytest.mark.anyio
async def test_owner_sees_own_project(rls_conn):
    """Project owner can see their project when RLS is active."""
    owner_id = str(uuid.uuid4())
    project_id = f"P-{uuid.uuid4().hex[:12]}"

    await rls_conn.execute(
        """
        INSERT INTO user_mundiai_projects (id, owner_uuid, maps)
        VALUES ($1, $2, '{}')
        """,
        project_id,
        uuid.UUID(owner_id),
    )

    # Set RLS context to the owner
    await rls_conn.execute(
        "SELECT set_config('app.user_id', $1, true)", owner_id
    )
    row = await rls_conn.fetchrow(
        "SELECT id FROM user_mundiai_projects WHERE id = $1", project_id
    )
    assert row is not None
    assert row["id"] == project_id


@pytest.mark.anyio
async def test_other_user_cannot_see_project(rls_conn):
    """A different user cannot see a project they have no access to."""
    owner_id = str(uuid.uuid4())
    other_id = str(uuid.uuid4())
    project_id = f"P-{uuid.uuid4().hex[:12]}"

    await rls_conn.execute(
        """
        INSERT INTO user_mundiai_projects (id, owner_uuid, maps)
        VALUES ($1, $2, '{}')
        """,
        project_id,
        uuid.UUID(owner_id),
    )

    # Set RLS context to a different user
    await rls_conn.execute(
        "SELECT set_config('app.user_id', $1, true)", other_id
    )
    row = await rls_conn.fetchrow(
        "SELECT id FROM user_mundiai_projects WHERE id = $1", project_id
    )
    assert row is None


@pytest.mark.anyio
async def test_editor_can_see_project(rls_conn):
    """An editor listed in editor_uuids can see the project."""
    owner_id = str(uuid.uuid4())
    editor_id = str(uuid.uuid4())
    project_id = f"P-{uuid.uuid4().hex[:12]}"

    await rls_conn.execute(
        """
        INSERT INTO user_mundiai_projects (id, owner_uuid, editor_uuids, maps)
        VALUES ($1, $2, $3, '{}')
        """,
        project_id,
        uuid.UUID(owner_id),
        [uuid.UUID(editor_id)],
    )

    # Set RLS context to the editor
    await rls_conn.execute(
        "SELECT set_config('app.user_id', $1, true)", editor_id
    )
    row = await rls_conn.fetchrow(
        "SELECT id FROM user_mundiai_projects WHERE id = $1", project_id
    )
    assert row is not None


@pytest.mark.anyio
async def test_no_user_id_bypasses_rls(rls_conn):
    """When app.user_id is not set (migrations/background), all rows visible."""
    owner_id = str(uuid.uuid4())
    project_id = f"P-{uuid.uuid4().hex[:12]}"

    await rls_conn.execute(
        """
        INSERT INTO user_mundiai_projects (id, owner_uuid, maps)
        VALUES ($1, $2, '{}')
        """,
        project_id,
        uuid.UUID(owner_id),
    )

    # Do NOT set app.user_id — simulates migration/background job
    await rls_conn.execute("RESET app.user_id")
    row = await rls_conn.fetchrow(
        "SELECT id FROM user_mundiai_projects WHERE id = $1", project_id
    )
    assert row is not None
