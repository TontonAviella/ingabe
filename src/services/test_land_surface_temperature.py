"""Heat condition from district ground temperature: a month is the mean of its clear-enough 8-day composites, each
satellite is compared with its own normal (the same month in the other years since 2003), and the harvest note only
claims the link that was measured (Season A maize, October-December)."""

import uuid
from datetime import date, timedelta

import pytest

from src.database.pool import get_async_db_connection
from src.services import land_surface_temperature as lst

RUN_TAG = uuid.uuid4().hex[:8]
YEARS = range(2003, 2016)  # 12 earlier years before 2015, above MIN_NORMAL_YEARS


def _octobers(product: str, kelvin_by_year: dict[int, float]) -> list[lst.Row]:
    return [(product, date(y, 10, d), k, 0.9) for y, k in kelvin_by_year.items() for d in (8, 16)]


def _rising() -> dict[int, float]:
    return {y: 300.0 + (y - 2003) * 0.1 for y in YEARS}


def test_cloudy_composites_are_left_out_of_the_month():
    rows = [("MOD", date(2025, 10, 8), 300.0, 0.9), ("MOD", date(2025, 10, 16), 310.0, 0.5),
            ("MOD", date(2025, 10, 24), 350.0, 0.1), ("MOD", date(2025, 11, 1), None, 0.0)]
    assert lst.monthly_means(rows) == {("MOD", 2025, 10): (305.0, 2)}


def test_condition_compares_with_the_other_years_since_2003():
    rows = _octobers("MOD", {2002: 400.0, **_rising()})
    c = lst.condition(lst.monthly_means(rows), 2015, 10)
    assert c is not None
    assert c["years_in_normal"] == 12  # 2003-2014: 2002 is before the baseline, 2015 is the month itself
    assert c["normal_c"] == round(300.55 - 273.15, 1)
    assert c["heat_condition"] == 0.0  # the hottest October on record
    assert c["composites"] == 2 and c["satellites"] == ["MOD"]
    assert lst.condition(lst.monthly_means(rows), 2016, 10) is None  # no reading: unknown, never a guess


def test_too_few_years_give_no_normal():
    rows = _octobers("MOD", {y: 300.0 + y % 3 for y in range(2003, 2010)})
    assert lst.condition(lst.monthly_means(rows), 2009, 10) is None


def test_no_condition_when_the_years_barely_differ():
    rows = _octobers("MOD", {y: 300.0 + (y % 2) * 0.1 for y in YEARS})
    c = lst.condition(lst.monthly_means(rows), 2015, 10)
    assert c is not None and c["heat_condition"] is None


def test_each_satellite_is_compared_with_its_own_normal():
    # Aqua passes after noon and reads 4 K hotter every year; this October only Aqua saw the ground.
    terra = _octobers("MOD", {y: 300.0 for y in YEARS if y < 2015})
    aqua = _octobers("MYD", {y: 304.0 for y in YEARS})
    c = lst.condition(lst.monthly_means(terra + aqua), 2015, 10)
    assert c is not None and c["satellites"] == ["MYD"]
    assert c["anomaly_c"] == 0.0  # mixed into one normal, it would read 2 K hotter than usual


def test_the_harvest_link_is_only_claimed_where_it_was_measured():
    assert lst.harvest_note(11, "maize") == lst.LINKED_NOTE
    assert lst.harvest_note(11, None) == lst.LINKED_NOTE
    assert lst.harvest_note(4, "maize") == lst.UNLINKED_NOTE  # Season B: no link found
    assert lst.harvest_note(11, "beans") == lst.UNLINKED_NOTE  # beans: no link found


def test_heat_lines_name_the_district_and_say_context_not_trigger():
    heat = {"district": "Musanze", "month": "2025-10", "lst_c": 31.2, "normal_c": 29.0, "anomaly_c": 2.2,
            "heat_condition": 0.1, "years_in_normal": 22, "composites": 1, "note": lst.LINKED_NOTE}
    text = "\n".join(lst.heat_lines(heat))
    assert "for all of Musanze district; context, not a trigger" in text
    assert "2.2°C hotter than the usual 29.0°C" in text and "from 1 8-day reading so far" in text
    assert lst.LINKED_NOTE in text


