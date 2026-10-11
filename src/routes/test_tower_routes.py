"""The tower routes (src/routes/tower_routes.py) end to end: a seeded photo inspection (test_fixtures/towers.py) with
the committed tower photo in object storage, the real rights checks (get_layer / edit_layer), Redis jobs and the
example finder with the recorded Ultralytics result (RecordedModel). Checks the shared API contract's shapes, the
switches, the industry gate, rights, the finder's refusals passed through as 422, and the cards."""

from __future__ import annotations

import asyncio
import time
import uuid

import pytest

from src.dependencies.session import LegacyUserContext, ServiceUserContext, verify_session_required
from src.services import example_finder, tower_cards, tower_inspection
from src.services.test_example_finder import RecordedModel, _redis
from src.wsgi import app
from test_fixtures.towers import MARKS, SHA, Inspections, admin, sha_of, tower_facts, variant

RUN_TAG = uuid.uuid4().hex[:6]
VIEWER, EDITOR = str(uuid.uuid4()), str(uuid.uuid4())
LEGACY = LegacyUserContext().get_user_id()
DISH, ANTENNA, RADIO = tower_inspection.PARTS
PHOTO_KEYS = {"sha256", "url", "width", "height", "taken_at", "lat", "lon", "relative_altitude_m", "gimbal_pitch",
              "gimbal_yaw"}
WORK_KEYS = {"marks", "lookalike_marks", "job", "suggestions", "reviewed", "counted_by_you", "waiting"}


@pytest.fixture
async def seeded():
    inspections = Inspections()
    yield inspections
    await inspections.remove()


@pytest.fixture
def switched_on(monkeypatch):
    """TOWERS, PHOTO_INSPECTIONS and EXAMPLE_FINDER on; Redis jobs and locks in a prefix of the test's own; the
    recorded model instead of the weights."""
    for name in ("TOWERS", "PHOTO_INSPECTIONS", "EXAMPLE_FINDER"):
        monkeypatch.setenv(name, "1")
    tag = f"{RUN_TAG}{uuid.uuid4().hex[:6]}"
    monkeypatch.setattr(example_finder, "REDIS_PREFIX", f"example_finder_test:{tag}")
    monkeypatch.setattr(tower_inspection, "REDIS_PREFIX", f"towers_test:{tag}")
    redis = _redis()
    from src.routes import tower_routes

    monkeypatch.setattr(tower_routes, "get_async_redis", lambda: redis)
    model = RecordedModel()
    monkeypatch.setattr(example_finder, "_stored_model", lambda: model)
    yield model
    app.dependency_overrides.pop(verify_session_required, None)


def _as(user) -> None:
    app.dependency_overrides[verify_session_required] = (
        lambda: user if isinstance(user, LegacyUserContext) else ServiceUserContext(user))


def _base(layer: str) -> str:
    return f"/api/towers/layer/{layer}"


async def _every_route(client, layer: str) -> list[int]:
    photo = f"{_base(layer)}/photos/{SHA}"
    return [
        (await client.get(f"{_base(layer)}/photos")).status_code,
        (await client.get(f"{photo}/work")).status_code,
        (await client.put(f"{photo}/marks", json={"part": DISH, "marks": MARKS})).status_code,
        (await client.post(f"{photo}/find", json={"part": DISH})).status_code,
        (await client.put(f"{photo}/review", json={"part": DISH})).status_code,
        (await client.get(f"{_base(layer)}/cards")).status_code,
        (await client.get(f"{_base(layer)}/cards/equipment")).status_code,
    ]


async def _done(client, layer: str, part: str = DISH, limit_s: float = 30.0) -> dict:
    deadline = time.monotonic() + limit_s
    while time.monotonic() < deadline:
        response = await client.get(f"{_base(layer)}/photos/{SHA}/work")
        assert response.status_code == 200, response.text
        work = response.json()["parts"][part]
        if work["job"] and work["job"]["status"] in (example_finder.DONE, example_finder.FAILED):
            return work
        await asyncio.sleep(0.1)
    raise AssertionError(f"the finder did not finish on {part} in {limit_s} s")


