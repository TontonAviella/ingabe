"""Regression coverage for full-orthophoto FastSAM chat timing."""

from src.routes.message_routes import _fast_raster_object_turn_timeout_seconds


# Regression: ISSUE-006 - outer chat watchdog discarded active FastSAM before its tool budget.
# Found by /qa on 2026-07-10
# Report: .gstack/qa-reports/qa-report-localhost-2026-07-09.md
def test_full_orthophoto_chat_budget_exceeds_fastsam_tool_budget(monkeypatch) -> None:
    monkeypatch.delenv("SAGE_FAST_RASTER_OBJECTS_TIMEOUT_SECONDS", raising=False)

    assert _fast_raster_object_turn_timeout_seconds() == 300.0


def test_invalid_full_orthophoto_timeout_uses_safe_default(monkeypatch) -> None:
    monkeypatch.setenv("SAGE_FAST_RASTER_OBJECTS_TIMEOUT_SECONDS", "not-a-number")

    assert _fast_raster_object_turn_timeout_seconds() == 300.0
