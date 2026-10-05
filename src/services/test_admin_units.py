"""list_admin_units: exact names and counts, and refusals instead of guesses.

Each test seeds a few boundary rows inside a transaction that is rolled back,
with names namespaced by RUN_TAG so parallel workers never collide.
"""

import uuid
from contextlib import asynccontextmanager

import pytest

from src.services.admin_boundaries import list_admin_units

RUN_TAG = uuid.uuid4().hex[:8]
DA, DB = f"Da{RUN_TAG}", f"Db{RUN_TAG}"
SHARED, ONLY = f"Shared{RUN_TAG}", f"Only{RUN_TAG}"
C1, C2, C3, C4 = (f"Cell{i}{RUN_TAG}" for i in range(1, 5))
SQUARE = "ST_GeomFromText('POLYGON((30 -2,30.01 -2,30.01 -1.99,30 -1.99,30 -2))', 4326)"


@asynccontextmanager
async def seeded():
    from src.structures import get_async_db_connection

    async with get_async_db_connection() as conn:
        tr = conn.transaction()
        await tr.start()
        try:
            for d in (DA, DB):
                await conn.execute(f"INSERT INTO rwanda_district_boundaries (district, geom) VALUES ($1, {SQUARE})", d)
            for s, d in ((SHARED, DA), (ONLY, DA), (SHARED, DB)):
                await conn.execute(
                    f"INSERT INTO rwanda_sector_boundaries (sector_name, district_name, geom) VALUES ($1, $2, {SQUARE})", s, d
                )
            for c, s, d in ((C1, ONLY, DA), (C2, ONLY, DA), (C3, SHARED, DA), (C4, SHARED, DB)):
                await conn.execute(
                    "INSERT INTO rwanda_cell_boundaries (cell_name, sector_name, district_name, geom) "
                    f"VALUES ($1, $2, $3, {SQUARE})", c, s, d
                )
            for v, c in (("Va", C1), ("Vb", C1), ("Vc", C2)):
                await conn.execute(
                    "INSERT INTO rwanda_village_boundaries (village_name, cell_name, sector_name, district_name, geom) "
                    f"VALUES ($1, $2, $3, $4, {SQUARE})", v, c, ONLY, DA
                )
            yield conn
        finally:
            await tr.rollback()


@pytest.mark.asyncio
async def test_sectors_of_a_district_with_exact_count_and_canonical_name() -> None:
    async with seeded() as conn:
        out = await list_admin_units(conn, "sector", district=DA.lower())
    assert out["within"] == {"district": DA}
    assert out["count"] == 2 and out["truncated"] is False
    assert [u["name"] for u in out["units"]] == sorted([ONLY, SHARED])


@pytest.mark.asyncio
async def test_villages_of_a_district_say_which_cell_and_sector_they_are_in() -> None:
    async with seeded() as conn:
        out = await list_admin_units(conn, "village", district=DA)
    assert out["count"] == 3
    assert out["units"][0] == {"name": "Va", "cell": C1, "sector": ONLY}


@pytest.mark.asyncio
async def test_a_sector_name_in_two_districts_is_refused_not_merged() -> None:
    async with seeded() as conn:
        with pytest.raises(ValueError, match="more than one place") as e:
            await list_admin_units(conn, "cell", sector=SHARED)
        assert DA in str(e.value) and DB in str(e.value)
        out = await list_admin_units(conn, "cell", district=DB, sector=SHARED)
    assert [u["name"] for u in out["units"]] == [C4]
    assert out["within"] == {"sector": SHARED, "district": DB}


@pytest.mark.asyncio
async def test_a_misspelt_district_gets_suggestions() -> None:
    async with seeded() as conn:
        with pytest.raises(ValueError, match="did you mean") as e:
            await list_admin_units(conn, "sector", district=DA[:-1] + "x")
    assert DA in str(e.value)


@pytest.mark.asyncio
async def test_a_sector_in_the_wrong_district_says_where_it_is() -> None:
    async with seeded() as conn:
        with pytest.raises(ValueError, match="is in") as e:
            await list_admin_units(conn, "cell", district=DB, sector=ONLY)
    assert DA in str(e.value)


@pytest.mark.asyncio
async def test_listing_below_district_level_needs_a_parent() -> None:
    async with seeded() as conn:
        with pytest.raises(ValueError, match="name the district"):
            await list_admin_units(conn, "village")
        districts = await list_admin_units(conn, "district")
    assert {DA, DB} <= {u["name"] for u in districts["units"]}
