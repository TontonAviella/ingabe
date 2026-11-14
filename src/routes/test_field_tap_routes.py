"""Field outlines from taps, end to end (src/routes/field_tap_routes.py): the switch, the rights checks (get_layer /
edit_layer), the farm-project gate, a prepared photo's manifest and window in real object storage, and the kept
outlines. The tap model is replaced by RecordedShapeModel (test_field_taps.py), which answers in the shapes the
real export returns."""

from __future__ import annotations

import io
import json
import os
import uuid

import asyncpg
import numpy as np
import pytest

from src.database.pool import _build_postgres_url
from src.dependencies.session import ServiceUserContext, verify_session_required
from src.services import field_taps, photo_plots
from src.services.test_field_taps import CRS, GRID, NORTH, WEST, WINDOW_ARRAY, RecordedShapeModel, _tap
from src.utils import get_bucket_name, get_s3_client
from src.wsgi import app

RUN_TAG = uuid.uuid4().hex[:6]
EDITOR, VIEWER = str(uuid.uuid4()), str(uuid.uuid4())


async def _admin():
    conn = await asyncpg.connect(_build_postgres_url())
    await conn.execute("SELECT set_config('app.user_id', '', false)")  # seeding: the admin branch of RLS
    return conn


def _store_folder(key: str) -> str:
    return os.path.dirname(field_taps.manifest_key(key))


@pytest.fixture
async def photo():
    """A project (owner, one editor, one viewer) whose map shows a drone photo layer; prepared for taps when asked
    (the manifest and the one window the taps land on, in object storage)."""
    made = []

    async def make(industry="agriculture", prepared=True):
        owner, tag = str(uuid.uuid4()), f"{RUN_TAG}{len(made)}"
        ids = {"owner": owner, "project": f"PT{tag}"[:12], "map": f"MT{tag}"[:12], "layer": f"LT{tag}"[:12],
               "key": f"test/field_taps/{tag}.tif"}
        conn = await _admin()
        try:
            await conn.execute(
                "INSERT INTO user_mundiai_projects (id, owner_uuid, editor_uuids, viewer_uuids, title, industry) "
                "VALUES ($1, $2::uuid, ARRAY[$3::uuid], ARRAY[$4::uuid], 'field taps test', $5)",
                ids["project"], owner, EDITOR, VIEWER, industry)
            await conn.execute(
                "INSERT INTO user_mundiai_maps (id, project_id, owner_uuid, title, layers) "
                "VALUES ($1, $2, $3::uuid, 'field taps test', ARRAY[$4])", ids["map"], ids["project"], owner,
                ids["layer"])
            await conn.execute(
                "INSERT INTO map_layers (layer_id, owner_uuid, name, type, metadata, source_map_id) "
                "VALUES ($1, $2::uuid, 'farm photo', 'raster', $3::jsonb, $4)",
                ids["layer"], owner, json.dumps({"cog_key": ids["key"], "cog_status": "ready", "band_count": 3}),
                ids["map"])
        finally:
            await conn.close()
        if prepared:
            store, bucket = get_s3_client(), get_bucket_name()
            store.put_object(Bucket=bucket, Key=field_taps.manifest_key(ids["key"]),
                             Body=json.dumps(field_taps.manifest(CRS, WEST, NORTH, sorted(GRID.windows))).encode())
            raw = io.BytesIO()
            np.save(raw, WINDOW_ARRAY)
            store.put_object(Bucket=bucket, Key=field_taps.window_key(ids["key"], (1, 0)), Body=raw.getvalue())
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
            for item in store.list_objects_v2(Bucket=bucket, Prefix=_store_folder(ids["key"]) + "/").get("Contents", []):
                store.delete_object(Bucket=bucket, Key=item["Key"])
    finally:
        await conn.close()


@pytest.fixture
def taps_on(monkeypatch):
    """The switch on and the recorded model instead of the stored export."""
    monkeypatch.setenv("FIELD_TAPS", "1")
    model = RecordedShapeModel()

    async def recorded_model(s3, bucket):
        return model

    monkeypatch.setattr(field_taps, "_tap_model", recorded_model)
    monkeypatch.setattr(field_taps, "_windows", type(field_taps._windows)())
    yield model
    app.dependency_overrides.pop(verify_session_required, None)


def _as(user: str) -> None:
    app.dependency_overrides[verify_session_required] = lambda: ServiceUserContext(user)


def _json(*taps, pick=None):
    return {"taps": [{"lon": t.lon, "lat": t.lat, "add": t.add} for t in taps], "pick": pick}


