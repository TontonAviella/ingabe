"""The example finder (src/services/example_finder.py): settings from the measured table, input checks, the measured
protocol around the model, content-addressed results kept per project, and jobs shared by app workers through Redis.

The model is replaced by `RecordedModel`, which returns a real Ultralytics result recorded on the committed tower
photo (test_fixtures/example_finder/recorded_yoloe_result.json, scripts/finder_eval/record_test_result.py), read
through the same `result_rows` the finder process uses. The real model runs in test_example_finder_model.py.
Redis (database 15, a key prefix per run) and object storage (MinIO) are the real services.
"""

from __future__ import annotations

import asyncio
import hashlib
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
from src.services.example_finder_pictures import FinderInputError, Picture
from src.utils import get_async_s3_client, get_bucket_name, get_s3_client

FIXTURES = Path(__file__).resolve().parents[2] / "test_fixtures" / "example_finder"
SPEC = json.loads((FIXTURES / "tower_dishes.json").read_text())
RECORDED = json.loads((FIXTURES / "recorded_yoloe_result.json").read_text())
PHOTO = (FIXTURES / SPEC["picture"]).read_bytes()
PIXELS = np.asarray(Image.open(io.BytesIO(PHOTO)).convert("RGB"))
MARKS = SPEC["marks"]
RUN_TAG = uuid.uuid4().hex[:8]


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
        assert (s.image_px, s.finder_input_px, s.look_alike_marks) == (t.measured["image_px"], t.finder_input_px,
                                                                       t.look_alike_marks)


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
        example_finder.find(PIXELS, marks, [], "tower_dish", runner=RecordedModel())
    assert says in str(raised.value)


def test_lookalike_marks_only_for_targets_measured_with_them():
    with pytest.raises(FinderInputError, match="look-alike marks are not used for dishes"):
        example_finder.find(PIXELS, MARKS, [[10, 10, 30, 30]], "tower_dish", runner=RecordedModel())
    with pytest.raises(FinderInputError, match="at most 20 look-alike marks"):
        example_finder.find(PIXELS, MARKS, [[10, 10, 30, 30]] * 21, "tower_antenna", runner=RecordedModel())


def test_a_picture_that_is_not_rgb8_is_refused():
    with pytest.raises(FinderInputError):
        example_finder.find(PIXELS.astype(np.float32), MARKS, [], "tower_dish", runner=RecordedModel())
    with pytest.raises(FinderInputError):
        example_finder.find(b"not a picture", MARKS, [], "tower_dish", runner=RecordedModel())


# --------------------------------------------------------------------------- #
# The measured protocol around the model
# --------------------------------------------------------------------------- #

def test_the_model_is_called_as_measured_and_finds_on_the_marks_are_dropped():
    model = RecordedModel()
    finds = example_finder.find(PHOTO, MARKS, [], "tower_dish", runner=model)
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
    finds = example_finder.find(big, big_marks, [], "tower_dish", runner=model)
    (call,) = model.calls
    assert np.array_equal(call["rgb"], PIXELS)  # shrunk back to 1333 px, exactly the measured picture
    assert np.allclose(call["prompts"], MARKS)
    small = example_finder.find(PIXELS, MARKS, [], "tower_dish", runner=RecordedModel())
    assert len(finds) == len(small)
    for f, g in zip(finds, small):
        assert f.box == pytest.approx([v * 2 for v in g.box], abs=0.02)
    assert max(f.box[2] for f in finds) > PIXELS.shape[1]  # in the large picture's pixels


