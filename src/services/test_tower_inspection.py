"""Tower inspections, the domain (src/services/tower_inspection.py): marks, look-alike marks from the measured settings,
the finder's suggestions, a person's review, counted by you only once every suggestion is checked, work kept per
project, and the inspection-wide facts (inventory, photos to check, camera facts, the same site before).

Redis (database 15, a key prefix per test) and object storage (MinIO) are the real services. The finder's model is
RecordedModel (a real Ultralytics result recorded on the committed tower photo, test_example_finder.py); the photo
is that committed tower photo, stored as an inspection's original."""

from __future__ import annotations

import asyncio
import hashlib
import time
import uuid
from dataclasses import replace

import pytest

from src.services import answer_levels, example_finder, photo_inspections, tower_inspection
from src.services.example_finder_pictures import FinderInputError, InspectionPhoto
from src.services.photo_inspections import PhotoFacts
from src.services.test_example_finder import ANTENNA_MARKS, MARKS, PHOTO, RecordedModel, _redis
from src.services.tower_inspection import Review, TowerConflict, TowerInputError
from src.utils import get_async_s3_client, get_bucket_name, get_s3_client

RUN_TAG = uuid.uuid4().hex[:8]
USER = f"user-{RUN_TAG}"
SHA = hashlib.sha256(PHOTO).hexdigest()
DISH, ANTENNA, RADIO = tower_inspection.PARTS


def facts(sha: str = SHA, name: str = "DJI_0395.JPG", pitch=-30.0, lat=-1.9441, lon=30.0619,
          taken="2026-10-09T14:03:22", **changes) -> PhotoFacts:
    """The committed tower photo's facts as an inspection keeps them (1333 x 750, taken at an angle)."""
    base = PhotoFacts(sha256=sha, filename=name, width=1333, height=750, taken_at=taken, lat=lat, lon=lon,
                      altitude_m=1530.0, relative_altitude_m=45.2, gimbal_pitch=pitch, gimbal_yaw=-87.3,
                      flight_yaw=-88.1, make="DJI", model="M3E", orientation=1)
    return replace(base, **changes)


@pytest.fixture(autouse=True)
def _example_finder_for_towers(monkeypatch):
    """These tests run the example finder (YOLOE) on tower photos: our own detectors are switched off here, as the way
    back does (OWN_DETECTORS=0). The tests of our detectors switch them on themselves."""
    monkeypatch.setenv("OWN_DETECTORS", "0")


@pytest.fixture
def towers(monkeypatch):
    """Each call gives (project, layer, layer metadata) with the tower photo stored as the inspection's original;
    jobs and locks in a Redis prefix of the test's own; the recorded model instead of the weights. Everything stored
    is removed afterwards."""
    tag = f"{RUN_TAG}{uuid.uuid4().hex[:6]}"
    monkeypatch.setattr(example_finder, "REDIS_PREFIX", f"example_finder_test:{tag}")
    monkeypatch.setattr(tower_inspection, "REDIS_PREFIX", f"towers_test:{tag}")
    model = RecordedModel()
    monkeypatch.setattr(example_finder, "_stored_model", lambda: model)
    made: list[tuple[str, str]] = []
    store, bucket = get_s3_client(), get_bucket_name()

    def inspection(photo: bytes = PHOTO) -> tuple[str, str, dict]:
        project, layer = f"Ptw{tag}{len(made)}", f"Ltw{tag}{len(made)}"
        prefix = photo_inspections.storage_prefix(project, layer)
        store.put_object(Bucket=bucket, Key=f"{prefix}{hashlib.sha256(photo).hexdigest()}.jpg", Body=photo)
        made.append((project, layer))
        return project, layer, {"photo_inspection": {"storage_prefix": prefix}}

    inspection.model = model  # type: ignore[attr-defined]
    yield inspection
    for project, _ in made:
        for root in (f"inspections/{project}/", f"{tower_inspection.STORE_PREFIX}/{project}/",
                     f"{example_finder.STORE_PREFIX}/{project}/"):
            for item in store.list_objects_v2(Bucket=bucket, Prefix=root).get("Contents", []):
                store.delete_object(Bucket=bucket, Key=item["Key"])


async def _services():
    return _redis(), await get_async_s3_client(), get_bucket_name()


