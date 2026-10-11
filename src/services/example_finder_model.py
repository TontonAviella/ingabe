"""The example finder's model (YOLOE-11s with visual prompts) and the separate process it runs in.

Why a process and not a thread: torch and Ultralytics hold the GIL for long stretches and are not thread-safe, so a
thread would stall every request the app answers (Lessons log 2026-10-07: raster_process). This mirrors
src/services/raster_process.py (spawn, a function defined at module level, plain values in and out, the caller
waits without holding the GIL) with one difference: it is a single long-lived process with a pipe, not a pool, so a
picture that never finishes can be killed (a pool cannot stop a running task).

* One picture at a time per app process (a lock); the app-wide limit is in example_finder (Redis), which also calls
  `stop` after a picture unless this app worker runs the next one, so one finder process lives in the whole app.
* Torch threads capped by FINDER_THREADS (default 4); YOLO_OFFLINE and YOLO_AUTOINSTALL are set in the finder
  process only, so Ultralytics never reaches the internet there (the app's FastSAM path is left as it was).
* Weights come only from object storage (MODEL_KEY), fetched once and checked by sha256 (stored_files).
* The process is started on first use and stops when told (`stop`) or by itself after IDLE_EXIT_S (2 minutes)
  without a picture, giving its memory back; the next picture starts it again.
"""

from __future__ import annotations

import logging
import multiprocessing
import os
import threading
from multiprocessing.connection import Connection
from multiprocessing.reduction import ForkingPickler
from typing import Any, Callable, Optional, Sequence

import numpy as np
from PIL import Image

from src.services import stored_files

logger = logging.getLogger(__name__)

MODEL_NAME = "yoloe-11s-seg"  # the finder named in answer_levels_targets.json; levels hold for this model only
MODEL_KEY = "models/yoloe-11s-seg-8e439445c87338b7.pt"
MODEL_SHA256 = "8e439445c87338b79d9ce21dec109f4621e26df67e94d26ea1a98c1e64dce3e3"
_WEIGHTS_PATH = "/tmp/ingabe_cache/models/yoloe-11s-seg-8e439445c87338b7.pt"

DEFAULT_THREADS = 4
# One picture, including starting the process. Measured 2026-10-11 in the amd64 app image on the M1 Mac with 4
# threads, a new process then a warm one (docs/rules/EXAMPLE_FINDER_RULES.md has the table): read at 640 px 18/5 s,
# at 1280 px 28-39/14-18 s, at 2560 px 78/60 s (1333 x 750 px) and 137/89 s (1333 x 1333 px). Past this the
# process is killed.
WAIT_LIMIT_S = 600.0
# The process peaks at 0.9-1.4 GB reading at 640-1280 px and 2.8-3.3 GB at 2560 px, and keeps 0.9-1.5 GB after a
# picture (same measurement). example_finder stops it after each picture unless the same app worker runs the next
# one, so one lives in the whole app; this is the fallback: a process with no picture for this long stops itself.
IDLE_EXIT_S = 120.0
# Set in the finder process before Ultralytics is imported. Ultralytics 8.4 reads YOLO_OFFLINE as offline only
# when it is "true" (utils.is_online), not "1"; YOLO_AUTOINSTALL=False stops it pip-installing what it misses.
OFFLINE_ENV = {"YOLO_OFFLINE": "true", "YOLO_AUTOINSTALL": "False"}

# (x0, y0, x1, y1, score, class) in the pixels of the picture the model was given
Row = tuple[float, float, float, float, float, int]
Runner = Callable[[np.ndarray, Sequence[Sequence[float]], Sequence[int], int, float, int], list[Row]]


def threads() -> int:
    """FINDER_THREADS: torch threads in the finder process (default 4). Anything but a whole number >= 1 raises."""
    raw = os.environ.get("FINDER_THREADS", "").strip()
    if not raw:
        return DEFAULT_THREADS
    if not raw.isdigit() or int(raw) < 1:
        raise ValueError(f"FINDER_THREADS must be a whole number of 1 or more, got {raw!r}")
    return int(raw)


def ensure_weights(s3: Any, bucket: str) -> str:
    """The local path of the YOLOE weights, fetched from object storage once and refused if the sha256 differs.
    `s3` is a synchronous boto3 client (call from a thread)."""
    return stored_files.ensure_local_copy(s3, bucket, MODEL_KEY, MODEL_SHA256, _WEIGHTS_PATH, "example finder model")


def result_rows(result: Any) -> list[Row]:
    """The boxes of one Ultralytics result as plain rows, in the pixels of the picture it was given."""
    boxes = result.boxes
    return [(float(x0), float(y0), float(x1), float(y1), float(score), int(cls))
            for (x0, y0, x1, y1), score, cls in zip(boxes.xyxy.tolist(), boxes.conf.tolist(), boxes.cls.tolist())]


