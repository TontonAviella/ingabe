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
from src.services import example_finder, fastsam_model, part_outlines, tower_cards, tower_inspection
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


@pytest.fixture(autouse=True)
def _example_finder_for_towers(monkeypatch):
    """These tests run the example finder (YOLOE) on tower photos: our own detectors are switched off here, as the way
    back does (OWN_DETECTORS=0)."""
    monkeypatch.setenv("OWN_DETECTORS", "0")
    monkeypatch.setenv("PART_OUTLINES", "0")  # FastSAM per test worker runs the test machine out of memory


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
@pytest.mark.parametrize("off", ["TOWERS", "PHOTO_INSPECTIONS"])
async def test_with_a_switch_off_even_a_malformed_write_is_not_found(client, switched_on, monkeypatch, off):
    """The switch is checked before the body is read: bad JSON, signed out, still 404 (not a 422 showing the route)."""
    monkeypatch.setenv(off, "0")
    photo = f"{_base('LTnothing0000')}/photos/{SHA}"
    for method, url in (("PUT", f"{photo}/marks"), ("POST", f"{photo}/find"), ("PUT", f"{photo}/review")):
        response = await client.request(method, url, content=b"{", headers={"Content-Type": "application/json"})
        assert response.status_code == 404, (method, url, response.status_code)
    # Nor a wrong method (405) or a trailing slash (307): while off, the routes do not match at all.
    for method, url in (("GET", f"{photo}/marks"), ("DELETE", f"{_base('LTnothing0000')}/photos"),
                        ("GET", f"{_base('LTnothing0000')}/photos/")):
        assert (await client.request(method, url)).status_code == 404, (method, url)


@pytest.mark.anyio
async def test_power_grid_marks_pylons_insulators_and_nests_and_has_its_own_cards(client, seeded, switched_on):
    """Power Grid: the photos are marked for its own three parts (found only by the trained detector); a tower part
    is refused (422); its cards ask about the line and its bird nests, never about tower equipment or bolts."""
    ids = await seeded.make(industry="power_grid")
    _as(ids["owner"])
    photos = (await client.get(f"{_base(ids['layer'])}/photos")).json()
    assert [p["id"] for p in photos["parts"]] == ["power_pylon", "power_insulator", "power_nest"]
    assert [p["label"] for p in photos["parts"]] == ["Pylon", "Insulator", "Bird nest"]
    assert await _every_route(client, ids["layer"]) == [200, 200, 422, 422, 422, 200, 200]
    titles = {c["id"]: c["title"] for c in (await client.get(f"{_base(ids['layer'])}/cards")).json()["cards"]}
    assert titles["equipment"] == "What is on this line?" and titles["defects"] == "Are there bird nests on the line?"
    assert (await client.get(f"{_base(ids['layer'])}/cards/defects")).json()["question"] == titles["defects"]
    assert switched_on.calls == []


@pytest.mark.anyio
async def test_agriculture_marks_maize_plants_and_has_no_tower_cards(client, seeded, switched_on):
    """In Agriculture the photos are marked for maize plants only: a tower part is refused (422) before anything is
    saved or found, and the tower cards do not exist there (404)."""
    ids = await seeded.make(industry="agriculture")
    _as(ids["owner"])
    photos = (await client.get(f"{_base(ids['layer'])}/photos")).json()
    assert photos["parts"] == [{"id": "maize_seedling", "label": "Maize plant", "plural": "maize plants",
                               "scanned": False}]  # HOSTED_FINDER off: the example finder, marks first
    assert await _every_route(client, ids["layer"]) == [200, 200, 422, 422, 422, 404, 404]
    work = (await client.get(f"{_base(ids['layer'])}/photos/{SHA}/work")).json()
    assert list(work["parts"]) == ["maize_seedling"]
    saved = await client.put(f"{_base(ids['layer'])}/photos/{SHA}/marks", json={"part": "maize_seedling", "marks": MARKS})
    assert saved.status_code == 200 and saved.json()["marks"] == MARKS
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


async def _add_to_current_map(layer: str, project: str) -> None:
    conn = await admin()
    try:
        await conn.execute("UPDATE user_mundiai_maps SET layers = array_append(layers, $1) WHERE id = "
                           "(SELECT maps[array_length(maps, 1)] FROM user_mundiai_projects WHERE id = $2)", layer, project)
    finally:
        await conn.close()


