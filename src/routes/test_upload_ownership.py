"""The browser's upload path (multipart init -> parts -> assemble -> upload-complete), end to end against the real
object store, and the ownership check on every step that takes a storage key back from the browser."""

from pathlib import Path

import pytest

FIXTURE = Path(__file__).parent.parent.parent / "test_fixtures" / "barcelona_beaches.fgb"


async def _new_map(auth_client, title):
    response = await auth_client.post("/api/maps/create", json={"title": title})
    assert response.status_code == 200, response.text
    return response.json()["id"]


async def _start(auth_client, map_id, filename, size):
    response = await auth_client.post(f"/api/maps/{map_id}/upload-multipart-init",
                                      params={"filename": filename, "file_size": size})
    assert response.status_code == 200, response.text
    return response.json()


@pytest.mark.anyio
async def test_the_browser_upload_path_creates_the_layer(auth_client):
    data = FIXTURE.read_bytes()
    init = await _start(auth_client, await _new_map(auth_client, "Upload path"), FIXTURE.name, len(data))
    child = init["dag_child_map_id"]
    part = await auth_client.put(f"/api/maps/{child}/upload-multipart-part", content=data,
                                 params={"upload_id": init["upload_id"], "s3_key": init["s3_key"], "part_number": 1})
    assert part.status_code == 200, part.text
    status = await auth_client.get(f"/api/maps/{child}/upload-multipart-status",
                                   params={"upload_id": init["upload_id"], "s3_key": init["s3_key"]})
    assert status.status_code == 200 and status.json()["exists"] is True
    assembled = await auth_client.post(f"/api/maps/{child}/upload-multipart-complete", json={
        "s3_key": init["s3_key"], "upload_id": init["upload_id"], "layer_id": init["layer_id"],
        "filename": FIXTURE.name, "parts": [{"part_number": 1, "etag": part.json()["etag"]}]})
    assert assembled.status_code == 200, assembled.text
    done = await auth_client.post(f"/api/maps/{child}/upload-complete", json={
        "s3_key": init["s3_key"], "layer_id": init["layer_id"], "filename": FIXTURE.name})
    assert done.status_code == 200, done.text
    layers = await auth_client.get(f"/api/maps/{done.json()['dag_child_map_id']}/layers")
    assert init["layer_id"] in {layer["id"] for layer in layers.json()["layers"]}


@pytest.mark.anyio
async def test_every_step_refuses_a_key_that_is_not_the_callers_upload(auth_client):
    data = FIXTURE.read_bytes()
    init = await _start(auth_client, await _new_map(auth_client, "Own upload"), FIXTURE.name, len(data))
    child = init["dag_child_map_id"]
    own = init["s3_key"]
    someone_else = own.replace("uploads/", "uploads/someone-else-", 1)
    outside = "drone_vision/v2/some-partner/survey.json"
    traversal = own.rsplit("/", 1)[0] + "/../../other/" + own.rsplit("/", 1)[1]
    for key in (someone_else, outside, traversal):
        part = await auth_client.put(f"/api/maps/{child}/upload-multipart-part", content=b"x",
                                     params={"upload_id": init["upload_id"], "s3_key": key, "part_number": 1})
        status = await auth_client.get(f"/api/maps/{child}/upload-multipart-status",
                                       params={"upload_id": init["upload_id"], "s3_key": key})
        presign = await auth_client.post(f"/api/maps/{child}/upload-multipart-presign",
                                         json={"s3_key": key, "upload_id": init["upload_id"], "part_numbers": [1]})
        assembled = await auth_client.post(f"/api/maps/{child}/upload-multipart-complete", json={
            "s3_key": key, "upload_id": init["upload_id"], "layer_id": init["layer_id"], "filename": FIXTURE.name,
            "parts": []})
        done = await auth_client.post(f"/api/maps/{child}/upload-complete", json={
            "s3_key": key, "layer_id": init["layer_id"], "filename": FIXTURE.name})
        assert [r.status_code for r in (part, status, presign, assembled, done)] == [403] * 5, key


@pytest.mark.anyio
async def test_upload_complete_refuses_the_users_own_key_for_another_project_or_layer(auth_client):
    data = FIXTURE.read_bytes()
    first = await _start(auth_client, await _new_map(auth_client, "Project A"), FIXTURE.name, len(data))
    other_map = await _new_map(auth_client, "Project B")
    other_project = await auth_client.post(f"/api/maps/{other_map}/upload-complete", json={
        "s3_key": first["s3_key"], "layer_id": first["layer_id"], "filename": FIXTURE.name})
    other_layer = await auth_client.post(f"/api/maps/{first['dag_child_map_id']}/upload-complete", json={
        "s3_key": first["s3_key"], "layer_id": "Lnotthislayer", "filename": FIXTURE.name})
    assert other_project.status_code == 403 and other_layer.status_code == 403
