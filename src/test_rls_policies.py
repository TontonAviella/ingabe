"""Tests for PostgreSQL Row-Level Security policies.

Verifies that RLS on user_mundiai_projects correctly filters rows based
on the app.user_id session variable set via set_config().

Uses synchronous psycopg2 connections to avoid asyncio event-loop
conflicts with pytest-xdist and the session-scoped anyio backend.

IMPORTANT: PostgreSQL superusers bypass RLS even with FORCE ROW LEVEL
SECURITY.  CI connects as the 'postgres' superuser, so we SET ROLE to
a non-superuser to actually exercise the policies.
"""

import uuid
import pytest
import psycopg2

from src.database.pool import _build_postgres_url


@pytest.fixture
def rls_conn():
    """Provide a psycopg2 connection using a non-superuser role for RLS testing."""
    conn = psycopg2.connect(_build_postgres_url())
    conn.autocommit = True
    cur = conn.cursor()

    # Create a non-superuser role (superusers bypass RLS unconditionally).
    # mundiuser had SUPERUSER + BYPASSRLS revoked in migration a0b1c2d3e4f5,
    # so SET ROLE now requires mundiuser to be a MEMBER of rls_test_role.
    cur.execute("""
        DO $$
        BEGIN
            IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'rls_test_role') THEN
                CREATE ROLE rls_test_role NOLOGIN;
            END IF;
        END $$
    """)
    cur.execute("GRANT USAGE ON SCHEMA public TO rls_test_role")
    cur.execute("GRANT ALL ON ALL TABLES IN SCHEMA public TO rls_test_role")
    cur.execute("GRANT ALL ON ALL SEQUENCES IN SCHEMA public TO rls_test_role")
    cur.execute("GRANT rls_test_role TO current_user")

    conn.autocommit = False
    cur.execute("SET ROLE rls_test_role")

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
