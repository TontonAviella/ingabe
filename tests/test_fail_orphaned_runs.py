from __future__ import annotations

from dagster import DagsterInstance, DagsterRun, DagsterRunStatus

from src.pipelines.fail_orphaned_runs import fail_orphaned_runs


def _add(instance: DagsterInstance, run_id: str, status: DagsterRunStatus) -> None:
    instance.add_run(DagsterRun(job_name="nightly_field_ndvi_job", run_id=run_id, status=status))


def test_in_progress_runs_are_failed_and_others_untouched():
    with DagsterInstance.ephemeral() as instance:
        _add(instance, "started", DagsterRunStatus.STARTED)
        _add(instance, "starting", DagsterRunStatus.STARTING)
        _add(instance, "pending", DagsterRunStatus.NOT_STARTED)
        _add(instance, "done", DagsterRunStatus.SUCCESS)

        failed = fail_orphaned_runs(instance)

        assert sorted(failed) == ["started", "starting"]
        status = {r.run_id: r.status for r in instance.get_runs()}
        assert status["started"] == DagsterRunStatus.FAILURE
        assert status["starting"] == DagsterRunStatus.FAILURE
        assert status["pending"] == DagsterRunStatus.NOT_STARTED
        assert status["done"] == DagsterRunStatus.SUCCESS


def test_no_in_progress_runs_is_a_no_op():
    with DagsterInstance.ephemeral() as instance:
        _add(instance, "pending", DagsterRunStatus.NOT_STARTED)
        assert fail_orphaned_runs(instance) == []