async def _found(redis, s3, bucket, project, layer, photo, part, limit_s=30.0):
    """The part's work once its finder job has finished."""
    deadline = time.monotonic() + limit_s
    while time.monotonic() < deadline:
        work = (await tower_inspection.photo_work(redis, s3, bucket, project, layer, photo))[part]
        if work.job is not None and work.job.status in (example_finder.DONE, example_finder.FAILED):
            return work
        await asyncio.sleep(0.1)
    raise AssertionError(f"the finder did not finish on {part} in {limit_s} s")


async def _marked_and_found(towers, part=DISH, marks=MARKS, project_layer=None):
    redis, s3, bucket = await _services()
    project, layer, metadata = project_layer or towers()
    photo = facts()
    await tower_inspection.set_marks(redis, s3, bucket, project, layer, photo, part, marks, [])
    job = await tower_inspection.start_find(redis, s3, bucket, project, USER, layer, metadata, photo, part)
    assert job.status in (example_finder.QUEUED, example_finder.RUNNING, example_finder.DONE)
    work = await _found(redis, s3, bucket, project, layer, photo, part)
    assert work.job.status == example_finder.DONE and work.job.job_id == job.job_id
    return redis, s3, bucket, project, layer, metadata, photo, work


# --- Counted by you: only once every suggestion is reviewed ---------------------------------------------------------

@pytest.mark.anyio
async def test_counted_by_you_appears_only_once_every_suggestion_is_reviewed(towers):
    redis, s3, bucket, project, layer, _, photo, work = await _marked_and_found(towers)
    try:
        ids = [s.id for s in work.suggestions]
        assert len(ids) >= 4 and work.waiting == len(ids)
        assert work.counted_by_you is None  # suggestions are never a count
        assert work.answer.level is answer_levels.Level.FULL_REVIEW  # answer_levels decides, not this module
        assert all(s.id.startswith(work.job.job_id[:12]) for s in work.suggestions)

        partly = await tower_inspection.set_review(redis, s3, bucket, project, layer, photo, DISH, ids[:2], [ids[2]],
                                                   [], [])
        assert partly.counted_by_you is None and partly.waiting == len(ids) - 3

        fixed_box = [10.0, 10.0, 40.0, 40.0]
        added = [[600.0, 100.0, 630.0, 130.0], [700.0, 100.0, 730.0, 130.0]]
        done = await tower_inspection.set_review(redis, s3, bucket, project, layer, photo, DISH, ids[:2],
                                                 ids[2:-1], [{"id": ids[-1], "box": fixed_box}], added)
        assert done.waiting == 0
        # 3 marks + 2 kept + 1 fixed + 2 added; the rejected count for nothing.
        assert done.counted_by_you == len(MARKS) + 2 + 1 + 2
        shown = done.as_dict()
        assert shown["reviewed"] == {"kept": ids[:2], "rejected": ids[2:-1], "fixed": [{"id": ids[-1], "box": fixed_box}],
                                     "added": added}
        assert set(shown) == {"marks", "lookalike_marks", "job", "suggestions", "reviewed", "counted_by_you", "waiting"}
        assert shown["job"] == {"job_id": work.job.job_id, "status": example_finder.DONE}
        again = (await tower_inspection.photo_work(redis, s3, bucket, project, layer, photo))[DISH]
        assert again.counted_by_you == 8  # kept in object storage
    finally:
        await redis.aclose()


class NothingFound(RecordedModel):
    """The finder process answering with no boxes at all."""

    def __call__(self, rgb, prompts, classes, imgsz, conf, max_det):
        super().__call__(rgb, prompts, classes, imgsz, conf, max_det)
        return []


@pytest.mark.anyio
async def test_a_finder_that_found_nothing_counts_the_marks_and_the_added(towers, monkeypatch):
    model = NothingFound()
    monkeypatch.setattr(example_finder, "_stored_model", lambda: model)
    redis, s3, bucket = await _services()
    project, layer, metadata = towers()
    photo = facts()
    try:
        await tower_inspection.set_marks(redis, s3, bucket, project, layer, photo, DISH, MARKS, [])
        await tower_inspection.start_find(redis, s3, bucket, project, USER, layer, metadata, photo, DISH)
        work = await _found(redis, s3, bucket, project, layer, photo, DISH)
        assert work.suggestions == () and work.waiting == 0 and len(model.calls) == 1
        assert work.counted_by_you is None  # nothing waits, but the person has not said nothing was missed
        none_missed = await tower_inspection.set_review(redis, s3, bucket, project, layer, photo, DISH, [], [], [],
                                                        [])
        assert none_missed.counted_by_you == len(MARKS)
        done = await tower_inspection.set_review(redis, s3, bucket, project, layer, photo, DISH, [], [], [],
                                                 [[5.0, 5.0, 30.0, 30.0]])
        assert done.counted_by_you == len(MARKS) + 1
    finally:
        await redis.aclose()


