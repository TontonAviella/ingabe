"""The example finder (src/services/example_finder.py): settings from the measured table, input checks, the measured
protocol around the model, content-addressed results kept per project, and jobs shared by app workers through Redis.

The model is replaced by `RecordedModel`, which returns a real Ultralytics result recorded on the committed tower
photo (test_fixtures/example_finder/recorded_yoloe_result.json, scripts/finder_eval/record_test_result.py), read
through the same `result_rows` the finder process uses. The real model runs in test_example_finder_model.py.
Redis (database 15, a key prefix per run) and object storage (MinIO) are the real services.
"""

from __future__ import annotations

import asyncio
import dataclasses
import hashlib
import random
import io
import json
import os
import subprocess
import sys
import threading
import time
import uuid
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pytest
import torch
from PIL import Image
from redis.asyncio import Redis

from src.services import answer_levels, boxes, example_finder, example_finder_model
from src.services.answer_levels import NADIR, OBLIQUE
from src.services.example_finder_pictures import FinderInputError, Picture
from src.utils import get_async_s3_client, get_bucket_name, get_s3_client

FIXTURES = Path(__file__).resolve().parents[2] / "test_fixtures" / "example_finder"
SPEC = json.loads((FIXTURES / "tower_dishes.json").read_text())
RECORDED = json.loads((FIXTURES / "recorded_yoloe_result.json").read_text())
PHOTO = (FIXTURES / SPEC["picture"]).read_bytes()
PIXELS = np.asarray(Image.open(io.BytesIO(PHOTO)).convert("RGB"))
MARKS = SPEC["marks"]
RUN_TAG = uuid.uuid4().hex[:8]
USER = f"user-{RUN_TAG}"


def _grown(box, factor):
    """The box grown around its centre (a larger object at the same place)."""
    cx, cy, half_w, half_h = (box[0] + box[2]) / 2, (box[1] + box[3]) / 2, (box[2] - box[0]) / 2, (box[3] - box[1]) / 2
    return [cx - half_w * factor, cy - half_h * factor, cx + half_w * factor, cy + half_h * factor]


# Antennas were measured from 33 px wide as the finder reads them; the marked dishes are about 30 px: grown 1.3x.
ANTENNA_MARKS = [_grown(m, 1.3) for m in MARKS]


def recorded_result() -> SimpleNamespace:
    """The recorded Ultralytics result, with its boxes as the torch tensors Ultralytics gives."""
    b = RECORDED["boxes"]
    return SimpleNamespace(boxes=SimpleNamespace(xyxy=torch.tensor(b["xyxy"]), conf=torch.tensor(b["conf"]),
                                                 cls=torch.tensor(b["cls"])), orig_shape=tuple(RECORDED["orig_shape"]))


class RecordedModel:
    """Stands in for the finder process: the recorded result's rows, scaled to the picture it is given (the same
    picture when the picture is the recorded one), with every call kept."""

    def __init__(self, delay_s: float = 0.0, fail: str = "", lookalike_rows: int = 0) -> None:
        self.calls: list[dict] = []
        self.delay_s, self.fail, self.lookalike_rows = delay_s, fail, lookalike_rows
        self.running = 0
        self.most_at_once = 0
        self._lock = threading.Lock()

    def __call__(self, rgb, prompts, classes, imgsz, conf, max_det):
        with self._lock:
            self.running += 1
            self.most_at_once = max(self.most_at_once, self.running)
        try:
            self.calls.append({"rgb": rgb, "prompts": [list(p) for p in prompts], "classes": list(classes),
                               "imgsz": imgsz, "conf": conf, "max_det": max_det})
            time.sleep(self.delay_s)
            if self.fail:
                raise RuntimeError(self.fail)
            height, width = rgb.shape[:2]
            oh, ow = RECORDED["orig_shape"]
            rows = example_finder_model.result_rows(recorded_result())
            rows = [(x0 * width / ow, y0 * height / oh, x1 * width / ow, y1 * height / oh, s, c)
                    for x0, y0, x1, y1, s, c in rows]
            # A result with look-alike prompts also has class 1 boxes: the first rows, relabelled.
            return [(*r[:5], 1) if i < self.lookalike_rows else r for i, r in enumerate(rows)]
        finally:
            with self._lock:
                self.running -= 1


def _expected_finds(marks):
    """The recorded rows the protocol keeps: not on a mark (IoU >= 0.3), best first."""
    rows = example_finder_model.result_rows(recorded_result())
    kept = [r for r in rows if not any(boxes.iou(r[:4], m) >= example_finder.MARK_IOU for m in marks)]
    return sorted(kept, key=lambda r: -r[4])


# --------------------------------------------------------------------------- #
# The recorded result, settings, inputs
# --------------------------------------------------------------------------- #

def test_result_rows_read_a_recorded_ultralytics_result():
    rows = example_finder_model.result_rows(recorded_result())
    assert len(rows) == len(RECORDED["boxes"]["conf"]) > 3
    for row, xyxy, conf, cls in zip(rows, RECORDED["boxes"]["xyxy"], RECORDED["boxes"]["conf"],
                                    RECORDED["boxes"]["cls"]):
        assert row[:4] == pytest.approx(xyxy) and row[4] == pytest.approx(conf) and row[5] == int(cls)
        assert all(type(v) is float for v in row[:5]) and type(row[5]) is int


def test_the_recording_was_made_with_the_finders_model_and_protocol():
    call = RECORDED["call"]
    s = example_finder.settings(SPEC["target"])
    assert RECORDED["model_sha256"] == example_finder_model.MODEL_SHA256
    assert (call["imgsz"], call["conf"], call["max_det"]) == (s.finder_input_px, example_finder.CONF,
                                                             example_finder.MAX_DET)
    assert call["prompts"] == MARKS and RECORDED["orig_shape"] == [PIXELS.shape[0], PIXELS.shape[1]]


