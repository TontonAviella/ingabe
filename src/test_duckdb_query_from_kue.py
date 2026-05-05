import pytest
import json
import os
import random
from pathlib import Path
from unittest.mock import patch, AsyncMock
from src.structures import get_async_db_connection
from openai.types.chat import (
    ChatCompletionMessageToolCall,
)
from openai.types.chat.chat_completion_message_tool_call import Function

from src._test_streaming_mock import MockResponse, recv_non_streaming


@pytest.fixture
def sync_test_map_with_vector_layers(sync_auth_client):
    map_response = sync_auth_client.post(
        "/api/maps/create",
        json={
            "title": "Geoprocessing Test Map",
        },
    )
    assert map_response.status_code == 200, f"Failed to create map: {map_response.text}"
    data = map_response.json()
    map_id = data["id"]
    project_id = data["project_id"]
    layer_ids = {}

    def _upload_layer(file_name, layer_name_in_db):
        file_path = str(Path(__file__).parent.parent / "test_fixtures" / file_name)
        assert os.path.exists(file_path)
        with open(file_path, "rb") as f:
            layer_response = sync_auth_client.post(
                f"/api/maps/{map_id}/layers",
                files={"file": (file_name, f, "application/octet-stream")},
                data={"layer_name": layer_name_in_db},
            )
            assert layer_response.status_code == 200, (
                f"Failed to upload layer {file_name}: {layer_response.text}"
            )
            return layer_response.json()["id"]

    random.seed(42)
    layer_ids["cafes_layer_id"] = _upload_layer(
        "barcelona_cafes.fgb", "Barcelona Cafes"
    )
    return {"map_id": map_id, "project_id": project_id, **layer_ids}