def test_composites_follow_every_page(monkeypatch):
    def item(day, product):
        return {"id": f"{product}11A2.A{day}.h20v09", "properties": {"start_datetime": f"{day}T00:00:00Z"}}

    pages = {
        lst._STAC: {"features": [item("2025-10-08", "MOD"), item("2025-10-08", "MYD")],
                    "links": [{"rel": "next", "href": "https://next.example/search", "method": "POST",
                               "body": {"token": "page2"}}]},
        "https://next.example/search": {"features": [item("2025-10-16", "MOD")], "links": []},
    }
    bodies = []

    def get_json(url, body=None):
        bodies.append(body)
        return pages[url]

    monkeypatch.setattr(lst, "_get_json", get_json)
    found = lst.composites(date(2025, 10, 1), date(2025, 10, 31))
    assert sorted(found) == [("2025-10-08", "MOD"), ("2025-10-08", "MYD"), ("2025-10-16", "MOD")]
    assert bodies[1]["token"] == "page2" and bodies[1]["collections"] == ["modis-11A2-061"]


@pytest.fixture
async def district():
    name = f"Heat{RUN_TAG}"
    async with get_async_db_connection() as conn:
        for product, start, k, clear in _octobers("MOD", _rising()):
            await conn.execute(
                "INSERT INTO lst_district_8day (district, composite_start, product, lst_day_k, day_clear_share) "
                "VALUES ($1, $2, $3, $4, $5)", name, start, product, k, clear)
    yield name
    async with get_async_db_connection() as conn:
        await conn.execute("DELETE FROM lst_district_8day WHERE district = $1", name)


@pytest.mark.anyio
async def test_heat_condition_reads_this_month_or_the_one_before(district):
    async with get_async_db_connection() as conn:
        this_month = await lst.heat_condition(conn, f" {district.upper()} ", date(2015, 10, 30), "maize")
        next_month = await lst.heat_condition(conn, district, date(2015, 11, 5), "maize")
        too_late = await lst.heat_condition(conn, district, date(2015, 12, 5), "maize")
        many = await lst.heat_conditions(conn, [district, f"Nowhere{RUN_TAG}"], date(2015, 10, 30))
    assert this_month is not None and this_month["month"] == "2015-10" and this_month["heat_condition"] == 0.0
    assert this_month["note"] == lst.LINKED_NOTE and this_month["source"] == lst.SOURCE
    assert next_month is not None and next_month["month"] == "2015-10"
    assert too_late is None
    assert list(many) == [district]


class _DroughtConn:
    """drought_cache answers with one district; lst_district_8day with clear Octobers 2003-2015, or fails."""

    def __init__(self, heat_fails: bool = False):
        self.heat_fails = heat_fails

    async def fetch(self, sql, *args):
        if "drought_cache" in sql:
            return [{"district": "Bugesera", "drought_status": "normal", "current_vci": 55.0, "latest_ndvi": 0.5,
                     "latest_ndwi": 0.1, "drought_period_count": 0, "description": "Normal"}]
        if self.heat_fails:
            raise RuntimeError("relation lst_district_8day does not exist")
        return [{"d": "bugesera", "product": p, "composite_start": d, "lst_day_k": k, "day_clear_share": c}
                for p, d, k, c in _octobers("MOD", _rising())]


async def _drought(monkeypatch, conn) -> dict:
    from src.routes import message_routes
    import src.services.legacy_tool_shim as shim
    from src.services.legacy_tool_shim import LEGACY_HANDLERS, LegacyToolContext

    async def no_connection(*args, **kwargs):
        return None

    monkeypatch.setattr(message_routes, "_ensure_rwanda_postgis_connection", no_connection)
    monkeypatch.setattr(shim, "date", type("Today", (), {"today": staticmethod(lambda: date(2015, 10, 20))}))
    ctx = LegacyToolContext(user_id="u", partner_id="p", conversation_id=1, map_id="MTESTAAAAAAA",
                            project_id="PTESTBBBBBBB", conn=conn, arguments={"district": "Bugesera"})
    return await LEGACY_HANDLERS["get_drought_status"](ctx)