def test_settings_are_read_from_the_measured_table():
    with_settings = [t for t in answer_levels.targets() if t.finder_input_px and t.measured["image_px"]]
    assert {t.name for t in with_settings} >= {"tower_dish", "tower_antenna", "maize_seedling", "weed"}
    for t in with_settings:
        s = example_finder.settings(t.name)
        assert (s.image_px, s.finder_input_px, s.look_alike_marks, s.view, s.suggestion_filter) == (
            t.measured["image_px"], t.finder_input_px, t.look_alike_marks, t.measured["view"], t.suggestion_filter)


@pytest.mark.parametrize("name", ["banana_mat", "no_such_thing"])
def test_a_target_without_measured_settings_is_refused(name):
    with pytest.raises(FinderInputError):
        example_finder.settings(name)


def test_finder_model_matches_the_one_the_levels_were_measured_for():
    assert {t.finder_model for t in answer_levels.targets()} == {example_finder_model.MODEL_NAME}


@pytest.mark.parametrize("marks, says", [
    ([], "give 1 to 20 marks"),
    ([[0, 0, 5, 5]] * 21, "give 1 to 20 marks"),
    ([[0, 0, 5]], "four numbers"),
    ([[0, 0, 5, float("nan")]], "four numbers"),
    ([[0, 0, True, 5]], "four numbers"),
    ([[10, 10, 5, 20]], "no width or no height"),
    ([[10, 10, 20, 10]], "no width or no height"),
    ([[1300, 10, 1400, 20]], "not inside the picture (1333 x 750 px)"),
    ([[-5, 10, 20, 20]], "not inside the picture"),
    ("not boxes", "must be a list of boxes"),
])
def test_marks_are_checked(marks, says):
    with pytest.raises(FinderInputError) as raised:
        example_finder.find(PIXELS, marks, [], "tower_dish", view=OBLIQUE, runner=RecordedModel())
    assert says in str(raised.value)


def test_lookalike_marks_only_for_targets_measured_with_them():
    with pytest.raises(FinderInputError, match="look-alike marks are not used for dishes"):
        example_finder.find(PIXELS, MARKS, [[10, 10, 30, 30]], "tower_dish", view=OBLIQUE, runner=RecordedModel())
    with pytest.raises(FinderInputError, match="at most 20 look-alike marks"):
        example_finder.find(PIXELS, MARKS, [[10, 10, 30, 30]] * 21, "tower_antenna", view=OBLIQUE,
                            runner=RecordedModel())


def test_a_picture_that_is_not_rgb8_is_refused():
    with pytest.raises(FinderInputError):
        example_finder.find(PIXELS.astype(np.float32), MARKS, [], "tower_dish", view=OBLIQUE,
                            runner=RecordedModel())
    with pytest.raises(FinderInputError):
        example_finder.find(b"not a picture", MARKS, [], "tower_dish", view=OBLIQUE, runner=RecordedModel())


# --------------------------------------------------------------------------- #
# Only pictures like the measured ones (answer_levels: the way they look, the size gate)
# --------------------------------------------------------------------------- #

def _field(side: int) -> np.ndarray:
    """A nadir picture of side x side px (its content does not matter to the recorded model)."""
    return np.asarray(Image.fromarray(PIXELS).resize((side, side), Image.Resampling.BOX))


PLANT_MARKS = [[100, 100, 140, 140], [300, 200, 340, 240], [200, 400, 240, 440]]  # 40 px plants


def test_a_picture_taken_another_way_than_measured_is_refused():
    model = RecordedModel()
    with pytest.raises(FinderInputError, match="dishes were measured only on photos taken at an angle, not on "
                                               "pictures looking straight down"):
        example_finder.find(PIXELS, MARKS, [], "tower_dish", view=NADIR, runner=model)
    with pytest.raises(FinderInputError, match="maize plants were measured only on pictures looking straight down"):
        example_finder.find(_field(600), PLANT_MARKS, [], "maize_seedling", view=OBLIQUE, runner=model)
    with pytest.raises(FinderInputError, match="a picture looks one of"):
        example_finder.find(PIXELS, MARKS, [], "tower_dish", view="sideways", runner=model)
    assert model.calls == []


def test_marked_objects_smaller_than_measured_are_refused_and_the_same_marks_pass_on_a_smaller_window():
    """40 px plants in a 2048 px window are read at 25 px (shrunk to 640, read at 1280): below the 34 px maize
    plants were measured from. In a 600 px window the same plants are read at 85 px and the finder runs."""
    model = RecordedModel()
    with pytest.raises(FinderInputError) as refused:
        example_finder.find(_field(2048), PLANT_MARKS, [], "maize_seedling", view=NADIR, runner=model)
    assert str(refused.value) == answer_levels.size_gate("maize_seedling", 2048, [(40, 40)] * 3).reason
    assert "were measured only from 34 px wide, so this picture is outside the measurement" in str(refused.value)
    assert model.calls == []
    example_finder.find(_field(600), PLANT_MARKS, [], "maize_seedling", view=NADIR, runner=model)
    (call,) = model.calls
    assert call["rgb"].shape == (600, 600, 3) and call["imgsz"] == 1280  # 600 px is under the measured 640


def test_a_target_whose_size_was_never_measured_is_refused():
    with pytest.raises(FinderInputError, match="no object size was measured for tree crowns"):
        example_finder.find(_field(600), PLANT_MARKS, [], "tree_crown_forest", view=NADIR, runner=RecordedModel())


def test_the_gate_reads_the_marks_in_the_pictures_own_pixels():
    s = example_finder.settings("tower_dish")
    gate = example_finder.check_picture(s, OBLIQUE, 2666, 1500, [[v * 2 for v in m] for m in MARKS])
    same = example_finder.check_picture(s, OBLIQUE, 1333, 750, MARKS)  # the finder reads both alike
    assert gate.passes and gate.object_px == pytest.approx(same.object_px) and gate.needed_px == 17


