"""Regression coverage for public satellite scene polling."""

from contextlib import contextmanager
from datetime import datetime, timezone

from dagster import build_sensor_context

from src.pipelines import sensors


# Regression: ISSUE-004 - satellite sensor skipped every run with invalid Sentinel Hub credentials.
# Found by /qa on 2026-07-10
# Report: .gstack/qa-reports/qa-report-localhost-2026-07-09.md
def test_scene_sensor_uses_public_earth_search_and_filters_cursor(monkeypatch) -> None:
    calls: dict = {}

    class FakeSTACService:
        def __init__(self, catalog_name: str):
            calls["catalog_name"] = catalog_name

        def search_imagery(self, **kwargs):
            calls.update(kwargs)
            return {
                "items": [
                    {"id": "old", "datetime": "2026-07-08T10:00:00Z"},
                    {"id": "new", "datetime": "2026-07-09T12:00:00Z"},
                ]
            }

    monkeypatch.setattr(sensors, "STACService", FakeSTACService)

    scenes, latest = sensors._search_new_satellite_scenes(
        "2026-07-09T00:00:00Z",
        now=datetime(2026, 7, 10, tzinfo=timezone.utc),
    )

    assert calls["catalog_name"] == "earth_search"
    assert calls["bbox"] == sensors._RWANDA_BBOX
    assert calls["datetime_range"] == "2026-07-09T00:00:00Z/2026-07-10T00:00:00Z"
    assert calls["max_cloud_cover"] == 100.0
    assert [scene["id"] for scene in scenes] == ["new"]
    assert latest == "2026-07-09T12:00:00Z"


def test_scene_sensor_limits_fresh_database_to_seven_day_lookback(monkeypatch) -> None:
    calls: dict = {}

    class FakeSTACService:
        def __init__(self, catalog_name: str):
            assert catalog_name == "earth_search"

        def search_imagery(self, **kwargs):
            calls.update(kwargs)
            return {"items": []}

    monkeypatch.setattr(sensors, "STACService", FakeSTACService)

    scenes, latest = sensors._search_new_satellite_scenes(
        "2020-01-01T00:00:00Z",
        now=datetime(2026, 7, 10, tzinfo=timezone.utc),
    )

    assert calls["datetime_range"] == "2026-07-03T00:00:00Z/2026-07-10T00:00:00Z"
    assert scenes == []
    assert latest == "2020-01-01T00:00:00Z"


def test_scene_sensor_invalidates_and_notifies_without_starting_warmer(monkeypatch) -> None:
    published = []
    captured = {}

    class FakeRedisClient:
        def scan(self, **kwargs):
            return 0, [b"sat:old-tile"]

        def delete(self, *keys):
            return len(keys)

        def publish(self, channel, notification):
            published.append((channel, notification))

    class FakeRedisResource:
        @contextmanager
        def get_client(self):
            yield FakeRedisClient()

    monkeypatch.setattr(
        sensors,
        "_search_new_satellite_scenes",
        lambda cursor: ([{"id": "new-scene"}], "2026-07-10T08:00:00Z"),
    )
    monkeypatch.setattr(
        sensors,
        "capture_satellite_scene_sensor_success",
        lambda context, **kwargs: captured.update(kwargs),
    )

    sensor_def = sensors.build_satellite_scene_sensor()
    with build_sensor_context(
        cursor="2026-07-09T08:00:00Z",
        resources={"redis": FakeRedisResource()},
    ) as context:
        result = sensor_def.evaluate_tick(context)

    assert result.cursor == "2026-07-10T08:00:00Z"
    assert published and published[0][0] == "ws:satellite"
    assert captured["tiles_invalidated"] == 1
    assert captured["cache_warming_started"] is False


def test_scene_sensor_keeps_cursor_when_update_delivery_fails(monkeypatch) -> None:
    class FailingRedisResource:
        @contextmanager
        def get_client(self):
            raise ConnectionError("redis unavailable")
            yield

    monkeypatch.setattr(
        sensors,
        "_search_new_satellite_scenes",
        lambda cursor: ([{"id": "new-scene"}], "2026-07-10T08:00:00Z"),
    )

    sensor_def = sensors.build_satellite_scene_sensor()
    with build_sensor_context(
        cursor="2026-07-09T08:00:00Z",
        resources={"redis": FailingRedisResource()},
    ) as context:
        result = sensor_def.evaluate_tick(context)

    assert result.cursor == "2026-07-09T08:00:00Z"
    assert result.skip_message == (
        "Satellite update delivery failed; keeping the previous cursor for retry"
    )