@pytest.mark.anyio
@pytest.mark.timeout(120)
async def test_chat_completions(
    sync_test_map_with_vector_layers,
    auth_client,
    sync_auth_client,
    websocket_url_for_map,
):
    layer_id = sync_test_map_with_vector_layers["cafes_layer_id"]
    map_id = sync_test_map_with_vector_layers["map_id"]
    project_id = sync_test_map_with_vector_layers["project_id"]

    def create_response_queue():
        return [
            MockResponse(
                "I'll help you count the cafes where name=Starbucks using a SQL query.",
                [
                    ChatCompletionMessageToolCall(
                        id="call_1",
                        type="function",
                        function=Function(
                            name="query_duckdb_sql",
                            arguments=json.dumps(
                                {
                                    "layer_ids": [layer_id],
                                    "sql_query": f"SELECT COUNT(*) as count FROM {layer_id} WHERE name = 'Starbucks'",
                                    "head_n_rows": 20,
                                }
                            ),
                        ),
                    )
                ],
            ),
            MockResponse(
                "Based on the query results, there are 18 cafes where name=Starbucks.",
                None,
            ),
        ]

    response_queue = create_response_queue()

    with patch("src.routes.message_routes.get_openai_client") as mock_get_client:
        mock_client = AsyncMock()

        async def mock_create(*args, **kwargs):
            response = response_queue.pop(0)
            return response

        mock_client.chat.completions.create = AsyncMock(side_effect=mock_create)
        mock_get_client.return_value = mock_client

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
                        "content": "Count how many cafes have name=Starbucks",
                    },
                    "selected_feature": None,
                },
            )
            assert response.status_code == 200
            data = response.json()
            assert data["status"] == "processing_started"
            assert "message_id" in data

            # Message 1: User message
            msg1 = recv_non_streaming(websocket)
            assert msg1["role"] == "user"
            assert msg1["content"] == "Count how many cafes have name=Starbucks"

            # Message 2: Sage is thinking (start)
            msg2 = recv_non_streaming(websocket)
            assert msg2["ephemeral"]
            assert msg2["action"] == "Sage is thinking..."
            assert msg2["status"] == "active"

            # Message 3: Sage is thinking (completed)
            msg3 = recv_non_streaming(websocket)
            assert msg3["ephemeral"]
            assert msg3["action"] == "Sage is thinking..."
            assert msg3["status"] == "completed"

            # Message 4: Assistant message with tool call
            msg4 = recv_non_streaming(websocket)
            assert msg4["role"] == "assistant"
            assert (
                msg4["content"]
                == "I'll help you count the cafes where name=Starbucks using a SQL query."
            )
            assert msg4["has_tool_calls"]
            assert len(msg4["tool_calls"]) == 1
            assert msg4["tool_calls"][0]["id"] == "call_1"

            # Message 5: Querying with SQL (start)
            msg5 = recv_non_streaming(websocket)
            assert msg5["ephemeral"]
            assert msg5["action"] == "Querying with SQL..."
            assert msg5["status"] == "active"

            # Message 6: Querying with SQL (completed)
            msg6 = recv_non_streaming(websocket)
            assert msg6["ephemeral"]
            assert msg6["action"] == "Querying with SQL..."
            assert msg6["status"] == "completed"

            # Tool response message after SQL query completes
            msg6_tool = recv_non_streaming(websocket)
            assert msg6_tool["role"] == "tool"
            assert msg6_tool["tool_response"]["id"] == "call_1"

            assert msg6_tool["tool_response"]["status"] == "success", (
                f"DuckDB query failed with status={msg6_tool['tool_response']['status']}"
            )

            async with get_async_db_connection() as conn:
                messages = await conn.fetch(
                    "SELECT id, sender_id, message_json, created_at FROM chat_completion_messages WHERE conversation_id = $1 ORDER BY created_at",
                    conversation_id,
                )

                tool_response = None
                for m in messages:
                    msg_json = json.loads(dict(m)["message_json"])
                    if (
                        msg_json.get("role") == "tool"
                        and msg_json.get("tool_call_id") == "call_1"
                    ):
                        tool_response = json.loads(msg_json["content"])
                        break

                assert tool_response is not None

                assert tool_response["status"] == "success"
                assert "result" in tool_response
                assert "row_count" in tool_response
                assert "query" in tool_response

                result_lines = (
                    tool_response["result"].replace("\r", "").strip().split("\n")
                )
                assert len(result_lines) == 2
                assert result_lines[0] == "count"
                assert result_lines[1] == "18"
                assert tool_response["row_count"] == 1

            # Message 7: Final thinking (start)
            msg7 = recv_non_streaming(websocket)
            assert msg7["ephemeral"]
            assert msg7["action"] == "Sage is thinking..."
            assert msg7["status"] == "active"

            # Message 8: Final thinking (completed)
            msg8 = recv_non_streaming(websocket)
            assert msg8["ephemeral"]
            assert msg8["action"] == "Sage is thinking..."
            assert msg8["status"] == "completed"

            # Message 9: Final assistant response
            msg9 = recv_non_streaming(websocket)
            assert msg9["role"] == "assistant"
            assert "18 cafes where name=Starbucks" in msg9["content"]