# --------------------------------------------------------------------------- #
# The measured protocol around the model
# --------------------------------------------------------------------------- #

def test_the_model_is_called_as_measured_and_finds_on_the_marks_are_dropped():
    model = RecordedModel()
    finds = example_finder.find(PHOTO, MARKS, [], "tower_dish", view=OBLIQUE, runner=model)
    (call,) = model.calls
    s = example_finder.settings("tower_dish")
    assert np.array_equal(call["rgb"], PIXELS)  # 1333 px is the measured size: not shrunk
    assert call["prompts"] == MARKS and call["classes"] == [0, 0, 0]
    assert (call["imgsz"], call["conf"], call["max_det"]) == (s.finder_input_px, 0.01, 300)
    expected = _expected_finds(MARKS)
    assert len(finds) == len(expected) < len(RECORDED["boxes"]["conf"])  # the marks themselves were found again
    for f, row in zip(finds, expected):
        assert f.box == pytest.approx(row[:4], abs=0.01) and f.score == pytest.approx(row[4], abs=1e-4)
    assert all(boxes.iou(f.box, m) < example_finder.MARK_IOU for f in finds for m in MARKS)
    # the dishes nobody marked are among the finds
    unmarked = [b for b in SPEC["dish_boxes"] if b not in MARKS]
    assert sum(any(boxes.iou(f.box, d) >= 0.5 for f in finds) for d in unmarked) >= 3


def test_a_larger_picture_is_shrunk_to_the_measured_size_and_finds_come_back_in_its_pixels():
    big = PIXELS.repeat(2, axis=0).repeat(2, axis=1)  # 2666 x 1500: each pixel a 2 x 2 block
    big_marks = [[v * 2 for v in m] for m in MARKS]
    model = RecordedModel()
    finds = example_finder.find(big, big_marks, [], "tower_dish", view=OBLIQUE, runner=model)
    (call,) = model.calls
    assert np.array_equal(call["rgb"], PIXELS)  # shrunk back to 1333 px, exactly the measured picture
    assert np.allclose(call["prompts"], MARKS)
    small = example_finder.find(PIXELS, MARKS, [], "tower_dish", view=OBLIQUE, runner=RecordedModel())
    assert len(finds) == len(small)
    for f, g in zip(finds, small):
        assert f.box == pytest.approx([v * 2 for v in g.box], abs=0.02)
    assert max(f.box[2] for f in finds) > PIXELS.shape[1]  # in the large picture's pixels


def test_lookalike_marks_are_a_second_class_and_its_finds_are_not_given():
    looks = [SPEC["dish_boxes"][1]]  # any box: a look-alike prompt
    model = RecordedModel(lookalike_rows=2)
    finds = example_finder.find(PIXELS, ANTENNA_MARKS, looks, "tower_antenna", view=OBLIQUE, runner=model)
    (call,) = model.calls
    assert call["classes"] == [0, 0, 0, 1] and call["prompts"][-1] == looks[0]
    rows = example_finder_model.result_rows(recorded_result())[2:]
    kept = [example_finder.Find(box=tuple(r[:4]), score=r[4]) for r in rows
            if not any(boxes.iou(r[:4], m) >= 0.3 for m in call["prompts"][:3])]
    # antennas use the suggestion filter (measured not to lower their AP50)
    shown = example_finder.filter_suggestions(kept, call["prompts"][:3], example_finder.settings("tower_antenna"))
    assert len(finds) == len(shown) < len(kept)


# --------------------------------------------------------------------------- #
# The suggestion filter: on only for targets where it was measured not to lower AP50
# --------------------------------------------------------------------------- #

# The function on its own, whichever targets the table turns it on for (test_answer_levels.py checks that).
FILTER_ON = dataclasses.replace(example_finder.settings("tower_dish"), suggestion_filter=True)
FILTER_OFF = dataclasses.replace(FILTER_ON, suggestion_filter=False)


def _square(x: float, side: float) -> tuple[float, float, float, float]:
    """A square box of the given side whose left edge is at x (one row: boxes far apart do not overlap)."""
    return (x, 0.0, x + side, side)


def _finds(*boxes_scores):
    return [example_finder.Find(box=box, score=score) for box, score in boxes_scores]


def test_the_filter_keeps_finds_sized_like_the_median_mark():
    """Marks of 100, 400 and 10,000 px2: the median is 400, so finds from 100 to 1,600 px2 are kept. With the mean
    (3,500) the 100 px2 find would go and the 3,600 px2 one would stay."""
    marks = [_square(0, 10), _square(100, 20), _square(200, 100)]
    finds = _finds((_square(1000, 9.9), 0.9), (_square(2000, 10), 0.8), (_square(3000, 40), 0.7),
                   (_square(4000, 40.1), 0.6), (_square(5000, 60), 0.5))
    shown = example_finder.filter_suggestions(finds, marks, FILTER_ON)
    assert [f.box for f in shown] == [_square(2000, 10), _square(3000, 40)]
    assert [boxes.area(f.box) for f in shown] == [100, 1600]  # both bounds are kept


def test_the_filter_keeps_the_better_of_two_overlapping_finds():
    marks = [_square(500, 20)] * 3  # 400 px2
    best = example_finder.Find(box=(0.0, 0.0, 20.0, 20.0), score=0.9)
    half = example_finder.Find(box=(0.0, 0.0, 20.0, 10.0), score=0.5)  # IoU with the best: exactly 0.5
    under = example_finder.Find(box=(0.0, 0.0, 20.0, 9.9), score=0.4)  # IoU 0.495
    near = example_finder.Find(box=(2.0, 0.0, 22.0, 20.0), score=0.3)  # IoU 0.82
    assert boxes.iou(best.box, half.box) == 0.5
    shown = example_finder.filter_suggestions([near, under, half, best], marks, FILTER_ON)  # worst first
    assert shown == [best, under]


