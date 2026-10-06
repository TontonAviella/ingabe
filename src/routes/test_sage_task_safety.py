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


@pytest.mark.asyncio
async def test_a_tool_that_never_returns_is_stopped_and_the_model_told(monkeypatch):
    monkeypatch.setattr(message_routes, "_tool_timeout_seconds", lambda: 0.05)

    async def stalled() -> dict:
        await asyncio.sleep(60)
        return {"status": "success"}

    result = await message_routes._within_tool_limit("get_insurance_intelligence", stalled())
    assert result["status"] == "error"
    assert "did not finish within 0 seconds" in result["error"] and "which part is missing" in result["error"]


@pytest.mark.asyncio
async def test_a_tool_within_the_limit_returns_its_own_result():
    async def quick() -> dict:
        return {"status": "success", "value": 1}

    assert await message_routes._within_tool_limit("x", quick()) == {"status": "success", "value": 1}


def _call(call_id: str) -> dict:
    return {"id": call_id, "type": "function", "function": {"name": "get_ndvi_stats", "arguments": "{}"}}


def test_a_turn_cut_off_mid_tool_does_not_break_the_conversation():
    """A restart killed a turn after 2 of its 3 tool results: every later message got HTTP 400."""
    history = [
        {"role": "user", "content": "How is cassava doing?"},
        {"role": "assistant", "content": "", "tool_calls": [_call("a"), _call("b"), _call("c")]},
        {"role": "tool", "tool_call_id": "a", "content": "{}"},
        {"role": "tool", "tool_call_id": "b", "content": "{}"},
        {"role": "system", "content": "<MapState />"},
        {"role": "user", "content": "And now?"},
    ]
    paired = message_routes._pair_tool_results(history)
    assert [m.get("tool_call_id") for m in paired[2:5]] == ["a", "b", "c"]
    assert "did not finish" in paired[4]["content"]
    assert paired[5]["role"] == "system" and paired[-1]["content"] == "And now?"


def test_a_result_without_its_call_is_dropped_and_whole_turns_are_untouched():
    history = [
        {"role": "assistant", "content": "", "tool_calls": [_call("a")]},
        {"role": "tool", "tool_call_id": "a", "content": "{}"},
        {"role": "tool", "tool_call_id": "gone", "content": "{}"},
        {"role": "assistant", "content": "Cassava looks fine."},
    ]
    paired = message_routes._pair_tool_results(history)
    assert paired == [history[0], history[1], history[3]]