# --- Switches and the industry -------------------------------------------------------------------------------------

@pytest.mark.anyio
@pytest.mark.parametrize("off", ["TOWERS", "PHOTO_INSPECTIONS"])
async def test_with_a_switch_off_no_route_exists(client, seeded, switched_on, monkeypatch, off):
    ids = await seeded.make()
    _as(ids["owner"])
    monkeypatch.setenv(off, "0")
    assert await _every_route(client, ids["layer"]) == [404] * 7
    assert switched_on.calls == []


@pytest.mark.anyio
@pytest.mark.parametrize("other", ["agriculture", "power_grid"])
async def test_outside_telecom_no_route_exists(client, seeded, switched_on, other):
    ids = await seeded.make(industry=other)
    _as(ids["owner"])
    assert await _every_route(client, ids["layer"]) == [404] * 7
    assert switched_on.calls == []


@pytest.mark.anyio
async def test_a_layer_that_is_not_an_inspection_or_has_no_kept_facts(client, seeded, switched_on):
    plain = await seeded.make(is_inspection=False)
    _as(plain["owner"])
    assert (await client.get(f"{_base(plain['layer'])}/photos")).status_code == 404
    old = await seeded.make(keep_facts=False)
    _as(old["owner"])
    response = await client.get(f"{_base(old['layer'])}/photos")
    assert response.status_code == 409 and "upload the photos again" in response.json()["detail"]


@pytest.mark.anyio
async def test_an_inspection_on_maps_of_two_projects_is_refused_not_followed(client, seeded, switched_on):
    """Its work would otherwise follow whichever project's map was edited last (photo_context.project_of)."""
    ids = await seeded.make()
    other = await seeded.make(owner=ids["owner"])  # another Telecom project of the same owner
    _as(ids["owner"])
    assert (await client.get(f"{_base(ids['layer'])}/photos")).status_code == 200
    conn = await admin()
    try:
        await conn.execute("UPDATE user_mundiai_maps SET layers = array_append(layers, $1) WHERE id = $2",
                           ids["layer"], other["map"])
    finally:
        await conn.close()
    codes = await _every_route(client, ids["layer"])
    assert codes == [409] * 7
    detail = (await client.get(f"{_base(ids['layer'])}/cards")).json()["detail"]
    assert "on maps of 2 projects" in detail and "remove it from the maps of the others" in detail
    assert switched_on.calls == []


# --- The contract ---------------------------------------------------------------------------------------------------

@pytest.mark.anyio
async def test_photos_are_listed_as_the_contract_says(client, seeded, switched_on):
    phone = tower_facts(sha="ab" * 32, name="phone.jpg", lat=None, lon=None, gimbal_pitch=None, gimbal_yaw=None,
                        relative_altitude_m=None, orientation=6)
    ids = await seeded.make(facts=[tower_facts(), phone])
    _as(ids["owner"])
    response = await client.get(f"{_base(ids['layer'])}/photos")
    assert response.status_code == 200, response.text
    first, second = response.json()["photos"]
    assert set(first) == PHOTO_KEYS
    assert first == {"sha256": SHA, "url": f"/api/layer/{ids['layer']}/photos/{SHA}", "width": 1333, "height": 750,
                     "taken_at": "2026-10-09T14:03:22", "lat": -1.9441, "lon": 30.0619, "relative_altitude_m": 45.2,
                     "gimbal_pitch": -30.0, "gimbal_yaw": -87.3}
    # A photo without GPS is listed too, its size as shown (turned a quarter), its unknowns None.
    assert (second["width"], second["height"], second["lat"], second["gimbal_pitch"]) == (750, 1333, None, None)
    assert (await client.get(f"{_base(ids['layer'])}/photos/{'cd' * 32}/work")).status_code == 404


