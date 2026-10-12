"""Photo inspections end to end (real database, real object store): a ZIP of photos or one photo with no map
position uploads as one vector layer of camera points with the switch on, today's paths stay with it off, and the
photo route serves only the layer's own photos to whoever may view the layer."""

import hashlib
import json
import uuid
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi import HTTPException

from src.database.models import MundiMap
from src.routes.postgres_routes import remove_layer_from_map
from src.services import photo_inspections
from src.structures import get_async_db_connection
from src.upload.base import HandlerResult, UploadContext
from src.upload.handlers.photo_set_handler import PhotoSetUploadHandler
from src.upload.handlers.raster_handler import NO_MAP_POSITION, RasterUploadHandler
from src.upload.handlers.vector_handler import VectorUploadHandler
from src.upload.registry import handler_for
from src.utils import get_async_s3_client, get_bucket_name
from test_fixtures.drone_photos import DJI_XMP, drone_jpg, photo_zip

RUN_TAG = uuid.uuid4().hex[:8]
FIXTURES = Path(__file__).parent.parent.parent / "test_fixtures"
SHAPEFILE_ZIP = FIXTURES / "banffopendata_trails.zip"
GEOTIFF = FIXTURES / "waterboard.tif"

# Three camera positions around a tower near Kigali.
TOWER = [(-1.94410, 30.06190), (-1.94420, 30.06210), (-1.94400, 30.06230)]


@pytest.fixture
def switch_on(monkeypatch):
    monkeypatch.setenv("PHOTO_INSPECTIONS", "1")


@pytest.fixture
def switch_off(monkeypatch):
    monkeypatch.setenv("PHOTO_INSPECTIONS", "0")


def _three_tower_photos() -> list[tuple[str, bytes]]:
    return [(f"tower/DJI_000{i}.JPG", drone_jpg(lat, lon, altitude=1530.0 + i, xmp=DJI_XMP))
            for i, (lat, lon) in enumerate(TOWER, start=1)]


async def _new_map(auth_client, title: str) -> dict:
    response = await auth_client.post("/api/maps/create", json={"title": f"{title} {RUN_TAG}"})
    assert response.status_code == 200, response.text
    return response.json()


async def _upload(auth_client, map_id: str, filename: str, data: bytes):
    return await auth_client.post(f"/api/maps/{map_id}/layers", files={"file": (filename, data, "application/zip")},
                                  data={"layer_name": filename})


async def _metadata(layer_id: str) -> dict:
    async with get_async_db_connection() as conn:
        raw = await conn.fetchval("SELECT metadata FROM map_layers WHERE layer_id = $1", layer_id)
    return json.loads(raw) if isinstance(raw, str) else raw


async def _stored_keys(prefix: str) -> list[str]:
    s3 = await get_async_s3_client()
    listing = await s3.list_objects_v2(Bucket=get_bucket_name(), Prefix=prefix)
    return sorted(o["Key"] for o in listing.get("Contents", []))


async def _features(auth_client, layer_id: str) -> list[dict]:
    response = await auth_client.get(f"/api/layer/{layer_id}.geojson")
    assert response.status_code == 200, response.text
    return response.json()["features"]


@pytest.mark.anyio
async def test_a_zip_of_three_photos_becomes_one_layer_of_three_camera_points(auth_client, switch_on):
    photos = _three_tower_photos()
    created = await _new_map(auth_client, "Tower inspection")
    response = await _upload(auth_client, created["id"], "tower.zip", photo_zip(photos))
    assert response.status_code == 200, response.text
    assert response.json()["type"] == "vector"
    layer_id = response.json()["id"]

    prefix = f"inspections/{created['project_id']}/{layer_id}/"
    assert (await _metadata(layer_id))["photo_inspection"] == {
        "photos": 3, "with_gps": 3, "without_gps": [], "storage_prefix": prefix}
    shas = sorted(hashlib.sha256(data).hexdigest() for _, data in photos)
    assert await _stored_keys(prefix) == sorted([f"{prefix}{sha}.jpg" for sha in shas] + [f"{prefix}facts.json"])
    kept = await photo_inspections.load_facts(await get_async_s3_client(), get_bucket_name(), prefix)
    assert [f.filename for f in kept] == [name for name, _ in photos] and sorted(f.sha256 for f in kept) == shas

    features = await _features(auth_client, layer_id)
    assert len(features) == 3
    by_name = {f["properties"]["filename"]: f for f in features}
    first = by_name["tower/DJI_0001.JPG"]
    assert first["geometry"]["coordinates"] == pytest.approx([30.06190, -1.94410], abs=1e-6)
    assert first["properties"]["relative_altitude_m"] == pytest.approx(45.2)
    assert first["properties"]["photo_key"].startswith(prefix)

    async with get_async_db_connection() as conn:
        hooks = await conn.fetchval(
            "SELECT count(*) FROM brain_pending_hooks WHERE payload->'layer_ids' ? $1", layer_id)
    assert hooks == 0, "camera positions are not turned into Brain pages"


