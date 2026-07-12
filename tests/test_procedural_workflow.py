from __future__ import annotations

import pytest

from src.services.procedural_workflow import raster_object_workflow


def test_raster_workflow_records_ordered_evidence() -> None:
    workflow = raster_object_workflow("Lsource")
    workflow.complete("resolve_source", layer_id="Lsource")
    workflow.complete("inspect_input", raster_type="raster", storage="cog")
    workflow.complete("plan_analysis", engine="fastsam", threshold=0.65)
    workflow.complete("execute_analysis", engine_used="fastsam", candidates=12)
    workflow.complete("validate_output", valid_candidates=12)
    workflow.complete("persist_artifacts", layer_id="Lmask", feature_count=12)
    workflow.complete("verify_delivery", pmtiles=True, database=True)
    workflow.complete("present_result", visibility_claim="verified")

    result = workflow.as_dict()
    assert result["status"] == "completed"
    assert result["completed_step_count"] == 8
    assert [step["step_id"] for step in result["steps"]] == [
        "resolve_source",
        "inspect_input",
        "plan_analysis",
        "execute_analysis",
        "validate_output",
        "persist_artifacts",
        "verify_delivery",
        "present_result",
    ]


def test_workflow_rejects_out_of_order_execution() -> None:
    workflow = raster_object_workflow("Lsource")

    with pytest.raises(RuntimeError, match="resolve_source"):
        workflow.complete("execute_analysis", candidates=1)


def test_workflow_marks_delivery_failure_as_partial() -> None:
    workflow = raster_object_workflow("Lsource")
    workflow.complete("resolve_source", layer_id="Lsource")
    workflow.complete("inspect_input", raster_type="raster")
    workflow.complete("plan_analysis", engine="fastsam")
    workflow.complete("execute_analysis", candidates=5)
    workflow.complete("validate_output", valid_candidates=5)
    workflow.complete("persist_artifacts", layer_id="Lmask")
    workflow.fail("verify_delivery", "PMTiles object was not readable")
    workflow.complete("present_result", visibility_claim="not_verified")

    result = workflow.as_dict()
    assert result["status"] == "partial"
    delivery = next(
        step for step in result["steps"] if step["step_id"] == "verify_delivery"
    )
    assert delivery["status"] == "failed"
    assert "not readable" in delivery["error"]