@pytest.mark.anyio
async def test_a_review_needs_a_finished_finder_and_this_jobs_ids_once_each(towers):
    redis, s3, bucket = await _services()
    project, layer, metadata = towers()
    photo = facts()
    try:
        with pytest.raises(TowerConflict, match="has not found dishes on this photo yet"):
            await tower_inspection.set_review(redis, s3, bucket, project, layer, photo, DISH, [], [], [], [])
        _, _, _, project, layer, _, photo, work = await _marked_and_found(towers, project_layer=(project, layer,
                                                                                               metadata))
        ids = [s.id for s in work.suggestions]
        with pytest.raises(TowerInputError, match="is not one of this photo's suggestions"):
            await tower_inspection.set_review(redis, s3, bucket, project, layer, photo, DISH, ["0123456789ab-1"],
                                              [], [], [])
        with pytest.raises(TowerInputError, match="given more than once"):
            await tower_inspection.set_review(redis, s3, bucket, project, layer, photo, DISH, [ids[0]], [ids[0]],
                                              [], [])
        with pytest.raises(FinderInputError, match="added box 1 is not inside the picture"):
            await tower_inspection.set_review(redis, s3, bucket, project, layer, photo, DISH, [], [], [],
                                              [[1300.0, 700.0, 1400.0, 800.0]])
    finally:
        await redis.aclose()


@pytest.mark.anyio
async def test_changed_marks_drop_the_job_and_the_review_of_the_old_marks(towers):
    redis, s3, bucket, project, layer, metadata, photo, work = await _marked_and_found(towers)
    try:
        ids = [s.id for s in work.suggestions]
        await tower_inspection.set_review(redis, s3, bucket, project, layer, photo, DISH, ids, [], [], [])
        same = await tower_inspection.set_marks(redis, s3, bucket, project, layer, photo, DISH, MARKS, [])
        assert same.counted_by_you is not None  # the same marks keep the job and the review
        moved = await tower_inspection.set_marks(redis, s3, bucket, project, layer, photo, DISH, MARKS[:2], [])
        assert moved.job is None and moved.review == Review() and moved.counted_by_you is None
        job = await tower_inspection.start_find(redis, s3, bucket, project, USER, layer, metadata, photo, DISH)
        assert job.job_id != work.job.job_id
        await _found(redis, s3, bucket, project, layer, photo, DISH)
        with pytest.raises(TowerInputError, match="the suggestions change when the marks do"):
            await tower_inspection.set_review(redis, s3, bucket, project, layer, photo, DISH, ids[:1], [], [], [])
    finally:
        await redis.aclose()


# --- Look-alike marks, as each part was measured --------------------------------------------------------------------

@pytest.mark.anyio
async def test_look_alikes_are_the_other_parts_marks_only_where_the_measurement_used_them(towers):
    redis, s3, bucket = await _services()
    project, layer, metadata = towers()
    photo = facts()
    try:
        await tower_inspection.set_marks(redis, s3, bucket, project, layer, photo, DISH, MARKS, [])
        antenna = await tower_inspection.set_marks(redis, s3, bucket, project, layer, photo, ANTENNA,
                                                   ANTENNA_MARKS, [])
        work = await tower_inspection.photo_work(redis, s3, bucket, project, layer, photo)
        as_boxes = [tuple(map(float, b)) for b in MARKS]
        antenna_boxes = [tuple(round(v, 3) for v in b) for b in ANTENNA_MARKS]
        assert work[DISH].lookalike_marks == ()  # dishes: measured not to help
        assert list(antenna.lookalike_marks) == as_boxes  # antennas: the dishes marked on the photo
        assert list(work[RADIO].lookalike_marks) == as_boxes + antenna_boxes  # radio units: both
        with pytest.raises(FinderInputError, match="look-alike marks are not used for dishes"):
            await tower_inspection.set_marks(redis, s3, bucket, project, layer, photo, DISH, MARKS, [MARKS[0]])
        own = [[900.0, 100.0, 960.0, 160.0]]
        mine = await tower_inspection.set_marks(redis, s3, bucket, project, layer, photo, ANTENNA, ANTENNA_MARKS,
                                                own)
        assert list(mine.lookalike_marks) == as_boxes + [tuple(own[0])]  # the measured ones first, then the own

        await tower_inspection.start_find(redis, s3, bucket, project, USER, layer, metadata, photo, ANTENNA)
        await _found(redis, s3, bucket, project, layer, photo, ANTENNA)
        (call,) = towers.model.calls
        assert call["classes"] == [0] * len(ANTENNA_MARKS) + [1] * (len(MARKS) + 1)  # the finder was given them
    finally:
        await redis.aclose()


