"""End-to-end cursor regression coverage for the S3 upload sensor."""

from datetime import datetime, timezone

from dagster import build_sensor_context, job

from src.database.models import LAYER_TYPE_POINT_CLOUD, LAYER_TYPE_RASTER, LAYER_TYPE_VECTOR
from src.pipelines import sensors


@job(name="raster_processing_job")
def _raster_job():
    pass


@job(name="vector_processing_job")
def _vector_job():
    pass


_SENSOR = sensors.build_s3_upload_sensor(_raster_job, _vector_job)
_START_CURSOR = "2026-07-10T09:00:00+00:00"


def _as_datetime(value: datetime | str) -> datetime:
    if isinstance(value, datetime):
        return value
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


class _RowsDatabase:
    def __init__(self, rows=()):
        self.rows = list(rows)
        self.calls = []

    def execute_query(self, query, params=()):
        self.calls.append((query, params))
        cursor_time, cursor_layer_id, watermark, limit = params
        visible = [
            row
            for row in self.rows
            if (_as_datetime(row[4]), row[0]) > (cursor_time, cursor_layer_id)
            and _as_datetime(row[4]) <= watermark
        ]
        return sorted(visible, key=lambda row: (_as_datetime(row[4]), row[0]))[:limit]


class _FailingDatabase:
    def execute_query(self, query, params=()):
        raise RuntimeError("database unavailable")


def _evaluate(database, cursor: str):
    with build_sensor_context(
        cursor=cursor,
        resources={"s3": object(), "postgres": database},
    ) as context:
        return _SENSOR.evaluate_tick(context)


def test_empty_evaluation_keeps_cursor_to_avoid_skipping_late_commits(monkeypatch):
    watermark = datetime(2026, 7, 10, 10, 0, tzinfo=timezone.utc)
    database = _RowsDatabase()
    monkeypatch.setattr(sensors, "_utc_now", lambda: watermark)

    result = _evaluate(database, _START_CURSOR)

    query, params = database.calls[0]
    assert "(created_on, layer_id) > (%s, %s)" in query
    assert params == (_as_datetime(_START_CURSOR), "", watermark, 5)
    assert result.run_requests == []
    assert result.cursor == _START_CURSOR


def test_concurrent_insert_after_watermark_is_seen_on_next_tick(monkeypatch):
    first_watermark = datetime(2026, 7, 10, 10, 0, tzinfo=timezone.utc)
    second_watermark = datetime(2026, 7, 10, 10, 1, tzinfo=timezone.utc)
    concurrent_created_on = datetime(2026, 7, 10, 9, 59, 30, tzinfo=timezone.utc)
    now_values = iter((first_watermark, second_watermark))

    class ConcurrentInsertDatabase(_RowsDatabase):
        def execute_query(self, query, params=()):
            visible = super().execute_query(query, params)
            if len(self.calls) == 1:
                self.rows.append(
                    (
                        "raster-concurrent",
                        "Concurrent raster",
                        LAYER_TYPE_RASTER,
                        "raster.tif",
                        concurrent_created_on,
                    )
                )
            return visible

    database = ConcurrentInsertDatabase()
    monkeypatch.setattr(sensors, "_utc_now", lambda: next(now_values))

    first_result = _evaluate(database, _START_CURSOR)
    second_result = _evaluate(database, first_result.cursor)

    assert first_result.cursor == _START_CURSOR
    assert database.calls[1][1][0] == _as_datetime(_START_CURSOR)
    assert [request.run_key for request in second_result.run_requests] == [
        f"raster_raster-concurrent_{concurrent_created_on.isoformat()}"
    ]
    assert sensors._decode_upload_cursor(second_result.cursor) == (
        concurrent_created_on,
        "raster-concurrent",
    )