@pytest.mark.anyio
async def test_mark_find_review_count_through_the_contract(client, seeded, switched_on):
    ids = await seeded.make()
    _as(ids["owner"])
    photo = f"{_base(ids['layer'])}/photos/{SHA}"
    empty = (await client.get(f"{photo}/work")).json()
    assert set(empty) == {"parts"} and set(empty["parts"]) == set(tower_inspection.PARTS)
    assert empty["parts"][DISH] == {"marks": [], "lookalike_marks": [], "job": None, "suggestions": [],
                                    "reviewed": {"kept": [], "rejected": [], "fixed": [], "added": []},
                                    "counted_by_you": None, "waiting": 0}

    marked = await client.put(f"{photo}/marks", json={"part": DISH, "marks": MARKS, "lookalike_marks": []})
    assert marked.status_code == 200, marked.text
    assert set(marked.json()) == WORK_KEYS and marked.json()["marks"] == MARKS

    early = await client.put(f"{photo}/review", json={"part": DISH, "kept": []})
    assert early.status_code == 409  # nothing found yet

    started = await client.post(f"{photo}/find", json={"part": DISH})
    assert started.status_code == 200, started.text
    assert set(started.json()) == {"job_id", "status"} and example_finder.is_job_id(started.json()["job_id"])
    work = await _done(client, ids["layer"])
    assert work["job"] == {"job_id": started.json()["job_id"], "status": example_finder.DONE}
    assert work["counted_by_you"] is None and work["waiting"] == len(work["suggestions"]) > 3
    assert set(work["suggestions"][0]) == {"id", "box", "score"} and len(work["suggestions"][0]["box"]) == 4

    ids_ = [s["id"] for s in work["suggestions"]]
    fixed = {"id": ids_[1], "box": [100.0, 100.0, 140.0, 140.0]}
    reviewed = await client.put(f"{photo}/review", json={"part": DISH, "kept": ids_[:1], "rejected": ids_[2:],
                                                         "fixed": [fixed], "added": [[10.0, 10.0, 40.0, 40.0]]})
    assert reviewed.status_code == 200, reviewed.text
    assert set(reviewed.json()) == WORK_KEYS
    assert reviewed.json()["counted_by_you"] == len(MARKS) + 1 + 1 + 1 and reviewed.json()["waiting"] == 0

    stale = await client.put(f"{photo}/review", json={"part": DISH, "kept": ["0123456789ab-1"]})
    assert stale.status_code == 422 and "not one of this photo's suggestions" in stale.json()["detail"]
    assert len(switched_on.calls) == 1


@pytest.mark.anyio
@pytest.mark.parametrize("body, says", [
    ({"part": "tower_mast", "marks": MARKS}, "the part must be one of"),
    ({"part": DISH, "marks": [[1300, 700, 1400, 800]]}, "mark 1 is not inside the picture"),
    ({"part": DISH, "marks": MARKS, "lookalike_marks": MARKS[:1]}, "look-alike marks are not used for dishes"),
    ({"part": DISH, "marks": [MARKS[0]] * 21}, "give at most 20 marks"),
])
async def test_marks_that_cannot_be_saved_say_why(client, seeded, switched_on, body, says):
    ids = await seeded.make()
    _as(ids["owner"])
    response = await client.put(f"{_base(ids['layer'])}/photos/{SHA}/marks", json=body)
    assert response.status_code == 422 and says in response.json()["detail"], response.text


