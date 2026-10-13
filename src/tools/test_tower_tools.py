"""Sage's tower tools (src/tools/tower_tools.py) on a seeded photo inspection (test_fixtures/towers.py): their shape,
the same facts as the cards, and never a count before a person checked every suggestion on a photo. The finder's
model is the recorded Ultralytics result (RecordedModel)."""

from __future__ import annotations

import uuid

import pytest

from src.dependencies.session import ServiceUserContext
from src.services import example_finder, tower_cards, tower_inspection
from src.services.test_example_finder import RecordedModel, _redis
from src.services.test_tower_inspection import _found
from src.tools import tower_tools
from src.tools.pyd import IngabeToolCallMetaArgs
from src.utils import get_async_s3_client, get_bucket_name
from test_fixtures.towers import MARKS, Inspections, admin, tower_facts

DISH = tower_inspection.PARTS[0]


@pytest.fixture(autouse=True)
def _example_finder_for_towers(monkeypatch):
    """These tests run the example finder (YOLOE) on tower photos: our own detectors are switched off here, as the way
    back does (OWN_DETECTORS=0)."""
    monkeypatch.setenv("OWN_DETECTORS", "0")


@pytest.fixture
async def seeded(monkeypatch):
    monkeypatch.setenv("TOWERS", "1")
    tag = uuid.uuid4().hex[:8]
    monkeypatch.setattr(example_finder, "REDIS_PREFIX", f"example_finder_test:{tag}")
    monkeypatch.setattr(tower_inspection, "REDIS_PREFIX", f"towers_test:{tag}")
    redis = _redis()
    monkeypatch.setattr(tower_tools, "get_async_redis", lambda: redis)
    model = RecordedModel()
    monkeypatch.setattr(example_finder, "_stored_model", lambda: model)
    inspections = Inspections()
    yield inspections, redis  # the client is left to the test's event loop (closing it here runs on another one)
    await inspections.remove()


def _meta(ids) -> IngabeToolCallMetaArgs:
    return IngabeToolCallMetaArgs(user_uuid=ids["owner"], conversation_id=1, map_id=ids["map"],
                                  project_id=ids["project"], session=ServiceUserContext(ids["owner"]))


def _args(ids) -> tower_tools.TowerInspectionArgs:
    return tower_tools.TowerInspectionArgs(layer_id=ids["layer"])


@pytest.mark.anyio
async def test_nothing_checked_gives_no_count_and_says_what_to_do(seeded):
    inspections, _ = seeded
    ids = await inspections.make()
    found = await tower_tools.get_tower_inspection_findings(_args(ids), _meta(ids))
    assert found["status"] == "success" and found["counted"] is False
    assert found["answer"] == tower_cards.NOTHING_COUNTED and "mark a few" in found["what_to_do"]
    assert all(part["at_least"] is None for part in found["equipment"].values())
    assert found["rule"] == tower_cards.NEVER_COUNT_YOURSELF
    assert set(found) >= {"inspection", "photos", "photos_with_every_marked_part_counted",
                          "photos_with_nothing_marked", "complete", "equipment", "camera", "how_sure", "source"}
    review = await tower_tools.tower_photos_needing_review(_args(ids), _meta(ids))
    assert review["status"] == "success" and review["photos_to_check"] == 1
    assert review["first_photos"] == [{"photo": "DJI_0395.JPG", "why": "nothing marked yet"}]


async def _found_on_dishes(redis, ids):
    s3, bucket, photo = await get_async_s3_client(), get_bucket_name(), tower_facts()
    metadata = {"photo_inspection": {"storage_prefix": f"inspections/{ids['project']}/{ids['layer']}/"}}
    await tower_inspection.set_marks(redis, s3, bucket, ids["project"], ids["layer"], photo, DISH, MARKS, [])
    await tower_inspection.start_find(redis, s3, bucket, ids["project"], ids["owner"], ids["layer"], metadata,
                                      photo, DISH)
    return s3, bucket, photo, await _found(redis, s3, bucket, ids["project"], ids["layer"], photo, DISH)


@pytest.mark.anyio
async def test_suggestions_are_never_a_count_until_a_person_checked_them(seeded):
    inspections, redis = seeded
    ids = await inspections.make()
    s3, bucket, photo, work = await _found_on_dishes(redis, ids)
    waiting = await tower_tools.get_tower_inspection_findings(_args(ids), _meta(ids))
    dish = waiting["equipment"][DISH]
    assert waiting["counted"] is False and dish["at_least"] is None
    assert dish["suggestions_waiting"] == len(work.suggestions) > 3
    first = (await tower_tools.tower_photos_needing_review(_args(ids), _meta(ids)))["first_photos"][0]
    assert first == {"photo": "DJI_0395.JPG", "why": f"{len(work.suggestions)} suggestions waiting for you"}

    ids_ = [s.id for s in work.suggestions]
    await tower_inspection.set_review(redis, s3, bucket, ids["project"], ids["layer"], photo, DISH, ids_[:2],
                                      ids_[2:], [], [])
    counted = await tower_tools.get_tower_inspection_findings(_args(ids), _meta(ids))
    assert counted["counted"] is True and counted["equipment"][DISH]["at_least"] == len(MARKS) + 2
    assert counted["equipment"][DISH]["counted_on_photo"] == "DJI_0395.JPG"
    assert f"Dishes: at least {len(MARKS) + 2}, counted by you" in counted["answer"]  # the equipment card's words


@pytest.mark.anyio
async def test_a_layer_not_on_this_map_or_not_an_inspection_or_not_telecom_is_refused(seeded):
    inspections, _ = seeded
    ids = await inspections.make()
    other = await inspections.make(owner=ids["owner"])
    elsewhere = await tower_tools.get_tower_inspection_findings(_args(other), _meta(ids))
    assert elsewhere == {"status": "error", "error": f"No photo inspection {other['layer']} on this map."}
    plain = await inspections.make(is_inspection=False)
    refused = await tower_tools.tower_photos_needing_review(_args(plain), _meta(plain))
    assert refused["status"] == "error" and "is not a photo inspection" in refused["error"]
    farm = await inspections.make(industry="agriculture")
    assert (await tower_tools.get_tower_inspection_findings(_args(farm), _meta(farm)))["error"] == (
        "Tower work is for Telecom Towers projects only.")


@pytest.mark.anyio
async def test_the_work_read_is_the_chats_projects_never_the_last_edited_maps(seeded):
    """The same inspection layer on a map of another Telecom project: a chat there reads that project's work (none),
    not this one's counts, although that project's map was edited last."""
    inspections, redis = seeded
    ids = await inspections.make()
    other = await inspections.make(owner=ids["owner"])  # another Telecom project of the same owner
    conn = await admin()
    try:
        await conn.execute("UPDATE user_mundiai_maps SET layers = array_append(layers, $1), "
                           "last_edited = now() + interval '1 hour' WHERE id = $2", ids["layer"], other["map"])
    finally:
        await conn.close()
    s3, bucket, photo, work = await _found_on_dishes(redis, ids)
    await tower_inspection.set_review(redis, s3, bucket, ids["project"], ids["layer"], photo, DISH,
                                      [s.id for s in work.suggestions], [], [], [])
    theirs = await tower_tools.get_tower_inspection_findings(_args(ids), _meta(other))  # a chat in `other`
    assert theirs["status"] == "success" and theirs["counted"] is False  # project `other` has no work on it
    ours = await tower_tools.get_tower_inspection_findings(_args(ids), _meta(ids))
    assert ours["counted"] is True and ours["equipment"][DISH]["at_least"] == len(MARKS) + len(work.suggestions)
