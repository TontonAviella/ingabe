"""The nightly NDVI of outlined fields: which fields, which pass, and what a night keeps."""

from __future__ import annotations

import json
from datetime import date
from typing import Any

from src.services import field_ndvi as fn
from src.services import satellite_ahead

TODAY = date(2026, 11, 24)
SQUARE = {"type": "Polygon", "coordinates": [[[30.42, -1.70], [30.421, -1.70], [30.421, -1.699],
                                              [30.42, -1.699], [30.42, -1.70]]]}
READY = {"cog_status": "ready", "cog_key": "cog/cyampirita.tif"}


def _pass(day: str, clear: int, inside: int = 100, mean: float = 0.61) -> dict[str, Any]:
    return {"date_from": f"{day}T08:20:31Z", "date_to": f"{day}T08:20:31Z",
            "ndvi": {"mean": mean, "std": 0.05, "min": 0.4, "max": 0.8, "valid_pixels": clear,
                     "inside_pixels": inside}}


class _stats:
    """get_field_stats with these passes, remembering what it was asked."""

    def __init__(self, *intervals: dict[str, Any]):
        self.intervals = list(intervals)
        self.calls: list[dict[str, Any]] = []

    def __call__(self, **kwargs: Any) -> dict[str, Any]:
        self.calls.append(kwargs)
        return {"source": "deafrica_stac", "intervals": self.intervals}


def _field(number: int = 1, name: str | None = None) -> fn.Field:
    rows = [{"number": number, "geometry": SQUARE, "area_ha": 1.2, **({"name": name} if name else {})}]
    return fn.fields_of("Lcyampirita1", rows)[0]


def test_an_outlined_field_on_a_farm_photo_gets_a_row():
    """Regression (2026-11-24): the night looked only at vector layers tagged rwanda_parcels, which nothing sets,
    so ndvi_parcel_cache stayed empty for every outlined field. A field kept on a farm photo now gets its row."""
    photos = fn.photo_layers([("Lcyampirita1", [30.41, -1.71, 30.43, -1.69], READY),
                              ("Lmaking00001", [30.5, -1.8, 30.51, -1.79], {"cog_status": "processing"}),
                              ("Lnometa00001", [30.6, -1.8, 30.61, -1.79], json.dumps(READY)),
                              ("Lnobounds001", None, READY)])
    assert photos == [("Lcyampirita1", "cog/cyampirita.tif"), ("Lnometa00001", "cog/cyampirita.tif")]

    kept: list[dict[str, Any]] = []
    night = fn.run([_field(7)], TODAY, _stats(_pass("2026-11-20", clear=97)), kept.append)

    assert night["status"] == "ok" and night["kept"] == 1 and len(kept) == 1
    row = kept[0]
    assert row["layer_id"] == "Lcyampirita1" and row["parcel_name"] == "Field 7"
    assert row["pass_date"] == date(2026, 11, 20) and row["week_start"] == date(2026, 11, 16)
    assert row["mean_ndvi"] == 0.61 and row["clear_share"] == 0.97 and row["area_ha"] == 1.2
    assert set(fn.ROW_COLUMNS) == set(row)


def test_the_field_is_read_inside_its_outline_over_the_last_30_days():
    stats = _stats(_pass("2026-11-20", clear=100))
    fn.measure(_field(), TODAY, stats)
    call = stats.calls[0]
    assert call["clip_to_geometry"] and call["drop_water"] and call["geometry"] == SQUARE
    assert (call["date_from"], call["date_to"]) == ("2026-10-25", "2026-11-24")


def test_the_clearest_pass_is_said_not_the_newest_cloudy_one():
    stats = _stats(_pass("2026-11-05", clear=100, mean=0.70), _pass("2026-11-22", clear=30, mean=0.20),
                   _pass("2026-11-17", clear=90, mean=0.65))
    row = fn.measure(_field(), TODAY, stats)["row"]
    assert row["pass_date"] == date(2026, 11, 5) and row["mean_ndvi"] == 0.70


def test_of_equally_clear_passes_the_newest_is_said():
    stats = _stats(_pass("2026-11-05", clear=100, mean=0.70), _pass("2026-11-20", clear=100, mean=0.66),
                   _pass("2026-11-10", clear=100, mean=0.68))
    assert fn.measure(_field(), TODAY, stats)["row"]["pass_date"] == date(2026, 11, 20)


def test_a_field_no_pass_saw_clear_enough_is_cloudy_and_keeps_nothing():
    kept: list[dict[str, Any]] = []
    night = fn.run([_field()], TODAY, _stats(_pass("2026-11-05", clear=49), _pass("2026-11-20", clear=0)),
                   kept.append)
    assert kept == [] and night["cloudy"] == 1 and night["status"] == "ok"


def test_no_pass_too_small_and_failed_are_told_apart():
    assert fn.measure(_field(), TODAY, _stats())["outcome"] == "no_pass"
    assert fn.measure(_field(), TODAY, _stats(_pass("2026-11-20", clear=0, inside=0)))["outcome"] == "too_small"
    assert fn.measure(_field(), TODAY, lambda **_: {"error": "DE Africa scene search failed"})["outcome"] == "failed"


