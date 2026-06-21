import asyncio
import json
import time
import uuid
from datetime import datetime, timezone
from unittest.mock import AsyncMock, patch

import pytest

from src._test_streaming_mock import MockResponse, recv_non_streaming
from src.routes import websocket as websocket_routes


@pytest.fixture
def test_map_fixture(sync_auth_client):
    map_title = f"Test WebSocket Map {uuid.uuid4()}"

    response = sync_auth_client.post(
        "/api/maps/create",
        json={
            "title": map_title,
            "link_accessible": True,
        },
    )
    assert response.status_code == 200
    data = response.json()
    return {"map_id": data["id"], "project_id": data["project_id"]}


def test_websocket_successful_connection(
    test_map_fixture, sync_auth_client, websocket_url_for_map
):
    map_id = test_map_fixture["map_id"]
    project_id = test_map_fixture["project_id"]

    # Create conversation
    response = sync_auth_client.post(
        "/api/conversations",
        json={"project_id": project_id},
    )
    assert response.status_code == 200
    conversation_id = response.json()["id"]

    # no errors
    with sync_auth_client.websocket_connect(
        websocket_url_for_map(map_id, conversation_id)
    ):
        pass


def test_websocket_404(sync_auth_client):
    test_map_id = "should-404-doesntexist"

    with pytest.raises(Exception):  # TestClient raises different exceptions
        with sync_auth_client.websocket_connect(
            f"/api/maps/ws/{test_map_id}/messages/updates"
        ):
            pytest.fail("WebSocket connection should have failed without token")


def _ephemeral_payload(conversation_id: int):
    return websocket_routes.EphemeralNotificationPayload(
        conversation_id=conversation_id,
        ephemeral=True,
        action_id=str(uuid.uuid4()),
        layer_id=None,
        action="Sage is thinking...",
        timestamp=datetime.now(timezone.utc),
        completed_at=None,
        status="active",
        bounds=None,
        updates={"style_json": False},
    )


@pytest.mark.anyio
async def test_publish_and_distribute_sends_local_before_redis(monkeypatch):
    conversation_id = 987654321
    queue: asyncio.Queue = asyncio.Queue()
    payload = _ephemeral_payload(conversation_id)
    published_payloads = []

    async def fake_publish(payload_json: str) -> bool:
        published_payloads.append(json.loads(payload_json))
        assert queue.qsize() == 1
        return True

    monkeypatch.setattr(websocket_routes, "_publish_to_redis", fake_publish)

    async with websocket_routes.subscribers_lock:
        websocket_routes.subscribers_by_conversation[conversation_id].add(queue)
    try:
        await websocket_routes._publish_and_distribute(payload)

        assert queue.qsize() == 1
        delivered = await queue.get()
        assert delivered.action_id == payload.action_id
        assert (
            published_payloads[0]["_redis_origin_id"]
            == websocket_routes._REDIS_ORIGIN_ID
        )
    finally:
        async with websocket_routes.subscribers_lock:
            websocket_routes.subscribers_by_conversation[conversation_id].discard(queue)
            if not websocket_routes.subscribers_by_conversation[conversation_id]:
                del websocket_routes.subscribers_by_conversation[conversation_id]


@pytest.mark.anyio
async def test_redis_loopback_from_same_process_is_ignored():
    conversation_id = 987654322
    queue: asyncio.Queue = asyncio.Queue()
    payload_dict = _ephemeral_payload(conversation_id).model_dump(mode="json")
    payload_dict["_redis_origin_id"] = websocket_routes._REDIS_ORIGIN_ID

    async with websocket_routes.subscribers_lock:
        websocket_routes.subscribers_by_conversation[conversation_id].add(queue)
    try:
        await websocket_routes._distribute_from_json(json.dumps(payload_dict))

        assert queue.empty()
    finally:
        async with websocket_routes.subscribers_lock:
            websocket_routes.subscribers_by_conversation[conversation_id].discard(queue)
            if not websocket_routes.subscribers_by_conversation[conversation_id]:
                del websocket_routes.subscribers_by_conversation[conversation_id]


