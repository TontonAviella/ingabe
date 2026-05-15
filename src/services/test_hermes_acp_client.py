"""Tests for the IngabeAcpClient — specifically the session_update handler
that translates Hermes ACP chunks into kue_stream_token() calls.

The bug we're guarding against (caught 2026-05-15 via a direct subprocess
probe of hermes-acp): on chunk-bearing updates (agent_message_chunk,
thought_chunk), the ACP wire JSON places content as a SINGLE block:

    "content": {"text": "hello", "type": "text"}

NOT as a list of blocks. The previous handler did `for block in content:`
which iterated the Pydantic model's field NAMES (strings like "text",
"type") instead of yielding the block. Every chunk was silently dropped
and accumulated_text stayed empty even though Hermes was generating text.

These tests pin down the contract: text flows through whether content is
a single block OR a list of blocks. Both shapes show up in ACP traffic
depending on update kind.
"""
from __future__ import annotations

import pytest

from src.services.hermes_acp_client import build_ingabe_acp_client


class _FakeBlock:
    """Stand-in for an acp TextContentBlock. The real type isn't
    importable in tests without the agent-client-protocol package
    installed in the test env, so we duck-type."""
    def __init__(self, text: str | None = None, type_: str = "text"):
        self.text = text
        self.type = type_


class _FakeUpdate:
    """Stand-in for an acp SessionUpdate (agent_message_chunk variant)."""
    def __init__(self, content):
        self.content = content
        self.session_update = "agent_message_chunk"


class _FakeNotification:
    def __init__(self, update):
        self.update = update


@pytest.fixture
def collected():
    """Captures (conversation_id, text) tuples sent via stream_token."""
    out: list[tuple[str, str]] = []

    async def stream_token(cid: str, text: str) -> None:
        out.append((cid, text))

    async def notify_error(cid: str, msg: str) -> None:  # not used here
        pass

    return out, stream_token, notify_error


@pytest.mark.asyncio
async def test_session_update_single_block_emits_text(collected, monkeypatch):
    """The regression: content is a single block (not a list).

    Before the 2026-05-15 fix, `for block in content` iterated the
    block's Pydantic fields and dropped the text entirely.
    """
    captured, stream_token, notify_error = collected

    # Stub `acp` so build_ingabe_acp_client doesn't need the real package.
    monkeypatch.setitem(__import__("sys").modules, "acp", _StubAcpModule())

    client = build_ingabe_acp_client(
        stream_token=stream_token,
        notify_error=notify_error,
        conversation_id="conv-1",
    )
    await client.session_update(
        _FakeNotification(_FakeUpdate(content=_FakeBlock(text="hello")))
    )
    assert captured == [("conv-1", "hello")], (
        "single-block content should emit one stream_token call"
    )
    assert client.accumulated_text == ["hello"]


@pytest.mark.asyncio
async def test_session_update_list_of_blocks_still_works(collected, monkeypatch):
    """Some update kinds use list-of-blocks. We handle both shapes."""
    captured, stream_token, notify_error = collected
    monkeypatch.setitem(__import__("sys").modules, "acp", _StubAcpModule())

    client = build_ingabe_acp_client(
        stream_token=stream_token,
        notify_error=notify_error,
        conversation_id="conv-2",
    )
    await client.session_update(
        _FakeNotification(_FakeUpdate(content=[
            _FakeBlock(text="part1 "),
            _FakeBlock(text="part2"),
        ]))
    )
    assert captured == [("conv-2", "part1 "), ("conv-2", "part2")]
    assert client.accumulated_text == ["part1 ", "part2"]


@pytest.mark.asyncio
async def test_session_update_no_content_is_noop(collected, monkeypatch):
    """usage_update and other content-less updates: no error, no emission."""
    captured, stream_token, notify_error = collected
    monkeypatch.setitem(__import__("sys").modules, "acp", _StubAcpModule())

    client = build_ingabe_acp_client(
        stream_token=stream_token,
        notify_error=notify_error,
        conversation_id="conv-3",
    )
    await client.session_update(_FakeNotification(_FakeUpdate(content=None)))
    assert captured == []
    assert client.accumulated_text == []


@pytest.mark.asyncio
async def test_session_update_non_text_block_skipped(collected, monkeypatch):
    """Image/audio blocks have no text attribute — we silently skip them.

    Today's Sage UI is text-only; future PRs can extend this when we wire
    Hermes Image/Audio output through to the WebSocket as MIME messages.
    """
    captured, stream_token, notify_error = collected
    monkeypatch.setitem(__import__("sys").modules, "acp", _StubAcpModule())

    # Mix: an image-like block (no .text) and a text block.
    class _ImageBlock:
        type = "image"
        # no `.text` attribute
    client = build_ingabe_acp_client(
        stream_token=stream_token,
        notify_error=notify_error,
        conversation_id="conv-4",
    )
    await client.session_update(
        _FakeNotification(_FakeUpdate(content=[_ImageBlock(), _FakeBlock(text="hi")]))
    )
    assert captured == [("conv-4", "hi")]


# ---------------------------------------------------------------------------
# Test scaffolding: stub the `acp` module so the factory function imports.
# ---------------------------------------------------------------------------


class _StubAcpModule:
    """Minimal stub of the `acp` package — only what build_ingabe_acp_client
    touches at import-time inside the factory. We don't need real behavior;
    we just need Client to be a class our nested class can subclass.
    """
    class Client:
        def __init__(self):
            pass

    class RequestError:
        @staticmethod
        def method_not_found():
            return Exception("method_not_found (stub)")

    class RequestPermissionResponse:
        class Outcome:
            def __init__(self, outcome=""):
                self.outcome = outcome

        def __init__(self, outcome=None):
            self.outcome = outcome
