"""Raster reads in a worker process do not stall the app's event loop (2026-10-07).

rasterio holds the GIL during part of a remote read, so asyncio.to_thread did not protect the loop:
one WaPOR point read held it for ~2.5 s. `sum(range(n))` stands in for that read here: it holds the
GIL in C for its whole run, like the read does.
"""

from __future__ import annotations

import asyncio
import os
import time
from concurrent.futures.process import BrokenProcessPool

import pytest

from src.services import raster_process

GIL_HOLD = range(60_000_000)  # ~1-2 s of C code holding the GIL


def _worst_loop_delay(blocking_call) -> float:
    """Seconds the event loop woke up late, at worst, while blocking_call ran in a thread."""
    async def watch() -> float:
        work = asyncio.ensure_future(asyncio.to_thread(blocking_call))
        worst = 0.0
        while not work.done():
            started = time.monotonic()
            await asyncio.sleep(0.02)
            worst = max(worst, time.monotonic() - started - 0.02)
        await work
        return worst

    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(watch())
    finally:
        loop.close()


def test_work_runs_in_another_process_and_returns():
    assert raster_process.run(os.getpid) != os.getpid()
    assert raster_process.run(sum, [1, 2, 3]) == 6


def test_an_exception_comes_back_to_the_caller():
    with pytest.raises(ValueError):
        raster_process.run(int, "not a number")


def test_a_worker_that_dies_is_replaced():
    with pytest.raises(BrokenProcessPool):
        raster_process.run(os._exit, 1)
    assert raster_process.run(sum, [2, 2]) == 4


def test_the_loop_keeps_running_while_a_worker_holds_its_own_gil():
    raster_process.run(sum, [])  # start the workers first: spawning is not what is measured
    in_thread = _worst_loop_delay(lambda: sum(GIL_HOLD))
    in_worker = _worst_loop_delay(lambda: raster_process.run(sum, GIL_HOLD))
    assert in_thread > 0.5  # the stand-in really holds the GIL
    assert in_worker < 0.2