def test_the_look_alike_marks_shown_are_kept_only_as_the_persons_own():
    """PUT marks takes the look-alike list the work shows (the other parts' marks, then the person's own): the other
    parts' marks in it are not kept as the person's own, so sending the shown list back changes nothing."""
    dishes, antennas = ((10.0, 10.0, 50.0, 50.0),), ((100.0, 10.0, 140.0, 90.0),)
    mine = (900.0, 100.0, 960.0, 160.0)
    record = tower_inspection.PhotoRecord.empty(SHA).with_part(DISH, tower_inspection.PartRecord(marks=dishes))
    record = record.with_part(ANTENNA, tower_inspection.PartRecord(marks=antennas, own_lookalikes=(mine,)))
    shown = tower_inspection.lookalike_marks(ANTENNA, record)
    assert shown == dishes + (mine,)
    assert tower_inspection.own_lookalikes(ANTENNA, record, shown) == (mine,)
    assert tower_inspection.own_lookalikes(ANTENNA, record, dishes) == ()  # the other parts' marks alone
    assert tower_inspection.own_lookalikes(ANTENNA, record, [mine, mine]) == (mine,)
    assert tower_inspection.own_lookalikes(RADIO, record, dishes + antennas + (mine,)) == (mine,)
    # Dishes use no look-alikes: nothing given for them is taken for another part's mark (set_marks refuses it).
    assert tower_inspection.own_lookalikes(DISH, record, antennas) == antennas


@pytest.mark.anyio
async def test_sending_the_shown_look_alike_marks_back_keeps_the_job_and_the_review(towers):
    redis, s3, bucket = await _services()
    project, layer, metadata = towers()
    photo = facts()
    try:
        await tower_inspection.set_marks(redis, s3, bucket, project, layer, photo, DISH, MARKS, [])
        own = [[900.0, 100.0, 960.0, 160.0]]
        await tower_inspection.set_marks(redis, s3, bucket, project, layer, photo, ANTENNA, ANTENNA_MARKS, own)
        await tower_inspection.start_find(redis, s3, bucket, project, USER, layer, metadata, photo, ANTENNA)
        found = await _found(redis, s3, bucket, project, layer, photo, ANTENNA)
        ids = [s.id for s in found.suggestions]
        reviewed = await tower_inspection.set_review(redis, s3, bucket, project, layer, photo, ANTENNA, ids, [],
                                                     [], [])
        assert reviewed.counted_by_you is not None
        shown = [list(b) for b in reviewed.as_dict()["lookalike_marks"]]  # the dishes' marks, then the own
        assert len(shown) == len(MARKS) + 1
        again = await tower_inspection.set_marks(redis, s3, bucket, project, layer, photo, ANTENNA,
                                                 reviewed.as_dict()["marks"], shown)
        assert again.job.job_id == found.job.job_id and again.counted_by_you == reviewed.counted_by_you
        assert again.as_dict()["lookalike_marks"] == shown
        dropped = await tower_inspection.set_marks(redis, s3, bucket, project, layer, photo, ANTENNA,
                                                   reviewed.as_dict()["marks"], shown[:-1])
        assert dropped.job is None and dropped.counted_by_you is None  # the own look-alike removed: a new find
    finally:
        await redis.aclose()


# --- The photo the finder reads ----------------------------------------------------------------------------------

@pytest.mark.anyio
async def test_a_photo_that_does_not_say_how_it_looked_or_looked_straight_down_is_refused(towers):
    redis, s3, bucket = await _services()
    project, layer, metadata = towers()
    try:
        await tower_inspection.set_marks(redis, s3, bucket, project, layer, facts(), DISH, MARKS, [])
        with pytest.raises(TowerInputError, match="does not say which way the camera looked"):
            await tower_inspection.start_find(redis, s3, bucket, project, USER, layer, metadata, facts(pitch=None),
                                              DISH)
        with pytest.raises(FinderInputError, match="dishes were measured only on photos taken at an angle"):
            await tower_inspection.start_find(redis, s3, bucket, project, USER, layer, metadata, facts(pitch=-89.0),
                                              DISH)
        assert towers.model.calls == []
    finally:
        await redis.aclose()


