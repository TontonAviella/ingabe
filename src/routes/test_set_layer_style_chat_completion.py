import pytest
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
from openai.types.chat import (
    ChatCompletionMessageToolCall,
)
from openai.types.chat.chat_completion_message_tool_call import Function


# message_routes.py:1457 calls client.chat.completions.create(..., stream=True)
# and then `async for chunk in stream:`. Each chunk must have:
#   chunk.choices[0].delta.content (str | None)
#   chunk.choices[0].delta.tool_calls (list of {index, id, function:{name, arguments}} | None)
# We model each chunk with SimpleNamespace because the streaming code only
# accesses attributes (no isinstance checks against openai's chunk types).


class MockStream:
    """Async iterator over pre-built chunks. Mirrors the streaming shape of
    OpenAI's create(stream=True) so the production code at message_routes.py
    line 1460 can accumulate content + tool_calls deltas like it does in prod.
    """

    def __init__(self, chunks):
        self._chunks = list(chunks)
        self._idx = 0

    def __aiter__(self):
        return self

    async def __anext__(self):
        if self._idx >= len(self._chunks):
            raise StopAsyncIteration
        chunk = self._chunks[self._idx]
        self._idx += 1
        return chunk


def _make_stream(content: str, tool_calls=None):
    """Build a MockStream representing a single completion. Emits one chunk
    per tool call (with full id+name+arguments — the streaming accumulator
    handles partial deltas, but full-payload chunks are valid too) followed
    by one chunk per content snippet, then end."""
    chunks = []
    if tool_calls:
        for i, tc in enumerate(tool_calls):
            tc_delta = SimpleNamespace(
                index=i,
                id=tc.id,
                function=SimpleNamespace(
                    name=tc.function.name,
                    arguments=tc.function.arguments,
                ),
            )
            chunks.append(SimpleNamespace(
                choices=[SimpleNamespace(
                    delta=SimpleNamespace(content=None, tool_calls=[tc_delta]),
                )],
            ))
    if content:
        chunks.append(SimpleNamespace(
            choices=[SimpleNamespace(
                delta=SimpleNamespace(content=content, tool_calls=None),
            )],
        ))
    # Final empty-delta chunk to flush, mirroring real OpenAI streams
    chunks.append(SimpleNamespace(
        choices=[SimpleNamespace(
            delta=SimpleNamespace(content=None, tool_calls=None),
        )],
    ))
    return MockStream(chunks)


class MockResponse:
    """Backwards-compatible shim for tests that build "responses" the old way.
    Returns an async stream when iterated by message_routes.py."""

    def __init__(self, content: str, tool_calls=None):
        self._content = content
        self._tool_calls = tool_calls

    def __aiter__(self):
        return _make_stream(self._content, self._tool_calls).__aiter__()


@pytest.fixture
def test_map_with_layer_and_conversation(sync_auth_client):
    map_response = sync_auth_client.post(
        "/api/maps/create",
        json={
            "title": "Chat Completion Style Test Map",
        },
    )
    assert map_response.status_code == 200
    map_id = map_response.json()["id"]

    file_path = str(
        Path(__file__).parent.parent.parent / "test_fixtures" / "coho_range.gpkg"
    )
    with open(file_path, "rb") as f:
        layer_response = sync_auth_client.post(
            f"/api/maps/{map_id}/layers",
            files={"file": ("coho_range.gpkg", f, "application/octet-stream")},
            data={"layer_name": "Coho Salmon Range"},
        )
        assert layer_response.status_code == 200
        layer_data = layer_response.json()
        layer_id = layer_data["id"]
        child_map_id = layer_data["dag_child_map_id"]

    map_detail_response = sync_auth_client.get(f"/api/maps/{child_map_id}")
    assert map_detail_response.status_code == 200
    project_id = map_detail_response.json()["project_id"]

    conversation_response = sync_auth_client.post(
        "/api/conversations", json={"project_id": project_id}
    )
    assert conversation_response.status_code == 200
    conversation_id = conversation_response.json()["id"]

    return {
        "map_id": map_id,
        "child_map_id": child_map_id,
        "layer_id": layer_id,
        "conversation_id": conversation_id,
    }