@pytest.mark.anyio
async def test_the_finders_refusals_come_back_as_422_with_the_reason(client, seeded, switched_on):
    top, phone = variant(1), variant(2)
    ids = await seeded.make(facts=[tower_facts(), tower_facts(sha=sha_of(top), name="top.JPG", gimbal_pitch=-90.0),
                                   tower_facts(sha=sha_of(phone), name="phone.jpg", gimbal_pitch=None)],
                            files={sha_of(top): top, sha_of(phone): phone})
    _as(ids["owner"])
    base = _base(ids["layer"])
    small = [[x, 300, x + 10, 310] for x in (100, 200, 300)]  # about 9 px as the finder reads them
    await client.put(f"{base}/photos/{SHA}/marks", json={"part": ANTENNA, "marks": small})
    refused = await client.post(f"{base}/photos/{SHA}/find", json={"part": ANTENNA})
    assert refused.status_code == 422
    assert "antennas were measured only from 33 px wide, so this picture is outside the measurement" in (
        refused.json()["detail"])
    unmarked = await client.post(f"{base}/photos/{SHA}/find", json={"part": RADIO})
    assert unmarked.status_code == 422 and "give 1 to 20 marks" in unmarked.json()["detail"]
    for sha, says in ((sha_of(top), "measured only on photos taken at an angle"),
                      (sha_of(phone), "does not say which way the camera looked")):
        await client.put(f"{base}/photos/{sha}/marks", json={"part": DISH, "marks": MARKS})
        response = await client.post(f"{base}/photos/{sha}/find", json={"part": DISH})
        assert response.status_code == 422 and says in response.json()["detail"], response.text
    assert switched_on.calls == []


@pytest.mark.anyio
async def test_with_the_finder_switched_off_find_is_not_found_and_the_rest_works(client, seeded, switched_on,
                                                                                  monkeypatch):
    monkeypatch.setenv("EXAMPLE_FINDER", "0")
    ids = await seeded.make()
    _as(ids["owner"])
    photo = f"{_base(ids['layer'])}/photos/{SHA}"
    assert (await client.put(f"{photo}/marks", json={"part": DISH, "marks": MARKS})).status_code == 200
    assert (await client.post(f"{photo}/find", json={"part": DISH})).status_code == 404


# --- Rights ---------------------------------------------------------------------------------------------------------

@pytest.mark.anyio
async def test_reads_need_view_rights_and_writes_edit_rights(client, seeded, switched_on, monkeypatch):
    ids = await seeded.make(owner=LEGACY)
    _as(LegacyUserContext())
    photo = f"{_base(ids['layer'])}/photos/{SHA}"
    assert (await client.put(f"{photo}/marks", json={"part": DISH, "marks": MARKS})).status_code == 200
    monkeypatch.setenv("MUNDI_AUTH_MODE", "view_only")
    assert (await client.get(f"{photo}/work")).json()["parts"][DISH]["marks"] == MARKS
    assert (await client.get(f"{_base(ids['layer'])}/cards")).status_code == 200
    assert (await client.put(f"{photo}/marks", json={"part": DISH, "marks": MARKS[:1]})).status_code == 403
    assert (await client.post(f"{photo}/find", json={"part": DISH})).status_code == 403
    assert (await client.put(f"{photo}/review", json={"part": DISH})).status_code == 403
    assert switched_on.calls == []


@pytest.mark.anyio
async def test_a_project_member_who_does_not_own_the_layer_gets_404_today(client, seeded, switched_on):
    """As every route on get_layer: map_layers' row-level security shows a layer only to its owner
    (EXAMPLE_FINDER_RULES.md, "Project editors and viewers"); a decision for Roger, pinned here."""
    ids = await seeded.make(editors=(EDITOR,), viewers=(VIEWER,))
    for user in (VIEWER, EDITOR):
        _as(user)
        assert (await client.get(f"{_base(ids['layer'])}/photos")).status_code == 404
        assert (await client.put(f"{_base(ids['layer'])}/photos/{SHA}/marks",
                                 json={"part": DISH, "marks": MARKS})).status_code == 404


@pytest.mark.anyio
async def test_starting_the_finder_is_rate_limited_per_user(client, seeded, switched_on):
    ids = await seeded.make()
    _as(ids["owner"])
    find = f"{_base(ids['layer'])}/photos/{SHA}/find"
    statuses = [(await client.post(find, json={"part": "tower_mast"})).status_code for _ in range(30)]
    assert 429 in statuses and statuses[0] == 422


# --- Cards ----------------------------------------------------------------------------------------------------------

