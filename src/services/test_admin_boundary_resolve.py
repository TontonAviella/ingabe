"""resolve_admin_boundary: asked for one unit, deliver that unit or nothing.

A name shared by several units, a parent that does not exist, or a unit that is not where the
parents say must not draw anything; exactly one match draws exactly one polygon. Each test seeds
a few boundary rows inside a transaction that is rolled back, with names namespaced by RUN_TAG so
parallel workers never collide.
"""

import uuid
from contextlib import asynccontextmanager

import pytest

from src.services.admin_boundaries import resolve_admin_boundary

RUN_TAG = uuid.uuid4().hex[:8]
DA, DB = f"Da{RUN_TAG}", f"Db{RUN_TAG}"
SHARED, ONLY = f"Shared{RUN_TAG}", f"Only{RUN_TAG}"
CSAME, CONE = f"Csame{RUN_TAG}", f"Cone{RUN_TAG}"
VSAME, VONE = f"Vsame{RUN_TAG}", f"Vone{RUN_TAG}"


def _square(i: int) -> str:
    x = 30 + i * 0.02
    return f"ST_GeomFromText('POLYGON(({x} -2,{x + 0.01} -2,{x + 0.01} -1.99,{x} -1.99,{x} -2))', 4326)"


@asynccontextmanager
async def seeded():
    """District DA has sectors SHARED and ONLY; district DB has another SHARED.
    Cell CSAME is in ONLY/DA and in SHARED/DA; cell CONE is in SHARED/DB.
    Village VSAME is in CSAME/ONLY/DA and in CONE/SHARED/DB; VONE is in CSAME/SHARED/DA."""
    from src.structures import get_async_db_connection

    async with get_async_db_connection() as conn:
        tr = conn.transaction()
        await tr.start()
        try:
            for i, d in enumerate((DA, DB)):
                await conn.execute(f"INSERT INTO rwanda_district_boundaries (district, geom) VALUES ($1, {_square(i)})", d)
            for i, (s, d) in enumerate(((SHARED, DA), (ONLY, DA), (SHARED, DB))):
                await conn.execute(
                    f"INSERT INTO rwanda_sector_boundaries (sector_name, district_name, geom) VALUES ($1, $2, {_square(i)})",
                    s, d,
                )
            for i, (c, s, d) in enumerate(((CSAME, ONLY, DA), (CSAME, SHARED, DA), (CONE, SHARED, DB))):
                await conn.execute(
                    "INSERT INTO rwanda_cell_boundaries (cell_name, sector_name, district_name, geom) "
                    f"VALUES ($1, $2, $3, {_square(i)})", c, s, d,
                )
            for i, (v, c, s, d) in enumerate(
                ((VSAME, CSAME, ONLY, DA), (VSAME, CONE, SHARED, DB), (VONE, CSAME, SHARED, DA))
            ):
                await conn.execute(
                    "INSERT INTO rwanda_village_boundaries (village_name, cell_name, sector_name, district_name, geom) "
                    f"VALUES ($1, $2, $3, $4, {_square(i)})", v, c, s, d,
                )
            yield conn
        finally:
            await tr.rollback()


async def _drawn(conn, result) -> list:
    """The rows the map layer would draw: its stored query, run as the layer runs it."""
    return await conn.fetch(result["query"])


@pytest.mark.asyncio
async def test_one_district_draws_one_polygon() -> None:
    async with seeded() as conn:
        result = await resolve_admin_boundary(conn, {"admin_level": "district", "name": DA.lower()})
        rows = await _drawn(conn, result)
    assert result["status"] == "success" and result["feature_count"] == 1
    assert [r["district"] for r in rows] == [DA]


@pytest.mark.asyncio
async def test_a_shared_sector_name_draws_nothing_and_says_where_each_is() -> None:
    async with seeded() as conn:
        result = await resolve_admin_boundary(conn, {"admin_level": "sector", "name": SHARED})
    assert result["status"] == "ambiguous"
    assert "query" not in result, "nothing is drawn"
    assert result["match_count"] == 2
    assert sorted(c["district"] for c in result["candidates"]) == [DA, DB]


@pytest.mark.asyncio
async def test_the_sector_with_its_district_draws_only_that_sector() -> None:
    async with seeded() as conn:
        result = await resolve_admin_boundary(conn, {"admin_level": "sector", "name": SHARED, "district": DB})
        rows = await _drawn(conn, result)
    assert result["status"] == "success"
    assert result["within"] == {"district": DB}
    assert [(r["sector_name"], r["district_name"]) for r in rows] == [(SHARED, DB)]