@pytest.mark.anyio
async def test_the_inspection_photo_is_read_from_the_layers_own_prefix_only(towers):
    _, s3, bucket = await _services()
    _, layer, metadata = towers()
    _, other_layer, other_metadata = towers()
    picture = await InspectionPhoto(s3, bucket, layer, metadata, SHA, answer_levels.OBLIQUE).read()
    assert picture.sha256 == SHA and (picture.width, picture.height) == (1333, 750)
    with pytest.raises(FinderInputError, match="not a photo inspection"):  # another layer's prefix, this layer's id
        await InspectionPhoto(s3, bucket, layer, other_metadata, SHA, answer_levels.OBLIQUE).read()
    with pytest.raises(FinderInputError, match="no such photo"):
        await InspectionPhoto(s3, bucket, layer, metadata, "ab" * 32, answer_levels.OBLIQUE).read()
    with pytest.raises(FinderInputError, match="SHA-256"):
        await InspectionPhoto(s3, bucket, layer, metadata, "../" + SHA, answer_levels.OBLIQUE).read()
    assert other_layer != layer


# --- Kept per project, never shared, even for the same photo ---------------------------------------------------------

@pytest.mark.anyio
async def test_identical_photos_in_two_projects_keep_their_own_work(towers):
    redis, s3, bucket, project, layer, _, photo, work = await _marked_and_found(towers)
    other_project, other_layer, other_metadata = towers()  # the same photo bytes, another project
    try:
        ids = [s.id for s in work.suggestions]
        await tower_inspection.set_review(redis, s3, bucket, project, layer, photo, DISH, ids, [], [], [])
        theirs = await tower_inspection.photo_work(redis, s3, bucket, other_project, other_layer, photo)
        assert all(w.marks == () and w.job is None and w.counted_by_you is None for w in theirs.values())
        # The same marks there are found again there, not taken from the first project's finds.
        await tower_inspection.set_marks(redis, s3, bucket, other_project, other_layer, photo, DISH, MARKS, [])
        job = await tower_inspection.start_find(redis, s3, bucket, other_project, USER, other_layer,
                                                other_metadata, photo, DISH)
        assert job.job_id == work.job.job_id and job.status != example_finder.DONE
        found = await _found(redis, s3, bucket, other_project, other_layer, photo, DISH)
        assert found.review == Review() and found.counted_by_you is None and len(towers.model.calls) == 2
        mine = await tower_inspection.photo_work(redis, s3, bucket, project, layer, photo)
        assert mine[DISH].counted_by_you == len(MARKS) + len(ids)
        assert tower_inspection.record_key(project, layer, SHA) != tower_inspection.record_key(other_project,
                                                                                               other_layer, SHA)
    finally:
        await redis.aclose()


# --- An inspection's facts: inventory, photos to check, camera, the same site before ---------------------------------

def _work(part, marks=0, done=True, suggestions=0, reviewed=0, added=0) -> tower_inspection.PartWork:
    """A part's work as part_work builds it, without storage (the counting rule is tower_inspection.counted)."""
    job = example_finder.Job(job_id="a" * 64, status=example_finder.DONE if done else example_finder.RUNNING,
                             finds=[{"box": [0, 0, 1, 1], "box_px": [0, 0, 1, 1], "score": 0.5}] * suggestions)
    job = job if marks else None
    sugg = tower_inspection.suggestions_of(job) if job is not None and done else ()
    review = Review(kept=tuple(s.id for s in sugg[:reviewed]), added=((0.0, 0.0, 9.0, 9.0),) * added)
    record = tower_inspection.PartRecord(marks=((0.0, 0.0, 60.0, 60.0),) * marks, job_id=job.job_id if job else None,
                                         review=review, review_saved=bool(reviewed or added))
    waiting = len(sugg) - reviewed
    answer = tower_inspection.level_answer(part, record.marks, facts())
    return tower_inspection.PartWork(part=part, marks=record.marks, lookalike_marks=(), job=job, suggestions=sugg,
                                     review=review, counted_by_you=tower_inspection.counted(record, done and job is not None,
                                                                                            waiting, answer),
                                     waiting=waiting, answer=answer, review_saved=record.review_saved)