@pytest.mark.anyio
async def test_photos_without_gps_are_listed_and_left_off_the_map(auth_client, switch_on):
    photos = _three_tower_photos()[:2] + [("tower/no_fix.jpg", drone_jpg())]
    created = await _new_map(auth_client, "Partial GPS")
    response = await _upload(auth_client, created["id"], "partial.zip", photo_zip(photos))
    assert response.status_code == 200, response.text
    inspection = (await _metadata(response.json()["id"]))["photo_inspection"]
    assert (inspection["photos"], inspection["with_gps"], inspection["without_gps"]) == (3, 2, ["tower/no_fix.jpg"])
    assert len(await _features(auth_client, response.json()["id"])) == 2
    # The kept facts hold every photo, also the one the map leaves out.
    kept = await photo_inspections.load_facts(await get_async_s3_client(), get_bucket_name(),
                                              inspection["storage_prefix"])
    assert [(f.filename, f.has_position) for f in kept] == [
        ("tower/DJI_0001.JPG", True), ("tower/DJI_0002.JPG", True), ("tower/no_fix.jpg", False)]


@pytest.mark.anyio
async def test_a_zip_of_photos_without_any_gps_is_refused(auth_client, switch_on):
    created = await _new_map(auth_client, "No GPS")
    response = await _upload(auth_client, created["id"], "nogps.zip",
                             photo_zip([("a.jpg", drone_jpg()), ("b.jpg", drone_jpg(model="other"))]))
    assert response.status_code == 400
    assert "None of the 2 photos has a GPS position" in response.json()["detail"]


@pytest.mark.anyio
async def test_a_zipped_shapefile_keeps_todays_path_with_the_switch_on(auth_client, switch_on):
    created = await _new_map(auth_client, "Shapefile")
    response = await _upload(auth_client, created["id"], "trails.zip", SHAPEFILE_ZIP.read_bytes())
    assert response.status_code == 200, response.text
    metadata = await _metadata(response.json()["id"])
    assert metadata["original_format"] == "shapefile_zip"
    assert "photo_inspection" not in metadata


@pytest.mark.anyio
async def test_switch_off_a_zip_of_photos_is_refused_as_today(auth_client, switch_off):
    created = await _new_map(auth_client, "Switch off zip")
    response = await _upload(auth_client, created["id"], "tower.zip", photo_zip(_three_tower_photos()))
    assert response.status_code == 400
    assert "does not contain any shapefiles" in response.json()["detail"]


@pytest.mark.anyio
async def test_switch_on_a_single_photo_without_map_position_is_a_one_photo_inspection(auth_client, switch_on):
    created = await _new_map(auth_client, "One photo")
    data = drone_jpg(*TOWER[0], xmp=DJI_XMP)
    response = await auth_client.post(f"/api/maps/{created['id']}/layers",
                                      files={"file": ("DJI_0042.JPG", data, "image/jpeg")})
    assert response.status_code == 200, response.text
    assert response.json()["type"] == "vector"
    layer_id = response.json()["id"]
    metadata = await _metadata(layer_id)
    assert metadata["original_format"] == "photo"
    assert metadata["photo_inspection"]["photos"] == 1 and metadata["photo_inspection"]["with_gps"] == 1
    [feature] = await _features(auth_client, layer_id)
    assert feature["properties"]["filename"] == "DJI_0042.JPG"
    assert feature["properties"]["sha256"] == hashlib.sha256(data).hexdigest()


async def _browser_upload(auth_client, map_id: str, filename: str, data: bytes):
    """The app's own upload: multipart to storage, then upload-complete. Returns (init answer, completion)."""
    init = (await auth_client.post(f"/api/maps/{map_id}/upload-multipart-init",
                                   params={"filename": filename, "file_size": len(data)})).json()
    child = init["dag_child_map_id"]
    part = await auth_client.put(f"/api/maps/{child}/upload-multipart-part", content=data,
                                 params={"upload_id": init["upload_id"], "s3_key": init["s3_key"], "part_number": 1})
    assert part.status_code == 200, part.text
    assembled = await auth_client.post(f"/api/maps/{child}/upload-multipart-complete", json={
        "s3_key": init["s3_key"], "upload_id": init["upload_id"], "layer_id": init["layer_id"],
        "filename": filename, "parts": [{"part_number": 1, "etag": part.json()["etag"]}]})
    assert assembled.status_code == 200, assembled.text
    done = await auth_client.post(f"/api/maps/{child}/upload-complete", json={
        "s3_key": init["s3_key"], "layer_id": init["layer_id"], "filename": filename})
    return init, done


