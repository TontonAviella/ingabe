"""Sage's raster fast paths read only a photo with a place on the map and a ready COG, and never answer
questions about masts and antennas. Drives the real handlers against layer rows in the database.

Before: on a Telecom project with any raster on the map, "how many antennas are on this tower photo?" drew
screening cells with no model call, and a phone picture with no coordinates was read like an orthophoto.
"""

import json
import uuid

import pytest

RUN_TAG = uuid.uuid4().hex[:8]
KIGALI_FLIGHT = [30.0589, -1.9536, 30.0646, -1.9489]
PHOTO_PIXEL_CORNERS = [0.0, 3000.0, 4000.0, 0.0]  # gdalinfo's corners of a 4000 x 3000 photo with no coordinates

READY = {"band_count": 3, "original_srid": 32735, "cog_status": "ready", "cog_key": "cog/layer/x.cog.tif"}
NO_PLACE = {"band_count": 3, "crs_missing": True, "cog_status": "failed"}  # gdalwarp cannot place it
OPTIMISING = {"band_count": 3, "original_srid": 32735, "cog_status": "generating"}

# The text each handler answers when the photo can be read.
HANDLER_TEXTS = {
    "_maybe_run_fast_raster_context_turn": "describe this photo",
    "_maybe_run_fast_raster_object_turn": "where are most houses in this photo?",
    "_maybe_run_fast_raster_fact_turn": "how big is this image?",
}


async def _project_with_one_raster(auth_client, conn, metadata: dict, bounds: list, project_industry: str):
    created = await auth_client.post("/api/maps/create", json={"title": f"Fast gate {RUN_TAG}"})
    assert created.status_code == 200, created.text
    project_id, map_id = created.json()["project_id"], created.json()["id"]
    conversation = await auth_client.post("/api/conversations", json={"project_id": project_id})
    assert conversation.status_code == 200, conversation.text
    owner = str(await conn.fetchval("SELECT owner_uuid FROM user_mundiai_maps WHERE id = $1", map_id))
    await conn.execute("UPDATE user_mundiai_projects SET industry = $2 WHERE id = $1", project_id, project_industry)
    layer_id = f"L{uuid.uuid4().hex[:11]}"
    metadata = {**metadata, **({"cog_key": f"cog/layer/{layer_id}.cog.tif"} if "cog_key" in metadata else {})}
    # The row the raster upload handler writes (src/upload/handlers/raster_handler.py), with the COG job's status.
    await conn.execute(
        "INSERT INTO map_layers (layer_id, owner_uuid, name, type, metadata, bounds, s3_key, source_map_id) "
        "VALUES ($1, $2, $3, 'raster', $4, $5, $6, $7)",
        layer_id, owner, f"Site_photo_{RUN_TAG}", json.dumps(metadata), bounds, f"uploads/{layer_id}.tif", map_id,
    )
    await conn.execute("UPDATE user_mundiai_maps SET layers = array_append(layers, $1) WHERE id = $2", layer_id, map_id)
    return {"map_id": map_id, "project_id": project_id, "owner": owner, "layer_id": layer_id,
            "conversation_id": conversation.json()["id"]}


async def _turn(handler_name: str, place: dict, text: str) -> bool:
    from src.database.models import Conversation
    from src.dependencies.session import ServiceUserContext
    from src.routes import message_routes

    return await getattr(message_routes, handler_name)(
        map_id=place["map_id"],
        session=ServiceUserContext(place["owner"]),
        user_id=place["owner"],
        conversation=Conversation(id=place["conversation_id"], project_id=place["project_id"],
                                  owner_uuid=place["owner"]),
        openai_messages=[{"role": "user", "content": text}],
    )


def _never_called(monkeypatch) -> list:
    """Replace the three fast-path tools; a gated turn must not reach any of them."""
    calls: list = []

    async def tool(args, meta):
        calls.append(args)
        return {"error": "should not run"}

    for module, name in (("src.tools.raster_h3_context", "create_raster_h3_context_layer"),
                         ("src.tools.raster_object_candidates", "analyze_raster_object_candidates"),
                         ("src.tools.raster_query", "describe_user_raster")):
        monkeypatch.setattr(f"{module}.{name}", tool)
    return calls


@pytest.mark.anyio
@pytest.mark.timeout(120)
@pytest.mark.parametrize("handler_name", sorted(HANDLER_TEXTS))
@pytest.mark.parametrize(("metadata", "bounds"), [(NO_PLACE, PHOTO_PIXEL_CORNERS), (OPTIMISING, KIGALI_FLIGHT)],
                         ids=["no_place_on_the_map", "cog_not_ready"])
async def test_a_photo_the_fast_paths_cannot_read_is_left_to_the_model(auth_client, monkeypatch, handler_name,
                                                                       metadata, bounds):
    from src.database.pool import set_request_industry
    from src.structures import get_async_db_connection

    calls = _never_called(monkeypatch)
    async with get_async_db_connection() as conn:
        place = await _project_with_one_raster(auth_client, conn, metadata, bounds, "agriculture")
        set_request_industry("agriculture")
        try:
            assert await _turn(handler_name, place, HANDLER_TEXTS[handler_name]) is False
        finally:
            set_request_industry(None)
        assert calls == []
        assert await conn.fetchval("SELECT count(*) FROM chat_completion_messages WHERE conversation_id = $1",
                                   place["conversation_id"]) == 0


@pytest.mark.anyio
@pytest.mark.timeout(120)
async def test_a_telecom_orthophoto_keeps_the_shared_fast_path_but_not_for_tower_questions(auth_client, monkeypatch):
    from src.database.pool import set_request_industry
    from src.structures import get_async_db_connection

    calls: list = []

    async def h3_tool(args, meta):
        calls.append(args.layer_id)
        return {"error": "Raster cell analysis failed: no pixels in this test"}  # the tool's own failure shape

    monkeypatch.setattr("src.tools.raster_h3_context.create_raster_h3_context_layer", h3_tool)
    async with get_async_db_connection() as conn:
        place = await _project_with_one_raster(auth_client, conn, READY, KIGALI_FLIGHT, "telecom")
        set_request_industry("telecom")
        try:
            for text in ("how many antennas are on this tower photo?", "where are the dishes in this photo",
                         "is there any damage on the tower in this image?", "describe the tower photo"):
                assert await _turn("_maybe_run_fast_raster_context_turn", place, text) is False, text
            assert calls == []
            # A question the screening cells can answer still takes the fast path on a Telecom project.
            assert await _turn("_maybe_run_fast_raster_context_turn", place, "describe this photo") is True
        finally:
            set_request_industry(None)
        assert calls == [place["layer_id"]]
        reply = await conn.fetchval(
            "SELECT message_json FROM chat_completion_messages WHERE conversation_id = $1 ORDER BY id DESC LIMIT 1",
            place["conversation_id"])
        assert "the raster analysis failed" in (json.loads(reply) if isinstance(reply, str) else reply)["content"]
