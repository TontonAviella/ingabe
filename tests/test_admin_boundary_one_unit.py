"""Asked for one sector, Sage's admin-boundary fast path draws that sector or nothing, and the
project card frames it.

2026-10-08: "Remera sector in Gasabo district" drew all four Remera sectors in Rwanda: the parser
dropped "in Gasabo district" and a name shared by several units was drawn in full. This drives
the real chat turn (_maybe_run_fast_admin_boundary_turn) against seeded boundaries.
"""

import json
import uuid

import pytest

RUN_TAG = uuid.uuid4().hex[:8]
SHARED, DA, DB = f"Shared{RUN_TAG}", f"Da{RUN_TAG}", f"Db{RUN_TAG}"
# Two sectors called SHARED, in two districts far apart.
SQUARES = {DA: (30.10, -1.95, 30.12, -1.93), DB: (29.60, -2.40, 29.62, -2.38)}


def _envelope(box) -> str:
    return "ST_Multi(ST_MakeEnvelope({}, {}, {}, {}, 4326))".format(*box)


async def _turn(map_id: str, owner: str, conversation_id: int, project_id: str, text: str) -> None:
    from src.database.models import Conversation
    from src.dependencies.session import ServiceUserContext
    from src.routes.message_routes import _maybe_run_fast_admin_boundary_turn

    handled = await _maybe_run_fast_admin_boundary_turn(
        map_id=map_id,
        session=ServiceUserContext(owner),
        user_id=owner,
        conversation=Conversation(id=conversation_id, project_id=project_id, owner_uuid=owner),
        openai_messages=[{"role": "user", "content": text}],
    )
    assert handled, f"the admin-boundary fast path did not take {text!r}"


@pytest.mark.anyio
@pytest.mark.timeout(150)
async def test_one_sector_is_drawn_only_when_it_is_one_place(auth_client):
    from src.dependencies.base_map import get_base_map_provider
    from src.services.map_service import pull_bounds_from_map
    from src.structures import get_async_db_connection

    created = await auth_client.post("/api/maps/create", json={"title": "One sector"})
    assert created.status_code == 200, created.text
    project_id, map_id = created.json()["project_id"], created.json()["id"]
    conversation = await auth_client.post("/api/conversations", json={"project_id": project_id})
    assert conversation.status_code == 200, conversation.text
    conversation_id = conversation.json()["id"]

    async with get_async_db_connection() as conn:
        owner = str(await conn.fetchval("SELECT owner_uuid FROM user_mundiai_maps WHERE id = $1", map_id))
        for district, box in SQUARES.items():
            await conn.execute(
                f"INSERT INTO rwanda_sector_boundaries (sector_name, district_name, geom) VALUES ($1, $2, {_envelope(box)})",
                SHARED, district,
            )
        try:
            async def layers() -> list:
                return await conn.fetch(
                    "SELECT ml.layer_id, ml.postgis_query, ml.feature_count FROM map_layers ml "
                    "JOIN user_mundiai_maps m ON ml.layer_id = ANY(m.layers) WHERE m.id = $1",
                    map_id,
                )

            async def last_reply() -> str:
                row = await conn.fetchval(
                    "SELECT message_json FROM chat_completion_messages WHERE conversation_id = $1 ORDER BY id DESC LIMIT 1",
                    conversation_id,
                )
                return (json.loads(row) if isinstance(row, str) else row)["content"]

            # The name alone is two places: nothing is drawn, and the reply says where each one is.
            await _turn(map_id, owner, conversation_id, project_id, f"show {SHARED} sector")
            assert await layers() == []
            reply = await last_reply()
            assert f"There are 2 sectors called {SHARED}" in reply
            assert DA in reply and DB in reply and "haven't added any" in reply

            # With its district it is one place: exactly that sector is drawn.
            await _turn(map_id, owner, conversation_id, project_id, f"show {SHARED} sector in {DB} district")
            drawn = await layers()
            assert len(drawn) == 1 and drawn[0]["feature_count"] == 1
            rows = await conn.fetch(drawn[0]["postgis_query"])
            assert [(r["sector_name"], r["district_name"]) for r in rows] == [(SHARED, DB)]
            assert await last_reply() == f"I added {SHARED} sector ({DB} district) to the map."

            # The card frames that sector, not the other one, and is a real render.
            assert await pull_bounds_from_map(map_id) == pytest.approx(SQUARES[DB])
            card = await auth_client.get(f"/api/projects/{project_id}/social.webp")
            assert card.status_code == 200 and card.headers["content-type"] == "image/webp"
            with open(get_base_map_provider().get_default_preview_path(), "rb") as f:
                assert card.content != f.read(), "the card fell back to the default basemap preview"
        finally:
            await conn.execute(
                "DELETE FROM rwanda_sector_boundaries WHERE sector_name = $1", SHARED
            )
