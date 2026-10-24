"""Heat condition from district ground temperature: a month is the mean of its clear-enough 8-day composites, its
normal is the same calendar month since 2003, and the harvest note only claims the link that was measured
(Season A maize, October-December)."""

import uuid
from datetime import date, timedelta

import pytest

from src.database.pool import get_async_db_connection
from src.services import land_surface_temperature as lst

RUN_TAG = uuid.uuid4().hex[:8]


def test_cloudy_composites_are_left_out_of_the_month():
    rows = [(date(2025, 10, 8), 300.0, 0.9), (date(2025, 10, 16), 310.0, 0.5), (date(2025, 10, 24), 350.0, 0.1),
            (date(2025, 11, 1), None, 0.0)]
    assert lst.monthly_means(rows) == {(2025, 10): 305.0}


def test_condition_compares_with_the_same_month_since_2003():
    monthly = {(2002, 10): 400.0, (2003, 10): 300.0, (2004, 10): 302.0, (2005, 10): 304.0, (2005, 11): 290.0}
    c = lst.condition(monthly, 2005, 10)
    assert c is not None
    assert c["normal_c"] == round(302.0 - 273.15, 1)  # 2002 is before the baseline
    assert c["anomaly_c"] == 2.0
    assert c["heat_condition"] == 0.0  # the hottest October on record
    assert c["years_in_normal"] == 3
    assert lst.condition(monthly, 2006, 10) is None  # no reading: unknown, never a guess


def test_no_condition_when_the_years_barely_differ():
    monthly = {(2003, 10): 300.0, (2004, 10): 300.2}
    c = lst.condition(monthly, 2004, 10)
    assert c is not None and c["heat_condition"] is None


def test_the_harvest_link_is_only_claimed_where_it_was_measured():
    assert lst.harvest_note(11, "maize") == lst.LINKED_NOTE
    assert lst.harvest_note(11, None) == lst.LINKED_NOTE
    assert lst.harvest_note(4, "maize") == lst.UNLINKED_NOTE  # Season B: no link found
    assert lst.harvest_note(11, "beans") == lst.UNLINKED_NOTE  # beans: no link found


def test_heat_lines_say_context_not_trigger():
    heat = {"month": "2025-10", "lst_c": 31.2, "normal_c": 29.0, "anomaly_c": 2.2, "heat_condition": 0.1,
            "years_in_normal": 23, "note": lst.LINKED_NOTE}
    text = "\n".join(lst.heat_lines(heat))
    assert "not a trigger" in text
    assert "2.2°C hotter than the usual 29.0°C" in text
    assert lst.LINKED_NOTE in text


@pytest.fixture
async def district():
    name = f"Heat{RUN_TAG}"
    async with get_async_db_connection() as conn:
        for year, k in ((2003, 300.0), (2004, 302.0), (2005, 304.0)):
            for day, clear in ((8, 0.9), (16, 0.8)):
                await conn.execute(
                    "INSERT INTO lst_district_8day (district, composite_start, product, lst_day_k, day_clear_share) "
                    "VALUES ($1, $2, 'MOD', $3, $4)", name, date(year, 10, day), k, clear)
    yield name
    async with get_async_db_connection() as conn:
        await conn.execute("DELETE FROM lst_district_8day WHERE district = $1", name)


@pytest.mark.anyio
async def test_heat_condition_reads_this_month_or_the_one_before(district):
    async with get_async_db_connection() as conn:
        this_month = await lst.heat_condition(conn, district.upper(), date(2005, 10, 30), "maize")
        next_month = await lst.heat_condition(conn, district, date(2005, 11, 5), "maize")
        too_late = await lst.heat_condition(conn, district, date(2005, 12, 5), "maize")
        many = await lst.heat_conditions(conn, [district, f"Nowhere{RUN_TAG}"], date(2005, 10, 30))
    assert this_month is not None and this_month["month"] == "2005-10" and this_month["heat_condition"] == 0.0
    assert this_month["note"] == lst.LINKED_NOTE and this_month["source"] == lst.SOURCE
    assert next_month is not None and next_month["month"] == "2005-10"
    assert too_late is None
    assert list(many) == [district]


class _DroughtConn:
    """drought_cache answers with one district; lst_district_8day with a clear October 2003, 2004, 2025, or fails."""

    def __init__(self, heat_fails: bool = False):
        self.heat_fails = heat_fails

    async def fetch(self, sql, *args):
        if "drought_cache" in sql:
            return [{"district": "Bugesera", "drought_status": "normal", "current_vci": 55.0, "latest_ndvi": 0.5,
                     "latest_ndwi": 0.1, "drought_period_count": 0, "description": "Normal"}]
        if self.heat_fails:
            raise RuntimeError("relation lst_district_8day does not exist")
        return [{"d": "bugesera", "composite_start": date(y, 10, 8), "lst_day_k": k, "day_clear_share": 0.9}
                for y, k in ((2003, 300.0), (2004, 302.0), (2025, 304.0))]


