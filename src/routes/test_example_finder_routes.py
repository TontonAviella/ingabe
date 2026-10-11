"""The example finder's routes (src/routes/example_finder_routes.py) end to end: a real orthophoto in object storage
read through the raster workers, the real rights checks (edit_layer / get_layer), Redis jobs, the switch, the rate
limit and the line's caps. The model is replaced by the recorded Ultralytics result (RecordedModel,
test_example_finder.py), which does not care what the picture shows.

An orthophoto looks straight down, so the requests name a target measured on such pictures: maize seedlings
(640 px pictures read at 1280 px, plants from 34 px). The orthophoto is a 640 x 640 px part of the recorded tower
photo given map coordinates, and the marks are three dishes in it: about 63 px as the finder reads them."""

from __future__ import annotations

import asyncio
import io
import json
import time
import uuid

import asyncpg
import numpy as np
import pytest
import rasterio
from rasterio.transform import from_origin

from src.database.pool import _build_postgres_url
from src.dependencies.session import LegacyUserContext, ServiceUserContext, verify_session_required
from src.routes import example_finder_routes
from src.services import example_finder
from src.services.answer_levels import NADIR
from src.services.test_example_finder import MARKS, PIXELS, RecordedModel, _redis
from src.utils import get_bucket_name, get_s3_client
from src.wsgi import app

RUN_TAG = uuid.uuid4().hex[:6]
EDITOR, VIEWER = str(uuid.uuid4()), str(uuid.uuid4())
LEGACY = LegacyUserContext().get_user_id()
CRS = "EPSG:32735"
WEST, NORTH, PX_M = 800_000.0, 9_790_000.0, 0.01
TARGET = "maize_seedling"
LEFT, TOP, SIDE = 350, 50, 640
ORTHO = np.ascontiguousarray(PIXELS[TOP:TOP + SIDE, LEFT:LEFT + SIDE])
ORTHO_MARKS = [[x0 - LEFT, y0 - TOP, x1 - LEFT, y1 - TOP] for x0, y0, x1, y1 in MARKS]


def _to_layer(box):
    """A box in the orthophoto's pixels as (west, south, east, north) in its CRS."""
    x0, y0, x1, y1 = box
    return [WEST + x0 * PX_M, NORTH - y1 * PX_M, WEST + x1 * PX_M, NORTH - y0 * PX_M]


WHOLE_PHOTO = {"bbox": _to_layer((0, 0, SIDE, SIDE)), "crs": "layer"}
LAYER_MARKS = [_to_layer(m) for m in ORTHO_MARKS]


def _orthophoto_bytes() -> bytes:
    height, width = ORTHO.shape[:2]
    with rasterio.MemoryFile() as memfile:
        with memfile.open(driver="GTiff", width=width, height=height, count=3, dtype="uint8", crs=CRS,
                          transform=from_origin(WEST, NORTH, PX_M, PX_M), tiled=True, blockxsize=256,
                          blockysize=256) as dst:
            dst.write(ORTHO.transpose(2, 0, 1))
        return memfile.read()


async def _admin():
    conn = await asyncpg.connect(_build_postgres_url())
    await conn.execute("SELECT set_config('app.user_id', '', false)")  # seeding: the admin branch of RLS
    return conn


