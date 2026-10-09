"""The internal Rwanda connection's logins read only their approved tables (audit R1-5), and only agriculture
projects' login reads the farm caches (R1-13)."""

import uuid

import asyncpg
import pytest

from src.database.pool import get_async_db_connection
from src.database.rwanda_reader import GENERAL_READER_ROLE, READER_ROLE, reader_uri

_OTHER_PEOPLES_DATA = (
    "SELECT email FROM users LIMIT 1",
    "SELECT d.district, u.internal_uuid FROM rwanda_district_boundaries d, users u LIMIT 1",  # the comma join
    "SELECT compiled_truth FROM brain_pages LIMIT 1",
    "SELECT connection_uri FROM project_postgres_connections LIMIT 1",
    "SELECT query_to_xml('SELECT * FROM user_mundiai_projects', true, false, '')",
    "SELECT * FROM ndvi_parcel_cache LIMIT 1",  # every user's parcels, no owner column
)


async def _login(industry: str) -> asyncpg.Connection:
    async with get_async_db_connection() as conn:  # runs the migrations first
        await conn.fetchval("SELECT 1")
    return await asyncpg.connect(reader_uri(industry).replace("?sslmode=disable", ""), ssl=False)


@pytest.mark.anyio
@pytest.mark.parametrize("industry, role", [("agriculture", READER_ROLE), ("power_grid", GENERAL_READER_ROLE),
                                            ("telecom", GENERAL_READER_ROLE)])
async def test_each_login_reads_boundaries_and_nothing_private(industry, role):
    reader = await _login(industry)
    try:
        assert await reader.fetchval("SELECT current_user") == role
        assert await reader.fetchval("SELECT count(*) FROM rwanda_district_boundaries") >= 0
        assert await reader.fetchval("SELECT count(*) FROM rwanda_province_boundaries") >= 0
        for query in _OTHER_PEOPLES_DATA:
            with pytest.raises(asyncpg.exceptions.InsufficientPrivilegeError):
                await reader.fetch(query)
        with pytest.raises(asyncpg.exceptions.ReadOnlySQLTransactionError):
            await reader.execute("CREATE TEMP TABLE x (a int)")
    finally:
        await reader.close()


@pytest.mark.anyio
async def test_only_agriculture_reads_the_farm_caches():
    farm, grid = await _login("agriculture"), await _login("power_grid")
    try:
        assert await farm.fetchval("SELECT count(*) FROM ndvi_cell_cache") >= 0
        with pytest.raises(asyncpg.exceptions.InsufficientPrivilegeError):
            await grid.fetch("SELECT * FROM ndvi_cell_cache LIMIT 1")
        with pytest.raises(asyncpg.exceptions.InsufficientPrivilegeError):
            await grid.fetch("SELECT * FROM drought_cache LIMIT 1")
    finally:
        await farm.close()
        await grid.close()


@pytest.mark.anyio
@pytest.mark.parametrize("industry, role", [("agriculture", READER_ROLE), ("telecom", GENERAL_READER_ROLE)])
async def test_internal_connections_log_in_as_their_industrys_reader(industry, role):
    from src.routes.message_routes import (
        RWANDA_INTERNAL_CONNECTION_NAME,
        _ensure_rwanda_postgis_connection,
        validate_internal_rwanda_query,
    )

    project_id = f"P{uuid.uuid4().hex[:11]}"
    async with get_async_db_connection() as conn:
        await conn.execute("INSERT INTO user_mundiai_projects (id, owner_uuid, maps, industry) VALUES ($1, "
                           "'00000000-0000-0000-0000-000000000000', '{}', $2)", project_id, industry)
        connection_id = await _ensure_rwanda_postgis_connection(conn, project_id, "00000000-0000-0000-0000-000000000000")
        row = await conn.fetchrow("SELECT connection_uri, connection_name FROM project_postgres_connections WHERE id = $1",
                                  connection_id)
    assert row["connection_uri"].startswith(f"postgresql://{role}:")
    assert row["connection_name"] == RWANDA_INTERNAL_CONNECTION_NAME == "Rwanda data (internal)"
    validate_internal_rwanda_query("SELECT * FROM rwanda_district_boundaries", industry)
    if industry != "agriculture":
        from fastapi import HTTPException

        with pytest.raises(HTTPException):
            validate_internal_rwanda_query("SELECT * FROM ndvi_cell_cache", industry)
