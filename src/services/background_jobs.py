"""Long work started by a request and finished in the background: one job per key, with its progress.

The job lives in this process only. If the app restarts, the next request starts it again; results
are kept by the caller (in object storage), not here.
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass
from typing import Awaitable, Callable, Optional

logger = logging.getLogger(__name__)

Progress = Callable[[int, int], None]  # (parts done, parts)


@dataclass(frozen=True)
class Job:
    state: str  # "running" or "failed"
    parts_done: int
    parts: int
    started: float
    error: Optional[str] = None

    @property
    def minutes_left(self) -> Optional[int]:
        if self.state != "running" or self.parts_done == 0:
            return None
        per_part = (time.time() - self.started) / self.parts_done
        return max(1, round(per_part * (self.parts - self.parts_done) / 60))


_jobs: dict[str, Job] = {}


def status(key: str) -> Optional[Job]:
    """The running or failed job for this key; None when there is none (never started, or finished)."""
    return _jobs.get(key)


def start(key: str, work: Callable[[Progress], Awaitable[None]]) -> Job:
    """Run `work` in the background once per key; a job already running is returned as it is."""
    current = _jobs.get(key)
    if current is not None and current.state == "running":
        return current
    started = time.time()
    _jobs[key] = Job(state="running", parts_done=0, parts=1, started=started)

    def progress(done: int, parts: int) -> None:
        _jobs[key] = Job(state="running", parts_done=done, parts=parts, started=started)

    async def run() -> None:
        try:
            await work(progress)
            _jobs.pop(key, None)
        except Exception as exc:  # the caller shows the failure; the next request may start it again
            logger.exception("background job %s failed", key)
            last = _jobs.get(key)
            _jobs[key] = Job(state="failed", parts_done=last.parts_done if last else 0,
                             parts=last.parts if last else 1, started=started, error=str(exc)[:200])

    asyncio.get_running_loop().create_task(run())
    return _jobs[key]