@pytest.fixture
async def photo():
    """A project (its owner, one editor, one viewer) with a map showing a drone orthophoto layer owned by the
    project's owner, a new user unless given (so each test has its own rate limit)."""
    made = []

    async def make(owner=None):
        owner = owner or str(uuid.uuid4())
        tag = f"{RUN_TAG}{len(made)}"
        ids = {"owner": owner, "project": f"PF{tag}"[:12], "map": f"MF{tag}"[:12], "layer": f"LF{tag}"[:12],
               "key": f"test/example_finder/{tag}.tif"}
        get_s3_client().put_object(Bucket=get_bucket_name(), Key=ids["key"], Body=_orthophoto_bytes())
        conn = await _admin()
        try:
            await conn.execute(
                "INSERT INTO user_mundiai_projects (id, owner_uuid, editor_uuids, viewer_uuids, title) "
                "VALUES ($1, $2::uuid, ARRAY[$3::uuid], ARRAY[$4::uuid], 'finder test')",
                ids["project"], owner, EDITOR, VIEWER)
            await conn.execute(
                "INSERT INTO user_mundiai_maps (id, project_id, owner_uuid, title, layers) "
                "VALUES ($1, $2, $3::uuid, 'finder test', ARRAY[$4])", ids["map"], ids["project"], owner, ids["layer"])
            metadata = {"cog_key": ids["key"], "cog_status": "ready", "band_count": 3}
            await conn.execute(
                "INSERT INTO map_layers (layer_id, owner_uuid, name, type, metadata, source_map_id) "
                "VALUES ($1, $2::uuid, 'tower photo', 'raster', $3::jsonb, $4)",
                ids["layer"], owner, json.dumps(metadata), ids["map"])
        finally:
            await conn.close()
        made.append(ids)
        return ids

    yield make
    conn = await _admin()
    store, bucket = get_s3_client(), get_bucket_name()
    try:
        for ids in made:
            await conn.execute("DELETE FROM map_layers WHERE layer_id = $1", ids["layer"])
            await conn.execute("DELETE FROM user_mundiai_maps WHERE id = $1", ids["map"])
            await conn.execute("DELETE FROM user_mundiai_projects WHERE id = $1", ids["project"])
            store.delete_object(Bucket=bucket, Key=ids["key"])
            listed = store.list_objects_v2(Bucket=bucket, Prefix=f"{example_finder.STORE_PREFIX}/{ids['project']}/")
            for item in listed.get("Contents", []):
                store.delete_object(Bucket=bucket, Key=item["Key"])
    finally:
        await conn.close()


@pytest.fixture
def finder(monkeypatch):
    """The switch on, Redis jobs in a test database and prefix, the recorded model instead of the weights."""
    monkeypatch.setenv("EXAMPLE_FINDER", "1")
    monkeypatch.setattr(example_finder, "REDIS_PREFIX", f"example_finder_test:{RUN_TAG}:{uuid.uuid4().hex[:6]}")
    redis = _redis()
    monkeypatch.setattr(example_finder_routes, "get_async_redis", lambda: redis)
    model = RecordedModel()
    monkeypatch.setattr(example_finder, "_stored_model", lambda: model)
    yield model
    app.dependency_overrides.pop(verify_session_required, None)


def _as(user) -> None:
    app.dependency_overrides[verify_session_required] = (
        lambda: user if isinstance(user, LegacyUserContext) else ServiceUserContext(user))


def _body(**changes):
    return {"target": TARGET, "window": WHOLE_PHOTO, "marks": LAYER_MARKS, **changes}


async def _settled(client, layer, job_id, limit_s=30.0):
    deadline = time.monotonic() + limit_s
    while time.monotonic() < deadline:
        response = await client.get(f"/api/layer/{layer}/finder/{job_id}")
        assert response.status_code == 200, response.text
        if response.json()["status"] in (example_finder.DONE, example_finder.FAILED):
            return response.json()
        await asyncio.sleep(0.1)
    raise AssertionError(f"finder job {job_id} did not finish in {limit_s} s")


@pytest.mark.anyio
async def test_with_the_switch_off_neither_route_exists(client, photo, monkeypatch):
    monkeypatch.delenv("EXAMPLE_FINDER", raising=False)
    ids = await photo()
    _as(ids["owner"])
    try:
        posted = await client.post(f"/api/layer/{ids['layer']}/finder", json=_body())
        read = await client.get(f"/api/layer/{ids['layer']}/finder/{'a' * 64}")
    finally:
        app.dependency_overrides.pop(verify_session_required, None)
    assert posted.status_code == 404 and read.status_code == 404
    # Checked before the body is read: bad JSON, signed out, still 404 (not a 422 showing the route exists).
    malformed = await client.post(f"/api/layer/{ids['layer']}/finder", content=b"{",
                                  headers={"Content-Type": "application/json"})
    assert malformed.status_code == 404


