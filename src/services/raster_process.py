"""Remote raster reads run in a separate process.

rasterio 1.4 holds the GIL during part of every read of a remote COG: one WaPOR point read held the
app's event loop for ~2.5 s even inside asyncio.to_thread, and an insurance report's WaPOR reads for
10-24 s (measured in mundi-app on 2026-10-07; GDAL's own Python bindings did not hold it, at 0.03 s).
The app answers every user from one event loop, so everyone waited. Reads that go over the network
therefore run in a small pool of worker processes: the calling thread waits on the result without
holding the GIL, and the loop keeps running.

A function run here must be defined at module level (it is pickled by name), take and return
picklable values (numbers, strings, numpy arrays, rasterio Affine), and do no logging of its own:
it returns what happened and the caller logs it.
"""

from __future__ import annotations

import multiprocessing
import threading
from concurrent.futures import ProcessPoolExecutor
from concurrent.futures.process import BrokenProcessPool
from typing import Any, Callable, TypeVar

T = TypeVar("T")

# Each worker holds rasterio, numpy and GDAL (~110 MB, measured 2026-10-07). Started on first use.
WORKERS = 3
# A read is bounded by its own GDAL limits (gdal_http.GDAL_HTTP_TIMEOUTS); this only stops a caller
# waiting for ever on a worker that is wedged.
WAIT_LIMIT_S = 180.0

_pool: ProcessPoolExecutor | None = None
_pool_lock = threading.Lock()


def _executor() -> ProcessPoolExecutor:
    global _pool
    with _pool_lock:
        if _pool is None:
            # spawn, not fork: forking a process that runs threads (uvicorn, GDAL) can deadlock the child.
            _pool = ProcessPoolExecutor(max_workers=WORKERS, mp_context=multiprocessing.get_context("spawn"))
        return _pool


def run(fn: Callable[..., T], *args: Any) -> T:
    """fn(*args) in a raster worker process; its exception, if any, is raised here.

    A pool whose worker died (killed, out of memory) is replaced for the next call; this call raises
    BrokenProcessPool. Waiting longer than WAIT_LIMIT_S raises TimeoutError.
    """
    global _pool
    pool = _executor()
    try:
        return pool.submit(fn, *args).result(timeout=WAIT_LIMIT_S)
    except BrokenProcessPool:
        with _pool_lock:
            if _pool is pool:
                _pool = None
        raise
