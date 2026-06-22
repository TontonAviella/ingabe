from __future__ import annotations

import pytest

from src.pipelines import posthog_observability as observability


@pytest.fixture(autouse=True)
def _disable_local_evidence_writes(monkeypatch):
    monkeypatch.setattr(observability, "record_pipeline_evidence", lambda event, properties: True)


class _FakeOp:
    name = "fake_op"


class _FakeContext:
    run_id = "run-123"
    job_name = "nightly_field_ndvi_job"
    op = _FakeOp()
    cursor = "2026-06-18T00:00:00Z"


def test_observed_asset_emits_dagster_geospatial_and_satellite_events(monkeypatch):
    captured: list[tuple[str, dict]] = []

    def fake_capture(event, *, distinct_id=None, properties=None, groups=None):
        captured.append((event, dict(properties or {})))
        return True

    monkeypatch.setattr(observability, "capture_backend_event", fake_capture)

    @observability.observed_dagster_asset(
        asset_name="nightly_field_ndvi",
        pipeline_family="satellite_agri_indices",
        source_category="satellite",
        analysis_domain="agriculture",
        evidence_kind="district_ndvi_cache",
    )
    def asset_fn(context):
        return {
            "status": "ok",
            "backend": "deafrica",
            "districts_processed": 30,
            "errors": [],
            "date_range": "2026-06-11/2026-06-18",
            "files_uploaded": ["should-not-be-captured"],
        }

    assert asset_fn(_FakeContext())["status"] == "ok"

    events = [event for event, _props in captured]
    assert events == [
        "dagster_asset_completed",
        "geospatial_pipeline_flow_completed",
        "satellite_pipeline_completed",
    ]
    props = captured[0][1]
    assert props["asset_name"] == "nightly_field_ndvi"
    assert props["pipeline_family"] == "satellite_agri_indices"
    assert props["source_category"] == "satellite"
    assert props["analysis_domain"] == "agriculture"
    assert props["districts_processed"] == 30
    assert props["errors_count"] == 0
    assert props["success"] is True
    assert "files_uploaded" not in props


def test_observed_geolibre_asset_preserves_runtime_counts(monkeypatch):
    captured: list[tuple[str, dict]] = []

    def fake_capture(event, *, distinct_id=None, properties=None, groups=None):
        captured.append((event, dict(properties or {})))
        return True

    monkeypatch.setattr(observability, "capture_backend_event", fake_capture)

    @observability.observed_dagster_asset(
        asset_name="geolibre_runtime_probe",
        pipeline_family="geolibre_runtime",
        source_category="geolibre",
        analysis_domain="platform",
        evidence_kind="runtime_smoke",
    )
    def asset_fn(context):
        return {
            "status": "success",
            "tool_count": 747,
            "sample_workflow_count": 2,
            "sample_success_count": 2,
            "workflows": [{"large": "object should not be captured"}],
        }

    asset_fn(_FakeContext())

    props = captured[0][1]
    assert props["pipeline_family"] == "geolibre_runtime"
    assert props["source_category"] == "geolibre"
    assert props["tool_count"] == 747
    assert props["sample_workflow_count"] == 2
    assert props["sample_success_count"] == 2
    assert "workflows" not in props


def test_observed_asset_emits_failure_without_error_message(monkeypatch):
    captured: list[tuple[str, dict]] = []

    def fake_capture(event, *, distinct_id=None, properties=None, groups=None):
        captured.append((event, dict(properties or {})))
        return True

    monkeypatch.setattr(observability, "capture_backend_event", fake_capture)

    @observability.observed_dagster_asset(
        asset_name="weekly_crop_classification",
        pipeline_family="satellite_crop_classification",
        source_category="satellite",
        analysis_domain="agriculture",
        evidence_kind="crop_classification_cache",
    )
    def asset_fn(context):
        raise RuntimeError("secret path or URL should not be captured")

    with pytest.raises(RuntimeError):
        asset_fn(_FakeContext())

    events = [event for event, _props in captured]
    assert events == [
        "dagster_asset_failed",
        "geospatial_pipeline_flow_completed",
        "satellite_pipeline_completed",
    ]
    props = captured[0][1]
    assert props["status"] == "error"
    assert props["success"] is False
    assert props["error_type"] == "RuntimeError"
    assert "secret path" not in str(props)


def test_satellite_scene_success_reports_freshness_and_counts(monkeypatch):
    captured: list[tuple[str, dict]] = []

    def fake_capture(event, *, distinct_id=None, properties=None, groups=None):
        captured.append((event, dict(properties or {})))
        return True

    monkeypatch.setattr(observability, "capture_backend_event", fake_capture)

    observability.capture_satellite_scene_sensor_success(
        _FakeContext(),
        scene_count=5,
        latest_datetime="2026-06-18T08:00:00Z",
        tiles_invalidated=42,
        cache_warming_started=True,
        elapsed_ms_value=1234,
    )

    events = [event for event, _props in captured]
    assert events == [
        "satellite_pipeline_completed",
        "geospatial_pipeline_flow_completed",
    ]
    props = captured[0][1]
    assert props["sensor_name"] == "satellite_scene_sensor"
    assert props["scene_count"] == 5
    assert props["tiles_invalidated"] == 42
    assert props["cache_warming_started"] is True
    assert props["freshness_lag_hours"] is not None


def test_sensor_observer_summarizes_run_requests(monkeypatch):
    captured: list[tuple[str, dict]] = []

    def fake_capture(event, *, distinct_id=None, properties=None, groups=None):
        captured.append((event, dict(properties or {})))
        return True

    monkeypatch.setattr(observability, "capture_backend_event", fake_capture)

    @observability.observed_dagster_sensor(
        sensor_name="s3_upload_sensor",
        pipeline_family="upload_ingest",
        source_category="upload",
    )
    def sensor_fn(context):
        return [object(), object()]

    sensor_fn(_FakeContext())

    assert captured[0][0] == "dagster_sensor_evaluated"
    props = captured[0][1]
    assert props["sensor_name"] == "s3_upload_sensor"
    assert props["run_request_count"] == 2
    assert props["success"] is True
