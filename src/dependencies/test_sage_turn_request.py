"""Sage's per-turn request: tool catalog snapshot and routing plan."""
from __future__ import annotations

import json
from pathlib import Path

from src.dependencies.pydantic_tools import get_pydantic_tool_calls
from src.dependencies.sage_routing import FAST_PATH_TOOLS, SMALL_TALK_SYSTEM_PROMPT
from src.dependencies.sage_turn_request import build_sage_tools_payload, plan_sage_turn

CATALOG = Path(__file__).resolve().parents[2] / "evals" / "sage_routing" / "tool_catalog.json"


def _names(tools: list[dict]) -> list[str]:
    return [t["function"]["name"] for t in tools]


def test_tool_catalog_matches_eval_snapshot() -> None:
    """The routing eval's labels are written against this snapshot.

    If this fails, a tool was added, renamed or removed: update the labels in
    evals/sage_routing/corpus.jsonl, then run
    `python scripts/eval_sage_routing.py catalog --write`.
    """
    snapshot = json.loads(CATALOG.read_text())
    live = sorted(_names(build_sage_tools_payload(get_pydantic_tool_calls(), {})))
    assert live == snapshot["model_tools"]
    assert sorted(FAST_PATH_TOOLS) == snapshot["fast_path_tools"]


def test_layer_enum_is_kept_only_when_layers_exist() -> None:
    def layer_param(tools: list[dict]) -> dict:
        tool = next(t for t in tools if t["function"]["name"] == "add_layer_to_map")
        return tool["function"]["parameters"]["properties"]["layer_id"]

    calls = get_pydantic_tool_calls()
    assert "enum" not in layer_param(build_sage_tools_payload(calls, {}))
    assert layer_param(build_sage_tools_payload(calls, {"L1": "Field"}))["enum"] == ["L1"]


def test_small_talk_sends_no_tools_and_skips_the_full_prompt() -> None:
    called = []
    plan = plan_sage_turn("hi", [{"role": "user", "content": "hi"}],
                          [{"function": {"name": "x"}}], lambda: called.append(1) or "FULL")
    assert plan.tools == [] and plan.tool_choice is None
    assert plan.system_prompt == SMALL_TALK_SYSTEM_PROMPT
    assert called == []


def test_domain_turn_gets_full_prompt_and_auto_tool_choice() -> None:
    tools = build_sage_tools_payload(get_pydantic_tool_calls(), {})
    text = "what is the NDVI of my fields in Huye"
    plan = plan_sage_turn(text, [{"role": "user", "content": text}], tools, lambda: "FULL PROMPT")
    assert plan.system_prompt.startswith("FULL PROMPT")
    assert plan.tools and plan.tool_choice == "auto"
    assert set(_names(plan.tools)) <= set(_names(tools))