@pytest.mark.anyio
async def test_sage_drought_answer_carries_heat_as_context(monkeypatch):
    result = await _drought(monkeypatch, _DroughtConn())
    district = result["districts"][0]
    assert district["drought_status"] == "normal"
    assert district["heat"]["month"] == "2015-10" and district["heat"]["heat_condition"] == 0.0
    assert district["heat"]["note"] == lst.LINKED_NOTE
    assert "never a drought verdict or an insurance trigger" in result["heat_note"]


@pytest.mark.anyio
async def test_a_failed_heat_read_leaves_the_drought_answer_whole(monkeypatch):
    result = await _drought(monkeypatch, _DroughtConn(heat_fails=True))
    assert result["status"] == "success" and result["districts"][0]["drought_status"] == "normal"
    assert "heat" not in result["districts"][0] and "heat_note" not in result


@pytest.mark.postgres
def test_the_weekly_run_adds_new_composites_and_retries_failed_ones(monkeypatch):
    from dagster import materialize

    from src.pipelines.resources import PostgresResource
    from src.pipelines.rwanda_assets import LST_RETRY_DAYS, weekly_land_surface_temperature

    name = f"Weekly{RUN_TAG}"
    newest = date(2099, 1, 1)
    postgres = PostgresResource.from_env()
    with postgres.get_sync_connection() as conn, conn.cursor() as cur:
        cur.execute("INSERT INTO rwanda_district_boundaries (district, geom) VALUES "
                    "(%s, ST_GeomFromText('POLYGON((30 -2, 30.1 -2, 30.1 -1.9, 30 -1.9, 30 -2))', 4326))", (name,))
        for stored in ("2098-11-01", newest.isoformat()):  # an old composite already read, and the newest
            cur.execute("INSERT INTO lst_district_8day (district, composite_start, product, lst_day_k, day_clear_share) "
                        "VALUES (%s, %s, 'MOD', 290, 0.9)", (name, stored))
        conn.commit()
    asked = {}
    starts = ["2098-11-01", "2098-11-09", "2098-12-24", "2099-01-01", "2099-01-09", "2099-01-17"]
    failing = {"2099-01-17"}

    def composites(date_from, date_to):
        asked["from"] = date_from
        return {(d, "MOD"): [{"id": f"MOD11A2.{d}"}] for d in starts}

    def district_means(items, districts):
        if items[0]["id"].split(".")[1] in failing:
            raise RuntimeError("tile read timed out")
        assert [d for d, _ in districts if d == name] == [name]
        return [{"district": name, "lst_day_k": 300.0, "day_clear_share": 0.8, "lst_night_k": 285.0,
                 "night_clear_share": 0.7}]

    monkeypatch.setattr(lst, "composites", composites)
    monkeypatch.setattr(lst, "district_means", district_means)

    def stored_rows():
        with postgres.get_sync_connection() as conn, conn.cursor() as cur:
            cur.execute("SELECT composite_start::text, lst_day_k FROM lst_district_8day WHERE district = %s "
                        "ORDER BY 1", (name,))
            return cur.fetchall()

    try:
        run = materialize([weekly_land_surface_temperature], resources={"postgres": postgres})
        assert run.success
        out = run.output_for_node("weekly_land_surface_temperature")
        assert asked["from"] == newest - timedelta(days=LST_RETRY_DAYS)
        # 2098-11-01 was read before and is outside the 16-day re-read window: left alone. 2098-11-09 and
        # 2098-12-24 were missing, 2099-01-01 and -09 are new or re-read; 2099-01-17 fails (1 of 5, under a quarter).
        assert out["composites_written"] == 4 and out["composites_failed"] == 1
        assert stored_rows() == [("2098-11-01", 290.0)] + [(d, 300.0) for d in starts[1:5]]

        failing.clear()  # next week the read works: the missing composite is tried again
        run = materialize([weekly_land_surface_temperature], resources={"postgres": postgres})
        assert run.success
        assert ("2099-01-17", 300.0) in stored_rows()
    finally:
        with postgres.get_sync_connection() as conn, conn.cursor() as cur:
            cur.execute("DELETE FROM lst_district_8day WHERE district = %s", (name,))
            cur.execute("DELETE FROM rwanda_district_boundaries WHERE district = %s", (name,))
            conn.commit()
