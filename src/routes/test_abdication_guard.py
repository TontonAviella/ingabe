"""The live loop's abdication-guard retry (src/routes/message_routes.py)."""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from src.dependencies.pydantic_tools import get_pydantic_tool_calls
from src.dependencies.sage_turn_request import build_sage_tools_payload
from src.routes import message_routes


class _FakeCompletions:
    def __init__(self, response=None, error=None):
        self.response, self.error, self.kwargs = response, error, None

    async def create(self, **kwargs):
        self.kwargs = kwargs
        if self.error:
            raise self.error
        return self.response


def _client(completions):
    return SimpleNamespace(chat=SimpleNamespace(completions=completions))


def _response(*names):
    calls = [SimpleNamespace(id=f"c{i}", function=SimpleNamespace(name=n, arguments='{"district": "Musanze"}'))
             for i, n in enumerate(names)]
    return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(tool_calls=calls, content=None))])


TOOLS = build_sage_tools_payload(get_pydantic_tool_calls(), {})
TEXT = "will it rain in Musanze next week?"


@pytest.mark.asyncio
async def test_guard_retry_forces_a_tool_over_the_short_list(monkeypatch) -> None:
    monkeypatch.setenv("BRAIN_EMBEDDINGS_DISABLED", "1")  # BM25 ranking only
    completions = _FakeCompletions(response=_response("get_forecast"))
    calls = await message_routes._run_abdication_guard(
        _client(completions), {"model": "test-chat-model", "messages": []}, TEXT, [], TOOLS)
    assert calls == {0: {"id": "c0", "type": "function",
                         "function": {"name": "get_forecast", "arguments": '{"district": "Musanze"}'}}}
    assert completions.kwargs["tool_choice"] == "required"
    assert completions.kwargs["stream"] is False
    assert len(completions.kwargs["tools"]) == 5


@pytest.mark.asyncio
async def test_guard_retry_without_tool_call_keeps_the_prose(monkeypatch) -> None:
    monkeypatch.setenv("BRAIN_EMBEDDINGS_DISABLED", "1")
    calls = await message_routes._run_abdication_guard(
        _client(_FakeCompletions(response=_response())), {"model": "m", "messages": []}, TEXT, [], TOOLS)
    assert calls == {}


@pytest.mark.asyncio
async def test_guard_retry_failure_is_logged_not_raised(monkeypatch) -> None:
    monkeypatch.setenv("BRAIN_EMBEDDINGS_DISABLED", "1")
    # Recorded on the module logger itself: once the app's logging config is
    # loaded (full suite), "src" loggers stop propagating to caplog's handler.
    warnings: list[str] = []
    monkeypatch.setattr(message_routes.logger, "warning",
                        lambda msg, *args, **kwargs: warnings.append(msg % args if args else msg))
    calls = await message_routes._run_abdication_guard(
        _client(_FakeCompletions(error=RuntimeError("provider down"))), {"model": "m", "messages": []},
        TEXT, [], TOOLS)
    assert calls == {}
    assert any("abdication guard retry failed" in w for w in warnings)