@pytest.mark.anyio
async def test_switch_on_the_browser_upload_path_makes_a_vector_inspection_not_a_raster(auth_client, switch_on):
    created = await _new_map(auth_client, "Browser photo")
    init, done = await _browser_upload(auth_client, created["id"], "DJI_0007.JPG", drone_jpg(*TOWER[1]))
    assert done.status_code == 200, done.text
    assert done.json()["type"] == "vector"
    assert done.json()["url"] == f"/api/layer/{init['layer_id']}.pmtiles"
    metadata = await _metadata(init["layer_id"])
    assert metadata["photo_inspection"]["with_gps"] == 1
    assert "cog_status" not in metadata, "no raster optimisation was started for the photo"


@pytest.mark.anyio
async def test_switch_on_the_browser_path_keeps_each_photo_once_not_the_uploaded_zip_too(auth_client, switch_on):
    photos = _three_tower_photos()
    created = await _new_map(auth_client, "Browser zip")
    init, done = await _browser_upload(auth_client, created["id"], "tower.zip", photo_zip(photos))
    assert done.status_code == 200, done.text
    prefix = f"inspections/{created['project_id']}/{init['layer_id']}/"
    assert len(await _stored_keys(prefix)) == 4  # three photos and their facts
    assert await _stored_keys(init["s3_key"]) == [], "the uploaded ZIP is not kept next to the photos"
    async with get_async_db_connection() as conn:
        layer_key = await conn.fetchval("SELECT s3_key FROM map_layers WHERE layer_id = $1", init["layer_id"])
    assert layer_key.endswith(".geojson") and await _stored_keys(layer_key) == [layer_key]  # the camera points


@pytest.mark.anyio
@pytest.mark.parametrize("switch", ["0", "1"])
async def test_the_browser_path_keeps_other_uploads_as_today(auth_client, monkeypatch, switch):
    monkeypatch.setenv("PHOTO_INSPECTIONS", switch)
    created = await _new_map(auth_client, f"Browser shapefile {switch}")
    init, done = await _browser_upload(auth_client, created["id"], "trails.zip", SHAPEFILE_ZIP.read_bytes())
    assert done.status_code == 200, done.text
    assert await _stored_keys(init["s3_key"]) == [init["s3_key"]], "a shapefile upload keeps its uploaded file"


async def _fail_after_the_photos_are_stored(monkeypatch) -> list[str]:
    """Make creating the layer fail once the photos are stored; returns the keys that were stored at that moment."""
    stored: list[str] = []

    async def create_layers(self, ctx, result):
        stored.extend(await _stored_keys(photo_inspections.storage_prefix(ctx.project_id, ctx.layer_id)))
        raise RuntimeError("the database went away")

    monkeypatch.setattr(VectorUploadHandler, "create_layers", create_layers)
    return stored


@pytest.mark.anyio
async def test_switch_on_a_failed_upload_removes_the_photos_it_stored(auth_client, switch_on, monkeypatch):
    stored = await _fail_after_the_photos_are_stored(monkeypatch)
    created = await _new_map(auth_client, "Failed inspection")
    response = await _upload(auth_client, created["id"], "tower.zip", photo_zip(_three_tower_photos()))
    assert response.status_code == 500
    assert len(stored) == 4, "the photos and their facts were stored before the failure"
    assert await _stored_keys(f"inspections/{created['project_id']}/") == []


@pytest.mark.anyio
async def test_switch_on_a_failed_browser_upload_removes_the_photos_it_stored(auth_client, switch_on, monkeypatch):
    stored = await _fail_after_the_photos_are_stored(monkeypatch)
    created = await _new_map(auth_client, "Failed browser inspection")
    init, done = await _browser_upload(auth_client, created["id"], "tower.zip", photo_zip(_three_tower_photos()))
    assert done.status_code == 500
    assert len(stored) == 4, "the photos and their facts were stored before the failure"
    assert await _stored_keys(f"inspections/{created['project_id']}/") == []
    assert await _metadata(init["layer_id"]) is None


