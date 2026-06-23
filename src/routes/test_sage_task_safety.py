from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from src.routes import message_routes


@pytest.mark.anyio
async def test_safe_chat_task_cancellation_clears_frontend_state(monkeypatch):
    captured: list[tuple[str, dict]] = []
    stream_events: list[dict] = []
    error_messages: list[str] = []
    deleted_keys: list[str] = []

    async def cancel_task(*args, **kwargs):
        raise asyncio.CancelledError()

    async def fake_stream_token(conversation_id, token, done=False, turn_id=None):
        stream_events.append(
            {
                "conversation_id": conversation_id,
                "token": token,
                "done": done,
                "turn_id": turn_id,
            }
        )

    async def fake_notify_error(conversation_id, error_message):
        error_messages.append(error_message)

    def fake_capture_for_session(event, session, properties):
        captured.append((event, properties))

    class FakeRedis:
        def delete(self, key):
            deleted_keys.append(key)

    monkeypatch.setattr(message_routes, "process_chat_interaction_task", cancel_task)
    monkeypatch.setattr(message_routes, "kue_stream_token", fake_stream_token)
    monkeypatch.setattr(message_routes, "kue_notify_error", fake_notify_error)
    monkeypatch.setattr(message_routes, "capture_for_session", fake_capture_for_session)
    monkeypatch.setattr(message_routes, "redis", FakeRedis())

    conversation = SimpleNamespace(id=123)

    with pytest.raises(asyncio.CancelledError):
        await message_routes.process_chat_interaction_task_safely(
            request=None,
            map_id="Mtest",
            session=object(),
            user_id="user-1",
            chat_args=None,
            map_state=None,
            conversation=conversation,
            system_prompt_provider=None,
            connection_manager=None,
            pydantic_tool_calls={},
            client_turn_id="turn_cancelled",
            user_message_id="42",
        )

    assert captured[0][0] == "backend_sage_message_failed"
    assert captured[0][1]["error_type"] == "CancelledError"
    assert captured[0][1]["client_turn_id"] == "turn_cancelled"
    assert stream_events == [
        {
            "conversation_id": 123,
            "token": "",
            "done": True,
            "turn_id": None,
        }
    ]
    assert error_messages == [
        "Sage stopped before finishing this request. Please try again.",
    ]
    assert deleted_keys == ["chat_lock:123"]