@pytest.mark.anyio
async def test_with_the_switch_off_no_route_exists(client, photo, monkeypatch):
    monkeypatch.delenv("FIELD_TAPS", raising=False)
    ids = await photo()
    _as(ids["owner"])
    base = f"/api/layer/{ids['layer']}/field-taps"
    try:
        answers = [await client.get(base), await client.post(f"{base}/outline", json=_json(_tap(60, 10))),
                   await client.post(f"{base}/kept", json=_json(_tap(60, 10), pick=0)),
                   await client.post(f"{base}/turned-down", json={**_json(_tap(60, 10)), "reason": "closed"}),
                   await client.post(f"{base}/moved-line", json={"first_tap": {"lon": 30, "lat": -2},
                                                                 "moved": [[30, -2], [30.1, -2], [30.1, -1.9]]}),
                   await client.delete(f"{base}/kept/abc")]
    finally:
        app.dependency_overrides.pop(verify_session_required, None)
    assert [a.status_code for a in answers] == [404] * 6
    # Checked before the body is read: bad JSON, signed out, still 404 (not a 422 showing the route exists).
    malformed = await client.post(f"{base}/outline", content=b"{", headers={"Content-Type": "application/json"})
    assert malformed.status_code == 404


@pytest.mark.anyio
async def test_a_tap_is_outlined_corrected_kept_and_removed(client, photo, taps_on):
    ids = await photo()
    base = f"/api/layer/{ids['layer']}/field-taps"
    _as(ids["owner"])
    state = (await client.get(base)).json()
    assert state["prepared"] is True and state["windows"] == 4 and state["kept"] == []
    assert state["measured"] == field_taps.MEASURED and state["max_corrections"] == field_taps.MAX_CORRECTIONS

    three = await client.post(f"{base}/outline", json=_json(_tap(60, 10)))
    assert three.status_code == 200, three.text
    assert [o["choice"] for o in three.json()["outlines"]] == [0, 1, 2]
    assert three.json()["outlines"][0]["geometry"]["type"] == "Polygon"
    assert three.json()["left_out"] == 0  # none of these choices runs along the window's border

    no_pick = await client.post(f"{base}/outline", json=_json(_tap(60, 10), _tap(70, 10)))
    assert no_pick.status_code == 422 and "Pick one" in no_pick.json()["detail"]
    corrected = await client.post(f"{base}/outline", json=_json(_tap(60, 10), _tap(70, 10), pick=0))
    assert [o["choice"] for o in corrected.json()["outlines"]] == [0]
    assert corrected.json()["outlines"][0]["area_ha"] > three.json()["outlines"][0]["area_ha"]

    kept = await client.post(f"{base}/kept", json=_json(_tap(60, 10), _tap(70, 10), pick=0))
    assert kept.status_code == 200, kept.text
    assert kept.json()["area_ha"] == corrected.json()["outlines"][0]["area_ha"] and kept.json()["corrections"] == 1
    # who kept it stays in the record: the answer to the keep is what the screen is shown, as in the state
    assert "kept_by" not in kept.json()
    # this photo is not marked as one the green rule was checked on: the field says so, and nothing is measured
    shown = (await client.get(base)).json()
    assert shown["cover_measured"] == field_taps.COVER_MEASURED and shown["cover_filling"] is False
    assert shown["kept"][0]["green_cover"] == {"share": None, "why_not": field_taps.COVER_NOT_CHECKED}
    assert [k["id"] for k in (await client.get(base)).json()["kept"]] == [kept.json()["id"]]

    square = [[t.lon, t.lat] for t in (_tap(50, 0), _tap(72, 0), _tap(72, 20), _tap(50, 20))]
    first = {"lon": _tap(60, 10).lon, "lat": _tap(60, 10).lat}
    measured = await client.post(f"{base}/moved-line", json={"first_tap": first, "moved": square})
    assert measured.status_code == 200, measured.text
    crossed = await client.post(f"{base}/moved-line",
                                json={"first_tap": first, "moved": [square[0], square[2], square[1], square[3]]})
    assert crossed.status_code == 422 and "crosses itself" in crossed.json()["detail"]
    by_hand = await client.post(f"{base}/kept", json={**_json(_tap(60, 10), pick=0), "moved": square})
    assert by_hand.status_code == 200, by_hand.text
    assert by_hand.json()["by_hand"] is True and by_hand.json()["area_ha"] == measured.json()["area_ha"]
    assert (await client.delete(f"{base}/kept/{by_hand.json()['id']}")).status_code == 200

    turned = await client.post(f"{base}/turned-down", json={**_json(_tap(60, 10)), "reason": "none_of_these"})
    assert turned.status_code == 200, turned.text
    assert turned.json()["reason"] == "none_of_these" and "turned_down_by" not in turned.json()
    assert (await client.post(f"{base}/turned-down", json={**_json(_tap(60, 10)), "reason": "bored"})).status_code == 422
    state = (await client.get(base)).json()
    assert state["turned_down"] == 1 and len(state["kept"]) == 1

    assert (await client.delete(f"{base}/kept/nope")).status_code == 404
    assert (await client.delete(f"{base}/kept/{kept.json()['id']}")).status_code == 200
    assert (await client.get(base)).json()["kept"] == []


