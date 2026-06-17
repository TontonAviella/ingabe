import sys

from src.services import posthog_analytics as analytics


def test_sanitize_properties_drops_sensitive_values():
    props = analytics.sanitize_properties(
        {
            "filename": "private-field-name.tif",
            "message": "what is inside my field?",
            "connection_uri": "postgresql://user:pass@example/db",
            "file_ext": ".tif",
            "duration_ms": 12.34567,
            "viewport_bounds": [29.0, -2.0, 30.0, -1.0],
            "metadata": {"bands": 4},
        }
    )

    assert props["source"] == "backend"
    assert props["service"] == "ingabe"
    assert props["file_ext"] == ".tif"
    assert props["duration_ms"] == 12.346
    assert props["viewport_bounds"] == {"count": 4}
    assert props["metadata"] == {"keys": 1}
    assert "filename" not in props
    assert "message" not in props
    assert "connection_uri" not in props


def test_capture_noops_without_key(monkeypatch):
    analytics.reset_posthog_for_tests()
    monkeypatch.delenv("POSTHOG_API_KEY", raising=False)
    monkeypatch.delenv("POSTHOG_PROJECT_API_KEY", raising=False)
    monkeypatch.delenv("VITE_POSTHOG_KEY", raising=False)
    monkeypatch.delitem(sys.modules, "posthog", raising=False)

    assert analytics.capture_backend_event("backend_test") is False


def test_capture_uses_configured_posthog_module(monkeypatch):
    analytics.reset_posthog_for_tests()
    calls = []

    class FakePosthog:
        api_key = None
        host = None
        disable_geoip = None
        privacy_mode = None
        sync_mode = None
        on_error = None

        @staticmethod
        def capture(**kwargs):
            calls.append(kwargs)

    monkeypatch.setitem(sys.modules, "posthog", FakePosthog)
    monkeypatch.setenv("POSTHOG_API_KEY", "phc_test_backend")
    monkeypatch.setenv("POSTHOG_HOST", "https://us.i.posthog.com")
    monkeypatch.setenv("POSTHOG_BACKEND_DISABLED", "0")

    captured = analytics.capture_backend_event(
        "backend_upload_processing_completed",
        distinct_id="user-1",
        groups={"organization": "org-1"},
        properties={
            "file_ext": ".tif",
            "message": "secret prompt",
            "duration_ms": 25,
        },
    )

    assert captured is True
    assert FakePosthog.api_key == "phc_test_backend"
    assert FakePosthog.host == "https://us.i.posthog.com"
    assert FakePosthog.disable_geoip is True
    assert FakePosthog.privacy_mode is True
    assert calls == [
        {
            "distinct_id": "user-1",
            "event": "backend_upload_processing_completed",
            "properties": {
                "source": "backend",
                "service": "ingabe",
                "file_ext": ".tif",
                "duration_ms": 25,
            },
            "groups": {"organization": "org-1"},
        }
    ]


def test_capture_swallow_sdk_errors(monkeypatch):
    analytics.reset_posthog_for_tests()

    class BrokenPosthog:
        @staticmethod
        def capture(**kwargs):
            raise RuntimeError("network down")

    monkeypatch.setitem(sys.modules, "posthog", BrokenPosthog)
    monkeypatch.setenv("POSTHOG_API_KEY", "phc_test_backend")
    monkeypatch.setenv("POSTHOG_BACKEND_DISABLED", "0")

    assert (
        analytics.capture_backend_event(
            "backend_sage_message_failed",
            distinct_id="user-1",
            properties={"error_type": "RuntimeError"},
        )
        is False
    )