def test_the_filter_keeps_at_most_50_the_best_ones():
    marks = [_square(0, 20)] * 3
    finds = _finds(*((_square(100 * i, 20), round(0.01 * (i + 1), 2)) for i in range(60)))
    random.Random(0).shuffle(finds)
    shown = example_finder.filter_suggestions(finds, marks, FILTER_ON)
    assert example_finder.FILTER_MAX == 50 and len(shown) == 50
    assert [f.score for f in shown] == sorted((f.score for f in finds), reverse=True)[:50]


def test_with_the_filter_off_the_finds_are_given_as_they_are():
    finds = _finds((_square(0, 20), 0.2), (_square(1, 20), 0.9), (_square(1000, 1), 0.5))  # it would drop two
    marks = [_square(500, 20)] * 3
    assert example_finder.filter_suggestions(finds, marks, FILTER_OFF) == finds
    assert len(example_finder.filter_suggestions(finds, marks, FILTER_ON)) == 1
    # Through the finder, dishes (off) still get every find not on a mark: test_the_model_is_called_as_measured...


def test_content_sha_changes_with_every_input():
    s = example_finder.settings("tower_dish")
    base = example_finder.content_sha("p", [tuple(MARKS[0])], [], s)
    assert base == example_finder.content_sha("p", [tuple(MARKS[0])], [], s)
    assert example_finder.is_job_id(base)
    others = {
        example_finder.content_sha("q", [tuple(MARKS[0])], [], s),
        example_finder.content_sha("p", [tuple(MARKS[1])], [], s),
        example_finder.content_sha("p", [tuple(MARKS[0])], [tuple(MARKS[1])], s),
        example_finder.content_sha("p", [tuple(MARKS[0])], [], example_finder.settings("tower_antenna")),
        example_finder.content_sha("p", [tuple(MARKS[0])], [], dataclasses.replace(s, suggestion_filter=True)),
    }
    assert base not in others and len(others) == 5


def test_job_ids_and_keys_refuse_anything_else():
    assert not example_finder.is_job_id("../" + "a" * 61)
    assert not example_finder.is_job_id("A" * 64)
    with pytest.raises(ValueError):
        example_finder.result_key("P1/../P2", "a" * 64)


# --------------------------------------------------------------------------- #
# The model's weights and the finder process (no model run here)
# --------------------------------------------------------------------------- #

def test_weights_come_from_storage_by_their_key_and_a_mismatch_is_refused(tmp_path, monkeypatch):
    asked = []

    class Store:  # boto3's download_file(bucket, key, path)
        def __init__(self, data):
            self.data = data

        def download_file(self, bucket, key, path):
            asked.append((bucket, key))
            Path(path).write_bytes(self.data)

    monkeypatch.setattr(example_finder_model, "_WEIGHTS_PATH", str(tmp_path / "models" / "yoloe.pt"))
    monkeypatch.setattr(example_finder_model, "MODEL_SHA256", hashlib.sha256(b"weights").hexdigest())
    with pytest.raises(RuntimeError, match="does not match its sha256"):
        example_finder_model.ensure_weights(Store(b"other"), "bucket")
    assert not (tmp_path / "models" / "yoloe.pt").exists()
    assert example_finder_model.ensure_weights(Store(b"weights"), "bucket") == str(tmp_path / "models" / "yoloe.pt")
    assert asked == [("bucket", example_finder_model.MODEL_KEY)] * 2


@pytest.mark.parametrize("raw, threads", [(None, 4), ("2", 2), (" 8 ", 8)])
def test_finder_threads(monkeypatch, raw, threads):
    if raw is None:
        monkeypatch.delenv("FINDER_THREADS", raising=False)
    else:
        monkeypatch.setenv("FINDER_THREADS", raw)
    assert example_finder_model.threads() == threads


@pytest.mark.parametrize("raw", ["0", "-1", "four"])
def test_finder_threads_refuses_junk(monkeypatch, raw):
    monkeypatch.setenv("FINDER_THREADS", raw)
    with pytest.raises(ValueError):
        example_finder_model.threads()


def test_a_picture_that_takes_too_long_stops_the_finder_process(monkeypatch):
    monkeypatch.setattr(example_finder_model, "WAIT_LIMIT_S", 0.2)  # the process is still starting
    with pytest.raises(TimeoutError):
        example_finder_model.run("/nowhere.pt", PIXELS, MARKS, [0, 0, 0], 1280, 0.01, 300)
    assert example_finder_model._process is None


@pytest.mark.timeout(600)
def test_the_finder_process_settings_keep_ultralytics_offline():
    """What the finder process sets before importing Ultralytics, checked against Ultralytics itself."""
    code = "import ultralytics.utils as u; print(repr((u.ONLINE, u.AUTOINSTALL)))"
    done = subprocess.run([sys.executable, "-c", code], env={**os.environ, **example_finder_model.OFFLINE_ENV},
                          capture_output=True, text=True, timeout=500)
    assert done.returncode == 0, done.stderr[-2000:]
    assert done.stdout.strip().splitlines()[-1] == "(False, False)"


