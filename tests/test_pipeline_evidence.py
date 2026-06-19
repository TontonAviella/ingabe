from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor

from src.services.pipeline_evidence import (
    read_pipeline_evidence,
    record_pipeline_evidence,
)
from src.tools.pipeline_evidence import (
    GetPipelineEvidenceStatusArgs,
    get_pipeline_evidence_status,
)
from src.tools.pyd import tool_from


def test_pipeline_evidence_records_and_filters_latest_safe_records(tmp_path, monkeypatch):
    evidence_path = tmp_path / "pipeline_evidence.json"
    monkeypatch.setenv("PIPELINE_EVIDENCE_PATH", str(evidence_path))

    assert record_pipeline_evidence(
        "satellite_pipeline_completed",
        {
            "asset_name": "nightly_ndvi_vector_tiles",
            "pipeline_family": "satellite_h3_tiles",
            "source_category": "satellite",
            "analysis_domain": "agriculture",
            "status": "ok",
            "success": True,
            "features": 42,
            "s3_key": "must-not-be-written",
            "url": "must-not-be-written",
        },
    )
    assert record_pipeline_evidence(
        "geospatial_pipeline_flow_completed",
        {
            "asset_name": "daily_weather_ingest",
            "pipeline_family": "weather_ingest",
            "source_category": "weather",
            "status": "ok",
            "total_rows": 30,
        },
    )

    satellite = read_pipeline_evidence(source_category="satellite")
    assert satellite["status"] == "ok"
    assert satellite["evidence_count"] == 1
    record = satellite["latest"][0]
    assert record["asset_name"] == "nightly_ndvi_vector_tiles"
    assert record["features"] == 42
    assert "s3_key" not in record
    assert "url" not in record
    assert record["stale"] is False

    all_evidence = read_pipeline_evidence()
    assert all_evidence["evidence_count"] == 2


def test_pipeline_evidence_reports_no_evidence_for_missing_filter(tmp_path, monkeypatch):
    monkeypatch.setenv("PIPELINE_EVIDENCE_PATH", str(tmp_path / "missing.json"))

    result = read_pipeline_evidence(source_category="satellite")

    assert result["status"] == "no_evidence"
    assert result["evidence_count"] == 0
    assert result["latest"] == []


def test_pipeline_evidence_concurrent_writes_keep_latest_records(tmp_path, monkeypatch):
    evidence_path = tmp_path / "pipeline_evidence.json"
    monkeypatch.setenv("PIPELINE_EVIDENCE_PATH", str(evidence_path))

    def write_one(idx: int) -> bool:
        return record_pipeline_evidence(
            "pipeline_completed",
            {
                "asset_name": f"asset_{idx}",
                "pipeline_family": "satellite_h3_tiles",
                "source_category": "satellite",
                "status": "ok",
                "features": idx,
            },
        )

    with ThreadPoolExecutor(max_workers=8) as pool:
        assert all(pool.map(write_one, range(16)))

    result = read_pipeline_evidence(source_category="satellite", max_items=50)

    assert result["status"] == "ok"
    assert result["evidence_count"] == 16
    assert {record["asset_name"] for record in result["latest"]} == {
        f"asset_{idx}" for idx in range(16)
    }


def test_pipeline_evidence_tool_schema_is_strict_compatible():
    tool = tool_from(get_pipeline_evidence_status, GetPipelineEvidenceStatusArgs)
    params = tool["function"]["parameters"]

    assert params["additionalProperties"] is False
    assert sorted(params["required"]) == sorted(params["properties"].keys())