@pytest.mark.anyio
@pytest.mark.timeout(120)
async def test_set_layer_style_via_chat_completion(
    auth_client,
    test_map_with_layer_and_conversation,
    sync_auth_client,
    websocket_url_for_map,
):
    test_data = test_map_with_layer_and_conversation
    layer_id = test_data["layer_id"]
    child_map_id = test_data["child_map_id"]
    conversation_id = test_data["conversation_id"]

    test_fill_color = "#FF5733"
    test_stroke_color = "#2E86AB"
    test_fill_opacity = 0.69

    maplibre_layers = [
        {
            "id": f"{layer_id}-fill",
            "type": "fill",
            "source": layer_id,
            "paint": {
                "fill-color": test_fill_color,
                "fill-opacity": test_fill_opacity,
                "fill-outline-color": test_stroke_color,
            },
            "metadata": {"foo": "bar"},
        }
    ]

    response_queue = [
        MockResponse(
            "I'll apply a custom style to your layer with the specified colors.",
            [
                ChatCompletionMessageToolCall(
                    id="call_test123",
                    type="function",
                    function=Function(
                        name="set_layer_style",
                        arguments=json.dumps(
                            {
                                "layer_id": layer_id,
                                "maplibre_json_layers_str": json.dumps(maplibre_layers),
                            }
                        ),
                    ),
                )
            ],
        ),
        MockResponse(
            "I've applied a custom style to your layer with the specified colors.",
            None,
        ),
    ]

    with patch("src.routes.message_routes.get_openai_client") as mock_get_client:
        mock_client = AsyncMock()

        async def mock_create(*args, **kwargs):
            return response_queue.pop(0)

        mock_client.chat.completions.create = AsyncMock(side_effect=mock_create)
        mock_get_client.return_value = mock_client

        message_payload = {
            "message": {
                "role": "user",
                "content": f"Please style layer {layer_id} with custom colors",
            },
            "selected_feature": None,
        }

        # message_routes.py now streams content tokens via kue_stream_token,
        # which publishes WebSocket events with streaming=True between the
        # discrete state messages this test cares about. Drop them.
        def next_state_msg(ws):
            while True:
                m = ws.receive_json()
                if m.get("streaming"):
                    continue
                return m

        with sync_auth_client.websocket_connect(
            websocket_url_for_map(child_map_id, conversation_id)
        ) as websocket:
            response = sync_auth_client.post(
                f"/api/maps/conversations/{conversation_id}/maps/{child_map_id}/send",
                json=message_payload,
            )

            assert response.status_code == 200
            assert response.json()["status"] == "processing_started"

            msg1 = next_state_msg(websocket)
            assert msg1["role"] == "user"
            assert "style layer" in msg1["content"]
            assert msg1["has_tool_calls"] is False

            msg2 = next_state_msg(websocket)
            assert msg2["ephemeral"] is True
            assert msg2["action"] == "Sage is thinking..."
            assert msg2["status"] == "active"
            assert msg2["updates"]["style_json"] is False

            msg3 = next_state_msg(websocket)
            assert msg3["ephemeral"] is True
            assert msg3["action"] == "Sage is thinking..."
            assert msg3["status"] == "completed"
            assert msg3["updates"]["style_json"] is False

            msg4 = next_state_msg(websocket)
            assert msg4["role"] == "assistant"
            assert "apply a custom style" in msg4["content"]
            assert msg4["has_tool_calls"] is True
            assert len(msg4["tool_calls"]) == 1
            assert msg4["tool_calls"][0]["tagline"] == "Setting layer style..."
            assert msg4["tool_calls"][0]["icon"] == "brush"

            msg5 = next_state_msg(websocket)
            assert msg5["ephemeral"] is True
            assert "Styling layer" in msg5["action"]
            assert msg5["status"] == "active"
            assert msg5["updates"]["style_json"] is True

            msg6 = next_state_msg(websocket)
            assert msg6["ephemeral"] is True
            assert "Styling layer" in msg6["action"]
            assert msg6["status"] == "completed"
            assert msg6["updates"]["style_json"] is True

            # Tool response message after styling completes
            msg6_tool = next_state_msg(websocket)
            assert msg6_tool["role"] == "tool"
            assert msg6_tool["tool_response"]["status"] == "success"

            msg7 = next_state_msg(websocket)
            assert msg7["ephemeral"] is True
            assert msg7["action"] == "Sage is thinking..."
            assert msg7["status"] == "active"
            assert msg7["updates"]["style_json"] is False

            msg8 = next_state_msg(websocket)
            assert msg8["ephemeral"] is True
            assert msg8["action"] == "Sage is thinking..."
            assert msg8["status"] == "completed"
            assert msg8["updates"]["style_json"] is False

            msg9 = next_state_msg(websocket)
            assert msg9["role"] == "assistant"
            assert "applied a custom style" in msg9["content"]
            assert msg9["has_tool_calls"] is False

    style_response = sync_auth_client.get(f"/api/maps/{child_map_id}/style.json")
    assert style_response.status_code == 200

    style_json = style_response.json()

    matching_layers = []
    fill_layers = []
    line_layers = []

    for layer in style_json.get("layers", []):
        if layer.get("source") == layer_id:
            matching_layers.append(layer)

            if layer.get("type") == "fill":
                fill_layers.append(layer)
                actual_color = layer.get("paint", {}).get("fill-color")

                assert actual_color == test_fill_color, (
                    f"Expected {test_fill_color}, got {actual_color}"
                )
                assert layer.get("metadata", {}).get("foo") == "bar"

            elif layer.get("type") == "line":
                line_layers.append(layer)

    assert len(fill_layers) == 1, (
        f"Expected at least 1 fill layer with source {layer_id}, found {len(fill_layers)}"
    )
    assert len(line_layers) == 0, (
        f"Expected 0 line layer with source {layer_id}, found {len(line_layers)}"
    )
