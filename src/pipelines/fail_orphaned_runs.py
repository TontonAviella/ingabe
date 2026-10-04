"""Mark runs left in progress by a previous daemon as failed.

Run before ``dagster-daemon run`` (see the dagster-daemon service in
docker-compose.yml). With the DefaultRunLauncher every run executes as a
subprocess inside the daemon's container, so a run still STARTING or STARTED
when the container boots belongs to a process that no longer exists. The
launcher cannot health-check run workers, so Dagster never fails these runs
itself, and with ``max_concurrent_runs: 1`` (dagster.yaml) one orphan would
block the run queue forever.
"""

from __future__ import annotations

from dagster import DagsterInstance, DagsterRunStatus, RunsFilter

IN_PROGRESS = [DagsterRunStatus.STARTING, DagsterRunStatus.STARTED, DagsterRunStatus.CANCELING]
MESSAGE = "Orphaned: the Dagster daemon restarted while this run was in progress."


def fail_orphaned_runs(instance: DagsterInstance) -> list[str]:
    """Report every in-progress run as failed; return their run ids."""
    failed: list[str] = []
    for run in instance.get_runs(filters=RunsFilter(statuses=IN_PROGRESS)):
        instance.report_run_failed(run, message=MESSAGE)
        failed.append(run.run_id)
    return failed


def main() -> None:
    with DagsterInstance.get() as instance:
        failed = fail_orphaned_runs(instance)
    print(f"fail_orphaned_runs: marked {len(failed)} orphaned run(s) failed")


if __name__ == "__main__":
    main()