@pytest.mark.anyio
async def test_taps_that_cannot_be_answered_say_why(client, photo, taps_on):
    ids = await photo()
    base = f"/api/layer/{ids['layer']}/field-taps"
    _as(ids["owner"])
    off_grid = await client.post(f"{base}/outline", json=_json(_tap(10, 70)))
    assert off_grid.status_code == 422 and "not prepared" in off_grid.json()["detail"]
    far = await client.post(f"{base}/outline", json=_json(_tap(60, 10), _tap(125, 10), pick=1))
    assert far.status_code == 422 and "too far" in far.json()["detail"]
    too_many = await client.post(f"{base}/outline", json=_json(*[_tap(60, 10)] * (2 + field_taps.MAX_CORRECTIONS), pick=1))
    assert too_many.status_code == 422
    assert (await client.post(f"{base}/kept", json=_json(_tap(60, 10)))).status_code == 422


@pytest.mark.anyio
async def test_a_viewer_gets_outlines_but_cannot_keep_or_remove(client, photo, taps_on):
    ids = await photo()
    base = f"/api/layer/{ids['layer']}/field-taps"
    _as(VIEWER)
    assert (await client.post(f"{base}/outline", json=_json(_tap(60, 10)))).status_code == 200
    assert (await client.post(f"{base}/kept", json=_json(_tap(60, 10), pick=0))).status_code in (403, 404)
    refused = await client.post(f"{base}/turned-down", json={**_json(_tap(60, 10)), "reason": "closed"})
    assert refused.status_code in (403, 404)
    _as(EDITOR)
    kept = await client.post(f"{base}/kept", json=_json(_tap(60, 10), pick=0))
    assert kept.status_code == 200, kept.text
    _as(VIEWER)
    assert (await client.delete(f"{base}/kept/{kept.json()['id']}")).status_code in (403, 404)
    assert len((await client.get(base)).json()["kept"]) == 1
    _as(str(uuid.uuid4()))  # a stranger learns nothing
    assert (await client.get(base)).status_code in (403, 404)


@pytest.mark.anyio
async def test_a_photo_that_was_not_prepared_and_a_tower_project(client, photo, taps_on):
    plain = await photo(prepared=False)
    _as(plain["owner"])
    base = f"/api/layer/{plain['layer']}/field-taps"
    assert (await client.get(base)).json()["prepared"] is False
    refused = await client.post(f"{base}/outline", json=_json(_tap(60, 10)))
    assert refused.status_code == 422 and "not prepared" in refused.json()["detail"]

    tower = await photo(industry="telecom")
    _as(tower["owner"])
    assert (await client.get(f"/api/layer/{tower['layer']}/field-taps")).status_code == 404


@pytest.mark.anyio
async def test_kept_fields_can_be_chosen_as_the_photos_plots_or_set_aside(client, photo, taps_on):
    ids = await photo()
    _as(ids["owner"])
    source = f"/api/layer/{ids['layer']}/plots/source"
    taps = f"/api/layer/{ids['layer']}/field-taps"

    async def chosen():
        conn = await _admin()
        try:
            return await conn.fetchval("SELECT metadata->>$2 FROM map_layers WHERE layer_id = $1", ids["layer"],
                                       photo_plots.PLOT_MAP_KEY)
        finally:
            await conn.close()

    # no field kept yet: the fields are not a source that can be chosen
    assert (await client.put(source, json={"source": photo_plots.FIELDS})).status_code == 400
    kept = await client.post(f"{taps}/kept", json=_json(_tap(60, 10), pick=0))
    assert kept.status_code == 200, kept.text
    # with a field kept, "found" is remembered as a choice: with nothing chosen the fields would be used
    assert (await client.put(source, json={"source": photo_plots.FOUND})).status_code == 200
    assert await chosen() == photo_plots.FOUND
    assert (await client.put(source, json={"source": photo_plots.FIELDS})).status_code == 200
    assert await chosen() == photo_plots.FIELDS
    # any other source is still looked up among the project's maps; this test photo has no outline on the map,
    # so that lookup stops where it always did ("the photo's outline is not known yet") and nothing is stored
    assert (await client.put(source, json={"source": "Lnotamap0000"})).status_code == 409
    assert await chosen() == photo_plots.FIELDS
    # once the last field is gone, choosing "found" clears the choice, as it did before fields existed
    assert (await client.delete(f"{taps}/kept/{kept.json()['id']}")).status_code == 200
    assert (await client.put(source, json={"source": photo_plots.FOUND})).status_code == 200
    assert await chosen() is None
