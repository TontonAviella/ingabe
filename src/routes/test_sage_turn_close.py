"""A Sage turn that ends without an answer is closed, so the screen never says "Sage working…" for ever."""

from __future__ import annotations

import json
from contextlib import asynccontextmanager
from datetime import datetime, timezone

import pytest

from src.routes import message_routes


class _Conn:
    def __init__(self, last, age):
        self.last, self.age, self.inserted = last, age, []

    async def fetchrow(self, sql, *args):
        return self.last

    async def fetchval(self, sql, *args):
        return self.age

    async def execute(self, sql, *args):
        self.inserted.append(json.loads(args[2]))


def _patch(monkeypatch, conn):
    @asynccontextmanager
    async def fake(*a, **k):
        yield conn

    monkeypatch.setattr(message_routes, "async_conn", fake)


@pytest.mark.parametrize("message, waits", [
    ({"role": "user", "content": "how many antennas?"}, True),
    ({"role": "tool", "content": "{}"}, True),
    ({"role": "assistant", "content": None, "tool_calls": [{}]}, True),
    ({"role": "assistant", "content": "  "}, True),
    ({"role": "assistant", "content": "Three antennas."}, False),
])
def test_what_still_waits_for_sage(message, waits):
    assert message_routes._waits_for_sage(json.dumps(message)) is waits
    assert message_routes._waits_for_sage(message) is waits


@pytest.mark.anyio
async def test_a_turn_that_ended_on_a_tool_result_gets_a_closing_message(monkeypatch):
    # 2026-10-14: the turn hit its round limit after a tool result; a week later the screen still said "working".
    conn = _Conn({"message_json": json.dumps({"role": "tool", "content": "{}"}), "created_at": datetime.now(timezone.utc)}, 600.0)
    _patch(monkeypatch, conn)
    assert await message_routes.close_unfinished_turn(7, "M1", "u1") is True
    assert conn.inserted == [{"role": "assistant", "content": message_routes.UNFINISHED_TURN_REPLY}]

    conn.last = {"message_json": json.dumps({"role": "assistant", "content": "Done."}), "created_at": None}
    conn.inserted = []
    assert await message_routes.close_unfinished_turn(7, "M1", "u1") is False
    assert conn.inserted == []


@pytest.mark.anyio
async def test_stop_leaves_a_turn_that_is_still_running_to_stop_itself(monkeypatch):
    conn = _Conn({"message_json": json.dumps({"role": "tool", "content": "{}"}), "created_at": datetime.now(timezone.utc)}, 20.0)
    _patch(monkeypatch, conn)
    assert await message_routes.close_unfinished_turn(7, "M1", "u1", message_routes.STOPPED_TURN_REPLY,
                                                      older_than_seconds=message_routes.STALE_TURN_SECONDS) is False
    conn.age = 4000.0
    assert await message_routes.close_unfinished_turn(7, "M1", "u1", message_routes.STOPPED_TURN_REPLY,
                                                      older_than_seconds=message_routes.STALE_TURN_SECONDS) is True
    assert conn.inserted == [{"role": "assistant", "content": "Stopped."}]
