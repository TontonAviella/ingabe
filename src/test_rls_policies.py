"""Tests for PostgreSQL Row-Level Security policies.

Verifies that RLS on user_mundiai_projects correctly filters rows based
on the app.user_id session variable set via set_config().

Uses synchronous psycopg2 connections to avoid asyncio event-loop
conflicts with pytest-xdist and the session-scoped anyio backend.
"""

import uuid
import pytest
import psycopg2

from src.database.pool import _build_postgres_url


@pytest.fixture
def rls_conn():
    """Provide a synchronous psycopg2 connection for direct RLS testing."""
    conn = psycopg2.connect(_build_postgres_url())
    conn.autocommit = False
    try:
        yield conn
    finally:
        conn.rollback()
        conn.close()


def test_owner_sees_own_project(rls_conn):
    """Project owner can see their project when RLS is active."""
    owner_id = str(uuid.uuid4())
    project_id = f"P{uuid.uuid4().hex[:11]}"

    cur = rls_conn.cursor()
    cur.execute(
        "INSERT INTO user_mundiai_projects (id, owner_uuid, maps) VALUES (%s, %s, '{}')",
        (project_id, owner_id),
    )

    # Set RLS context to the owner
    cur.execute("SELECT set_config('app.user_id', %s, true)", (owner_id,))
    cur.execute("SELECT id FROM user_mundiai_projects WHERE id = %s", (project_id,))
    row = cur.fetchone()
    assert row is not None
    assert row[0] == project_id


def test_other_user_cannot_see_project(rls_conn):
    """A different user cannot see a project they have no access to."""
    owner_id = str(uuid.uuid4())
    other_id = str(uuid.uuid4())
    project_id = f"P{uuid.uuid4().hex[:11]}"

    cur = rls_conn.cursor()
    cur.execute(
        "INSERT INTO user_mundiai_projects (id, owner_uuid, maps) VALUES (%s, %s, '{}')",
        (project_id, owner_id),
    )

    # Set RLS context to a different user
    cur.execute("SELECT set_config('app.user_id', %s, true)", (other_id,))
    cur.execute("SELECT id FROM user_mundiai_projects WHERE id = %s", (project_id,))
    row = cur.fetchone()
    assert row is None


def test_editor_can_see_project(rls_conn):
    """An editor listed in editor_uuids can see the project."""
    owner_id = str(uuid.uuid4())
    editor_id = str(uuid.uuid4())
    project_id = f"P{uuid.uuid4().hex[:11]}"

    cur = rls_conn.cursor()
    cur.execute(
        "INSERT INTO user_mundiai_projects (id, owner_uuid, editor_uuids, maps) VALUES (%s, %s, ARRAY[%s]::uuid[], '{}')",
        (project_id, owner_id, editor_id),
    )

    # Set RLS context to the editor
    cur.execute("SELECT set_config('app.user_id', %s, true)", (editor_id,))
    cur.execute("SELECT id FROM user_mundiai_projects WHERE id = %s", (project_id,))
    row = cur.fetchone()
    assert row is not None


def test_no_user_id_bypasses_rls(rls_conn):
    """When app.user_id is not set (migrations/background), all rows visible."""
    owner_id = str(uuid.uuid4())
    project_id = f"P{uuid.uuid4().hex[:11]}"

    cur = rls_conn.cursor()
    cur.execute(
        "INSERT INTO user_mundiai_projects (id, owner_uuid, maps) VALUES (%s, %s, '{}')",
        (project_id, owner_id),
    )

    # Do NOT set app.user_id — simulates migration/background job
    cur.execute("RESET app.user_id")
    cur.execute("SELECT id FROM user_mundiai_projects WHERE id = %s", (project_id,))
    row = cur.fetchone()
    assert row is not None


def test_empty_string_after_pool_reuse_bypasses_rls(rls_conn):
    """After set_config + RESET (pool reuse), app.user_id is '' not NULL — must still bypass."""
    owner_id = str(uuid.uuid4())
    project_id = f"P{uuid.uuid4().hex[:11]}"

    cur = rls_conn.cursor()
    cur.execute(
        "INSERT INTO user_mundiai_projects (id, owner_uuid, maps) VALUES (%s, %s, '{}')",
        (project_id, owner_id),
    )

    # Simulate pool reuse: set a user_id, then RESET (leaves '' not NULL)
    cur.execute("SELECT set_config('app.user_id', %s, true)", (str(uuid.uuid4()),))
    cur.execute("RESET app.user_id")
    cur.execute("SELECT id FROM user_mundiai_projects WHERE id = %s", (project_id,))
    row = cur.fetchone()
    assert row is not None
