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


def _observed(result):
    @observability.observed_dagster_asset(
        asset_name="weekly_drought_scan",
        pipeline_family="drought",
        source_category="satellite",
    )
    def asset_fn(context):
        return result

    return asset_fn


@pytest.mark.parametrize("status", ["error", "failed", "timeout"])
def test_asset_returning_failure_status_fails_the_step(monkeypatch, status):
    from dagster import Failure

    captured: list[str] = []
    monkeypatch.setattr(
        observability, "capture_backend_event",
        lambda event, **kwargs: captured.append(event) or True,
    )

    with pytest.raises(Failure, match=f"weekly_drought_scan returned status={status}: boom"):
        _observed({"status": status, "error": "boom"})(_FakeContext())
    assert "dagster_asset_completed" in captured  # telemetry still records the result


@pytest.mark.parametrize("result", [{"status": "ok"}, {"status": "skipped", "reason": "no_parcels"}, None])
def test_asset_returning_success_or_skip_does_not_raise(monkeypatch, result):
    monkeypatch.setattr(observability, "capture_backend_event", lambda event, **kwargs: True)

    assert _observed(result)(_FakeContext()) == result
