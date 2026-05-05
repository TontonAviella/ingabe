import pytest
import os
import aiohttp
from unittest.mock import patch
from src.structures import async_conn
from openai.types.chat import (
    ChatCompletionMessageToolCall,
)
from openai.types.chat.chat_completion_message_tool_call import Function
import json
from unittest.mock import AsyncMock

from src._test_streaming_mock import MockResponse as MockOSMResponse
from src._test_streaming_mock import recv_non_streaming


@pytest.mark.anyio
@pytest.mark.timeout(120)
async def test_download_from_openstreetmap_layers_created(
    sync_auth_client, websocket_url_for_map
):
    osm_path = "/app/test_fixtures/osm_lifeguard.geojson"
    with open(osm_path, "rb") as f:
        osm_data = f.read()

    class MockResponse:
        def __init__(self):
            self.status = 200

        async def read(self):
            return osm_data

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc_val, exc_tb):
            pass

    def mock_get(self, url, **kwargs):
        return MockResponse()

    payload = {
        "title": "OSM Layer Test Map",
        "description": "Test map for OpenStreetMap layer creation",
    }
    response = sync_auth_client.post("/api/maps/create", json=payload)
    assert response.status_code == 200
    map_data = response.json()
    test_map_id = map_data["id"]
    project_id = map_data["project_id"]

    async with async_conn("test_check_osm_layers") as conn:
        layers = await conn.fetch(
            "SELECT layer_id, name, type, metadata FROM map_layers WHERE source_map_id = $1 AND name LIKE 'lifeguard_%' ORDER BY created_on DESC LIMIT 2",
            test_map_id,
        )
        assert len(layers) == 0

    conversation_response = sync_auth_client.post(
        "/api/conversations",
        json={"project_id": project_id},
    )
    assert conversation_response.status_code == 200
    conversation_id = conversation_response.json()["id"]

    def create_response_queue():
        return [
            MockOSMResponse(
                "I'll download OpenStreetMap data for lifeguards.",
                [
                    ChatCompletionMessageToolCall(
                        id="call_1",
                        type="function",
                        function=Function(
                            name="download_from_openstreetmap",
                            arguments=json.dumps(
                                {
                                    "bbox": [-180, -90, 180, 90],
                                    "tags": "emergency=lifeguard",
                                    "new_layer_name": "lifeguard",
                                }
                            ),
                        ),
                    )
                ],
            ),
            MockOSMResponse(
                "Ok downloaded",
                None,
            ),
        ]

    response_queue = create_response_queue()

    with patch.object(aiohttp.ClientSession, "get", mock_get):
        with patch.dict(os.environ, {"INGABE_OSM_API_KEY": "test_api_key"}):
            with patch(
                "src.routes.message_routes.get_openai_client"
            ) as mock_get_client:
                mock_client = AsyncMock()

                async def mock_create(*args, **kwargs):
                    response = response_queue.pop(0)
                    return response

                mock_client.chat.completions.create = AsyncMock(side_effect=mock_create)
                mock_get_client.return_value = mock_client

                with sync_auth_client.websocket_connect(
                    websocket_url_for_map(test_map_id, conversation_id)
                ) as websocket:
                    response = sync_auth_client.post(
                        f"/api/maps/conversations/{conversation_id}/maps/{test_map_id}/send",
                        json={
                            "message": {
                                "role": "user",
                                "content": "Download OpenStreetMap data for emergency=lifeguard",
                            },
                            "selected_feature": None,
                        },
                    )
                    assert response.status_code == 200

                    msg1 = recv_non_streaming(websocket)
                    assert msg1["role"] == "user"
                    assert (
                        msg1["content"]
                        == "Download OpenStreetMap data for emergency=lifeguard"
                    )
                    assert not msg1["has_tool_calls"]
                    assert msg1["tool_calls"] == []

                    msg2 = recv_non_streaming(websocket)
                    assert msg2["ephemeral"]
                    assert msg2["action"] == "Sage is thinking..."
                    assert msg2["status"] == "active"

                    msg3 = recv_non_streaming(websocket)
                    assert msg3["ephemeral"]
                    assert msg3["action"] == "Sage is thinking..."
                    assert msg3["status"] == "completed"

                    msg4 = recv_non_streaming(websocket)
                    assert msg4["role"] == "assistant"
                    assert (
                        msg4["content"]
                        == "I'll download OpenStreetMap data for lifeguards."
                    )
                    assert msg4["has_tool_calls"]
                    assert len(msg4["tool_calls"]) == 1
                    assert msg4["tool_calls"][0]["id"] == "call_1"
                    assert (
                        msg4["tool_calls"][0]["tagline"]
                        == "Downloading from OpenStreetMap..."
                    )
                    assert msg4["tool_calls"][0]["icon"] == "cloud-download"

                    msg5 = recv_non_streaming(websocket)
                    assert msg5["ephemeral"]
                    assert (
                        "Downloading data from OpenStreetMap: emergency=lifeguard"
                        in msg5["action"]
                    )
                    assert msg5["status"] == "active"

                    msg6 = recv_non_streaming(websocket)
                    assert msg6["ephemeral"]
                    assert (
                        "Downloading data from OpenStreetMap: emergency=lifeguard"
                        in msg6["action"]
                    )
                    assert msg6["status"] == "completed"

                    msg7 = recv_non_streaming(websocket)
                    assert msg7["role"] == "tool"
                    assert msg7["tool_response"]["id"] == "call_1"
                    assert msg7["tool_response"]["status"] == "success"

                    msg8 = recv_non_streaming(websocket)
                    assert msg8["ephemeral"]
                    assert msg8["action"] == "Sage is thinking..."
                    assert msg8["status"] == "active"

                    msg9 = recv_non_streaming(websocket)
                    assert msg9["ephemeral"]
                    assert msg9["action"] == "Sage is thinking..."
                    assert msg9["status"] == "completed"

                    msg10 = recv_non_streaming(websocket)
                    assert msg10["role"] == "assistant"
                    assert "Ok downloaded" in msg10["content"]

    async with async_conn("test_check_osm_layers") as conn:
        layers = await conn.fetch(
            "SELECT layer_id, name, type, metadata FROM map_layers WHERE source_map_id = $1 AND name LIKE 'lifeguard_%' ORDER BY created_on DESC LIMIT 2",
            test_map_id,
        )
        assert len(layers) >= 2
        layer_names = [layer["name"] for layer in layers[:2]]
        assert "lifeguard_points" in layer_names
        assert "lifeguard_polygons" in layer_names
        for layer in layers[:2]:
            assert layer["type"] == "vector"
            metadata = json.loads(layer["metadata"])
            if layer["name"] == "lifeguard_points":
                assert metadata["feature_count"] == 1
            elif layer["name"] == "lifeguard_polygons":
                assert metadata["feature_count"] == 6
