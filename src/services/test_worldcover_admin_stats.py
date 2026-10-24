"""Land cover per district, sector and cell: the monthly Dagster asset writes worldcover_admin_stats in Postgres and
Sage's query_worldcover_stats reads it there (2026-10-25: the asset wrote DuckDB, the tool read Postgres, and the
table existed nowhere, so the tool could never answer)."""

import json
import uuid

import numpy as np
import pytest

from src.database.pool import get_async_db_connection

RUN_TAG = uuid.uuid4().hex[:8]
DISTRICT, OTHER, SECTOR, CELL = f"Dist{RUN_TAG}", f"Other{RUN_TAG}", f"Sect{RUN_TAG}", f"Cell{RUN_TAG}"
# A 0.02° x 0.02° square near Kigali, in the three boundary tables.
WEST, SOUTH, EAST, NORTH = 30.00, -2.00, 30.02, -1.98
SQUARE = f"POLYGON(({WEST} {SOUTH}, {EAST} {SOUTH}, {EAST} {NORTH}, {WEST} {NORTH}, {WEST} {SOUTH}))"


async def _call(**arguments) -> dict:
    from src.services.legacy_tool_shim import LEGACY_HANDLERS, LegacyToolContext

    async with get_async_db_connection() as conn:
        ctx = LegacyToolContext(user_id="u", partner_id="p", conversation_id=1, map_id="MTESTAAAAAAA",
                                project_id="PTESTBBBBBBB", conn=conn, arguments=arguments)
        return await LEGACY_HANDLERS["query_worldcover_stats"](ctx)


@pytest.fixture
async def stats():
    rows = [("district", DISTRICT, DISTRICT, None, 5, "crops", 1000, 10.0),
            ("district", DISTRICT, DISTRICT, None, 2, "trees", 3000, 30.0),
            ("sector", SECTOR, DISTRICT, SECTOR, 5, "crops", 400, 4.0),
            ("sector", SECTOR, OTHER, SECTOR, 5, "crops", 900, 9.0)]  # same sector name in another district
    async with get_async_db_connection() as conn:
        await conn.executemany(
            "INSERT INTO worldcover_admin_stats (admin_level, admin_name, district_name, sector_name, lulc_year, "
            "class_value, class_name, pixel_count, area_hectares) VALUES ($1, $2, $3, $4, 2024, $5, $6, $7, $8)", rows)
    yield
    async with get_async_db_connection() as conn:
        await conn.execute("DELETE FROM worldcover_admin_stats WHERE district_name = ANY($1::text[])", [DISTRICT, OTHER])


@pytest.mark.anyio
async def test_sage_reads_land_cover_for_a_district(stats):
    result = await _call(district=DISTRICT.upper())
    assert result["status"] == "success" and result["count"] == 2
    assert [r["class_name"] for r in result["data"]] == ["trees", "crops"]  # largest first
    assert result["source"].startswith("ESRI 10 m annual land use/land cover 2024")


@pytest.mark.anyio
async def test_a_sector_is_narrowed_by_its_district(stats):
    both = await _call(sector=SECTOR)
    assert both["count"] == 2  # the same name in two districts
    one = await _call(sector=SECTOR, district=DISTRICT)
    assert one["count"] == 1 and one["data"][0]["area_hectares"] == 4.0


@pytest.mark.anyio
async def test_an_unknown_area_says_why_there_is_nothing(stats):
    result = await _call(district=f"Nowhere{RUN_TAG}")
    assert result["status"] == "success" and result["count"] == 0
    assert "did not match" in result["note"]


@pytest.mark.postgres
def test_the_monthly_run_writes_land_cover_to_postgres(monkeypatch):
    import rasterio
    from dagster import materialize
    from rasterio.io import MemoryFile
    from rasterio.transform import from_bounds

    from src import worldcover
    from src.pipelines.resources import PostgresResource
    from src.pipelines.rwanda_assets import worldcover_zonal_stats

    name = f"Run{RUN_TAG}"
    postgres = PostgresResource.from_env()
    bbox = (WEST, SOUTH, EAST, NORTH)
    with postgres.get_sync_connection() as conn, conn.cursor() as cur:
        for table, cols in (("rwanda_district_boundaries", "district"),
                            ("rwanda_sector_boundaries", "sector_name, district_name"),
                            ("rwanda_cell_boundaries", "cell_name, sector_name, district_name")):
            values = {"district": [name], "sector_name, district_name": [name, name],
                      "cell_name, sector_name, district_name": [name, name, name]}[cols]
            cur.execute(f"INSERT INTO {table} ({cols}, geom, bbox_west, bbox_south, bbox_east, bbox_north) "
                        f"VALUES ({', '.join(['%s'] * len(values))}, ST_GeomFromText(%s, 4326), %s, %s, %s, %s)",
                        (*values, SQUARE, *bbox))
        conn.commit()

    # A synthetic land-cover map over a wider area: crops (5) in the west half, trees (2) in the east half.
    data = np.zeros((400, 400), dtype="uint8")
    data[:, :200], data[:, 200:] = 5, 2
    memfile = MemoryFile()
    with memfile.open(driver="GTiff", width=400, height=400, count=1, dtype="uint8", crs="EPSG:4326",
                      transform=from_bounds(WEST - 0.02, SOUTH - 0.02, EAST + 0.02, NORTH + 0.02, 400, 400)) as dst:
        dst.write(data, 1)

    def fake_open():
        ds = memfile.open()
        return [(ds, rasterio.open(memfile.name))]

    monkeypatch.setattr(worldcover, "open_rwanda_datasets_warped", fake_open)
    try:
        run = materialize([worldcover_zonal_stats], resources={"postgres": postgres})
        assert run.success
        with postgres.get_sync_connection() as conn, conn.cursor() as cur:
            cur.execute("SELECT admin_level, class_name, round(area_hectares) FROM worldcover_admin_stats "
                        "WHERE admin_name = %s ORDER BY 1, 2", (name,))
            got = cur.fetchall()
        levels = {lvl for lvl, _, _ in got}
        assert levels == {"district", "sector", "cell"}
        by_class = {cls: ha for lvl, cls, ha in got if lvl == "district"}
        assert set(by_class) == {worldcover.WORLDCOVER_CLASSES[5], worldcover.WORLDCOVER_CLASSES[2]}
        crops, trees = by_class[worldcover.WORLDCOVER_CLASSES[5]], by_class[worldcover.WORLDCOVER_CLASSES[2]]
        assert abs(crops - trees) <= 0.1 * max(crops, trees)  # the square is half crops, half trees
        assert json.dumps(run.output_for_node("worldcover_zonal_stats"))  # plain, serialisable result
    finally:
        with postgres.get_sync_connection() as conn, conn.cursor() as cur:
            cur.execute("DELETE FROM worldcover_admin_stats WHERE admin_name = %s OR district_name = %s", (name, name))
            for table, col in (("rwanda_cell_boundaries", "cell_name"), ("rwanda_sector_boundaries", "sector_name"),
                               ("rwanda_district_boundaries", "district")):
                cur.execute(f"DELETE FROM {table} WHERE {col} = %s", (name,))
            conn.commit()
        memfile.close()
