"""Streaming-compatible chat-completion mock for tests.

Production at ``src/routes/message_routes.py`` calls
``client.chat.completions.create(..., stream=True)`` and then iterates with
``async for chunk in stream``. Each chunk must expose
``chunk.choices[0].delta.content`` and ``chunk.choices[0].delta.tool_calls``.

The old non-streaming ``MockResponse`` (with ``.choices[0].message``) raised
``TypeError: 'async for' requires an object with __aiter__``. This helper is
the shim test files import to keep their fixtures small while satisfying the
streaming shape.
"""

from types import SimpleNamespace


class MockStream:
    def __init__(self, chunks):
        self._chunks = list(chunks)
        self._idx = 0

    def __aiter__(self):
        return self

    async def __anext__(self):
        if self._idx >= len(self._chunks):
            raise StopAsyncIteration
        chunk = self._chunks[self._idx]
        self._idx += 1
        return chunk


def _make_stream(content, tool_calls=None):
    chunks = []
    if tool_calls:
        for i, tc in enumerate(tool_calls):
            tc_delta = SimpleNamespace(
                index=i,
                id=tc.id,
                function=SimpleNamespace(
                    name=tc.function.name,
                    arguments=tc.function.arguments,
                ),
            )
            chunks.append(
                SimpleNamespace(
                    choices=[
                        SimpleNamespace(
                            delta=SimpleNamespace(content=None, tool_calls=[tc_delta]),
                        )
                    ],
                )
            )
    if content:
        chunks.append(
            SimpleNamespace(
                choices=[
                    SimpleNamespace(
                        delta=SimpleNamespace(content=content, tool_calls=None),
                    )
                ],
            )
        )
    chunks.append(
        SimpleNamespace(
            choices=[
                SimpleNamespace(
                    delta=SimpleNamespace(content=None, tool_calls=None),
                )
            ],
        )
    )
    return MockStream(chunks)


class MockResponse:
    def __init__(self, content, tool_calls=None):
        self._content = content
        self._tool_calls = tool_calls

    def __aiter__(self):
        return _make_stream(self._content, self._tool_calls).__aiter__()

    @property
    def choices(self):
        return [
            SimpleNamespace(
                message=SimpleNamespace(
                    content=self._content,
                    tool_calls=self._tool_calls,
                ),
                delta=SimpleNamespace(
                    content=self._content,
                    tool_calls=self._tool_calls,
                ),
            )
        ]


def recv_non_streaming(websocket):
    """Receive next websocket JSON message, skipping streaming token payloads.

    Production interleaves StreamingTokenPayload({streaming: True, token: ...})
    between ephemeral active/completed during streaming chat completion. Tests
    written against the pre-streaming protocol must skip these to see the
    ephemeral and assistant messages they assert on.
    """
    while True:
        msg = websocket.receive_json()
        if msg.get("streaming") is True:
            continue
        return msg