@pytest.mark.anyio
@pytest.mark.timeout(120)
async def test_chat_completions_with_error(
    sync_test_map_with_vector_layers,
    auth_client,
    sync_auth_client,
    websocket_url_for_map,
):
    layer_id = sync_test_map_with_vector_layers["cafes_layer_id"]
    map_id = sync_test_map_with_vector_layers["map_id"]
    project_id = sync_test_map_with_vector_layers["project_id"]

    def create_response_queue():
        return [
            MockResponse(
                "I'll run a SQL query that will fail due to invalid syntax.",
                [
                    ChatCompletionMessageToolCall(
                        id="call_1",
                        type="function",
                        function=Function(
                            name="query_duckdb_sql",
                            arguments=json.dumps(
                                {
                                    "layer_ids": [layer_id],
                                    "sql_query": "SELECT INVALID_SYNTAX FROM NONEXISTENT_TABLE WHERE this_will_fail = 'error'",
                                    "head_n_rows": 20,
                                }
                            ),
                        ),
                    )
                ],
            ),
            MockResponse(
                "I apologize, but there was an error with the SQL query.",
                None,
            ),
        ]

    response_queue = create_response_queue()

    with patch("src.routes.message_routes.get_openai_client") as mock_get_client:
        mock_client = AsyncMock()

        async def mock_create(*args, **kwargs):
            response = response_queue.pop(0)
            return response

        mock_client.chat.completions.create = AsyncMock(side_effect=mock_create)
        mock_get_client.return_value = mock_client

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
                        "content": "Run an invalid SQL query that will fail",
                    },
                    "selected_feature": None,
                },
            )
            assert response.status_code == 200
            data = response.json()
            assert data["status"] == "processing_started"
            assert "message_id" in data

            # Message 1: User message
            msg1 = recv_non_streaming(websocket)
            assert msg1["role"] == "user"
            assert msg1["content"] == "Run an invalid SQL query that will fail"

            # Message 2: Sage is thinking (start)
            msg2 = recv_non_streaming(websocket)
            assert msg2["ephemeral"]
            assert msg2["action"] == "Sage is thinking..."
            assert msg2["status"] == "active"

            # Message 3: Sage is thinking (completed)
            msg3 = recv_non_streaming(websocket)
            assert msg3["ephemeral"]
            assert msg3["action"] == "Sage is thinking..."
            assert msg3["status"] == "completed"

            # Message 4: Assistant message with tool call
            msg4 = recv_non_streaming(websocket)
            assert msg4["role"] == "assistant"
            assert (
                msg4["content"]
                == "I'll run a SQL query that will fail due to invalid syntax."
            )
            assert msg4["has_tool_calls"]
            assert len(msg4["tool_calls"]) == 1
            assert msg4["tool_calls"][0]["id"] == "call_1"

            # Message 5: Querying with SQL (start)
            msg5 = recv_non_streaming(websocket)
            assert msg5["ephemeral"]
            assert msg5["action"] == "Querying with SQL..."
            assert msg5["status"] == "active"

            # Message 6: Querying with SQL (completed)
            msg6 = recv_non_streaming(websocket)
            assert msg6["ephemeral"]
            assert msg6["action"] == "Querying with SQL..."
            assert msg6["status"] == "completed"

            # Tool response message after SQL query fails
            msg6_tool = recv_non_streaming(websocket)
            assert msg6_tool["role"] == "tool"
            assert msg6_tool["tool_response"]["id"] == "call_1"
            assert msg6_tool["tool_response"]["status"] == "error"

            async with get_async_db_connection() as conn:
                messages = await conn.fetch(
                    "SELECT id, sender_id, message_json, created_at FROM chat_completion_messages WHERE conversation_id = $1 ORDER BY created_at",
                    conversation_id,
                )

                tool_response = None
                for m in messages:
                    msg_json = json.loads(dict(m)["message_json"])
                    if (
                        msg_json.get("role") == "tool"
                        and msg_json.get("tool_call_id") == "call_1"
                    ):
                        tool_response = json.loads(msg_json["content"])
                        break

                assert tool_response is not None
                assert tool_response["status"] == "error"
                assert "error" in tool_response

            # Message 7: Final thinking (start)
            msg7 = recv_non_streaming(websocket)
            assert msg7["ephemeral"]
            assert msg7["action"] == "Sage is thinking..."
            assert msg7["status"] == "active"

            # Message 8: Final thinking (completed)
            msg8 = recv_non_streaming(websocket)
            assert msg8["ephemeral"]
            assert msg8["action"] == "Sage is thinking..."
            assert msg8["status"] == "completed"

            # Message 9: Final assistant response
            msg9 = recv_non_streaming(websocket)
            assert msg9["role"] == "assistant"
            assert "error" in msg9["content"].lower()