@pytest.mark.anyio
async def test_a_farm_inspection_on_maps_of_two_farm_projects_is_a_conflict(client, seeded, switched_on):
    """Two farm projects showing one inspection: its photo work cannot be kept in one of them (409, as for tower
    work), reported before anything else."""
    ids = await seeded.make(industry="agriculture")
    other = await seeded.make(owner=ids["owner"], industry="agriculture")
    await _add_to_current_map(ids["layer"], other["project"])
    _as(ids["owner"])
    assert await _every_route(client, ids["layer"]) == [409] * 7


@pytest.mark.anyio
async def test_an_inspection_on_one_of_two_sibling_maps_is_found(client, seeded, switched_on):
    """Two uploads at once fork two sibling maps from one parent; the inspection on the older sibling is still shown
    (it is not on the project's last listed map) and its tower work is found."""
    ids = await seeded.make()
    parent, sibling = f"MP{uuid.uuid4().hex[:10]}", f"MS{uuid.uuid4().hex[:10]}"
    conn = await admin()
    try:
        await conn.execute("INSERT INTO user_mundiai_maps (id, project_id, owner_uuid, title, layers) "
                           "VALUES ($1, $2, $3::uuid, 'tower test', ARRAY[]::text[])", parent, ids["project"], ids["owner"])
        await conn.execute("UPDATE user_mundiai_maps SET parent_map_id = $1 WHERE id = $2", parent, ids["map"])
        await conn.execute("INSERT INTO user_mundiai_maps (id, project_id, owner_uuid, title, layers, parent_map_id) "
                           "VALUES ($1, $2, $3::uuid, 'tower test', ARRAY[]::text[], $4)",
                           sibling, ids["project"], ids["owner"], parent)
        await conn.execute("UPDATE user_mundiai_projects SET maps = ARRAY[$1, $2, $3] WHERE id = $4",
                           parent, ids["map"], sibling, ids["project"])
    finally:
        await conn.close()
    for extra in (parent, sibling):
        seeded.made.append({**ids, "map": extra, "new_project": False, "layer": f"none{extra}"})
    _as(ids["owner"])
    assert (await client.get(f"{_base(ids['layer'])}/photos")).status_code == 200


@pytest.mark.anyio
async def test_an_earlier_inspection_removed_from_the_map_is_not_compared(client, seeded, switched_on):
    april = tower_facts(sha=sha_of(variant(3)), name="OLD_0001.JPG", taken_at="2026-04-01T10:00:00")
    earlier = await seeded.make(facts=[april], files={april.sha256: variant(3)})
    now = await seeded.make(owner=earlier["owner"], project=earlier["project"])
    _as(earlier["owner"])
    before = (await client.get(f"{_base(now['layer'])}/cards/changes")).json()
    assert "1 earlier inspection of this site" in before["what"]
    forked = f"MF{uuid.uuid4().hex[:10]}"  # what removing the earlier layer does: a new map without it
    conn = await admin()
    try:
        await conn.execute("INSERT INTO user_mundiai_maps (id, project_id, owner_uuid, title, layers, parent_map_id) "
                           "VALUES ($1, $2, $3::uuid, 'tower test', ARRAY[$4], $5)",
                           forked, earlier["project"], earlier["owner"], now["layer"], earlier["map"])
    finally:
        await conn.close()
    seeded.made.append({**earlier, "map": forked, "new_project": False, "layer": f"none{forked}"})
    after = (await client.get(f"{_base(now['layer'])}/cards/changes")).json()
    assert "1 earlier inspection" not in after["what"]
    assert after["what"].startswith("This is the only inspection of this site in the project")