def _predict(weights: str, rgb: np.ndarray, prompts: Sequence[Sequence[float]], classes: Sequence[int],
             imgsz: int, conf: float, max_det: int) -> list[Row]:
    """In the finder process: one picture, the prompts drawn on the same picture."""
    from ultralytics import YOLOE  # lazy: only the finder process imports torch and Ultralytics
    from ultralytics.models.yolo.yoloe import YOLOEVPSegPredictor  # lazy: as above

    # A PIL picture is read like a file (RGB to BGR): on a tower photo both gave the same 27 boxes.
    image = Image.fromarray(rgb)
    # A fresh model per picture, as measured: visual prompts replace the model's classes.
    model = YOLOE(weights)
    result = model.predict(
        image,
        refer_image=image,
        visual_prompts={"bboxes": np.asarray(prompts, dtype=float), "cls": np.asarray(classes, dtype=int)},
        predictor=YOLOEVPSegPredictor,
        conf=conf,
        imgsz=imgsz,
        device="cpu",
        verbose=False,
        max_det=max_det,
    )[0]
    return result_rows(result)


def _serve(conn: Connection, n_threads: int, idle_s: float) -> None:
    """The finder process: one picture at a time, until the app goes away or it has been idle `idle_s`."""
    os.environ.update(OFFLINE_ENV)
    import torch  # lazy: only the finder process holds torch
    import ultralytics.utils.torch_utils as ultralytics_torch  # lazy: as above

    torch.set_num_threads(n_threads)
    # Ultralytics resets torch's threads to its own NUM_THREADS (CPU count - 1, at most 8) whenever a model is set
    # up on the CPU (torch_utils.select_device): measured at ~5.8 cores busy with FINDER_THREADS=1.
    ultralytics_torch.NUM_THREADS = n_threads
    while conn.poll(idle_s):
        try:
            request = conn.recv()
        except EOFError:  # the app process went away
            return
        try:
            conn.send(("rows", _predict(*request)))
        except Exception as e:  # sent back: the caller raises it
            conn.send(("error", f"{type(e).__name__}: {e}"))


class _Process:
    def __init__(self) -> None:
        ctx = multiprocessing.get_context("spawn")  # not fork: the app runs threads (uvicorn, GDAL)
        self.conn, child = ctx.Pipe()
        self.process = ctx.Process(target=_serve, args=(child, threads(), IDLE_EXIT_S), daemon=True, name="example-finder")
        self.process.start()
        child.close()

    def stop(self) -> None:
        if self.process.is_alive():
            self.process.kill()
        self.process.join(5)
        self.conn.close()


_process: Optional[_Process] = None
_lock = threading.Lock()


def _send(conn: Connection, data: bytes) -> None:
    """In a helper thread: a large picture fills the pipe, and a process that never reads must not hold the caller
    past WAIT_LIMIT_S (the caller waits on the answer, not on the send)."""
    try:
        conn.send_bytes(data)
    except OSError as e:  # the process died or was stopped; the waiting side reports it (end of file or time limit)
        logger.info("the finder process did not take the picture: %r", e)


def _ask(request: tuple[Any, ...]) -> tuple[str, Any]:
    """Send one picture to the finder process (starting it if needed) and wait for its answer."""
    global _process
    data = bytes(ForkingPickler.dumps(request))  # read on the other side by conn.recv()
    for attempt in (1, 2):
        if _process is None or not _process.process.is_alive():
            if _process is not None:
                _process.stop()
            _process = _Process()
        current = _process
        threading.Thread(target=_send, args=(current.conn, data), daemon=True, name="example-finder-send").start()
        try:
            if not current.conn.poll(WAIT_LIMIT_S):
                raise TimeoutError(f"the finder gave no answer within {WAIT_LIMIT_S:.0f} s; it was stopped")
            return current.conn.recv()
        except TimeoutError:
            current.stop()
            _process = None
            raise
        except (EOFError, OSError) as e:
            current.stop()
            _process = None
            # Exit code 0: it stopped for being idle just as this picture came; once more with a new process.
            if current.process.exitcode == 0 and attempt == 1:
                continue
            raise RuntimeError(f"the finder process stopped (exit code {current.process.exitcode})") from e
    raise AssertionError("unreachable")


def stop() -> None:
    """Stops the finder process now, if there is one, giving its memory back; the next picture starts a new one.
    Waits for a picture this app process is running to finish first (the lock), so it never cuts one short."""
    global _process
    with _lock:
        if _process is not None:
            _process.stop()
            _process = None


def run(weights: str, rgb: np.ndarray, prompts: Sequence[Sequence[float]], classes: Sequence[int], imgsz: int,
        conf: float, max_det: int) -> list[Row]:
    """The model's boxes for one picture, computed in the finder process. Blocking: async code calls it through
    asyncio.to_thread (the wait does not hold the GIL). Raises RuntimeError when the model failed or the process
    died, TimeoutError past WAIT_LIMIT_S."""
    with _lock:
        kind, value = _ask((weights, np.ascontiguousarray(rgb), [list(map(float, p)) for p in prompts],
                            [int(c) for c in classes], int(imgsz), float(conf), int(max_det)))
    if kind == "error":
        raise RuntimeError(f"the finder failed: {value}")
    return value


def runner(weights: str) -> Runner:
    """A Runner that uses the weights at `weights` in the finder process."""

    def run_with_weights(rgb: np.ndarray, prompts: Sequence[Sequence[float]], classes: Sequence[int], imgsz: int,
                         conf: float, max_det: int) -> list[Row]:
        return run(weights, rgb, prompts, classes, imgsz, conf, max_det)

    return run_with_weights
