"""Model calls run off the app's event loop (2026-10-07).

Building a Sage model request held the loop 0.2-0.5 s on an idle machine (a new client, SSL
context and TLS handshake per call, then the openai SDK's walk over every message and tool
schema), and /healthz waited up to 1.9 s during live turns. These tests run the real openai SDK
against a local OpenAI-compatible server, so the SDK's own work is what is measured.
"""

from __future__ import annotations

import asyncio
import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

import pytest
from openai import AsyncOpenAI, BadRequestError

from src.services.llm_loop import ModelClient


def _chunk(content: str) -> bytes:
    body = {
        "id": "c1",
        "object": "chat.completion.chunk",
        "created": 1,
        "model": "m",
        "choices": [{"index": 0, "delta": {"content": content}, "finish_reason": None}],
    }
    return b"data: " + json.dumps(body).encode() + b"\n\n"


class _Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"  # keep-alive, so connection reuse shows
    connections: set[int] = set()

    def log_message(self, format: str, *args: Any) -> None:
        pass

    def _send(self, status: int, content_type: str, body: bytes) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self) -> None:
        _Handler.connections.add(self.client_address[1])
        request = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        if request["model"] == "slow":
            time.sleep(3)
        if request["model"] == "rejected":
            error = {"error": {"message": "invalid tool call arguments", "type": "invalid_request_error"}}
            self._send(400, "application/json", json.dumps(error).encode())
        elif request.get("stream"):
            self._send(200, "text/event-stream", _chunk("hello ") + _chunk("world") + b"data: [DONE]\n\n")
        else:
            completion = {
                "id": "c1",
                "object": "chat.completion",
                "created": 1,
                "model": "m",
                "choices": [{"index": 0, "message": {"role": "assistant", "content": "hi"}, "finish_reason": "stop"}],
            }
            self._send(200, "application/json", json.dumps(completion).encode())


@pytest.fixture(scope="module")
def base_url():
    server = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{server.server_address[1]}/v1"
    server.shutdown()


def _run(coro):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


def test_a_stream_yields_the_same_chunks(base_url):
    async def read() -> str:
        client = ModelClient(base_url=base_url, api_key="k")
        stream = await client.chat.completions.create(
            model="m", messages=[{"role": "user", "content": "hi"}], stream=True
        )
        return "".join([chunk.choices[0].delta.content async for chunk in stream])

    assert _run(read()) == "hello world"


def test_a_completion_comes_back(base_url):
    async def ask():
        client = ModelClient(base_url=base_url, api_key="k")
        return await client.chat.completions.create(model="m", messages=[{"role": "user", "content": "hi"}])

    assert _run(ask()).choices[0].message.content == "hi"


def test_an_api_error_reaches_the_caller_unchanged(base_url):
    async def ask():
        client = ModelClient(base_url=base_url, api_key="k")
        return await client.chat.completions.create(model="rejected", messages=[{"role": "user", "content": "hi"}])

    with pytest.raises(BadRequestError) as caught:
        _run(ask())
    assert caught.value.status_code == 400
    assert "invalid tool call arguments" in str(caught.value)


def test_calls_to_one_endpoint_share_a_connection(base_url):
    async def ask_twice() -> None:
        for _ in range(2):
            client = ModelClient(base_url=base_url, api_key="shared")
            await client.chat.completions.create(model="m", messages=[{"role": "user", "content": "hi"}])

    _Handler.connections.clear()
    _run(ask_twice())
    assert len(_Handler.connections) == 1


def test_cancelling_the_caller_stops_waiting(base_url):
    async def ask_slowly():
        client = ModelClient(base_url=base_url, api_key="k")
        return await client.chat.completions.create(model="slow", messages=[{"role": "user", "content": "hi"}])

    started = time.monotonic()
    with pytest.raises(TimeoutError):
        _run(asyncio.wait_for(ask_slowly(), 0.3))
    assert time.monotonic() - started < 2


def _heavy_request() -> dict[str, Any]:
    """Like a Sage turn with a long conversation, scaled up so the SDK's work is easy to see."""
    prop = {"type": "object", "properties": {f"p{i}": {"type": "string", "description": "x" * 40} for i in range(12)}}
    tools = [
        {"type": "function", "function": {"name": f"tool_{n}", "description": "d" * 200,
                                          "parameters": {"type": "object", "properties": {"a": prop, "b": prop}}}}
        for n in range(150)
    ]
    messages: list[dict[str, Any]] = [{"role": "system", "content": "s" * 20_000}]
    for n in range(80):
        messages += [
            {"role": "user", "content": f"question {n}"},
            {"role": "assistant", "content": "", "tool_calls": [
                {"id": f"c{n}", "type": "function", "function": {"name": "tool_1", "arguments": "{}"}}]},
            {"role": "tool", "tool_call_id": f"c{n}", "content": "r" * 2_000},
        ]
    return {"model": "m", "messages": messages, "tools": tools, "tool_choice": "auto", "stream": True}


def _worst_loop_delay(make_client, request: dict[str, Any]) -> float:
    """Seconds the event loop woke up late, at worst, while one streamed call ran."""
    async def watch() -> float:
        async def call() -> None:
            stream = await make_client().chat.completions.create(**request)
            async for _chunk_ in stream:
                pass

        await call()  # warm up: first-use imports and the connection are not what is measured
        work = asyncio.ensure_future(call())
        worst = 0.0
        while not work.done():
            started = time.monotonic()
            await asyncio.sleep(0.005)
            worst = max(worst, time.monotonic() - started - 0.005)
        await work
        return worst

    return _run(watch())


def test_the_loop_keeps_running_while_a_model_request_is_built(base_url):
    request = _heavy_request()
    on_loop = _worst_loop_delay(lambda: AsyncOpenAI(base_url=base_url, api_key="k"), request)
    off_loop = _worst_loop_delay(lambda: ModelClient(base_url=base_url, api_key="k"), request)
    assert on_loop > 0.15  # the stand-in really holds the loop when built on it
    assert off_loop < 0.06


def test_sage_and_the_ollama_route_get_off_loop_clients(monkeypatch):
    from src.utils import get_chat_client_for_model, get_openai_client

    monkeypatch.setenv("OPENAI_BASE_URL", "https://openrouter.ai/api/v1")
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    assert isinstance(get_openai_client(None), ModelClient)  # type: ignore[arg-type]
    client, model = get_chat_client_for_model(None, "ollama:gemma4:12b-it-qat")  # type: ignore[arg-type]
    assert isinstance(client, ModelClient) and model == "gemma4:12b-it-qat"
