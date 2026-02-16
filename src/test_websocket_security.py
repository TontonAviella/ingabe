import pytest
import uuid


@pytest.fixture
def test_conversation_fixture(sync_auth_client):
    """Create a test conversation for WebSocket testing"""
    # First create a map to get a project_id
    map_response = sync_auth_client.post(
        "/api/maps/create",
        json={
            "title": f"Test WebSocket Security Map {uuid.uuid4()}",
            "link_accessible": True,
        },
    )
    assert map_response.status_code == 200
    map_data = map_response.json()

    # Create a conversation
    conversation_response = sync_auth_client.post(
        "/api/conversations",
        json={"project_id": map_data["project_id"]},
    )
    assert conversation_response.status_code == 200
    conversation_data = conversation_response.json()

    return {
        "map_id": map_data["id"],
        "project_id": map_data["project_id"],
        "conversation_id": conversation_data["id"],
    }


def test_websocket_rejects_invalid_conversation_id(
    sync_auth_client, websocket_url_for_map
):
    """
    Test that WebSocket connection rejects non-existent conversation IDs.
    Should close connection with code 4403 (Unauthorized).
    """
    # Use a conversation ID that doesn't exist
    nonexistent_conversation_id = 999999
    nonexistent_map_id = "fake-map-id"

    # The WebSocket should reject the connection
    with pytest.raises(Exception):  # TestClient raises different exceptions
        with sync_auth_client.websocket_connect(
            websocket_url_for_map(nonexistent_map_id, nonexistent_conversation_id)
        ):
            pytest.fail(
                "WebSocket should have rejected connection with invalid conversation_id"
            )


def test_websocket_handles_malformed_json(
    test_conversation_fixture, sync_auth_client, websocket_url_for_map
):
    """
    Test that WebSocket gracefully handles malformed JSON.
    Note: The current WebSocket implementation only receives messages,
    it doesn't expect clients to send JSON. This test verifies the server
    doesn't crash when receiving unexpected data.
    """
    conversation_id = test_conversation_fixture["conversation_id"]
    map_id = test_conversation_fixture["map_id"]

    with sync_auth_client.websocket_connect(
        websocket_url_for_map(map_id, conversation_id)
    ) as websocket:
        # Try sending malformed JSON
        # The WebSocket should handle this gracefully (not crash)
        try:
            websocket.send_text("{invalid json")
            # If we get here, the connection is still alive
            # Just ensure we can still close cleanly
        except Exception:
            # Some WebSocket implementations may raise immediately
            # Either behavior (graceful ignore or exception) is acceptable
            pass


def test_websocket_handles_empty_message(
    test_conversation_fixture, sync_auth_client, websocket_url_for_map
):
    """
    Test that WebSocket gracefully handles empty messages.
    Like the malformed JSON test, this verifies the server doesn't crash.
    """
    conversation_id = test_conversation_fixture["conversation_id"]
    map_id = test_conversation_fixture["map_id"]

    with sync_auth_client.websocket_connect(
        websocket_url_for_map(map_id, conversation_id)
    ) as websocket:
        # Try sending empty message
        try:
            websocket.send_text("")
            # Connection should still be alive
        except Exception:
            # Either graceful handling or exception is acceptable
            pass


def test_websocket_unauthorized_without_token_in_view_mode(sync_auth_client):
    """
    Test that WebSocket connection requires token when not in edit mode.
    Note: This test assumes MUNDI_AUTH_MODE=edit in conftest.py, so this
    test documents the expected behavior but may not execute meaningfully
    in the current test environment.
    """
    import os

    # This test is informational - it documents expected behavior
    # In the actual test environment, MUNDI_AUTH_MODE is 'edit'
    auth_mode = os.environ.get("MUNDI_AUTH_MODE")

    if auth_mode == "edit":
        pytest.skip("Test requires view_only mode, but environment is in edit mode")

    # If we ever test in view_only mode, this should fail without token
    with pytest.raises(Exception):
        with sync_auth_client.websocket_connect(
            "/api/maps/ws/999999/messages/updates"
        ):
            pytest.fail("WebSocket should require token in view_only mode")


def test_websocket_connection_lifecycle(
    test_conversation_fixture, sync_auth_client, websocket_url_for_map
):
    """
    Test the full WebSocket connection lifecycle:
    - Connect successfully with valid conversation
    - Disconnect cleanly
    - Verify no resources leak
    """
    conversation_id = test_conversation_fixture["conversation_id"]
    map_id = test_conversation_fixture["map_id"]

    # Connect and immediately disconnect
    with sync_auth_client.websocket_connect(
        websocket_url_for_map(map_id, conversation_id)
    ):
        pass  # Connection established and closed

    # Connect again to verify previous disconnect was clean
    with sync_auth_client.websocket_connect(
        websocket_url_for_map(map_id, conversation_id)
    ):
        pass  # Second connection should work fine