@pytest.mark.asyncio
async def test_a_cell_name_twice_in_one_district_needs_its_sector() -> None:
    async with seeded() as conn:
        two = await resolve_admin_boundary(conn, {"admin_level": "cell", "name": CSAME, "district": DA})
        one = await resolve_admin_boundary(conn, {"admin_level": "cell", "name": CSAME, "district": DA, "sector": ONLY})
        rows = await _drawn(conn, one)
    assert two["status"] == "ambiguous" and "query" not in two
    assert two["given"] == {"district": DA}
    assert sorted(c["sector"] for c in two["candidates"]) == sorted([ONLY, SHARED])
    assert one["status"] == "success"
    assert [(r["cell_name"], r["sector_name"]) for r in rows] == [(CSAME, ONLY)]


@pytest.mark.asyncio
async def test_a_village_with_its_cell_sector_and_district_draws_one_village() -> None:
    async with seeded() as conn:
        result = await resolve_admin_boundary(
            conn,
            {"admin_level": "village", "name": VSAME, "cell": CONE, "sector": SHARED, "district": DB},
        )
        rows = await _drawn(conn, result)
    assert result["status"] == "success" and result["feature_count"] == 1
    assert result["within"] == {"cell": CONE, "sector": SHARED, "district": DB}
    assert [(r["village_name"], r["district_name"]) for r in rows] == [(VSAME, DB)]


@pytest.mark.asyncio
async def test_a_unique_village_needs_no_parents() -> None:
    async with seeded() as conn:
        result = await resolve_admin_boundary(conn, {"admin_level": "village", "name": VONE})
        rows = await _drawn(conn, result)
    assert result["status"] == "success"
    assert result["within"] == {"cell": CSAME, "sector": SHARED, "district": DA}
    assert len(rows) == 1


@pytest.mark.asyncio
async def test_a_unit_named_in_a_sector_is_looked_for_below_sectors() -> None:
    """"CONE in SHARED sector": the unit is a cell or village, never a sector called CONE."""
    async with seeded() as conn:
        result = await resolve_admin_boundary(conn, {"admin_level": "auto", "name": CONE, "sector": SHARED})
    assert result["status"] == "success" and result["admin_level"] == "cell"


@pytest.mark.asyncio
async def test_a_misspelt_parent_draws_nothing_and_suggests_the_real_name() -> None:
    async with seeded() as conn:
        result = await resolve_admin_boundary(
            conn, {"admin_level": "sector", "name": SHARED, "district": DA + "x"}
        )
    assert result["status"] == "not_found" and "query" not in result
    assert f"no district named {DA + 'x'!r}" in result["error"]
    assert f"Did you mean {DA}" in result["error"]


@pytest.mark.asyncio
async def test_a_unit_not_in_the_named_parent_says_where_it_is() -> None:
    async with seeded() as conn:
        result = await resolve_admin_boundary(conn, {"admin_level": "sector", "name": ONLY, "district": DB})
    assert result["status"] == "not_found" and "query" not in result
    assert f"no sector named {ONLY!r} in {DB} district" in result["error"]
    assert f"{ONLY} sector ({DA} district)" in result["error"]


@pytest.mark.asyncio
async def test_an_unknown_name_draws_nothing() -> None:
    async with seeded() as conn:
        result = await resolve_admin_boundary(conn, {"admin_level": "village", "name": f"Nowhere{RUN_TAG}"})
    assert result["status"] == "not_found" and "query" not in result
    assert "There is no Rwanda village named" in result["error"]


@pytest.mark.asyncio
async def test_the_cells_of_a_shared_sector_name_ask_which_sector() -> None:
    async with seeded() as conn:
        which = await resolve_admin_boundary(conn, {"admin_level": "cell", "name": "*", "sector": SHARED})
        da = await resolve_admin_boundary(conn, {"admin_level": "cell", "name": "*", "sector": SHARED, "district": DA})
        rows = await _drawn(conn, da)
    assert which["status"] == "ambiguous" and "query" not in which
    assert which["units_of"] == "cell" and which["admin_level"] == "sector"
    assert sorted(c["district"] for c in which["candidates"]) == [DA, DB]
    assert da["status"] == "success" and da["feature_count"] == 1
    assert [(r["cell_name"], r["district_name"]) for r in rows] == [(CSAME, DA)]
