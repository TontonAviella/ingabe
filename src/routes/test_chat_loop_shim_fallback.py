"""The chat loop runs a tool with no inline branch through the legacy shim.

brain_trajectory is offered to the model but has no branch in the chat
loop; before the fallback, calling it raised a 400 and ended the turn.
"""

import json
import uuid
from unittest.mock import AsyncMock, patch

import pytest
from openai.types.chat import ChatCompletionMessageToolCall
from openai.types.chat.chat_completion_message_tool_call import Function

from src._test_streaming_mock import MockResponse, recv_non_streaming


@pytest.fixture
def shim_map(sync_auth_client):
    response = sync_auth_client.post(
        "/api/maps/create",
        json={"title": f"Shim fallback {uuid.uuid4()}", "link_accessible": True},
    )
    assert response.status_code == 200
    data = response.json()
    return {"map_id": data["id"], "project_id": data["project_id"]}


@pytest.mark.anyio
@pytest.mark.timeout(120)
async def test_tool_without_inline_branch_reaches_its_shim_handler(
    shim_map, sync_auth_client, websocket_url_for_map
):
    responses = [
        MockResponse(
            "Let me look at that field's history.",
            [
                ChatCompletionMessageToolCall(
                    id="call_1",
                    type="function",
                    # Empty slug/key: the handler answers before touching the Brain.
                    function=Function(name="brain_trajectory", arguments=json.dumps({"slug": "", "key": ""})),
                )
            ],
        ),
        MockResponse("I need the field's name to look up its history.", None),
    ]
    calls: list[dict] = []

    async def mock_create(*args, **kwargs):
        calls.append(kwargs)
        return responses.pop(0)

    with patch("src.routes.message_routes.get_openai_client") as mock_get_client:
        mock_client = AsyncMock()
        mock_client.chat.completions.create = AsyncMock(side_effect=mock_create)
        mock_get_client.return_value = mock_client

        conversation = sync_auth_client.post("/api/conversations", json={"project_id": shim_map["project_id"]})
        assert conversation.status_code == 200
        conversation_id = conversation.json()["id"]

        with sync_auth_client.websocket_connect(
            websocket_url_for_map(shim_map["map_id"], conversation_id)
        ) as websocket:
            sent = sync_auth_client.post(
                f"/api/maps/conversations/{conversation_id}/maps/{shim_map['map_id']}/send",
                json={
                    "message": {"role": "user", "content": "how has the NDVI of my field changed?"},
                    "selected_feature": None,
                },
            )
            assert sent.status_code == 200

            tool_message = None
            for _ in range(12):
                msg = recv_non_streaming(websocket)
                if msg.get("role") == "tool":
                    tool_message = msg
                    break
            assert tool_message is not None
            assert tool_message["tool_response"] == {"id": "call_1", "status": "error"}

    # The model saw the handler's own message, not a failed turn.
    assert len(calls) >= 2
    tool_contents = [m["content"] for m in calls[1]["messages"] if m.get("role") == "tool"]
    assert any("slug and key are both required" in c for c in tool_contents)