@pytest.mark.anyio
async def test_the_photos_of_an_existing_layer_are_never_removed_by_a_failed_retry(auth_client, switch_on):
    created = await _new_map(auth_client, "Retry")
    layer_id = (await _upload(auth_client, created["id"], "a.zip", photo_zip(_three_tower_photos()))).json()["id"]
    prefix = photo_inspections.storage_prefix(created["project_id"], layer_id)
    async with get_async_db_connection() as conn:
        removed = await photo_inspections.forget_if_unused(conn, await get_async_s3_client(), get_bucket_name(),
                                                           prefix, layer_id)
    assert removed == []
    assert len(await _stored_keys(prefix)) == 4  # three photos and their facts


@pytest.mark.anyio
async def test_switch_off_other_handlers_have_nothing_to_discard(switch_off):
    conn = MagicMock()
    ctx = UploadContext(map_id="M", layer_id="L", layer_name="x", file_basename="x", user_id="U", project_id="P",
                        temp_file_path="/nonexistent", file_ext=".geojson", file_size_bytes=1,
                        s3_key="uploads/U/P/L.geojson", metadata_dict={}, conn=conn, bucket_name="b")
    for handler in (VectorUploadHandler(), RasterUploadHandler()):
        assert await handler.discard(ctx) is None
    assert conn.mock_calls == []


@pytest.mark.anyio
async def test_the_photos_go_with_the_layers_row(auth_client, switch_on, monkeypatch):
    created = await _new_map(auth_client, "Delete inspection")
    response = await _upload(auth_client, created["id"], "tower.zip", photo_zip(_three_tower_photos()))
    layer_id, child_id = response.json()["id"], response.json()["dag_child_map_id"]
    prefix = photo_inspections.storage_prefix(created["project_id"], layer_id)

    # Through the API the earlier map version still shows the layer: its row stays, and with it the photos.
    removed = await auth_client.delete(f"/api/maps/{child_id}/layer/{layer_id}")
    assert removed.status_code == 200, removed.text
    assert len(await _stored_keys(prefix)) == 4  # three photos and their facts

    # Once no map version holds the layer, its row is deleted and the photos go too (whatever the switch says:
    # the files follow the row). Through the API that branch is not reached today (the earlier version above holds
    # the layer, and its style rows reference it), so the test removes the style links and calls the route
    # function on the only map that shows the layer.
    monkeypatch.setenv("PHOTO_INSPECTIONS", "0")
    async with get_async_db_connection() as conn:
        await conn.execute("DELETE FROM map_layer_styles WHERE layer_id = $1", layer_id)
        await conn.execute("DELETE FROM layer_styles WHERE layer_id = $1", layer_id)
        child = MundiMap(**dict(await conn.fetchrow("SELECT * FROM user_mundiai_maps WHERE id = $1", child_id)))
    await remove_layer_from_map(child_id, layer_id, forked_map=child)
    assert await _metadata(layer_id) is None
    assert await _stored_keys(prefix) == []


@pytest.mark.anyio
async def test_switch_on_a_single_photo_without_gps_is_refused(auth_client, switch_on):
    created = await _new_map(auth_client, "One photo no GPS")
    response = await auth_client.post(f"/api/maps/{created['id']}/layers",
                                      files={"file": ("plain.jpg", drone_jpg(), "image/jpeg")})
    assert response.status_code == 400
    assert response.json()["detail"].startswith("This photo has no map position and no GPS position")


@pytest.mark.anyio
async def test_switch_off_a_photo_without_map_position_is_refused_instead_of_pixel_bounds(auth_client, switch_off):
    created = await _new_map(auth_client, "Pixel bounds")
    response = await auth_client.post(f"/api/maps/{created['id']}/layers",
                                      files={"file": ("DJI_0042.JPG", drone_jpg(*TOWER[0]), "image/jpeg")})
    assert response.status_code == 400
    assert response.json()["detail"] == NO_MAP_POSITION


@pytest.mark.anyio
@pytest.mark.parametrize("switch", ["0", "1"])
async def test_the_raster_handler_never_gives_a_photo_pixel_corner_bounds(tmp_path, monkeypatch, switch):
    monkeypatch.setenv("PHOTO_INSPECTIONS", switch)
    photo = tmp_path / "photo.png"
    photo.write_bytes(drone_jpg(fmt="PNG"))
    conn = MagicMock()
    conn.execute = AsyncMock()
    ctx = UploadContext(map_id="M", layer_id="L", layer_name="photo", file_basename="photo", user_id="U",
                        project_id="P", temp_file_path=str(photo), file_ext=".png", file_size_bytes=1,
                        s3_key="uploads/U/P/L.png", metadata_dict={}, conn=conn, bucket_name="b")
    with pytest.raises(HTTPException) as refused:
        await RasterUploadHandler().create_layers(ctx, HandlerResult(layer_type="raster"))
    assert refused.value.status_code == 400 and refused.value.detail == NO_MAP_POSITION
    conn.execute.assert_not_called()


