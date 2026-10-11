"""The example finder with the real model, in the finder process, on the committed tower photo with 3 marks.

Runs only when FINDER_TEST_WEIGHTS points at the YOLOE-11s weights (models/yoloe-11s-seg-8e439445c87338b7.pt in
object storage); skipped otherwise. Prints the wall and CPU time per picture (run with -s to see them).
"""

from __future__ import annotations

import os
import time

import pytest

from src.services import boxes, example_finder, example_finder_model, stored_files
from src.services.answer_levels import OBLIQUE
from src.services.test_example_finder import MARKS, PHOTO, PIXELS, SPEC, _expected_finds

WEIGHTS = os.environ.get("FINDER_TEST_WEIGHTS", "")

pytestmark = pytest.mark.skipif(not WEIGHTS, reason="FINDER_TEST_WEIGHTS does not point at the finder's weights")


def _cpu_seconds(pid: int) -> float:
    """User + system CPU time of a process so far, over all its threads (Linux)."""
    with open(f"/proc/{pid}/stat") as f:
        fields = f.read().rsplit(")", 1)[1].split()
    return (int(fields[11]) + int(fields[12])) / os.sysconf("SC_CLK_TCK")


def _timed(picture, marks):
    began = time.perf_counter()
    process = example_finder_model._process
    cpu_before = _cpu_seconds(process.process.pid) if process is not None else 0.0
    finds = example_finder.find(picture, marks, [], SPEC["target"], view=OBLIQUE,
                                runner=example_finder_model.runner(WEIGHTS))
    cpu = _cpu_seconds(example_finder_model._process.process.pid) - cpu_before
    return finds, time.perf_counter() - began, cpu


@pytest.mark.timeout(1200)
def test_the_real_finder_finds_unmarked_dishes_and_answers_in_the_pictures_own_pixels():
    assert stored_files.file_sha256(WEIGHTS) == example_finder_model.MODEL_SHA256
    example_finder_model.threads()  # FINDER_THREADS is valid
    small, cold_s, cold_cpu = _timed(PHOTO, MARKS)  # starts the finder process
    big = PIXELS.repeat(2, axis=0).repeat(2, axis=1)  # 2666 x 1500 px, larger than the measured 1333
    big_marks = [[v * 2 for v in m] for m in MARKS]
    large, warm_s, warm_cpu = _timed(big, big_marks)
    print(f"\nexample finder, tower_dish, {PIXELS.shape[1]} x {PIXELS.shape[0]} px read at "
          f"{example_finder.settings(SPEC['target']).finder_input_px} px, FINDER_THREADS="
          f"{example_finder_model.threads()}: first picture {cold_s:.1f} s wall / {cold_cpu:.1f} s CPU (process "
          f"start, torch import, model); next picture {warm_s:.1f} s wall / {warm_cpu:.1f} s CPU; "
          f"{len(small)} finds")

    # FINDER_THREADS caps the CPU the model uses (Ultralytics resets torch's threads unless told otherwise)
    assert warm_cpu <= warm_s * (example_finder_model.threads() + 0.5)
    # the marks themselves are not given back as finds
    assert all(boxes.iou(f.box, m) < example_finder.MARK_IOU for f in small for m in MARKS)
    assert all(boxes.iou(f.box, m) < example_finder.MARK_IOU for f in large for m in big_marks)
    # dishes nobody marked are found
    unmarked = [d for d in SPEC["dish_boxes"] if d not in MARKS]
    assert sum(any(boxes.iou(f.box, d) >= 0.5 for f in small) for d in unmarked) >= 3
    # the larger picture was read as the measured one: the same finds, in its own (twice as large) pixels
    assert len(large) == len(small)
    for f, g in zip(large, small):
        assert f.box == pytest.approx([v * 2 for v in g.box], abs=0.05) and f.score == pytest.approx(g.score)
    assert max(f.box[2] for f in large) > PIXELS.shape[1]
    # and the same as the recorded result the unit tests use
    recorded = _expected_finds(MARKS)
    assert len(small) == len(recorded)
    for f, row in zip(small, recorded):
        assert f.box == pytest.approx(row[:4], abs=0.5) and f.score == pytest.approx(row[4], abs=0.01)
