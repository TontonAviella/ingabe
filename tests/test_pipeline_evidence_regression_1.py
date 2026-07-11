"""Regression coverage for distinct pipeline proof events."""

from src.services.pipeline_evidence import read_pipeline_evidence, record_pipeline_evidence


# Regression: ISSUE-005 - generic sensor heartbeat hid detailed satellite scene proof.
# Found by /qa on 2026-07-10
# Report: .gstack/qa-reports/qa-report-localhost-2026-07-09.md
def test_distinct_sensor_events_remain_visible(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("PIPELINE_EVIDENCE_PATH", str(tmp_path / "evidence.json"))
    shared = {
        "sensor_name": "satellite_scene_sensor",
        "pipeline_family": "satellite_scene_catalog",
        "source_category": "satellite",
        "status": "ok",
        "success": True,
    }

    assert record_pipeline_evidence(
        "satellite_pipeline_completed",
        {
            **shared,
            "evidence_kind": "sentinel_2_scene_catalog",
            "scene_count": 22,
            "latest_datetime": "2026-07-09T08:21:23.666000Z",
            "freshness_lag_hours": 17.2,
        },
    )
    assert record_pipeline_evidence(
        "geospatial_pipeline_flow_completed",
        {**shared, "evidence_kind": "sentinel_2_scene_catalog", "scene_count": 22},
    )
    assert record_pipeline_evidence(
        "dagster_sensor_evaluated",
        {**shared, "result_type": "none"},
    )

    result = read_pipeline_evidence(source_category="satellite", max_items=10)

    assert result["evidence_count"] == 3
    assert {item["event"] for item in result["latest"]} == {
        "satellite_pipeline_completed",
        "geospatial_pipeline_flow_completed",
        "dagster_sensor_evaluated",
    }
    detailed = next(
        item for item in result["latest"] if item["event"] == "satellite_pipeline_completed"
    )
    assert detailed["scene_count"] == 22
    assert detailed["freshness_lag_hours"] == 17.2
