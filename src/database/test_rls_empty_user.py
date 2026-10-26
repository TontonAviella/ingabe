"""A connection without a user is handed back by the pool with app.user_id = '' (not unset): queries on projects and
conversations must still work (2026-10-26: naming 38 chats failed with 'invalid input syntax for type uuid: ""'; the
planner evaluates the policy's cast while estimating rows, so it needs table statistics to show), and a real user
still sees only their own."""

import uuid

import pytest

from src.database.pool import get_async_db_connection


@pytest.mark.anyio
async def test_an_empty_user_setting_does_not_break_projects_or_conversations():
    owner = str(uuid.uuid4())
    project = f"E{uuid.uuid4().hex[:11]}"
    async with get_async_db_connection() as conn:
        await conn.execute("INSERT INTO users (internal_uuid, email) VALUES ($1, $2)", owner, f"{owner}@example.test")
        await conn.execute("INSERT INTO user_mundiai_projects (id, owner_uuid, maps) VALUES ($1, $2::uuid, '{}')",
                           project, owner)
        await conn.execute("INSERT INTO conversations (project_id, owner_uuid, title) VALUES ($1, $2::uuid, 'x')",
                           project, owner)
    try:
        async with get_async_db_connection() as conn:
            # With statistics (as in production) the planner works out the policy's uuid cast while estimating rows,
            # before any row is read: that is where '' failed, even under EXPLAIN.
            await conn.execute("ANALYZE user_mundiai_projects")
            await conn.execute("ANALYZE conversations")
            async with conn.transaction():
                await conn.execute("SELECT set_config('app.user_id', '', true)")
                assert await conn.fetchval("SELECT count(*) FROM user_mundiai_projects WHERE id = $1", project) == 1
                assert await conn.fetchval("SELECT count(*) FROM conversations WHERE project_id = $1", project) == 1
    finally:
        async with get_async_db_connection() as conn:
            await conn.execute("DELETE FROM conversations WHERE project_id = $1", project)
            await conn.execute("DELETE FROM user_mundiai_projects WHERE id = $1", project)
            await conn.execute("DELETE FROM users WHERE internal_uuid = $1", owner)


@pytest.mark.anyio
async def test_a_user_still_sees_only_projects_they_belong_to():
    owner, stranger = str(uuid.uuid4()), str(uuid.uuid4())
    project = f"R{uuid.uuid4().hex[:11]}"
    async with get_async_db_connection() as conn:
        await conn.execute("INSERT INTO users (internal_uuid, email) VALUES ($1, $2), ($3, $4)",
                           owner, f"{owner}@example.test", stranger, f"{stranger}@example.test")
        await conn.execute("INSERT INTO user_mundiai_projects (id, owner_uuid, maps) VALUES ($1, $2::uuid, '{}')",
                           project, owner)
    try:
        for who, sees in ((owner, 1), (stranger, 0)):
            async with get_async_db_connection(user_id=who) as conn:
                assert await conn.fetchval("SELECT count(*) FROM user_mundiai_projects WHERE id = $1", project) == sees
    finally:
        async with get_async_db_connection() as conn:
            await conn.execute("DELETE FROM user_mundiai_projects WHERE id = $1", project)
            await conn.execute("DELETE FROM users WHERE internal_uuid = ANY($1::text[])", [owner, stranger])