@pytest.mark.timeout(600)
def test_a_model_failure_comes_back_and_an_idle_finder_process_stops_itself(tmp_path, monkeypatch):
    if example_finder_model._process is not None:  # a process started earlier, with the usual idle time
        example_finder_model._process.stop()
    monkeypatch.setattr(example_finder_model, "_process", None)
    monkeypatch.setattr(example_finder_model, "IDLE_EXIT_S", 3.0)
    junk = tmp_path / "junk.pt"
    junk.write_bytes(b"these are not weights")
    with pytest.raises(RuntimeError, match="the finder failed"):
        example_finder_model.run(str(junk), PIXELS, MARKS, [0, 0, 0], 1280, 0.01, 300)
    process = example_finder_model._process
    assert process is not None and process.process.is_alive() and process.process.pid != os.getpid()
    process.process.join(30)  # idle for 3 s: it stops by itself, giving its memory back
    assert process.process.exitcode == 0
    with pytest.raises(RuntimeError, match="the finder failed"):  # the next picture starts a new one
        example_finder_model.run(str(junk), PIXELS, MARKS, [0, 0, 0], 1280, 0.01, 300)
    started = example_finder_model._process
    assert started is not process and started.process.is_alive()
    example_finder_model.stop()  # told to stop (the next job is another worker's): its memory goes back at once
    assert example_finder_model._process is None and not started.process.is_alive()
    example_finder_model.stop()  # nothing to stop: nothing happens


# --------------------------------------------------------------------------- #
# Jobs: kept per project, run once across workers, one picture at a time
# --------------------------------------------------------------------------- #

class TowerPhoto:
    """A picture source giving the committed tower photo (boxes in its own pixels)."""

    def __init__(self, pixels=PIXELS):
        self.pixels = pixels

    async def read(self) -> Picture:
        return Picture(pixels=self.pixels, sha256=hashlib.sha256(self.pixels.tobytes()).hexdigest(), view=OBLIQUE)


def _redis() -> Redis:
    return Redis(host=os.environ.get("REDIS_HOST", "localhost"), port=int(os.environ.get("REDIS_PORT", 6379)), db=15)


@pytest.fixture
def isolated(monkeypatch):
    """A Redis key prefix of its own (with database 15) and object-storage keys removed afterwards."""
    monkeypatch.setattr(example_finder, "REDIS_PREFIX", f"example_finder_test:{RUN_TAG}:{uuid.uuid4().hex[:6]}")
    projects: list[str] = []

    def project() -> str:
        projects.append(f"Ptest{RUN_TAG}{len(projects)}")
        return projects[-1]

    yield project
    store, bucket = get_s3_client(), get_bucket_name()
    for p in projects:
        listed = store.list_objects_v2(Bucket=bucket, Prefix=f"{example_finder.STORE_PREFIX}/{p}/")
        for item in listed.get("Contents", []):
            store.delete_object(Bucket=bucket, Key=item["Key"])


async def _until_settled(redis, s3, bucket, project, job_id, limit_s=30.0):
    deadline = time.monotonic() + limit_s
    while time.monotonic() < deadline:
        job = await example_finder.status(redis, s3, bucket, project, job_id)
        if job is not None and job.status in (example_finder.DONE, example_finder.FAILED):
            return job
        await asyncio.sleep(0.1)
    raise AssertionError(f"job {job_id} did not finish in {limit_s} s")


@pytest.mark.anyio
async def test_a_second_request_reuses_the_kept_finds(isolated):
    redis, s3, bucket, project = _redis(), await get_async_s3_client(), get_bucket_name(), isolated()
    model = RecordedModel()
    try:
        job = await example_finder.start(redis, s3, bucket, project, USER, TowerPhoto(), MARKS, [], "tower_dish",
                                         runner=model)
        assert job.status == example_finder.QUEUED
        done = await _until_settled(redis, s3, bucket, project, job.job_id)
        assert done.status == example_finder.DONE and len(done.finds) == len(_expected_finds(MARKS))
        assert set(done.finds[0]) == {"box", "box_px", "score"} and done.finds[0]["box"] == done.finds[0]["box_px"]
        again = await example_finder.start(redis, s3, bucket, project, USER, TowerPhoto(), MARKS, [], "tower_dish",
                                           runner=model)
        assert again.status == example_finder.DONE and again.finds == done.finds and again.job_id == job.job_id
        assert len(model.calls) == 1  # never analysed twice
        stored = json.loads(await (await s3.get_object(Bucket=bucket, Key=example_finder.result_key(
            project, job.job_id)))["Body"].read())
        assert stored["model"]["sha256"] == example_finder_model.MODEL_SHA256 and stored["marks_px"] == MARKS
        assert stored["settings"] == example_finder.settings("tower_dish").as_dict()
    finally:
        await redis.aclose()


@pytest.mark.anyio
async def test_a_job_keeps_the_filtered_finds_where_the_filter_is_on(isolated):
    """Antennas use the suggestion filter: a job keeps the finds `find` gives, fewer than the protocol alone, and
    its record says the filter was on and with which values."""
    redis, s3, bucket, project = _redis(), await get_async_s3_client(), get_bucket_name(), isolated()
    try:
        job = await example_finder.start(redis, s3, bucket, project, USER, TowerPhoto(), ANTENNA_MARKS, [],
                                         "tower_antenna", runner=RecordedModel())
        done = await _until_settled(redis, s3, bucket, project, job.job_id)
        expected = example_finder.find(PIXELS, ANTENNA_MARKS, [], "tower_antenna", view=OBLIQUE,
                                       runner=RecordedModel())
        assert [f["box_px"] for f in done.finds] == [list(f.box) for f in expected]
        assert 0 < len(expected) < len(_expected_finds(ANTENNA_MARKS))
        stored = json.loads(await (await s3.get_object(Bucket=bucket, Key=example_finder.result_key(
            project, job.job_id)))["Body"].read())
        assert stored["settings"]["suggestion_filter"] is True
        assert stored["protocol"]["suggestion_filter"] == {"area_ratio": 4.0, "iou": 0.5, "max": 50}
    finally:
        await redis.aclose()


