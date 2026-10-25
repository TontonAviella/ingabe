"""👍/👎 on Sage's answers (src/routes/answer_rating_routes.py, src/services/sage_answer_ratings.py) and errors from
people's browsers (src/routes/client_health_routes.py /client-error)."""

import json
import uuid

import pytest

from src.database.pool import get_async_db_connection
from src.services import sage_answer_ratings


async def _answer(map_id: str, role: str = "assistant") -> tuple[int, int]:
    """A conversation on the map's project with one message; (conversation id, message id)."""
    async with get_async_db_connection() as conn:
        row = await conn.fetchrow("SELECT m.project_id, p.owner_uuid FROM user_mundiai_maps m "
                                  "JOIN user_mundiai_projects p ON p.id = m.project_id WHERE m.id = $1", map_id)
        conv = await conn.fetchval("INSERT INTO conversations (project_id, owner_uuid, title) VALUES ($1, $2, $3) "
                                   "RETURNING id", row["project_id"], row["owner_uuid"], f"rating {uuid.uuid4().hex[:6]}")
        msg = await conn.fetchval(
            "INSERT INTO chat_completion_messages (map_id, sender_id, message_json, conversation_id) "
            "VALUES ($1, $2, $3, $4) RETURNING id",
            map_id, row["owner_uuid"], json.dumps({"role": role, "content": "**Maize looks healthy.**\n- NDVI 0.71"}),
            conv)
    return conv, msg


@pytest.mark.anyio
async def test_a_person_rates_an_answer_and_can_change_it(auth_client):
    created = await auth_client.post("/api/maps/create", json={"title": "Rating test", "description": "r"})
    map_id = created.json()["id"]
    conv, msg = await _answer(map_id)
    url = f"/api/maps/{map_id}/messages/{msg}/rating"

    assert (await auth_client.post(url, json={"rating": "up"})).status_code == 204
    assert (await auth_client.post(url, json={"rating": "down", "comment": "Wrong district"})).status_code == 204
    async with get_async_db_connection() as conn:
        rows = await conn.fetch("SELECT rating, comment FROM sage_answer_ratings WHERE message_id = $1", msg)
    assert [(r["rating"], r["comment"]) for r in rows] == [("down", "Wrong district")]  # one per person, changed

    found = await sage_answer_ratings.recent(days=1)
    mine = next(r for r in found["rated"] if r["conversation_id"] == conv)
    assert mine["answer"] == "Maize looks healthy." and mine["comment"] == "Wrong district"

    _, question = await _answer(map_id, role="user")
    assert (await auth_client.post(f"/api/maps/{map_id}/messages/{question}/rating",
                                   json={"rating": "up"})).status_code == 404  # only Sage's answers
    assert (await auth_client.post(url, json={"rating": "meh"})).status_code == 422


@pytest.mark.anyio
async def test_browser_errors_are_counted_and_logged(monkeypatch):
    from src.dependencies import health_metrics as hm
    from src.routes import client_health_routes as routes

    class FakeRedis:
        def __init__(self):
            self.h, self.n = {}, {}

        async def hincrby(self, key, field, by):
            self.h[field] = str(int(self.h.get(field, 0)) + by)

        async def incr(self, key):
            self.n[key] = self.n.get(key, 0) + 1
            return self.n[key]

        async def expire(self, key, seconds):
            return True

    fake = FakeRedis()
    logged: list[tuple] = []
    monkeypatch.setattr(hm, "_redis", lambda: fake)
    monkeypatch.setattr(routes, "get_async_redis", lambda: fake)
    monkeypatch.setattr(routes.browser_errors, "warning", lambda msg, *args, extra=None: logged.append((msg % args, extra)))
    await routes.client_error(routes.ClientError(kind="react_crash", message="x is undefined", page="/project/P1",
                                                 source="SageReply.tsx:10:5"), session=None)  # type: ignore[arg-type]
    await routes.client_error(routes.ClientError(kind="<script>", message="nope"), session=None)  # type: ignore[arg-type]
    assert fake.h == {"frontend_errors_total|kind=react_crash": "1"}
    ((text, extra),) = logged
    assert "x is undefined" in text and extra["fields"]["page"] == "/project/P1"
