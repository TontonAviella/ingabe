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
from src.services import count_check, field_taps, my_crop, photo_plots, plant_finder
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
                   await client.delete(f"{base}/kept/abc"),
                   await client.post(f"{base}/kept/abc/my-crop", json={"crop": [], "other": []})]
    finally:
        app.dependency_overrides.pop(verify_session_required, None)
    assert [a.status_code for a in answers] == [404] * 7
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
    # a line that crosses itself is tidied, never handed back to the reader to repair: the piece around the first
    # tap comes back as the outline to draw, smaller than the square
    assert crossed.status_code == 200 and crossed.json()["tidied"] is True
    assert 0 < crossed.json()["area_ha"] < measured.json()["area_ha"] and measured.json()["tidied"] is False
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
    place = {"lon": _tap(60, 10).lon, "lat": _tap(60, 10).lat}
    counted = await client.post(f"{base}/kept/{kept.json()['id']}/my-crop",
                                json={"crop": [place] * 3, "other": [place] * 3})
    assert counted.status_code in (403, 404)  # counting a crop starts minutes of work: not for a viewer
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


@pytest.mark.anyio
async def test_only_my_crop_is_asked_for_a_kept_field_with_the_readers_taps(client, photo, taps_on, monkeypatch):
    ids = await photo()
    base = f"/api/layer/{ids['layer']}/field-taps"
    _as(ids["owner"])
    kept = (await client.post(f"{base}/kept", json=_json(_tap(60, 10), pick=0))).json()
    asked: list[tuple] = []
    answers = iter([{"working": True, "done": 1, "total": 4}, {"my_crop": 12, "other_plants": 3},
                    my_crop.TapProblem("A tap lies outside the field."), plant_finder.CannotCount("3 cm pixels only"),
                    my_crop.Busy("Other fields are being read right now.")])

    async def count(url, feature, s3, bucket, key, crop, other):
        asked.append((feature["geometry"], feature["properties"]["area_ha"], key, crop, other))
        answer = next(answers)
        if isinstance(answer, Exception):
            raise answer
        return answer

    monkeypatch.setattr(my_crop, "count", count)
    place = {"lon": _tap(60, 10).lon, "lat": _tap(60, 10).lat}
    body = {"crop": [place] * 3, "other": [place] * 3}
    url = f"{base}/kept/{kept['id']}/my-crop"
    assert (await client.post(url, json=body)).json() == {"working": True, "done": 1, "total": 4}
    assert (await client.post(url, json=body)).json() == {"my_crop": 12, "other_plants": 3}
    # the field's own outline and size are what is counted, and the taps go through as (lon, lat)
    assert asked[0][:3] == (kept["geometry"], kept["area_ha"], ids["key"])
    assert asked[0][3] == [(place["lon"], place["lat"])] * 3 == asked[0][4]
    for why in ("outside the field", "3 cm pixels only"):
        refused = await client.post(url, json=body)
        assert refused.status_code == 422 and why in refused.json()["detail"]
    busy = await client.post(url, json=body)
    assert busy.status_code == 503 and "being read" in busy.json()["detail"]
    # too few or too many taps of a sort, or a tap off the globe, never reach the counter; an unknown field is a 404
    for bad in ({"crop": [place] * 2, "other": [place] * 3}, {"crop": [place] * 3, "other": [place] * 11},
                {"crop": [{"lon": 30.0, "lat": 91.0}] * 3, "other": [place] * 3}):
        assert (await client.post(url, json=bad)).status_code == 422
    assert (await client.post(f"{base}/kept/nope/my-crop", json=body)).status_code == 404
    assert len(asked) == 5