@pytest.mark.anyio
@pytest.mark.parametrize("limit,value", [("MAX_PHOTOS", 2), ("MAX_UNPACKED_BYTES", 1000)])
async def test_an_inspection_past_its_limits_is_refused(auth_client, switch_on, monkeypatch, limit, value):
    monkeypatch.setattr(photo_inspections, limit, value)
    created = await _new_map(auth_client, f"Limit {limit}")
    response = await _upload(auth_client, created["id"], "big.zip", photo_zip(_three_tower_photos()))
    assert response.status_code == 413, response.text


@pytest.mark.anyio
async def test_the_photo_route_serves_only_the_layers_own_photos(auth_client, switch_on, monkeypatch):
    photos = _three_tower_photos()
    first = await _new_map(auth_client, "Photo route A")
    second = await _new_map(auth_client, "Photo route B")
    layer_a = (await _upload(auth_client, first["id"], "a.zip", photo_zip(photos[:2]))).json()["id"]
    layer_b = (await _upload(auth_client, second["id"], "b.zip", photo_zip(photos[2:]))).json()["id"]
    sha_a = hashlib.sha256(photos[0][1]).hexdigest()
    sha_b = hashlib.sha256(photos[2][1]).hexdigest()

    seen = await auth_client.get(f"/api/layer/{layer_a}/photos/{sha_a}")
    assert seen.status_code == 200
    assert seen.content == photos[0][1]
    assert seen.headers["content-type"] == "image/jpeg"

    assert (await auth_client.get(f"/api/layer/{layer_a}/photos/{sha_b}")).status_code == 404  # layer B's photo
    assert (await auth_client.get(f"/api/layer/{layer_b}/photos/{sha_b}")).status_code == 200
    assert (await auth_client.get(f"/api/layer/{layer_a}/photos/{sha_a.upper()}")).status_code == 400
    assert (await auth_client.get(f"/api/layer/{layer_a}/photos/not-a-hash")).status_code == 400

    shapefile = (await _upload(auth_client, first["id"], "trails.zip", SHAPEFILE_ZIP.read_bytes())).json()["id"]
    assert (await auth_client.get(f"/api/layer/{shapefile}/photos/{sha_a}")).status_code == 404

    monkeypatch.setenv("PHOTO_INSPECTIONS", "0")
    assert (await auth_client.get(f"/api/layer/{layer_a}/photos/{sha_a}")).status_code == 404


@pytest.mark.anyio
async def test_dispatch_looks_inside_the_file_only_with_the_switch_on(tmp_path, monkeypatch):
    jpg = tmp_path / "photo.jpg"
    jpg.write_bytes(drone_jpg(*TOWER[0]))
    zipped = tmp_path / "photos.zip"
    zipped.write_bytes(photo_zip(_three_tower_photos()))
    not_a_zip = tmp_path / "broken.zip"
    not_a_zip.write_bytes(b"not a zip")
    geotiff_named_png = tmp_path / "georeferenced.png"  # GDAL reads the content: a georeferenced raster
    geotiff_named_png.write_bytes(GEOTIFF.read_bytes())

    monkeypatch.setenv("PHOTO_INSPECTIONS", "1")
    assert isinstance(await handler_for(".jpg", str(jpg)), PhotoSetUploadHandler)
    assert isinstance(await handler_for(".ZIP", str(zipped)), PhotoSetUploadHandler)
    assert isinstance(await handler_for(".zip", str(SHAPEFILE_ZIP)), VectorUploadHandler)
    assert isinstance(await handler_for(".zip", str(not_a_zip)), VectorUploadHandler)
    assert isinstance(await handler_for(".png", str(geotiff_named_png)), RasterUploadHandler)
    assert isinstance(await handler_for(".tif", str(GEOTIFF)), RasterUploadHandler)

    monkeypatch.setenv("PHOTO_INSPECTIONS", "0")
    assert isinstance(await handler_for(".jpg", str(jpg)), RasterUploadHandler)
    assert not isinstance(await handler_for(".zip", str(zipped)), PhotoSetUploadHandler)