async def _drought(monkeypatch, conn) -> dict:
    from src.routes import message_routes
    import src.services.legacy_tool_shim as shim
    from src.services.legacy_tool_shim import LEGACY_HANDLERS, LegacyToolContext

    async def no_connection(*args, **kwargs):
        return None

    monkeypatch.setattr(message_routes, "_ensure_rwanda_postgis_connection", no_connection)
    monkeypatch.setattr(shim, "date", type("Today", (), {"today": staticmethod(lambda: date(2025, 10, 20))}))
    ctx = LegacyToolContext(user_id="u", partner_id="p", conversation_id=1, map_id="MTESTAAAAAAA",
                            project_id="PTESTBBBBBBB", conn=conn, arguments={"district": "Bugesera"})
    return await LEGACY_HANDLERS["get_drought_status"](ctx)


@pytest.mark.anyio
async def test_sage_drought_answer_carries_heat_as_context(monkeypatch):
    result = await _drought(monkeypatch, _DroughtConn())
    district = result["districts"][0]
    assert district["drought_status"] == "normal"
    assert district["heat"]["month"] == "2025-10" and district["heat"]["heat_condition"] == 0.0 and district["heat"]["note"] == lst.LINKED_NOTE
    assert "never a drought verdict or an insurance trigger" in result["heat_note"]


@pytest.mark.anyio
async def test_a_failed_heat_read_leaves_the_drought_answer_whole(monkeypatch):
    result = await _drought(monkeypatch, _DroughtConn(heat_fails=True))
    assert result["status"] == "success" and result["districts"][0]["drought_status"] == "normal"
    assert "heat" not in result["districts"][0] and "heat_note" not in result


@pytest.mark.postgres
def test_the_weekly_run_adds_new_composites_and_rereads_the_last_two(monkeypatch):
    from dagster import materialize

    from src.pipelines.resources import PostgresResource
    from src.pipelines.rwanda_assets import LST_REREAD_DAYS, weekly_land_surface_temperature

    name = f"Weekly{RUN_TAG}"
    postgres = PostgresResource.from_env()
    with postgres.get_sync_connection() as conn, conn.cursor() as cur:
        cur.execute("INSERT INTO rwanda_district_boundaries (district, geom) VALUES "
                    "(%s, ST_GeomFromText('POLYGON((30 -2, 30.1 -2, 30.1 -1.9, 30 -1.9, 30 -2))', 4326))", (name,))
        cur.execute("INSERT INTO lst_district_8day (district, composite_start, product, lst_day_k, day_clear_share) "
                    "VALUES (%s, '2099-01-01', 'MOD', 290, 0.9)", (name,))  # the newest stored composite
        conn.commit()
    asked = {}
    starts = ["2098-12-16", "2098-12-24", "2099-01-01", "2099-01-09", "2099-01-17"]

    def composites(date_from, date_to):
        asked["from"] = date_from
        return {(d, p): [{"id": f"{p}11A2.{d}"}] for d in starts for p in ("MOD",)}

    def district_means(items, districts):
        if items[0]["id"].endswith("2098-12-16"):
            raise RuntimeError("tile read timed out")
        assert [d for d, _ in districts if d == name] == [name]
        return [{"district": name, "lst_day_k": 300.0, "day_clear_share": 0.8, "lst_night_k": 285.0,
                 "night_clear_share": 0.7}]

    from src.services import land_surface_temperature as lst
    monkeypatch.setattr(lst, "composites", composites)
    monkeypatch.setattr(lst, "district_means", district_means)
    try:
        run = materialize([weekly_land_surface_temperature], resources={"postgres": postgres})
        assert run.success
        out = run.output_for_node("weekly_land_surface_temperature")
        assert out["composites_written"] == 4 and out["composites_failed"] == 1  # 1 of 5 is under a quarter
        assert asked["from"] == date(2099, 1, 1) - timedelta(days=LST_REREAD_DAYS)
        with postgres.get_sync_connection() as conn, conn.cursor() as cur:
            cur.execute("SELECT composite_start::text, lst_day_k FROM lst_district_8day WHERE district = %s "
                        "ORDER BY 1", (name,))
            assert cur.fetchall() == [(d, 300.0) for d in starts[1:]]  # 2099-01-01 re-read and updated
    finally:
        with postgres.get_sync_connection() as conn, conn.cursor() as cur:
            cur.execute("DELETE FROM lst_district_8day WHERE district = %s", (name,))
            cur.execute("DELETE FROM rwanda_district_boundaries WHERE district = %s", (name,))
            conn.commit()