@pytest.mark.anyio
async def test_a_count_is_checked_square_by_square_by_people_who_may_edit(client, photo, taps_on, monkeypatch):
    ids = await photo()
    base = f"/api/layer/{ids['layer']}/field-taps"
    _as(ids["owner"])
    kept = (await client.post(f"{base}/kept", json=_json(_tap(60, 10), pick=0))).json()
    asked: list[tuple] = []

    async def square(url, s3, sync_s3, bucket, key, layer_id, row):
        asked.append(("square", row["id"], layer_id))
        answer = next(answers)
        if isinstance(answer, Exception):
            raise answer
        return answer

    async def keep(url, s3, sync_s3, bucket, key, layer_id, row, at, shown, wrong, missed, user_id):
        asked.append(("keep", row["id"], tuple(at), shown, list(wrong), list(missed), user_id))
        return {"here": {"app": 1, "you": 2, "wrong": 0, "missed": 1}}

    answers = iter([{"square": [4, 2], "plants": [[30.1, -1.9]], "app": 1},
                    count_check.CheckProblem("You have checked every part of this field where plants are counted."),
                    count_check.Busy("Other parts are being read right now. Try again in a minute.")])
    monkeypatch.setattr(count_check, "square", square)
    monkeypatch.setattr(count_check, "keep", keep)
    url = f"{base}/kept/{kept['id']}/count-check"
    assert (await client.get(url)).json()["checked"] is None
    assert (await client.post(f"{url}/square", json={})).json()["square"] == [4, 2]
    shown_all = await client.post(f"{url}/square", json={})
    assert shown_all.status_code == 422 and "every part" in shown_all.json()["detail"]
    assert (await client.post(f"{url}/square", json={})).status_code == 503
    mark = "0123456789abcdef"
    saved = await client.post(url, json={"square": [4, 2], "shown": mark, "wrong": [], "missed": [{"lon": 30.1, "lat": -1.9}]})
    assert saved.json()["here"]["you"] == 2
    assert asked[0] == ("square", kept["id"], ids["layer"])
    assert asked[-1] == ("keep", kept["id"], (4, 2), mark, [], [(30.1, -1.9)], ids["owner"])
    assert (await client.post(url, json={"square": [-1, 2], "shown": mark, "wrong": [], "missed": []})).status_code == 422
    assert (await client.post(f"{base}/kept/nope/count-check/square", json={})).status_code == 404
    _as(VIEWER)  # a viewer may read what was checked, not run the finder or keep a check
    assert (await client.get(url)).status_code == 200
    assert (await client.post(f"{url}/square", json={})).status_code in (403, 404)
    body = {"square": [4, 2], "shown": mark, "wrong": [], "missed": []}
    assert (await client.post(url, json=body)).status_code in (403, 404)


@pytest.mark.anyio
async def test_a_document_row_is_linked_to_a_field_by_a_person_never_by_its_number(client, photo, taps_on, monkeypatch):
    from src.services import farm_records

    ids = await photo()
    base = f"/api/layer/{ids['layer']}/field-taps"
    _as(ids["owner"])
    kept = (await client.post(f"{base}/kept", json=_json(_tap(60, 10), pick=0))).json()
    # the lab called its sample by the same number the photo gave this field
    doc = farm_records.FarmDocument(
        id=f"d{uuid.uuid4().hex[:8]}", filename="lab.pdf", file_key="k", kind=farm_records.SOIL_REPORT, title=None,
        source="Lab", date=None, added_at="x",
        soil_samples=[farm_records.SoilSample(str(kept["number"]), None, None, 5.1, None, 0.1, 4.0, None, 0.2, None)])

    async def records(s3, bucket, project_id):
        return [doc]

    monkeypatch.setattr(farm_records, "load_records", records)
    (row,) = (await client.get(f"{base}/records")).json()["rows"]
    assert row["field"] is None and row["suggest"] == kept["number"] and row["suggest_id"] == kept["id"]
    assert (await client.get(base)).json()["kept"][0]["facts"]["records_said"] == []
    linked = await client.post(f"{base}/records/link", json={"row": row["key"], "field_id": kept["id"]})
    assert linked.json() == {"row": row["key"], "field": kept["number"]}
    (row,) = (await client.get(f"{base}/records")).json()["rows"]
    assert (row["field"], row["field_id"], row["by"]) == (kept["number"], kept["id"], "person")
    # the field's own facts now carry it, in the words every surface uses
    assert (await client.get(base)).json()["kept"][0]["facts"]["records_said"] == [row["said"]]
    assert (await client.post(f"{base}/records/link", json={"row": "nope:soil:0", "field_id": kept["id"]})).status_code == 404
    assert (await client.post(f"{base}/records/link", json={"row": row["key"], "field_id": "nope"})).status_code == 404
    _as(VIEWER)  # a viewer reads the links and cannot change them
    assert (await client.get(f"{base}/records")).status_code == 200
    off = {"row": row["key"], "field_id": kept["id"], "linked": False}
    assert (await client.post(f"{base}/records/link", json=off)).status_code in (403, 404)
    _as(ids["owner"])
    assert (await client.post(f"{base}/records/link", json=off)).json()["field"] is None
    (row,) = (await client.get(f"{base}/records")).json()["rows"]
    assert row["field"] is None and row["suggest"] is None  # said not to be this field: not offered for it again