def _inspection(photos, work) -> tower_inspection.Inspection:
    return tower_inspection.Inspection(project_id="P", layer_id="L", name="Rebero 2026", photos=tuple(photos),
                                       work=work)


PHOTOS = [facts(sha=c * 64, name=f"DJI_000{i}.JPG", taken=f"2026-10-09T14:0{i}:00", gimbal_yaw=yaw)
          for i, (c, yaw) in enumerate(zip("abcde", (0.0, 90.0, 180.0, -90.0, None)), 1)]


def test_the_inventory_gives_the_most_on_one_photo_and_names_the_sum_over_photos():
    parts = lambda **kw: {p: _work(p, **kw.get(p, {})) for p in tower_inspection.PARTS}  # noqa: E731
    work = {
        "a" * 64: parts(tower_dish=dict(marks=3, suggestions=4, reviewed=4)),  # 7 counted
        "b" * 64: parts(tower_dish=dict(marks=3, suggestions=2, reviewed=2, added=1)),  # 6 counted
        "c" * 64: parts(tower_dish=dict(marks=3, suggestions=5, reviewed=1)),  # 4 waiting: not counted
        "d" * 64: parts(tower_antenna=dict(marks=3, done=False)),  # the finder is working
    }
    inv = tower_inspection.inventory(_inspection(PHOTOS, work))
    dish = inv[DISH]
    assert (dish.at_least, dish.at_least_photo.filename, dish.counted_in_photos) == (7, "DJI_0001.JPG", 13)
    assert (dish.photos_marked, dish.photos_counted, dish.photos_waiting, dish.waiting) == (3, 2, 1, 4)
    assert dish.answer.level is answer_levels.Level.FULL_REVIEW
    assert inv[ANTENNA].at_least is None and inv[ANTENNA].photos_finding == 1  # missing is None, never 0
    assert inv[RADIO].at_least is None and inv[RADIO].photos_marked == 0
    # Photos a and b (dishes counted); c and d are marked but open; e has nothing marked.
    assert tower_inspection.photos_with_marked_parts_counted(_inspection(PHOTOS, work)) == 2
    assert tower_inspection.photos_unmarked(_inspection(PHOTOS, work)) == 1
    assert tower_inspection.photos_unmarked(_inspection(PHOTOS, {})) == len(PHOTOS)


def test_photos_to_check_come_nearly_done_first_then_in_flight_order():
    parts = lambda **kw: {p: _work(p, **kw.get(p, {})) for p in tower_inspection.PARTS}  # noqa: E731
    work = {
        "a" * 64: parts(tower_dish=dict(marks=3, suggestions=2, reviewed=2)),  # checked: not listed
        "b" * 64: parts(tower_dish=dict(marks=3, suggestions=3, reviewed=1)),  # 2 waiting
        "c" * 64: parts(tower_antenna=dict(marks=3, suggestions=5)),  # 5 waiting
        "d" * 64: parts(tower_radio_unit=dict(marks=3, done=False)),  # finding
    }
    found_none = parts(tower_dish=dict(marks=3, suggestions=0))  # done, nothing more found, no review saved yet
    assert found_none[DISH].unreviewed and found_none[DISH].counted_by_you is None
    confirm = tower_inspection.photos_to_check(_inspection(PHOTOS[:1], {"a" * 64: found_none}))
    assert [(c.photo.filename, c.reason, c.parts) for c in confirm] == [("DJI_0001.JPG", tower_inspection.CONFIRM,
                                                                         (DISH,))]
    work["e" * 64] = {p: replace(_work(p), marks=((0.0, 0.0, 60.0, 60.0),) * 3 if p == DISH else ())
                      for p in tower_inspection.PARTS}  # marked, never found
    listed = tower_inspection.photos_to_check(_inspection(PHOTOS, work))
    assert [(c.photo.filename, c.reason) for c in listed] == [
        ("DJI_0003.JPG", tower_inspection.WAITING), ("DJI_0002.JPG", tower_inspection.WAITING),
        ("DJI_0005.JPG", tower_inspection.NOT_FOUND), ("DJI_0004.JPG", tower_inspection.FINDING)]
    assert listed[0].waiting == 5 and listed[0].parts == (ANTENNA,)
    untouched = tower_inspection.photos_to_check(_inspection(PHOTOS, {}))
    assert [c.photo.filename for c in untouched] == [p.filename for p in PHOTOS]  # in flight order
    assert {c.reason for c in untouched} == {tower_inspection.UNMARKED}