@pytest.mark.anyio
async def test_two_workers_sharing_redis_run_the_request_once(isolated):
    one, two = _redis(), _redis()  # two app workers: separate connections, the same Redis
    s3, bucket, project = await get_async_s3_client(), get_bucket_name(), isolated()
    model = RecordedModel(delay_s=1.0)
    try:
        first, second = await asyncio.gather(
            example_finder.start(one, s3, bucket, project, USER, TowerPhoto(), MARKS, [], "tower_dish", runner=model),
            example_finder.start(two, s3, bucket, project, USER, TowerPhoto(), MARKS, [], "tower_dish", runner=model))
        assert first.job_id == second.job_id
        assert {first.status, second.status} <= {example_finder.QUEUED, example_finder.RUNNING}
        done_one = await _until_settled(one, s3, bucket, project, first.job_id)
        done_two = await _until_settled(two, s3, bucket, project, first.job_id)
        assert done_one.status == done_two.status == example_finder.DONE and done_one.finds == done_two.finds
        assert len(model.calls) == 1
    finally:
        await one.aclose()
        await two.aclose()


@pytest.mark.anyio
async def test_finds_are_kept_per_project(isolated):
    redis, s3, bucket = _redis(), await get_async_s3_client(), get_bucket_name()
    mine, theirs = isolated(), isolated()
    model = RecordedModel()
    try:
        job = await example_finder.start(redis, s3, bucket, mine, USER, TowerPhoto(), MARKS, [], "tower_dish",
                                         runner=model)
        await _until_settled(redis, s3, bucket, mine, job.job_id)
        assert await example_finder.status(redis, s3, bucket, theirs, job.job_id) is None
        other = await example_finder.start(redis, s3, bucket, theirs, USER, TowerPhoto(), MARKS, [], "tower_dish",
                                           runner=model)
        assert other.job_id == job.job_id and other.status == example_finder.QUEUED  # not served from `mine`
        await _until_settled(redis, s3, bucket, theirs, job.job_id)
        assert len(model.calls) == 2
        assert example_finder.result_key(mine, job.job_id) != example_finder.result_key(theirs, job.job_id)
    finally:
        await redis.aclose()


@pytest.mark.anyio
async def test_a_failed_job_says_why_and_starting_it_again_retries(isolated):
    redis, s3, bucket, project = _redis(), await get_async_s3_client(), get_bucket_name(), isolated()
    try:
        job = await example_finder.start(redis, s3, bucket, project, USER, TowerPhoto(), MARKS, [], "tower_dish",
                                         runner=RecordedModel(fail="the model ran out of memory"))
        failed = await _until_settled(redis, s3, bucket, project, job.job_id)
        assert failed.status == example_finder.FAILED and "ran out of memory" in failed.error
        assert failed.as_dict() == {"job_id": job.job_id, "status": "failed", "error": failed.error}
        model = RecordedModel()
        again = await example_finder.start(redis, s3, bucket, project, USER, TowerPhoto(), MARKS, [], "tower_dish",
                                           runner=model)
        assert again.status == example_finder.QUEUED
        assert (await _until_settled(redis, s3, bucket, project, job.job_id)).status == example_finder.DONE
        assert len(model.calls) == 1
    finally:
        await redis.aclose()


@pytest.mark.anyio
async def test_one_picture_at_a_time_in_the_whole_app(isolated):
    one, two = _redis(), _redis()
    s3, bucket, project = await get_async_s3_client(), get_bucket_name(), isolated()
    model = RecordedModel(delay_s=0.5)
    try:
        jobs = await asyncio.gather(
            example_finder.start(one, s3, bucket, project, USER, TowerPhoto(), MARKS, [], "tower_dish", runner=model),
            example_finder.start(two, s3, bucket, project, USER, TowerPhoto(), MARKS[:2], [], "tower_dish",
                                 runner=model))
        assert jobs[0].job_id != jobs[1].job_id  # two different requests
        for job, redis in zip(jobs, (one, two)):
            assert (await _until_settled(redis, s3, bucket, project, job.job_id)).status == example_finder.DONE
        assert len(model.calls) == 2 and model.most_at_once == 1
    finally:
        await one.aclose()
        await two.aclose()


@pytest.mark.anyio
async def test_a_bad_request_is_refused_before_anything_is_read_or_kept(isolated):
    redis, s3, bucket, project = _redis(), await get_async_s3_client(), get_bucket_name(), isolated()

    class NeverRead:
        async def read(self):
            raise AssertionError("the picture must not be read for a bad request")

    try:
        with pytest.raises(FinderInputError):
            await example_finder.start(redis, s3, bucket, project, USER, NeverRead(), [], [], "tower_dish")
        with pytest.raises(FinderInputError):
            await example_finder.start(redis, s3, bucket, project, USER, NeverRead(), MARKS, [], "banana_mat")
        with pytest.raises(FinderInputError, match="not inside the picture"):
            await example_finder.start(redis, s3, bucket, project, USER, TowerPhoto(), [[1300, 700, 1400, 800]], [],
                                       "tower_dish")
    finally:
        await redis.aclose()


# --------------------------------------------------------------------------- #
# The line: capped, first come first served, waiting jobs keep only the shrunk picture, one finder process
# --------------------------------------------------------------------------- #

# Six different requests on the tower photo (each passes the size gate for dishes).
VARIANTS = [MARKS, MARKS[:2], MARKS[1:], MARKS[:1], MARKS[1:2], MARKS[2:]]


@pytest.fixture
def small_line(monkeypatch):
    monkeypatch.setattr(example_finder, "MAX_JOBS_PER_USER", 2)
    monkeypatch.setattr(example_finder, "MAX_JOBS", 3)
    monkeypatch.setattr(example_finder, "QUEUE_POLL_S", 0.05)