@pytest.mark.anyio
async def test_the_owner_finds_the_rest_in_the_photos_coordinates_once(client, photo, finder):
    ids = await photo()
    _as(ids["owner"])
    started = await client.post(f"/api/layer/{ids['layer']}/finder", json=_body())
    assert started.status_code == 200, started.text
    job = started.json()
    assert job["status"] in (example_finder.QUEUED, example_finder.RUNNING) and example_finder.is_job_id(job["job_id"])
    done = await _settled(client, ids["layer"], job["job_id"])
    expected = example_finder.find(ORTHO, ORTHO_MARKS, [], TARGET, view=NADIR, runner=RecordedModel())
    assert done["status"] == example_finder.DONE and len(done["finds"]) == len(expected) > 0
    first = done["finds"][0]
    assert first["box_px"] == pytest.approx(expected[0].box, abs=0.01)
    assert first["box"] == pytest.approx(_to_layer(first["box_px"]), abs=0.001)  # metres in the photo's CRS
    (call,) = finder.calls
    assert np.array_equal(call["rgb"], ORTHO) and np.allclose(call["prompts"], ORTHO_MARKS, atol=0.001)
    again = await client.post(f"/api/layer/{ids['layer']}/finder", json=_body())
    assert again.json() == done and len(finder.calls) == 1  # kept, not found twice


@pytest.mark.anyio
async def test_a_project_viewer_never_starts_the_finder(client, photo, finder):
    """Today both routes answer 404 to anyone but the layer's owner: map_layers' row-level security shows a layer
    only to its owner, so get_layer finds nothing before any rights check runs (the xfail tests below say what the
    spec wants)."""
    ids = await photo()
    _as(VIEWER)
    refused = await client.post(f"/api/layer/{ids['layer']}/finder", json=_body())
    assert refused.status_code == 404 and finder.calls == []


# The spec: project editors start jobs (EDIT rights), project viewers read them (VIEW rights). Not so today:
RLS_GAP = pytest.mark.xfail(
    strict=True, raises=AssertionError,
    reason="map_layers row-level security (alembic c1d2e3f4a5b9) shows a layer only to its owner, so get_layer and "
           "edit_layer answer 404 to project editors and viewers; a decision for Roger (EXAMPLE_FINDER_RULES.md)")


@RLS_GAP
@pytest.mark.anyio
async def test_spec_a_project_viewer_is_told_403_when_starting(client, photo, finder):
    ids = await photo()
    _as(VIEWER)
    refused = await client.post(f"/api/layer/{ids['layer']}/finder", json=_body())
    assert refused.status_code == 403, refused.text


@RLS_GAP
@pytest.mark.anyio
async def test_spec_a_project_editor_starts_the_finder(client, photo, finder):
    ids = await photo()
    _as(EDITOR)
    started = await client.post(f"/api/layer/{ids['layer']}/finder", json=_body())
    assert started.status_code == 200, started.text


@RLS_GAP
@pytest.mark.anyio
async def test_spec_a_project_viewer_reads_the_finds(client, photo, finder):
    ids = await photo()
    _as(ids["owner"])
    job = (await client.post(f"/api/layer/{ids['layer']}/finder", json=_body())).json()
    await _settled(client, ids["layer"], job["job_id"])
    _as(VIEWER)
    read = await client.get(f"/api/layer/{ids['layer']}/finder/{job['job_id']}")
    assert read.status_code == 200, read.text


@pytest.mark.anyio
async def test_in_view_only_mode_the_finds_can_be_read_but_not_started(client, photo, finder, monkeypatch):
    ids = await photo(owner=LEGACY)
    _as(LegacyUserContext())
    job = (await client.post(f"/api/layer/{ids['layer']}/finder", json=_body())).json()
    await _settled(client, ids["layer"], job["job_id"])
    monkeypatch.setenv("MUNDI_AUTH_MODE", "view_only")
    read = await client.get(f"/api/layer/{ids['layer']}/finder/{job['job_id']}")
    assert read.status_code == 200 and read.json()["status"] == example_finder.DONE
    refused = await client.post(f"/api/layer/{ids['layer']}/finder", json=_body(marks=LAYER_MARKS[:2]))
    assert refused.status_code == 403 and len(finder.calls) == 1