@pytest.mark.anyio
@pytest.mark.parametrize("cleared_by", ["removing it from the other map", "deleting the other project"])
async def test_the_conflict_clears_once_one_project_shows_the_inspection(client, seeded, switched_on, cleared_by):
    """Only each live project's current map counts: removing a layer forks a new map and keeps the old one, which
    must not hold the conflict forever."""
    ids = await seeded.make()
    other = await seeded.make(owner=ids["owner"])
    await _add_to_current_map(ids["layer"], other["project"])
    _as(ids["owner"])
    assert (await client.get(f"{_base(ids['layer'])}/photos")).status_code == 409
    conn = await admin()
    try:
        if cleared_by == "deleting the other project":
            await conn.execute("UPDATE user_mundiai_projects SET soft_deleted_at = now() WHERE id = $1",
                               other["project"])
        else:  # what remove_layer_from_map does: a new current map without the layer; the old one stays
            forked = f"MF{uuid.uuid4().hex[:10]}"
            await conn.execute(
                "INSERT INTO user_mundiai_maps (id, project_id, owner_uuid, title, layers, parent_map_id) "
                "VALUES ($1, $2, $3::uuid, 'tower test', ARRAY[$4], $5)",
                forked, other["project"], ids["owner"], other["layer"], other["map"])
            await conn.execute("UPDATE user_mundiai_projects SET maps = array_append(maps, $1) WHERE id = $2",
                               forked, other["project"])
            seeded.made.append({**other, "map": forked, "new_project": False, "layer": f"none{forked}"})
    finally:
        await conn.close()
    assert (await client.get(f"{_base(ids['layer'])}/photos")).status_code == 200


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
    assert set(empty) == {"parts"} and set(empty["parts"]) == set(tower_inspection.PARTS + tower_inspection.DEFECT_PARTS)
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
async def test_project_members_work_on_an_inspection_they_did_not_upload_by_their_role(client, seeded, switched_on):
    """A layer follows the roles of the projects it is in (alembic e7a1c4d9b2f3, Roger 2026-10-12): a viewer reads the
    tower work, an editor also marks; someone outside the project still gets 404."""
    ids = await seeded.make(editors=(EDITOR,), viewers=(VIEWER,))
    marks = f"{_base(ids['layer'])}/photos/{SHA}/marks"
    _as(VIEWER)
    assert (await client.get(f"{_base(ids['layer'])}/photos")).status_code == 200
    assert (await client.put(marks, json={"part": DISH, "marks": MARKS})).status_code == 403
    _as(EDITOR)
    assert (await client.put(marks, json={"part": DISH, "marks": MARKS})).status_code == 200
    _as(str(uuid.uuid4()))
    assert (await client.get(f"{_base(ids['layer'])}/photos")).status_code == 404


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
        assert deck.json()["heading"] == "Tower checks" and all(set(c) == {"id", "title", "status", "preview"} for c in cards)
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


# --- The scan: trained detectors look at every photo by themselves -------------------------------------------------

@pytest.mark.anyio
async def test_a_scan_finds_every_part_without_marks_and_only_a_persons_review_counts(client, seeded, switched_on,
                                                                                      monkeypatch):
    """With our own detectors on, nobody draws first: the scan runs each one on every photo, in the background, and
    each part's suggestions wait on its photo. A count still exists only once a person checked them."""
    from src.services import own_detector

    monkeypatch.setenv("OWN_DETECTORS", "1")
    monkeypatch.setattr(tower_inspection, "SCAN_WAIT_S", 0.05)
    seen = {"mw": [(500.0, 400.0, 620.0, 520.0, 0.9, 0)],
            "antenna": [(800.0, 300.0, 860.0, 520.0, 0.8, 0), (900.0, 300.0, 960.0, 520.0, 0.7, 0)]}

    def runner(model, cls, s3, bucket):
        return lambda rgb, prompts, classes, imgsz, conf, max_det: list(seen.get(cls, []))

    monkeypatch.setattr(own_detector, "runner", runner)
    ids = await seeded.make()
    _as(ids["owner"])
    listed = (await client.get(f"{_base(ids['layer'])}/photos")).json()
    assert {p["id"]: p["scanned"] for p in listed["parts"]}[DISH] is True
    started = await client.post(f"{_base(ids['layer'])}/scan")
    assert started.status_code == 200, started.text
    assert set(started.json()) == {"running", "parts", "photos", "scanned"} and DISH in started.json()["parts"]
    for _ in range(200):
        state = (await client.get(f"{_base(ids['layer'])}/scan")).json()
        if not state["running"] and state["scanned"] == state["photos"]:
            break
        await asyncio.sleep(0.05)
    assert state["scanned"] == state["photos"] >= 1, state
    work = (await client.get(f"{_base(ids['layer'])}/photos/{SHA}/work")).json()["parts"]
    assert work[DISH]["marks"] == [] and len(work[DISH]["suggestions"]) == 1 and work[DISH]["waiting"] == 1
    assert len(work[ANTENNA]["suggestions"]) == 2 and work[RADIO]["suggestions"] == []
    assert work[DISH]["counted_by_you"] is None  # nothing is counted before a person checks
    dish = work[DISH]["suggestions"][0]["id"]
    reviewed = await client.put(f"{_base(ids['layer'])}/photos/{SHA}/review", json={"part": DISH, "kept": [dish]})
    assert reviewed.status_code == 200 and reviewed.json()["counted_by_you"] == 1
    again = await client.post(f"{_base(ids['layer'])}/scan")  # finished photos are not scanned twice
    assert again.json()["scanned"] == again.json()["photos"]
    assert switched_on.calls == []  # the example finder never ran