@pytest.mark.anyio
async def test_the_cards_follow_the_contract_and_the_reader(client, seeded, switched_on):
    ids = await seeded.make()
    _as(ids["owner"])
    for audience, first in (("owner", "equipment"), ("technician", "photos_to_check"), (None, "equipment")):
        query = f"?audience={audience}" if audience else ""
        deck = await client.get(f"{_base(ids['layer'])}/cards{query}")
        assert deck.status_code == 200, deck.text
        cards = deck.json()["cards"]
        assert set(deck.json()) == {"cards"} and all(set(c) == {"id", "title", "status", "preview"} for c in cards)
        assert {c["id"] for c in cards} == set(tower_cards.CARDS) and cards[-1]["id"] == "learn_review"
        assert cards[0]["id"] == first, (audience, [c["id"] for c in cards])
    answer = (await client.get(f"{_base(ids['layer'])}/cards/equipment?audience=technician")).json()
    assert {"what", "why", "todo", "how_sure", "facts", "items", "progress", "terms", "status"} <= set(answer)
    assert answer["what"] == tower_cards.NOTHING_COUNTED
    assert (await client.get(f"{_base(ids['layer'])}/cards/equipment?audience=farmer")).status_code == 422
    assert (await client.get(f"{_base(ids['layer'])}/cards/no_such_card")).status_code == 404


@pytest.mark.anyio
async def test_changes_compare_with_the_earlier_inspection_of_the_same_site_in_the_project(client, seeded,
                                                                                         switched_on):
    april = tower_facts(sha=sha_of(variant(3)), name="OLD_0001.JPG", taken_at="2026-04-01T10:00:00")
    earlier = await seeded.make(facts=[april], files={april.sha256: variant(3)})
    now = await seeded.make(owner=earlier["owner"], project=earlier["project"])
    far = tower_facts(sha=sha_of(variant(4)), name="FAR_0001.JPG", taken_at="2026-03-01T10:00:00", lat=-1.95)
    await seeded.make(owner=earlier["owner"], project=earlier["project"], facts=[far], files={far.sha256: variant(4)})
    _as(earlier["owner"])

    alone = (await client.get(f"{_base(now['layer'])}/cards/changes")).json()
    # An earlier inspection of the site exists, nothing counted on it yet: count there, no second inspection needed.
    assert alone["status"] == "cant_tell" and "1 earlier inspection of this site" in alone["what"]

    photo = f"{_base(earlier['layer'])}/photos/{april.sha256}"
    await client.put(f"{photo}/marks", json={"part": DISH, "marks": MARKS})
    await client.post(f"{photo}/find", json={"part": DISH})
    work = await _done_on(client, earlier["layer"], april.sha256)
    await client.put(f"{photo}/review", json={"part": DISH, "rejected": [s["id"] for s in work["suggestions"]]})
    mine = f"{_base(now['layer'])}/photos/{SHA}"
    await client.put(f"{mine}/marks", json={"part": DISH, "marks": MARKS})
    await client.post(f"{mine}/find", json={"part": DISH})
    work = await _done(client, now["layer"])
    keep = [s["id"] for s in work["suggestions"]]
    await client.put(f"{mine}/review", json={"part": DISH, "kept": keep[:1], "rejected": keep[1:]})

    compared = (await client.get(f"{_base(now['layer'])}/cards/changes?audience=owner")).json()
    assert compared["status"] == "ready", compared
    assert compared["what"].startswith("Compared with Rebero inspection ")
    assert "(taken 2026-04-01): Dishes: at least 3 then, at least 4 now (1 more counted now)." in compared["what"]
    # The farther inspection (another site) and the later one are not compared with.
    assert [i["title"] for i in compared["items"]] == ["OLD_0001.JPG", "DJI_0395.JPG"]


async def _done_on(client, layer: str, sha: str, limit_s: float = 30.0) -> dict:
    deadline = time.monotonic() + limit_s
    while time.monotonic() < deadline:
        work = (await client.get(f"{_base(layer)}/photos/{sha}/work")).json()["parts"][DISH]
        if work["job"] and work["job"]["status"] in (example_finder.DONE, example_finder.FAILED):
            return work
        await asyncio.sleep(0.1)
    raise AssertionError(f"the finder did not finish on {sha} in {limit_s} s")
