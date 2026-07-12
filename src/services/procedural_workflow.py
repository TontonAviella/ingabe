from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any, Iterable


_TERMINAL_STEP_STATUSES = {"completed", "failed", "skipped"}


@dataclass
class WorkflowStep:
    step_id: str
    label: str
    purpose: str
    status: str = "pending"
    evidence: dict[str, Any] = field(default_factory=dict)
    error: str | None = None


class ProceduralWorkflow:
    """Small deterministic execution record for geospatial tool chains.

    This is the code equivalent of a QGIS/ArcGIS Model Builder diagram: each
    step has an input purpose, a terminal state, and machine-readable evidence.
    It records what actually happened; it does not ask an LLM to invent a plan.
    """

    def __init__(
        self,
        *,
        workflow_id: str,
        kind: str,
        steps: Iterable[WorkflowStep],
    ) -> None:
        self.workflow_id = workflow_id
        self.kind = kind
        self._steps = list(steps)
        step_ids = [step.step_id for step in self._steps]
        if not step_ids or len(step_ids) != len(set(step_ids)):
            raise ValueError("workflow steps must have unique, non-empty IDs")

    def complete(self, step_id: str, **evidence: Any) -> None:
        step = self._step(step_id)
        self._require_prior_steps_terminal(step_id)
        step.status = "completed"
        step.evidence = _compact(evidence)
        step.error = None

    def fail(self, step_id: str, error: object, **evidence: Any) -> None:
        step = self._step(step_id)
        self._require_prior_steps_terminal(step_id)
        step.status = "failed"
        step.evidence = _compact(evidence)
        step.error = str(error).strip() or type(error).__name__

    def skip(self, step_id: str, reason: str, **evidence: Any) -> None:
        step = self._step(step_id)
        self._require_prior_steps_terminal(step_id)
        step.status = "skipped"
        step.evidence = _compact({"reason": reason, **evidence})
        step.error = None

    def as_dict(self) -> dict[str, Any]:
        statuses = [step.status for step in self._steps]
        if "failed" in statuses:
            status = "partial" if "completed" in statuses else "failed"
        elif all(value in _TERMINAL_STEP_STATUSES for value in statuses):
            status = "completed"
        else:
            status = "incomplete"
        return {
            "workflow_id": self.workflow_id,
            "kind": self.kind,
            "status": status,
            "step_count": len(self._steps),
            "completed_step_count": sum(value == "completed" for value in statuses),
            "steps": [asdict(step) for step in self._steps],
        }

    def _step(self, step_id: str) -> WorkflowStep:
        for step in self._steps:
            if step.step_id == step_id:
                return step
        raise KeyError(f"unknown workflow step: {step_id}")

    def _require_prior_steps_terminal(self, step_id: str) -> None:
        for step in self._steps:
            if step.step_id == step_id:
                return
            if step.status not in _TERMINAL_STEP_STATUSES:
                raise RuntimeError(
                    f"workflow step {step_id} cannot run before {step.step_id}"
                )


def raster_object_workflow(layer_id: str) -> ProceduralWorkflow:
    return ProceduralWorkflow(
        workflow_id=f"raster-object:{layer_id}",
        kind="raster_object_mask",
        steps=[
            WorkflowStep(
                "resolve_source",
                "Resolve source layer",
                "Select an owned raster and its local object-storage key.",
            ),
            WorkflowStep(
                "inspect_input",
                "Inspect data shape",
                "Read raster type, bounds, storage format, and requested targets.",
            ),
            WorkflowStep(
                "plan_analysis",
                "Choose execution plan",
                "Choose engine, confidence rule, area limits, and sampling budget.",
            ),
            WorkflowStep(
                "execute_analysis",
                "Run segmentation",
                "Execute FastSAM and deterministic target-specific post-processing.",
            ),
            WorkflowStep(
                "validate_output",
                "Validate candidates",
                "Check feature shape, count, classes, and confidence contract.",
            ),
            WorkflowStep(
                "persist_artifacts",
                "Persist map artifacts",
                "Write GeoParquet, PMTiles, style, and map-layer records locally.",
            ),
            WorkflowStep(
                "verify_delivery",
                "Verify frontend delivery",
                "Confirm database attachment and local MinIO objects exist.",
            ),
            WorkflowStep(
                "present_result",
                "Present evidence-backed result",
                "Expose only counts and visibility claims supported by prior steps.",
            ),
        ],
    )


def _compact(values: dict[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in values.items() if value is not None}