@pytest.mark.anyio
async def test_the_line_is_capped_per_user_and_for_the_whole_app(isolated, small_line):
    redis, s3, bucket, project = _redis(), await get_async_s3_client(), get_bucket_name(), isolated()
    model = RecordedModel(delay_s=0.3)

    async def ask(user, marks):
        return await example_finder.start(redis, s3, bucket, project, user, TowerPhoto(), marks, [], "tower_dish",
                                          runner=model)

    try:
        first = [await ask("ann", VARIANTS[0]), await ask("ann", VARIANTS[1])]
        with pytest.raises(example_finder.FinderBusy, match="you already have 2 finder jobs") as mine:
            await ask("ann", VARIANTS[2])
        assert mine.value.whole_app is False
        first.append(await ask("bob", VARIANTS[2]))  # another user still has room
        with pytest.raises(example_finder.FinderBusy, match="the finder already has 3 pictures") as everyone:
            await ask("cat", VARIANTS[3])
        assert everyone.value.whole_app is True
        assert await redis.zcard(example_finder.queue_key()) == 3
        for job in first:
            assert (await _until_settled(redis, s3, bucket, project, job.job_id)).status == example_finder.DONE
        assert await redis.zcard(example_finder.queue_key()) == 0  # finished jobs leave the line
        later = await ask("cat", VARIANTS[3])  # refused requests left nothing behind: asking again works
        assert later.status == example_finder.QUEUED
        assert (await _until_settled(redis, s3, bucket, project, later.job_id)).status == example_finder.DONE
        assert len(model.calls) == 4
    finally:
        await redis.aclose()


@pytest.mark.anyio
async def test_jobs_run_first_come_first_served_across_workers(isolated, monkeypatch):
    monkeypatch.setattr(example_finder, "QUEUE_POLL_S", 0.05)
    one, two = _redis(), _redis()
    s3, bucket, project = await get_async_s3_client(), get_bucket_name(), isolated()
    model = RecordedModel(delay_s=0.2)
    try:
        jobs = []
        for i, marks in enumerate(VARIANTS):  # alternating workers and users, in this order
            jobs.append(await example_finder.start((one, two)[i % 2], s3, bucket, project, f"user{i}", TowerPhoto(),
                                                   marks, [], "tower_dish", runner=model))
        for job in jobs:
            assert (await _until_settled(one, s3, bucket, project, job.job_id)).status == example_finder.DONE
        assert [call["prompts"] for call in model.calls] == [[list(m) for m in v] for v in VARIANTS]
        assert model.most_at_once == 1
    finally:
        await one.aclose()
        await two.aclose()


@pytest.mark.anyio
async def test_a_place_whose_worker_died_neither_blocks_the_line_nor_counts(isolated, small_line):
    redis, s3, bucket, project = _redis(), await get_async_s3_client(), get_bucket_name(), isolated()
    model = RecordedModel()
    dead = [json.dumps(["ghost", project, f"{i:064x}"], separators=(",", ":")) for i in range(3)]
    try:
        await redis.zadd(example_finder.queue_key(), {place: -1 - i for i, place in enumerate(dead)})  # in front
        job = await example_finder.start(redis, s3, bucket, project, USER, TowerPhoto(), MARKS, [], "tower_dish",
                                         runner=model)  # the line looked full, but those jobs have no lock
        assert (await _until_settled(redis, s3, bucket, project, job.job_id)).status == example_finder.DONE
        assert await redis.zcard(example_finder.queue_key()) == 0
    finally:
        await redis.aclose()


@pytest.mark.anyio
async def test_a_waiting_job_keeps_only_the_picture_the_finder_is_given(isolated, monkeypatch):
    redis, s3, bucket, project = _redis(), await get_async_s3_client(), get_bucket_name(), isolated()
    kept = []
    real_run = example_finder._run

    def run(redis_, s3_, bucket_, request, runner, lock):  # called by start(); the job runs later, in a task
        kept.append(request)
        return real_run(redis_, s3_, bucket_, request, runner, lock)

    monkeypatch.setattr(example_finder, "_run", run)
    big = PIXELS.repeat(2, axis=0).repeat(2, axis=1)  # a 2666 x 1500 picture, 12 MB
    try:
        job = await example_finder.start(redis, s3, bucket, project, USER, TowerPhoto(big),
                                         [[v * 2 for v in m] for m in MARKS], [], "tower_dish", runner=RecordedModel())
        (request,) = kept
        arrays = [v for v in vars(request.shown).values() if isinstance(v, np.ndarray)]
        arrays += [v for v in vars(request).values() if isinstance(v, np.ndarray)]
        assert [a.shape for a in arrays] == [(750, 1333, 3)]  # shrunk to the measured 1333 px before waiting
        done = await _until_settled(redis, s3, bucket, project, job.job_id)
        assert max(f["box_px"][2] for f in done.finds) > PIXELS.shape[1]  # finds in the large picture's pixels
    finally:
        await redis.aclose()


@pytest.mark.anyio
async def test_the_finder_process_stops_unless_this_worker_runs_the_next_job(isolated, monkeypatch):
    monkeypatch.setattr(example_finder, "QUEUE_POLL_S", 0.05)
    stops = []
    monkeypatch.setattr(example_finder_model, "stop", lambda: stops.append(time.monotonic()))
    redis, s3, bucket, project = _redis(), await get_async_s3_client(), get_bucket_name(), isolated()
    model = RecordedModel(delay_s=0.3)
    try:
        # Two jobs of this worker in a row: the process is kept for the second, stopped after it.
        mine = [await example_finder.start(redis, s3, bucket, project, USER, TowerPhoto(), marks, [], "tower_dish",
                                           runner=model) for marks in VARIANTS[:2]]
        for job in mine:
            await _until_settled(redis, s3, bucket, project, job.job_id)
        assert len(stops) == 1
        # A job of another worker waits behind this worker's: the process stops, so only one lives in the app.
        theirs = json.dumps(["other", project, "f" * 64], separators=(",", ":"))
        other_lock = redis.lock(example_finder._lock_key(project, "f" * 64), timeout=30, thread_local=False)
        assert await other_lock.acquire(blocking=False)
        job = await example_finder.start(redis, s3, bucket, project, USER, TowerPhoto(), VARIANTS[2], [],
                                         "tower_dish", runner=model)
        await redis.zadd(example_finder.queue_key(), {theirs: 10**9})
        await _until_settled(redis, s3, bucket, project, job.job_id)
        assert len(stops) == 2
        await redis.zrem(example_finder.queue_key(), theirs)
        await other_lock.release()
    finally:
        await redis.aclose()