def test_unsupported_batch_advances_before_later_raster(monkeypatch):
    watermark = datetime(2026, 7, 10, 10, 0, tzinfo=timezone.utc)
    point_cloud_time = datetime(2026, 7, 10, 9, 10, tzinfo=timezone.utc)
    unknown_time = datetime(2026, 7, 10, 9, 20, tzinfo=timezone.utc)
    raster_time = datetime(2026, 7, 10, 9, 30, tzinfo=timezone.utc)
    database = _RowsDatabase(
        [
            ("point-cloud", "Point cloud", LAYER_TYPE_POINT_CLOUD, "points.laz", point_cloud_time),
            ("unsupported", "Unsupported", "table", "table.csv", unknown_time),
            ("later-raster", "Later raster", LAYER_TYPE_RASTER, "later.tif", raster_time),
        ]
    )
    monkeypatch.setenv("S3_UPLOAD_SENSOR_BATCH_SIZE", "2")
    monkeypatch.setattr(sensors, "_utc_now", lambda: watermark)

    first_result = _evaluate(database, _START_CURSOR)
    second_result = _evaluate(database, first_result.cursor)

    assert first_result.run_requests == []
    assert sensors._decode_upload_cursor(first_result.cursor) == (
        unknown_time,
        "unsupported",
    )
    assert [request.run_key for request in second_result.run_requests] == [
        f"raster_later-raster_{raster_time.isoformat()}"
    ]
    assert sensors._decode_upload_cursor(second_result.cursor) == (
        raster_time,
        "later-raster",
    )


def test_supported_rows_emit_retry_safe_run_requests(monkeypatch):
    watermark = datetime(2026, 7, 10, 10, 0, tzinfo=timezone.utc)
    raster_time = datetime(2026, 7, 10, 9, 10, tzinfo=timezone.utc)
    vector_time = datetime(2026, 7, 10, 9, 20, tzinfo=timezone.utc)
    database = _RowsDatabase(
        [
            ("raster-1", "Raster", LAYER_TYPE_RASTER, "raster.tif", raster_time),
            ("vector-1", "Vector", LAYER_TYPE_VECTOR, "vector.fgb", vector_time),
        ]
    )
    monkeypatch.setattr(sensors, "_utc_now", lambda: watermark)

    result = _evaluate(database, _START_CURSOR)
    retry_result = _evaluate(database, _START_CURSOR)

    assert [request.job_name for request in result.run_requests] == [
        "raster_processing_job",
        "vector_processing_job",
    ]
    assert [request.run_key for request in retry_result.run_requests] == [
        request.run_key for request in result.run_requests
    ]
    assert result.run_requests[0].tags["s3_key"] == "raster.tif"
    assert result.run_requests[1].tags["s3_key"] == "vector.fgb"
    assert sensors._decode_upload_cursor(result.cursor) == (vector_time, "vector-1")


def test_same_timestamp_rows_are_paginated_without_loss(monkeypatch):
    watermark = datetime(2026, 7, 10, 10, 0, tzinfo=timezone.utc)
    shared_time = datetime(2026, 7, 10, 9, 30, tzinfo=timezone.utc)
    database = _RowsDatabase(
        [
            (f"raster-{index:02d}", f"Raster {index}", LAYER_TYPE_RASTER, f"{index}.tif", shared_time)
            for index in range(7)
        ]
    )
    monkeypatch.setenv("S3_UPLOAD_SENSOR_BATCH_SIZE", "5")
    monkeypatch.setattr(sensors, "_utc_now", lambda: watermark)

    first_result = _evaluate(database, _START_CURSOR)
    second_result = _evaluate(database, first_result.cursor)

    run_keys = [request.run_key for request in first_result.run_requests + second_result.run_requests]
    assert len(run_keys) == 7
    assert len(set(run_keys)) == 7
    assert sensors._decode_upload_cursor(second_result.cursor) == (
        shared_time,
        "raster-06",
    )


def test_database_error_does_not_advance_cursor(monkeypatch):
    watermark = datetime(2026, 7, 10, 10, 0, tzinfo=timezone.utc)
    monkeypatch.setattr(sensors, "_utc_now", lambda: watermark)

    result = _evaluate(_FailingDatabase(), _START_CURSOR)

    assert result.run_requests == []
    assert result.cursor == _START_CURSOR
