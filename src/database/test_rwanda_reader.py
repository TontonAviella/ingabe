"""The internal Rwanda connection's login can read the approved Rwanda tables and nothing else (audit R1-5)."""

import asyncpg
import pytest

from src.database.pool import get_async_db_connection
from src.database.rwanda_reader import READER_ROLE, reader_uri


@pytest.mark.anyio
async def test_the_reader_reads_rwanda_tables_and_nothing_else():
    async with get_async_db_connection() as conn:  # runs the migrations first
        await conn.fetchval("SELECT 1")
    reader = await asyncpg.connect(reader_uri().replace("?sslmode=disable", ""), ssl=False)
    try:
        assert await reader.fetchval("SELECT current_user") == READER_ROLE
        assert await reader.fetchval("SELECT count(*) FROM rwanda_district_boundaries") >= 0
        assert await reader.fetchval("SELECT count(*) FROM rwanda_province_boundaries") >= 0
        for query in (
            "SELECT email FROM users LIMIT 1",
            "SELECT d.district, u.internal_uuid FROM rwanda_district_boundaries d, users u LIMIT 1",  # the comma join
            "SELECT compiled_truth FROM brain_pages LIMIT 1",
            "SELECT connection_uri FROM project_postgres_connections LIMIT 1",
            "SELECT query_to_xml('SELECT * FROM user_mundiai_projects', true, false, '')",
        ):
            with pytest.raises(asyncpg.exceptions.InsufficientPrivilegeError):
                await reader.fetch(query)
        with pytest.raises(asyncpg.exceptions.ReadOnlySQLTransactionError):
            await reader.execute("CREATE TEMP TABLE x (a int)")
    finally:
        await reader.close()


@pytest.mark.anyio
async def test_internal_connections_log_in_as_the_reader():
    from src.routes.message_routes import _ensure_rwanda_postgis_connection, _rwanda_internal_conn_id

    async with get_async_db_connection() as conn:
        await conn.execute("INSERT INTO user_mundiai_projects (id, owner_uuid, maps) VALUES ('Prwreadtest1', "
                           "'00000000-0000-0000-0000-000000000000', '{}') ON CONFLICT DO NOTHING")
        connection_id = await _ensure_rwanda_postgis_connection(conn, "Prwreadtest1", "00000000-0000-0000-0000-000000000000")
        assert connection_id == _rwanda_internal_conn_id("Prwreadtest1")
        uri = await conn.fetchval("SELECT connection_uri FROM project_postgres_connections WHERE id = $1", connection_id)
        assert uri.startswith(f"postgresql://{READER_ROLE}:")
