import pytest


@pytest.mark.anyio
async def test_create_conversation(auth_client, test_map_with_vector_layers):
    """Test creating a new conversation"""
    project_id = test_map_with_vector_layers["project_id"]

    response = await auth_client.post(
        "/api/conversations", json={"project_id": project_id}
    )
    response.raise_for_status()
    conversation_data = response.json()

    assert "id" in conversation_data
    assert conversation_data["title"] == "title pending"
    assert conversation_data["project_id"] == project_id
    assert "owner_uuid" in conversation_data
    assert "created_at" in conversation_data
    assert "updated_at" in conversation_data
    assert isinstance(conversation_data["id"], int)
    assert conversation_data["id"] > 0


@pytest.mark.anyio
async def test_list_conversations(auth_client, test_map_with_vector_layers):
    """Test listing conversations"""
    project_id = test_map_with_vector_layers["project_id"]

    # Create a conversation first
    create_response = await auth_client.post(
        "/api/conversations", json={"project_id": project_id}
    )
    create_response.raise_for_status()

    # List conversations
    list_response = await auth_client.get(f"/api/conversations?project_id={project_id}")
    list_response.raise_for_status()
    conversations = list_response.json()

    assert isinstance(conversations, list)
    assert len(conversations) >= 1

    # Check that our created conversation is in the list
    conversation_ids = [conv["id"] for conv in conversations]
    created_conversation_id = create_response.json()["id"]
    assert created_conversation_id in conversation_ids


@pytest.mark.anyio
async def test_get_conversation_messages(auth_client, test_map_with_vector_layers):
    """Test getting messages from a conversation"""
    project_id = test_map_with_vector_layers["project_id"]

    # Create a conversation first
    create_response = await auth_client.post(
        "/api/conversations", json={"project_id": project_id}
    )
    create_response.raise_for_status()
    conversation_id = create_response.json()["id"]

    # Get messages (should be empty for new conversation)
    messages_response = await auth_client.get(
        f"/api/conversations/{conversation_id}/messages"
    )
    messages_response.raise_for_status()
    messages = messages_response.json()

    assert isinstance(messages, list)
    assert len(messages) == 0  # New conversation should have no messages


@pytest.mark.anyio
async def test_get_nonexistent_conversation(auth_client):
    """Test getting a conversation that doesn't exist"""
    response = await auth_client.get("/api/conversations/99999/messages")
    assert response.status_code == 404


@pytest.mark.anyio
async def test_multiple_conversations(auth_client, test_map_with_vector_layers):
    """Test creating multiple conversations"""
    project_id = test_map_with_vector_layers["project_id"]

    # Create two conversations
    response1 = await auth_client.post(
        "/api/conversations", json={"project_id": project_id}
    )
    response1.raise_for_status()

    response2 = await auth_client.post(
        "/api/conversations", json={"project_id": project_id}
    )
    response2.raise_for_status()

    # List conversations
    list_response = await auth_client.get(f"/api/conversations?project_id={project_id}")
    list_response.raise_for_status()
    conversations = list_response.json()

    assert len(conversations) >= 2

    # Verify both conversations exist
    conversation_ids = [conv["id"] for conv in conversations]
    assert response1.json()["id"] in conversation_ids
    assert response2.json()["id"] in conversation_ids
