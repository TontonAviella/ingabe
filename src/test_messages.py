import pytest
import uuid
from unittest.mock import patch, AsyncMock

from src._test_streaming_mock import MockResponse
from src.models.messages import _parse_tool_args


class TestParseToolArgs:
    def test_well_formed(self):
        assert _parse_tool_args('{"a": 1, "b": "x"}') == {"a": 1, "b": "x"}

    def test_empty_object(self):
        assert _parse_tool_args("{}") == {}

    def test_trailing_garbage_after_object(self):
        # gemma4:31b sometimes appends a stray token after the closing brace.
        assert _parse_tool_args('{"a": 1}garbage') == {"a": 1}

    def test_two_concatenated_objects_keeps_first(self):
        # The crash that produced "Error connecting to LLM" in prod.
        assert _parse_tool_args('{"a": 1}{"b": 2}') == {"a": 1}

    def test_unparseable_returns_empty_dict(self):
        # Total failure must NOT raise — chat must survive.
        assert _parse_tool_args("not json at all") == {}

    def test_empty_string_returns_empty_dict(self):
        assert _parse_tool_args("") == {}

    def test_leading_whitespace_tolerated_by_fallback(self):
        assert _parse_tool_args('   {"a": 1}xx') == {"a": 1}

    def test_top_level_array_returns_empty_dict(self):
        # raw_decode would parse [1, 2] but it is not a dict; fall through to {}.
        assert _parse_tool_args("[1, 2]") == {}


@pytest.fixture
def test_map_fixture(sync_auth_client):
    # Create a map with a project embedded
    response = sync_auth_client.post(
        "/api/maps/create",
        json={
            "title": f"Test Message Map {uuid.uuid4()}",
            "link_accessible": True,
        },
    )
    assert response.status_code == 200
    data = response.json()
    map_id = data["id"]
    project_id = data["project_id"]

    return {"map_id": map_id, "project_id": project_id}


@pytest.mark.anyio
@pytest.mark.timeout(120)
async def test_send_and_get_messages(
    test_map_fixture, sync_auth_client, websocket_url_for_map
):
    map_id = test_map_fixture["map_id"]
    project_id = test_map_fixture["project_id"]

    def create_response_queue():
        return [
            MockResponse(
                "I'll help analyze your map.",
                None,
            ),
        ]

    response_queue = create_response_queue()

    with patch("src.routes.message_routes.get_openai_client") as mock_get_client:
        mock_client = AsyncMock()

        async def mock_create(*args, **kwargs):
            return response_queue.pop(0)

        mock_client.chat.completions.create = AsyncMock(side_effect=mock_create)
        mock_get_client.return_value = mock_client

        # Create a conversation
        conversation_response = sync_auth_client.post(
            "/api/conversations",
            json={"project_id": project_id},
        )
        assert conversation_response.status_code == 200
        conversation_id = conversation_response.json()["id"]

        response = sync_auth_client.get(
            f"/api/conversations/{conversation_id}/messages"
        )
        assert response.status_code == 200
        messages = response.json()
        assert isinstance(messages, list)
        assert len(messages) == 0

        with sync_auth_client.websocket_connect(
            websocket_url_for_map(map_id, conversation_id)
        ) as websocket:
            response = sync_auth_client.post(
                f"/api/maps/conversations/{conversation_id}/maps/{map_id}/send",
                json={
                    "message": {
                        "role": "user",
                        "content": "Hello, can you help me analyze this map?",
                    },
                    "selected_feature": None,
                },
            )
            assert response.status_code == 200
            assert response.json()["status"] == "processing_started"

            sent_msg = websocket.receive_json()
            assert sent_msg["role"] == "user"
            assert "analyze this map" in sent_msg["content"]
            assert not sent_msg["has_tool_calls"]
            assert sent_msg["conversation_id"] == conversation_id

            msg = websocket.receive_json()
            assert msg["ephemeral"] and msg["action"] == "Sage is thinking..."
            msg = websocket.receive_json()
            assert (
                msg["ephemeral"]
                and msg["action"] == "Sage is thinking..."
                and msg["status"] == "completed"
            )

            assistant_msg = websocket.receive_json()
            assert assistant_msg["role"] == "assistant"
            assert "analyze" in assistant_msg["content"]
            assert assistant_msg["conversation_id"] == conversation_id

        response = sync_auth_client.get(
            f"/api/conversations/{conversation_id}/messages"
        )
        assert response.status_code == 200
        messages = response.json()
        assert len(messages) == 2
        # Messages are returned in flat structure from conversation endpoint
        assert messages[0]["role"] == "user"
        assert messages[1]["role"] == "assistant"
