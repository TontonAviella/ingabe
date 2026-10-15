"""Sage's history replay keeps every past call of a tool the loop offers (with its result)."""

from __future__ import annotations

from src.dependencies.pydantic_tools import get_pydantic_tool_calls
from src.dependencies.sage_turn_request import build_sage_tools_payload
from src.routes.message_routes import replayable_tool_names


def test_every_tool_the_loop_offers_is_replayed_not_dropped():
    names = replayable_tool_names()
    offered = {t["function"]["name"] for t in build_sage_tools_payload(get_pydantic_tool_calls(), {"L": "layer"})}
    assert offered <= names
    # 2026-10-14: dropped from each replay, so Sage asked for it again every 4 s until stopped.
    assert "reverse_geocode_coordinates" in names
