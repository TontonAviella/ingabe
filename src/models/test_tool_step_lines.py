"""Every step Sage takes is read in plain words in the conversation, never by its tool's name."""
import json
from pathlib import Path

from src.dependencies.sage_turn_request import KEEP_ANSWER_TOOL
from src.models.messages import STEP_LINE_OTHER, TC_TAGLINE_MAP, convert_openai_tool_call_to_sanitized_tool_call

ROOT = Path(__file__).resolve().parents[2]


def _offered() -> set[str]:
    """Every tool Sage can call: the catalog the routing test keeps equal to the live tools (tests/
    test_sage_routing_eval.py), the shortcuts, and the guard's own tool."""
    catalog = json.loads((ROOT / "evals" / "sage_routing" / "tool_catalog.json").read_text())
    return set(catalog["model_tools"]) | set(catalog["fast_path_tools"]) | {KEEP_ANSWER_TOOL}


def _call(name: str) -> dict:
    return {"id": "call_1", "type": "function", "function": {"name": name, "arguments": "{}"}}


def test_every_tool_has_a_plain_step_line():
    assert sorted(_offered() - set(TC_TAGLINE_MAP)) == []


def test_a_step_line_is_words_not_a_name():
    for name, line in TC_TAGLINE_MAP.items():
        assert "_" not in line and name not in line, name
        assert line[0].isupper() and not line.endswith((".", "…")), name
        assert len(line.split()) <= 8, name


def test_the_conversation_gets_the_line_and_never_the_name():
    said = convert_openai_tool_call_to_sanitized_tool_call(_call("reverse_geocode_coordinates"))  # type: ignore[arg-type]
    assert said.tagline == "Found which place this is"
    unknown = convert_openai_tool_call_to_sanitized_tool_call(_call("a_tool_added_later"))  # type: ignore[arg-type]
    assert unknown.tagline == STEP_LINE_OTHER
