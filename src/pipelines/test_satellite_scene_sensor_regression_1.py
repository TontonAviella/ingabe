"""Regression coverage for public satellite scene polling."""

from datetime import datetime, timezone

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