def test_a_night_where_every_field_failed_is_a_failure_not_a_quiet_night():
    def broken(**_: Any) -> dict[str, Any]:
        raise RuntimeError("the band could not be read")

    night = fn.run([_field(1), _field(2)], TODAY, broken, lambda row: None)
    assert night["status"] == "failed" and night["failed_count"] == 2


def test_one_failed_field_does_not_stop_the_others():
    def flaky(**kwargs: Any) -> dict[str, Any]:
        if kwargs["geometry"] is SQUARE and flaky.first:  # type: ignore[attr-defined]
            flaky.first = False  # type: ignore[attr-defined]
            raise RuntimeError("timeout")
        return {"intervals": [_pass("2026-11-20", clear=100)]}

    flaky.first = True  # type: ignore[attr-defined]
    kept: list[dict[str, Any]] = []
    night = fn.run([_field(1), _field(2)], TODAY, flaky, kept.append)
    assert night["status"] == "ok" and night["kept"] == 1 and night["failed_count"] == 1
    assert night["newest_pass"] == "2026-11-20"


def test_no_fields_is_said_as_such():
    assert fn.run([], TODAY, _stats(), lambda row: None)["status"] == "no_fields"


def test_a_field_keeps_its_id_and_takes_its_own_name():
    assert _field(3).parcel_id == _field(3).parcel_id != _field(4).parcel_id
    assert _field(3, name="Maize A").name == "Maize A · field 3"


def test_the_fields_come_from_the_photos_the_night_already_goes_through():
    assert "p.industry = ANY(%s)" in satellite_ahead.PHOTOS_SQL and "m.soft_deleted_at IS NULL" in satellite_ahead.PHOTOS_SQL


def test_the_row_written_matches_the_insert():
    assert fn.INSERT_SQL.count("%s") == len(fn.ROW_COLUMNS)
    assert "pass_date" in fn.INSERT_SQL and "clear_share" in fn.INSERT_SQL


class _Cursor:
    def __init__(self, written: list[tuple[str, tuple[Any, ...]]]):
        self.written = written

    def __enter__(self) -> "_Cursor":
        return self

    def __exit__(self, *_: Any) -> None:
        return None

    def execute(self, sql: str, params: tuple[Any, ...]) -> None:
        self.written.append((sql, params))


class _Connection:
    def __init__(self, written: list[tuple[str, tuple[Any, ...]]]):
        self.written = written

    def __enter__(self) -> "_Connection":
        return self

    def __exit__(self, *_: Any) -> None:
        return None

    def cursor(self) -> _Cursor:
        return _Cursor(self.written)

    def commit(self) -> None:
        return None


def test_the_nightly_job_writes_the_kept_field_into_the_store(monkeypatch):
    """Regression (2026-11-24), end to end through the pipeline asset with storage and the satellite faked: a field
    kept on a farm photo ends the night as a row in ndvi_parcel_cache, not as "no_parcels"."""
    from dagster import materialize

    from src.pipelines import rwanda_assets
    from src.pipelines.resources import PostgresResource
    from src.services import deafrica_stac, field_taps
    import src.utils

    written: list[tuple[str, tuple[Any, ...]]] = []

    class FakePostgres(PostgresResource):
        def execute_query(self, query: str, params: tuple = ()) -> list[tuple]:
            assert query == satellite_ahead.PHOTOS_SQL
            return [("Lcyampirita1", [30.41, -1.71, 30.43, -1.69], READY)]

        def get_sync_connection(self):  # type: ignore[override]
            return _Connection(written)

    async def kept(s3: Any, bucket: str, photo_key: str, layer_id: str) -> list[dict[str, Any]]:
        assert (photo_key, layer_id) == ("cog/cyampirita.tif", "Lcyampirita1")
        return [{"number": 7, "geometry": SQUARE, "area_ha": 1.2}]

    async def no_s3() -> None:
        return None

    class FakeDEAfrica:
        def get_field_stats(self, **_: Any) -> dict[str, Any]:
            return {"intervals": [_pass("2026-11-20", clear=95), _pass("2026-11-22", clear=10)]}

    monkeypatch.setattr(field_taps, "kept", kept)
    monkeypatch.setattr(src.utils, "get_async_s3_client", no_s3)
    monkeypatch.setattr(src.utils, "get_bucket_name", lambda: "bucket")
    monkeypatch.setattr(deafrica_stac, "get_deafrica_service", lambda: FakeDEAfrica())
    monkeypatch.setattr(fn, "today_utc", lambda: TODAY)

    run = materialize([rwanda_assets.nightly_parcel_ndvi],
                      resources={"postgres": FakePostgres(host="h", port=5432, database="d", user="u", password="p")})
    assert run.success
    result = run.output_for_node("nightly_parcel_ndvi")

    assert result["status"] == "ok" and result["kept"] == 1 and result["photos"] == 1
    inserts = [params for sql, params in written if sql.startswith("INSERT INTO ndvi_parcel_cache")]
    assert len(inserts) == 1
    row = dict(zip(fn.ROW_COLUMNS, inserts[0]))
    assert row["parcel_name"] == "Field 7" and row["pass_date"] == date(2026, 11, 20) and row["clear_share"] == 0.95