def test_camera_facts_come_from_the_photos_own_data_and_missing_stays_missing():
    photos = PHOTOS + [facts(sha="f" * 64, name="phone.jpg", lat=None, lon=None, pitch=None, gimbal_yaw=None,
                             relative_altitude_m=None, taken=None, make=None, model=None),
                       facts(sha="9" * 64, name="top.JPG", pitch=-90.0, relative_altitude_m=62.0, gimbal_yaw=45.0)]
    c = tower_inspection.camera_facts(photos)
    assert (c.photos, c.with_gps, c.without_gps) == (7, 6, ("phone.jpg",))
    assert (c.height_m.least, c.height_m.most) == (45.2, 62.0)
    assert (c.at_an_angle, c.straight_down, c.no_pitch) == (5, 1, 1)
    assert c.facing == {"north": 1, "north-east": 1, "east": 1, "south": 1, "west": 1}
    assert c.no_direction == 2 and c.cameras == ("DJI M3E",)
    assert (c.first_taken, c.last_taken) == ("2026-10-09T14:01:00", "2026-10-09T14:05:00")
    assert tower_inspection.compass(-22.4) == "north" and tower_inspection.compass(337.6) == "north"


def test_the_same_site_before_is_near_and_finished_earlier():
    now = [facts(taken="2026-10-09T14:00:00"), facts(sha="b" * 64, taken="2026-10-09T14:10:00")]
    before = [facts(lat=-1.9441 + 0.0005, taken="2026-04-01T10:00:00")]  # ~56 m north, months earlier
    assert tower_inspection.is_earlier_same_site(now, before)
    assert not tower_inspection.is_earlier_same_site(before, now)  # later, not earlier
    far = [facts(lat=-1.9441 + 0.002, taken="2026-04-01T10:00:00")]  # ~220 m away: another site
    assert tower_inspection.distance_m(tower_inspection.site_centre(now), tower_inspection.site_centre(far)) > 200
    assert not tower_inspection.is_earlier_same_site(now, far)
    undated = [facts(taken=None)]
    assert not tower_inspection.is_earlier_same_site(now, undated)  # unknown time: not compared
    assert not tower_inspection.is_earlier_same_site(now, [facts(lat=None, lon=None, taken="2026-04-01T10:00:00")])


def test_the_work_is_kept_in_the_one_project_showing_the_layer():
    assert tower_inspection.work_project(["P1"]) == "P1"
    assert tower_inspection.work_project([]) is None
    with pytest.raises(TowerConflict, match="on maps of 2 projects"):
        tower_inspection.work_project(["P1", "P2"])  # never the last edited map's: the work would move


def test_the_switch_reads_towers_only(monkeypatch):
    monkeypatch.delenv("TOWERS", raising=False)
    assert tower_inspection.enabled() is False
    for value, on in (("1", True), ("true", True), ("yes", True), ("0", False), ("off", False)):
        monkeypatch.setenv("TOWERS", value)
        assert tower_inspection.enabled() is on


def test_bolts_are_never_scanned_and_scanned_bolt_boxes_are_not_shown(monkeypatch):
    """On a wide photo a bolt is a few pixels: the bolt models boxed cars and bare steel (2026-10-14)."""
    monkeypatch.setenv("OWN_DETECTORS", "1")
    assert not set(tower_inspection.scan_parts(tower_inspection.PARTS_BY_INDUSTRY["telecom"])) & set(
        tower_inspection.DEFECT_PARTS)


def test_suggestions_below_the_measured_floor_are_not_shown_and_ids_keep_their_place():
    job = example_finder.Job(job_id="a" * 32, status=example_finder.DONE,
                             finds=[{"box_px": [0, 0, 10, 10], "score": 0.9}, {"box_px": [0, 0, 5, 5], "score": 0.1},
                                    {"box_px": [1, 1, 6, 6], "score": 0.45}])
    shown = tower_inspection.suggestions_of(job, 0.4)
    assert [(s.id[-2:], s.score) for s in shown] == [("-1", 0.9), ("-3", 0.45)]
    assert len(tower_inspection.suggestions_of(job)) == 3