@pytest.mark.anyio
async def test_the_example_finder_still_needs_marks(client, seeded, switched_on):
    """With our detectors off, nothing is scanned: the example finder (YOLOE) needs a person's examples first."""
    ids = await seeded.make()
    _as(ids["owner"])
    listed = (await client.get(f"{_base(ids['layer'])}/photos")).json()
    assert not any(p["scanned"] for p in listed["parts"])
    state = (await client.post(f"{_base(ids['layer'])}/scan")).json()
    assert state["running"] is False and state["parts"] == [] and state["scanned"] == 0


@pytest.mark.anyio
async def test_suggestions_get_outlines_a_tap_is_traced_and_a_photo_answers_the_questions(client, seeded, switched_on,
                                                                                         monkeypatch):
    """FastSAM replaced: the outlines are worked out once in the background and come back with the suggestions; a tap
    is traced, a tap that traces nothing or falls outside the photo says why; the photo's questions follow the
    contract."""
    monkeypatch.setenv("PART_OUTLINES", "1")
    monkeypatch.setattr(fastsam_model, "weights_status", lambda: {"available": True, "path": "fake.pt"})
    monkeypatch.setattr(part_outlines, "outline_boxes", lambda rgb, boxes: [
        [[[int(b[0]), int(b[1])], [int(b[2]), int(b[1])], [int(b[2]), int(b[3])]]] for b in boxes])
    monkeypatch.setattr(part_outlines, "trace", lambda rgb, x, y: part_outlines.Traced(
        polygons=[[[1, 1], [9, 1], [9, 9]]], box=(1.0, 1.0, 9.0, 9.0)) if x < 600 else None)
    ids = await seeded.make()
    _as(ids["owner"])
    photo = f"{_base(ids['layer'])}/photos/{SHA}"
    assert (await client.put(f"{photo}/marks", json={"part": DISH, "marks": MARKS})).status_code == 200
    assert (await client.post(f"{photo}/find", json={"part": DISH})).status_code == 200
    work = await _done(client, ids["layer"])
    deadline = time.monotonic() + 15
    while not any("outline" in s for s in work["suggestions"]) and time.monotonic() < deadline:
        await asyncio.sleep(0.2)
        work = (await client.get(f"{photo}/work")).json()["parts"][DISH]
    first = work["suggestions"][0]
    x0, y0, x1, y1 = (int(v) for v in first["box"])
    assert first["outline"] == [[[x0, y0], [x1, y0], [x1, y1]]]

    traced = await client.post(f"{photo}/trace", json={"x": 100, "y": 100})
    assert traced.status_code == 200, traced.text
    assert traced.json() == {"polygons": [[[1, 1], [9, 1], [9, 9]]], "box": [1.0, 1.0, 9.0, 9.0]}
    missed = await client.post(f"{photo}/trace", json={"x": 700, "y": 100})
    assert missed.status_code == 422 and "Nothing was traced there" in missed.json()["detail"]
    outside = await client.post(f"{photo}/trace", json={"x": 5000, "y": 100})
    assert outside.status_code == 422 and "outside the photo" in outside.json()["detail"]

    asked = await client.get(f"{photo}/questions?audience=technician")
    assert asked.status_code == 200, asked.text
    assert set(asked.json()) == {"summary", "questions"}
    assert all(set(q) == {"id", "question", "status", "status_label", "on_photo", "needs", "lights"}
               for q in asked.json()["questions"])
    equipment = next(q for q in asked.json()["questions"] if q["id"] == "equipment")
    assert equipment["status"] == "needs_check" and equipment["on_photo"].endswith("to check on this photo.")