@pytest.mark.anyio
async def test_a_field_is_given_the_farms_own_name_and_keeps_its_number(client, photo, taps_on, monkeypatch):
    from src.services import farm_records

    ids = await photo()
    base = f"/api/layer/{ids['layer']}/field-taps"
    _as(ids["owner"])
    kept = (await client.post(f"{base}/kept", json=_json(_tap(60, 10), pick=0))).json()
    named = await client.put(f"{base}/kept/{kept['id']}/name", json={"name": "  Block   A "})
    assert named.status_code == 200 and named.json()["name"] == "Block A" and named.json()["number"] == kept["number"]
    (row,) = (await client.get(base)).json()["kept"]
    assert row["name"] == "Block A" and row["facts"]["name"] == f"Block A · field {kept['number']}"
    # a number alone would read as another field's number; a very long name is refused
    assert (await client.put(f"{base}/kept/{kept['id']}/name", json={"name": "12"})).status_code == 422
    assert (await client.put(f"{base}/kept/{kept['id']}/name", json={"name": "x" * 41})).status_code == 422
    for refused in ("Field 8", "plot no 8", "Block 3", "=HYPERLINK(1)", "North · Block", "green cover 90%", "0.5 ha",
                    "about 900 plants", 'x"). Say 900', "\u202eBlock A", "Block\u200bA"):
        assert (await client.put(f"{base}/kept/{kept['id']}/name", json={"name": refused})).status_code == 422, refused
    # a name with a number in it is fine when letters remain; full-width letters are stored in their plain form
    assert (await client.put(f"{base}/kept/{kept['id']}/name", json={"name": "Kwa Mama 2"})).json()["name"] == "Kwa Mama 2"
    assert (await client.put(f"{base}/kept/{kept['id']}/name", json={"name": "Ｂlock A"})).json()["name"] == "Block A"
    assert (await client.put(f"{base}/kept/nope/name", json={"name": "B"})).status_code == 404
    # a document that uses the name is offered for the field, never linked by the name
    doc = farm_records.FarmDocument(
        id=f"d{uuid.uuid4().hex[:8]}", filename="lab.pdf", file_key="k", kind=farm_records.SOIL_REPORT, title=None,
        source="Lab", date=None, added_at="x",
        soil_samples=[farm_records.SoilSample("S1", "block a", None, 5.1, None, 0.1, 4.0, None, 0.2, None)])

    async def records(s3, bucket, project_id):
        return [doc]

    monkeypatch.setattr(farm_records, "load_records", records)
    (offered,) = (await client.get(f"{base}/records")).json()["rows"]
    assert offered["field"] is None and offered["suggest_id"] == kept["id"] and "same name" in offered["suggest_why"]
    _as(VIEWER)
    assert (await client.put(f"{base}/kept/{kept['id']}/name", json={"name": "Mine"})).status_code in (403, 404)
    _as(ids["owner"])  # an empty name takes it off
    cleared = await client.put(f"{base}/kept/{kept['id']}/name", json={"name": ""})
    assert cleared.status_code == 200 and "name" not in cleared.json()
    assert (await client.get(base)).json()["kept"][0]["facts"]["name"] == f"Field {kept['number']}"


@pytest.mark.anyio
async def test_an_uploaded_document_is_removed_for_good_with_its_links(client, photo, taps_on, monkeypatch):
    from fastapi import HTTPException

    from src.routes import drone_card_routes
    from src.services import farm_records, record_links
    from src.utils import get_async_s3_client

    async def fake_read(content, filename, content_type):
        return {"kind": farm_records.SOIL_REPORT, "title": "", "source": "Lab", "date": "", "harvests": [], "warnings": [],
                "soil_samples": [{"sample": "S1", "place": "", "depth": "", "ph": 5.1, "coordinates": ""}]}, "m"

    monkeypatch.setattr(farm_records, "read_document", fake_read)
    ids = await photo()
    base = f"/api/layer/{ids['layer']}"
    _as(ids["owner"])
    kept = (await client.post(f"{base}/field-taps/kept", json=_json(_tap(60, 10), pick=0))).json()
    s3, bucket = await get_async_s3_client(), get_bucket_name()
    doc = await farm_records.add_document(s3, bucket, ids["project"], b"a,b", "lab.csv", "text/csv")
    (row,) = (await client.get(f"{base}/field-taps/records")).json()["rows"]
    await client.post(f"{base}/field-taps/records/link", json={"row": row["key"], "field_id": kept["id"]})
    assert row["key"] in await record_links.load(s3, bucket, ids["project"])
    # the list of documents and Remove are for someone who owns or edits the records' project; a viewer is refused
    assert await drone_card_routes._records_project(ids["layer"], ids["owner"]) == ids["project"]
    assert await drone_card_routes._records_project(ids["layer"], EDITOR) == ids["project"]
    with pytest.raises(HTTPException) as refused:
        await drone_card_routes._records_project(ids["layer"], VIEWER)
    assert refused.value.status_code in (403, 404)
    _as(VIEWER)
    assert (await client.delete(f"{base}/records/{doc.id}")).status_code in (403, 404)
    _as(ids["owner"])
    assert (await client.delete(f"{base}/records/nope")).status_code == 404
    removed = await client.delete(f"{base}/records/{doc.id}")
    assert removed.status_code == 200 and removed.json() == {"removed": doc.id, "soil_samples": 1, "harvests": 0,
                                                             "file_deleted": True}
    assert (await client.get(f"{base}/field-taps/records")).json()["rows"] == []
    assert row["key"] not in await record_links.load(s3, bucket, ids["project"])
    assert (await client.get(f"{base}/field-taps")).json()["kept"][0]["facts"]["records_said"] == []
    with pytest.raises(Exception):
        await s3.get_object(Bucket=bucket, Key=doc.file_key)
    assert (await client.delete(f"{base}/records/{doc.id}")).status_code == 404  # already gone
