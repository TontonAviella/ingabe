"""Run time and memory of the example finder's own process per picture size and finder input size, in the app image.

Not a measurement of finding: the numbers in docs/rules/EXAMPLE_FINDER_RULES.md ("Measured run time and memory") and
the comments in src/services/example_finder_model.py. Each case (WIDTHxHEIGHTxIMGSZ) runs in a new finder process
(example_finder_model): one picture (with the process start), then a second in the same process; VmHWM (peak) and
VmRSS (kept) of the process are read from /proc after each. The picture is the committed tower photo resized to the
case's size, with its three marks. Prints one JSON line per case. Run inside the app image, one at a time, e.g.

    docker compose run --rm --no-deps -v $PWD/scripts:/app/scripts -v $PWD/test_fixtures:/app/test_fixtures app \
        python scripts/finder_eval/measure_finder_process.py 1333x750x1280 1333x750x2560 640x640x1280
"""

import io
import json
import sys
import time
from pathlib import Path

import numpy as np
from PIL import Image

from src.services import example_finder_model as m
from src.utils import get_bucket_name, get_s3_client

FIXTURES = Path(__file__).resolve().parents[2] / "test_fixtures" / "example_finder"
SPEC = json.loads((FIXTURES / "tower_dishes.json").read_text())
PHOTO = np.asarray(Image.open(io.BytesIO((FIXTURES / SPEC["picture"]).read_bytes())).convert("RGB"))
MARKS = SPEC["marks"]


def proc_kb(pid: int) -> dict:
    out = {}
    for line in Path(f"/proc/{pid}/status").read_text().splitlines():
        key, _, value = line.partition(":")
        if key in ("VmHWM", "VmRSS"):
            out[key] = int(value.split()[0]) // 1024
    return out


def picture(width: int, height: int) -> tuple[np.ndarray, list]:
    """The tower photo resized to width x height (marks scaled with it)."""
    oh, ow = PHOTO.shape[:2]
    pixels = np.asarray(Image.fromarray(PHOTO).resize((width, height), Image.Resampling.BOX))
    sx, sy = width / ow, height / oh
    return pixels, [[x0 * sx, y0 * sy, x1 * sx, y1 * sy] for x0, y0, x1, y1 in MARKS]


def main() -> None:
    weights = m.ensure_weights(get_s3_client(), get_bucket_name())
    cases = [(int(w), int(h), int(i)) for w, h, i in (c.split("x") for c in sys.argv[1:])]
    for width, height, imgsz in cases:
        if m._process is not None:
            m._process.stop()
            m._process = None
        pixels, marks = picture(width, height)
        row = {"picture": f"{width}x{height}", "imgsz": imgsz, "threads": m.threads()}
        for n in (1, 2):
            began = time.monotonic()
            rows = m.run(weights, pixels, marks, [0] * len(marks), imgsz, 0.01, 300)
            row[f"s{n}"] = round(time.monotonic() - began, 1)
            row[f"boxes{n}"] = len(rows)
            mem = proc_kb(m._process.process.pid)
            row[f"peak_mb{n}"], row[f"rss_mb{n}"] = mem["VmHWM"], mem["VmRSS"]
        print(json.dumps(row), flush=True)
    if m._process is not None:
        m._process.stop()


if __name__ == "__main__":
    main()
