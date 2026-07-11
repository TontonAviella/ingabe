from datetime import datetime, timedelta, timezone

from src.pipelines.sensors import _validated_upload_cursor


NOW = datetime(2026, 7, 10, 6, 0, tzinfo=timezone.utc)


def test_missing_cursor_starts_now_instead_of_replaying_all_layers():
    cursor, reason = _validated_upload_cursor(None, now=NOW, max_backlog_hours=24)

    assert cursor == NOW.isoformat()
    assert reason == "uninitialized"


def test_stale_cursor_is_advanced_to_now():
    stale = (NOW - timedelta(days=7)).isoformat()

    cursor, reason = _validated_upload_cursor(stale, now=NOW, max_backlog_hours=24)

    assert cursor == NOW.isoformat()
    assert reason == "stale"


def test_recent_cursor_is_preserved_for_normal_catchup():
    recent = NOW - timedelta(minutes=15)

    cursor, reason = _validated_upload_cursor(recent.isoformat(), now=NOW, max_backlog_hours=24)

    assert cursor == recent.isoformat()
    assert reason is None