def test_websocket_receive_ephemeral_action(
    test_map_fixture, sync_auth_client, websocket_url_for_map
):
    def create_response_queue():
        return [
            MockResponse("Hello! How can I help?", None),
        ]

    response_queue = create_response_queue()

    with patch("src.routes.message_routes.get_openai_client") as mock_get_client:
        mock_client = AsyncMock()

        async def mock_create(*args, **kwargs):
            return response_queue.pop(0)

        mock_client.chat.completions.create = AsyncMock(side_effect=mock_create)
        mock_get_client.return_value = mock_client

        map_id = test_map_fixture["map_id"]
        project_id = test_map_fixture["project_id"]

        # Create conversation
        response = sync_auth_client.post(
            "/api/conversations",
            json={"project_id": project_id},
        )
        assert response.status_code == 200
        conversation_id = response.json()["id"]

        with sync_auth_client.websocket_connect(
            websocket_url_for_map(map_id, conversation_id)
        ) as websocket:
            response = sync_auth_client.post(
                f"/api/maps/conversations/{conversation_id}/maps/{map_id}/send",
                json={
                    "message": {
                        "role": "user",
                        "content": "Hello",
                    },
                    "selected_feature": None,
                },
            )
            assert response.status_code == 200

            # Receive messages until we get the ephemeral action message
            ephemeral_msg = None
            max_attempts = 10
            for _ in range(max_attempts):
                recv_msg = recv_non_streaming(websocket)
                if recv_msg.get("ephemeral") is True:
                    ephemeral_msg = recv_msg
                    break

            assert ephemeral_msg is not None, "Did not receive ephemeral action message"
            assert "ephemeral" in ephemeral_msg
            assert ephemeral_msg["ephemeral"] is True
            assert "action_id" in ephemeral_msg
            assert "action" in ephemeral_msg
            assert "timestamp" in ephemeral_msg
            assert "status" in ephemeral_msg


def test_websocket_missed_messages(
    test_map_fixture, sync_auth_client, websocket_url_for_map
):
    def create_response_queue():
        return [
            MockResponse("Hello! How can I help?", None),
            MockResponse("Hello again! How can I assist?", None),
        ]

    response_queue = create_response_queue()

    with patch("src.routes.message_routes.get_openai_client") as mock_get_client:
        mock_client = AsyncMock()

        async def mock_create(*args, **kwargs):
            return response_queue.pop(0)

        mock_client.chat.completions.create = AsyncMock(side_effect=mock_create)
        mock_get_client.return_value = mock_client

        map_id = test_map_fixture["map_id"]
        project_id = test_map_fixture["project_id"]

        # Create conversation
        response = sync_auth_client.post(
            "/api/conversations",
            json={"project_id": project_id},
        )
        assert response.status_code == 200
        conversation_id = response.json()["id"]

        with sync_auth_client.websocket_connect(
            websocket_url_for_map(map_id, conversation_id)
        ) as websocket:
            response = sync_auth_client.post(
                f"/api/maps/conversations/{conversation_id}/maps/{map_id}/send",
                json={
                    "message": {
                        "role": "user",
                        "content": "Hello",
                    },
                    "selected_feature": None,
                },
            )
            assert response.status_code == 200

            # Receive messages until we get the ephemeral action message
            ephemeral_msg = None
            max_attempts = 10
            for _ in range(max_attempts):
                recv_msg = recv_non_streaming(websocket)
                if recv_msg.get("ephemeral") is True:
                    ephemeral_msg = recv_msg
                    break

            assert ephemeral_msg is not None, "Did not receive ephemeral action message"
            assert "ephemeral" in ephemeral_msg
            assert ephemeral_msg["ephemeral"] is True
            assert "action_id" in ephemeral_msg
            assert "action" in ephemeral_msg
            assert "timestamp" in ephemeral_msg
            assert "status" in ephemeral_msg

        response2 = sync_auth_client.post(
            f"/api/maps/conversations/{conversation_id}/maps/{map_id}/send",
            json={
                "message": {
                    "role": "user",
                    "content": "Hello again",
                },
                "selected_feature": None,
            },
        )
        assert response2.status_code == 200

        time.sleep(1)

        with sync_auth_client.websocket_connect(
            websocket_url_for_map(map_id, conversation_id)
        ) as websocket2:
            # Receive messages until we get the ephemeral action message
            ephemeral_msg = None
            max_attempts = 10
            for _ in range(max_attempts):
                recv_msg = websocket2.receive_json()
                if recv_msg.get("ephemeral") is True:
                    ephemeral_msg = recv_msg
                    break

            assert ephemeral_msg is not None, "Did not receive ephemeral action message"
            assert "ephemeral" in ephemeral_msg
            assert ephemeral_msg["ephemeral"] is True
            assert "action_id" in ephemeral_msg
            assert "action" in ephemeral_msg
            assert "timestamp" in ephemeral_msg
            assert "status" in ephemeral_msg
