"""Model calls run on an event loop of their own, in a background thread.

Before a Sage model call reached the network it held the app's event loop for 0.2-0.5 s on an idle
machine, and /healthz waited up to 1.9 s during live turns (measured in a copy of mundi-app on
2026-10-07). The time was Python work, not waiting: a new AsyncOpenAI client for every call (an
SSL context built from the CA bundle, then a fresh TLS handshake) ~110 ms, and the SDK's walk over
every message and tool schema before sending (`async_maybe_transform`) 85-250 ms. The app answers
every user from one event loop, so everyone waited.

Here that work runs on a second loop in a daemon thread. It is pure Python, so the interpreter
hands the GIL back to the app's loop every few milliseconds (sys.getswitchinterval) and the app
keeps answering. One AsyncOpenAI is built per endpoint, on that loop, and reused, so connections
stay open between calls.

`ModelClient(...).chat.completions.create` and `.embeddings.create` take AsyncOpenAI's arguments
and return its results; with stream=True a chat call returns an async iterator of the same chunks.
Exceptions (openai.APIError and the rest) reach the caller unchanged, and cancelling the caller
cancels the call.
"""

from __future__ import annotations

import asyncio
import threading
from collections.abc import Coroutine, Mapping
from typing import Any, TypeVar

from openai import AsyncOpenAI, AsyncStream

T = TypeVar("T")

_ClientKey = tuple[str, str, tuple[tuple[str, str], ...]]

_loop: asyncio.AbstractEventLoop | None = None
_loop_lock = threading.Lock()
# One client per endpoint; read and written only on the model-call loop.
_clients: dict[_ClientKey, AsyncOpenAI] = {}
_END = object()


def _model_loop() -> asyncio.AbstractEventLoop:
    global _loop
    with _loop_lock:
        if _loop is None:
            loop = asyncio.new_event_loop()
            threading.Thread(target=loop.run_forever, name="model-calls", daemon=True).start()
            _loop = loop
        return _loop


async def _on_model_loop(coro: Coroutine[Any, Any, T]) -> T:
    """Await ``coro`` on the model-call loop; cancelling the caller cancels it there."""
    return await asyncio.wrap_future(asyncio.run_coroutine_threadsafe(coro, _model_loop()))


def _client(key: _ClientKey) -> AsyncOpenAI:
    client = _clients.get(key)
    if client is None:
        base_url, api_key, headers = key
        client = AsyncOpenAI(base_url=base_url, api_key=api_key, default_headers=dict(headers))
        _clients[key] = client
    return client


async def _next_chunk(stream: AsyncStream[Any]) -> Any:
    try:
        return await stream.__anext__()
    except StopAsyncIteration:
        return _END


class _Stream:
    """A stream opened on the model-call loop, read from the caller's loop one chunk at a time."""

    def __init__(self, stream: AsyncStream[Any]) -> None:
        self._stream = stream

    def __aiter__(self) -> _Stream:
        return self

    async def __anext__(self) -> Any:
        chunk = await _on_model_loop(_next_chunk(self._stream))
        if chunk is _END:
            raise StopAsyncIteration
        return chunk


class _Create:
    """One AsyncOpenAI resource's ``create`` (chat completions, embeddings), run on the model-call loop."""

    def __init__(self, key: _ClientKey, resource: str) -> None:
        self._key = key
        self._resource = resource

    async def create(self, **kwargs: Any) -> Any:
        result = await _on_model_loop(self._create(kwargs))
        return _Stream(result) if isinstance(result, AsyncStream) else result

    async def _create(self, kwargs: dict[str, Any]) -> Any:
        client = _client(self._key)
        resource = client.chat.completions if self._resource == "chat" else client.embeddings
        return await resource.create(**kwargs)


class _Chat:
    def __init__(self, key: _ClientKey) -> None:
        self.completions = _Create(key, "chat")


class ModelClient:
    """An OpenAI-compatible endpoint whose calls run on the model-call loop.

    The only place in src/ that builds an AsyncOpenAI (gate: `llm-client` in check_standards.py).
    """

    def __init__(self, *, base_url: str, api_key: str, default_headers: Mapping[str, str] | None = None) -> None:
        key = (base_url, api_key, tuple(sorted((default_headers or {}).items())))
        self.chat = _Chat(key)
        self.embeddings = _Create(key, "embeddings")