def test_lookalike_marks_are_a_second_class_and_its_finds_are_not_given():
    looks = [SPEC["dish_boxes"][1]]  # any box: a look-alike prompt
    model = RecordedModel(lookalike_rows=2)
    finds = example_finder.find(PIXELS, MARKS, looks, "tower_antenna", runner=model)
    (call,) = model.calls
    assert call["classes"] == [0, 0, 0, 1] and call["prompts"][-1] == looks[0]
    rows = example_finder_model.result_rows(recorded_result())[2:]
    kept = [r for r in rows if not any(boxes.iou(r[:4], m) >= 0.3 for m in MARKS)]
    assert len(finds) == len(kept)


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
    }
    assert base not in others and len(others) == 4


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
def test_a_model_failure_in_the_finder_process_comes_back_to_the_caller(tmp_path):
    junk = tmp_path / "junk.pt"
    junk.write_bytes(b"these are not weights")
    with pytest.raises(RuntimeError, match="the finder failed"):
        example_finder_model.run(str(junk), PIXELS, MARKS, [0, 0, 0], 1280, 0.01, 300)
    process = example_finder_model._process
    assert process is not None and process.process.is_alive() and process.process.pid != os.getpid()


# --------------------------------------------------------------------------- #
# Jobs: kept per project, run once across workers, one picture at a time
# --------------------------------------------------------------------------- #

class TowerPhoto:
    """A picture source giving the committed tower photo (boxes in its own pixels)."""

    def __init__(self, pixels=PIXELS):
        self.pixels = pixels

    async def read(self) -> Picture:
        return Picture(pixels=self.pixels, sha256=hashlib.sha256(self.pixels.tobytes()).hexdigest())


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
        job = await example_finder.start(redis, s3, bucket, project, TowerPhoto(), MARKS, [], "tower_dish",
                                         runner=model)
        assert job.status == example_finder.QUEUED
        done = await _until_settled(redis, s3, bucket, project, job.job_id)
        assert done.status == example_finder.DONE and len(done.finds) == len(_expected_finds(MARKS))
        assert set(done.finds[0]) == {"box", "box_px", "score"} and done.finds[0]["box"] == done.finds[0]["box_px"]
        again = await example_finder.start(redis, s3, bucket, project, TowerPhoto(), MARKS, [], "tower_dish",
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
async def test_two_workers_sharing_redis_run_the_request_once(isolated):
    one, two = _redis(), _redis()  # two app workers: separate connections, the same Redis
    s3, bucket, project = await get_async_s3_client(), get_bucket_name(), isolated()
    model = RecordedModel(delay_s=1.0)
    try:
        first, second = await asyncio.gather(
            example_finder.start(one, s3, bucket, project, TowerPhoto(), MARKS, [], "tower_dish", runner=model),
            example_finder.start(two, s3, bucket, project, TowerPhoto(), MARKS, [], "tower_dish", runner=model))
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
        job = await example_finder.start(redis, s3, bucket, mine, TowerPhoto(), MARKS, [], "tower_dish",
                                         runner=model)
        await _until_settled(redis, s3, bucket, mine, job.job_id)
        assert await example_finder.status(redis, s3, bucket, theirs, job.job_id) is None
        other = await example_finder.start(redis, s3, bucket, theirs, TowerPhoto(), MARKS, [], "tower_dish",
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
        job = await example_finder.start(redis, s3, bucket, project, TowerPhoto(), MARKS, [], "tower_dish",
                                         runner=RecordedModel(fail="the model ran out of memory"))
        failed = await _until_settled(redis, s3, bucket, project, job.job_id)
        assert failed.status == example_finder.FAILED and "ran out of memory" in failed.error
        assert failed.as_dict() == {"job_id": job.job_id, "status": "failed", "error": failed.error}
        model = RecordedModel()
        again = await example_finder.start(redis, s3, bucket, project, TowerPhoto(), MARKS, [], "tower_dish",
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
            example_finder.start(one, s3, bucket, project, TowerPhoto(), MARKS, [], "tower_dish", runner=model),
            example_finder.start(two, s3, bucket, project, TowerPhoto(), MARKS[:2], [], "tower_dish", runner=model))
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
            await example_finder.start(redis, s3, bucket, project, NeverRead(), [], [], "tower_dish")
        with pytest.raises(FinderInputError):
            await example_finder.start(redis, s3, bucket, project, NeverRead(), MARKS, [], "banana_mat")
        with pytest.raises(FinderInputError, match="not inside the picture"):
            await example_finder.start(redis, s3, bucket, project, TowerPhoto(), [[1300, 700, 1400, 800]], [],
                                       "tower_dish")
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
