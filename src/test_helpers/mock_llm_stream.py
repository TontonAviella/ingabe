"""Streaming-compatible OpenAI chat completion mocks.

Production code (src/routes/message_routes.py) now calls
`client.chat.completions.create(stream=True)` and iterates with
`async for chunk in stream`, reading `chunk.choices[0].delta.content`
and `chunk.choices[0].delta.tool_calls[i].{index,id,function.{name,arguments}}`.

This helper wraps the legacy non-streaming (content, tool_calls) test shape
and yields ChatCompletionChunk-shaped objects so existing tests can keep
constructing `MockResponse("text", [ChatCompletionMessageToolCall(...)])`.
"""

from __future__ import annotations


class _MockDelta:
    def __init__(self, content=None, tool_calls=None):
        self.content = content
        self.tool_calls = tool_calls


class _MockChunkChoice:
    def __init__(self, delta):
        self.delta = delta


class _MockChunk:
    def __init__(self, content=None, tool_calls=None):
        self.choices = [_MockChunkChoice(_MockDelta(content, tool_calls))]


class _MockToolCallFunctionDelta:
    def __init__(self, name=None, arguments=None):
        self.name = name
        self.arguments = arguments


class _MockToolCallDelta:
    def __init__(self, index, id=None, function=None):
        self.index = index
        self.id = id
        self.type = "function"
        self.function = function


class MockStreamResponse:
    """Stand-in for the awaited result of `client.chat.completions.create(stream=True)`.

    Accepts the same (content, tool_calls) shape the old non-streaming MockResponse
    used. tool_calls is an iterable of openai.types.chat.ChatCompletionMessageToolCall
    (or any object exposing `.id`, `.function.name`, `.function.arguments`).
    """

    def __init__(self, content: str | None = None, tool_calls=None):
        chunks = []
        if content:
            chunks.append(_MockChunk(content=content))
        if tool_calls:
            for i, tc in enumerate(tool_calls):
                chunks.append(
                    _MockChunk(
                        tool_calls=[
                            _MockToolCallDelta(
                                index=i,
                                id=getattr(tc, "id", None),
                                function=_MockToolCallFunctionDelta(
                                    name=getattr(getattr(tc, "function", None), "name", None),
                                    arguments=getattr(getattr(tc, "function", None), "arguments", None),
                                ),
                            ),
                        ]
                    )
                )
        self._chunks = chunks

    def __aiter__(self):
        return self._aiter_impl()

    async def _aiter_impl(self):
        for c in self._chunks:
            yield c
