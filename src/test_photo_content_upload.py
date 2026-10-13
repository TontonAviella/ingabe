"""Uploading the same photo again, under any name, reuses the optimised photo and so every analysis kept for it."""
import asyncio
import json
import uuid
from pathlib import Path

import pytest

from src.structures import get_async_db_connection

RUN_TAG = uuid.uuid4().hex[:8]
PHOTO = Path(__file__).parent.parent / "test_fixtures" / "waterboard.tif"


async def _metadata_when_optimised(layer_id: str, seconds: float = 120) -> dict:
    for _ in range(int(seconds * 2)):
        async with get_async_db_connection() as conn:
            raw = await conn.fetchval("SELECT metadata FROM map_layers WHERE layer_id = $1", layer_id)
        metadata = json.loads(raw) if isinstance(raw, str) else (raw or {})
        if metadata.get("cog_status") in ("ready", "failed"):
            return metadata
        await asyncio.sleep(0.5)
    raise AssertionError(f"layer {layer_id} was not optimised in {seconds}s")


async def _upload(auth_client, map_id: str, filename: str, name: str) -> str:
    with open(PHOTO, "rb") as f:
        response = await auth_client.post(f"/api/maps/{map_id}/layers",
                                          files={"file": (filename, f, "image/tiff")}, data={"layer_name": name})
    assert response.status_code == 200, response.text
    layer_id = response.json()["id"]
    map_now = response.json()["dag_child_map_id"]
    # The browser's multipart upload starts optimisation itself; the direct upload used here does not.
    started = await auth_client.post(f"/api/maps/{map_now}/layers/{layer_id}/generate-cog")
    assert started.status_code == 200, started.text
    return layer_id


@pytest.mark.anyio
async def test_same_photo_under_another_name_shares_the_optimised_photo(auth_client):
    created = await auth_client.post("/api/maps/create", json={"title": f"Same photo {RUN_TAG}", "description": ""})
    map_id = created.json()["id"]
    first = await _upload(auth_client, map_id, f"farm-{RUN_TAG}.tif", f"Farm {RUN_TAG}")
    first_meta = await _metadata_when_optimised(first)
    assert first_meta["cog_status"] == "ready" and first_meta.get("content_sha256")

    second = await _upload(auth_client, map_id, f"renamed-{RUN_TAG}.tif", f"Renamed copy {RUN_TAG}")
    second_meta = await _metadata_when_optimised(second)
    assert second_meta["cog_key"] == first_meta["cog_key"]
    assert second_meta["cog_source"] == "same_content"
    assert second_meta["content_sha256"] == first_meta["content_sha256"]