@pytest.mark.anyio
async def test_a_job_of_another_photo_or_an_unknown_job_is_not_found(client, photo, finder):
    mine = await photo()
    other = await photo(owner=mine["owner"])
    _as(mine["owner"])
    job = (await client.post(f"/api/layer/{mine['layer']}/finder", json=_body())).json()
    await _settled(client, mine["layer"], job["job_id"])
    elsewhere = await client.get(f"/api/layer/{other['layer']}/finder/{job['job_id']}")
    assert elsewhere.status_code == 404  # another project: its finds are not this project's
    assert (await client.get(f"/api/layer/{mine['layer']}/finder/{'b' * 64}")).status_code == 404
    assert (await client.get(f"/api/layer/{mine['layer']}/finder/not-a-job")).status_code == 404


@pytest.mark.anyio
@pytest.mark.parametrize("changes, says", [
    ({"target": "banana_mat"}, "never been measured on banana mats"),
    ({"marks": []}, "give 1 to 20 marks"),
    ({"marks": [[WEST - 5, NORTH - 1, WEST - 4, NORTH]]}, "mark 1 is not inside the picture"),
    ({"lookalike_marks": LAYER_MARKS[:1]}, "look-alike marks are not used for maize plants"),
    ({"target": "tower_dish"}, "dishes were measured only on photos taken at an angle"),
    ({"target": "tree_crown_forest"}, "no object size was measured for tree crowns"),
    ({"marks": [_to_layer((x, 300, x + 10, 310)) for x in (100, 200, 300)]},
     "maize plants were measured only from 34 px wide, so this picture is outside the measurement"),
    ({"window": {"bbox": [WEST - 100, NORTH + 50, WEST - 90, NORTH + 60], "crs": "layer"}}, "does not overlap"),
    ({"window": {"bbox": WHOLE_PHOTO["bbox"], "crs": "EPSG:3857"}}, "must be one of"),
])
async def test_requests_that_cannot_be_answered_say_why(client, photo, finder, changes, says):
    ids = await photo()
    _as(ids["owner"])
    response = await client.post(f"/api/layer/{ids['layer']}/finder", json=_body(**changes))
    assert response.status_code == 422 and says in response.json()["detail"], response.text
    assert finder.calls == []


@pytest.mark.anyio
async def test_starting_the_finder_is_rate_limited_per_user(client, photo, finder):
    ids = await photo()
    _as(ids["owner"])
    statuses = [(await client.post(f"/api/layer/{ids['layer']}/finder", json=_body(target="banana_mat"))).status_code
                for _ in range(30)]
    assert 429 in statuses and statuses[0] == 422
    other = await photo()
    _as(other["owner"])
    assert (await client.post(f"/api/layer/{other['layer']}/finder",
                              json=_body(target="banana_mat"))).status_code == 422  # another user is not limited


@pytest.mark.anyio
async def test_a_full_line_is_told_429_for_the_user_and_503_for_the_app(client, photo, finder, monkeypatch):
    monkeypatch.setattr(example_finder, "MAX_JOBS_PER_USER", 1)
    monkeypatch.setattr(example_finder, "MAX_JOBS", 2)
    finder.delay_s = 6.0  # the first job is still running while the others ask
    ids, other = await photo(), await photo()
    _as(ids["owner"])
    first = await client.post(f"/api/layer/{ids['layer']}/finder", json=_body())
    assert first.status_code == 200, first.text
    mine = await client.post(f"/api/layer/{ids['layer']}/finder", json=_body(marks=LAYER_MARKS[:2]))
    assert mine.status_code == 429 and "you already have 1 finder jobs" in mine.json()["detail"]
    _as(other["owner"])
    theirs = await client.post(f"/api/layer/{other['layer']}/finder", json=_body())
    assert theirs.status_code == 200, theirs.text
    full = await client.post(f"/api/layer/{other['layer']}/finder", json=_body(marks=LAYER_MARKS[1:]))
    assert full.status_code == 429  # this user's own place counts first
    monkeypatch.setattr(example_finder, "MAX_JOBS_PER_USER", 5)  # now only the app's two places are taken
    _as(ids["owner"])
    busy = await client.post(f"/api/layer/{ids['layer']}/finder", json=_body(marks=LAYER_MARKS[1:]))
    assert busy.status_code == 503 and "the finder already has 2 pictures" in busy.json()["detail"]
    _as(ids["owner"])
    await _settled(client, ids["layer"], first.json()["job_id"])
    _as(other["owner"])
    await _settled(client, other["layer"], theirs.json()["job_id"])