def test_the_switch_is_off_by_default(monkeypatch):
    monkeypatch.delenv("EXAMPLE_FINDER", raising=False)
    assert example_finder.finder_enabled() is False
    for on in ("1", "true", "YES"):
        monkeypatch.setenv("EXAMPLE_FINDER", on)
        assert example_finder.finder_enabled() is True
    monkeypatch.setenv("EXAMPLE_FINDER", "0")
    assert example_finder.finder_enabled() is False


# --------------------------------------------------------------------------- #
# Trained detectors (hosted_detector): used only while HOSTED_FINDER is on
# --------------------------------------------------------------------------- #

def test_without_the_switch_towers_use_the_example_finder_and_power_parts_are_refused(monkeypatch):
    monkeypatch.delenv("HOSTED_FINDER", raising=False)
    assert example_finder.settings("tower_dish").model == example_finder.MODEL_NAME
    assert "model" not in example_finder.settings("tower_dish").as_dict()  # the example finder's requests keep their identity
    with pytest.raises(example_finder.FinderInputError, match="trained model, which is switched off"):
        example_finder.settings("power_insulator")


def test_with_the_switch_the_detector_runs_as_measured(monkeypatch):
    monkeypatch.setenv("HOSTED_FINDER", "1")
    s = example_finder.settings("power_insulator")
    d = answer_levels.detector("power_insulator")
    assert (s.model, s.detector_class, s.image_px, s.finder_input_px) == (d.finder_model, "insulator", 640, 640)
    assert not s.look_alike_marks and not s.suggestion_filter and s.uses_detector
    assert example_finder.settings("weed").model == example_finder.MODEL_NAME  # no detector: the example finder


def test_the_hosted_runner_keeps_its_class_and_never_puts_the_key_in_an_error(monkeypatch):
    from src.services import hosted_detector
    monkeypatch.setenv("HOSTED_FINDER", "1")
    monkeypatch.setenv("ROBOFLOW_API_KEY", "secret-key")
    sent = {}

    class Answer:
        status_code = 200

        @staticmethod
        def json():
            return {"predictions": [
                {"class": "insulator", "confidence": 0.9, "x": 50, "y": 40, "width": 20, "height": 10},
                {"class": "pylon", "confidence": 0.95, "x": 300, "y": 300, "width": 200, "height": 400},
                {"class": "insulator", "confidence": 0.3, "x": 100, "y": 90, "width": 10, "height": 30}]}

    def post(url, params, data, headers, timeout):
        sent.update(url=url, params=params)
        return Answer()

    monkeypatch.setattr(hosted_detector.requests, "post", post)
    s = example_finder.settings("power_insulator")
    rows = example_finder._runner_for(s)(np.zeros((64, 64, 3), np.uint8), [], [], 640, example_finder.CONF, example_finder.MAX_DET)
    assert rows == [(40.0, 35.0, 60.0, 45.0, 0.9, 0), (95.0, 75.0, 105.0, 105.0, 0.3, 0)]
    assert sent["url"].endswith("nests-towers-insulators-7d9vg-2-rfdetr-small-t1") and sent["params"]["confidence"] == 5

    Answer.status_code = 401
    with pytest.raises(hosted_detector.DetectorUnavailable) as refused:
        example_finder._runner_for(s)(np.zeros((64, 64, 3), np.uint8), [], [], 640, example_finder.CONF, example_finder.MAX_DET)
    assert "secret-key" not in str(refused.value) and "401" in str(refused.value)


def test_a_detector_without_a_key_is_a_plain_refusal(monkeypatch):
    monkeypatch.setenv("HOSTED_FINDER", "1")
    monkeypatch.delenv("ROBOFLOW_API_KEY", raising=False)
    with pytest.raises(example_finder.FinderInputError, match="no key"):
        example_finder._runner_for(example_finder.settings("tower_dish"))


def test_sam3_is_asked_for_the_prompt_and_every_box_is_the_target(monkeypatch):
    from src.services import hosted_detector
    monkeypatch.setenv("HOSTED_FINDER", "1")
    monkeypatch.setenv("ROBOFLOW_API_KEY", "secret-key")
    sent = {}

    class Answer:
        status_code = 200

        @staticmethod
        def json():
            return {"outputs": [{"out": {"predictions": [
                {"class_id": 0, "confidence": 0.8, "x": 30, "y": 30, "width": 10, "height": 20},
                {"class_id": 0, "confidence": 0.004, "x": 60, "y": 60, "width": 10, "height": 10}]}}]}

    def post(url, json, timeout):
        sent.update(url=url, body=json)
        return Answer()

    monkeypatch.setattr(hosted_detector.requests, "post", post)
    s = example_finder.settings("maize_seedling")
    assert s.model == "roboflow:sam3/sam3_final" and s.detector_class == "seedling" and s.view == answer_levels.NADIR
    rows = example_finder._runner_for(s)(np.zeros((64, 64, 3), np.uint8), [], [], 640, example_finder.CONF,
                                         example_finder.MAX_DET)
    assert rows == [(25.0, 20.0, 35.0, 40.0, 0.8, 0)]  # a box under the finder's CONF is dropped
    step = sent["body"]["specification"]["steps"][0]
    assert sent["url"].endswith("workflows/run") and step["class_names"] == ["seedling"] and step["confidence"] == 0.05
