"""Runtime harness helpers adapted from Life-Harness.

The upstream benchmark source is pinned as a submodule at
`external/life-harness`. We do not import benchmark runtime code in production;
we adapt its H2/H3/H4/H5 harness shape into deterministic guards around the
frozen Sage/Hermes brain model.
"""

from __future__ import annotations

import json
import os
import re
from typing import Any


NEMOTRON_SUPER3_MODEL = "nvidia/nemotron-3-super-120b-a12b:free"

_DISABLED_VALUES = {"0", "false", "no", "off"}
_CONTRACT_MARKER = "Runtime harness contract:"

_H5_SKILLS: list[dict[str, str]] = [
    {
        "title": "Rain impact workflow",
        "pattern": "rain rainfall storm flood flooding damage risk alert drainage weather forecast farmer",
        "tip": (
            "For rain/flood questions, first ground the area, then use forecast "
            "or rainfall totals, then call the impact tool with render_map=true "
            "when the user asks where damage/risk will happen."
        ),
    },
    {
        "title": "Drone raster workflow",
        "pattern": "drone raster tiff tif orthophoto ndvi ndre imagery upload field crop stress",
        "tip": (
            "For uploaded drone imagery, inspect metadata/available bands before "
            "interpreting. Prefer existing raster/query tools; do not invent crop "
            "stress claims without a layer id or polygon/AOI."
        ),
    },
    {
        "title": "Admin plus H3 workflow",
        "pattern": "district sector cell village admin boundary h3 hex hexagon aggregation coverage",
        "tip": (
            "For Rwanda admin questions, use official village/cell/sector/district "
            "boundaries for reporting and H3/hex cells for fast aggregation or "
            "visual comparison."
        ),
    },
    {
        "title": "Brain memory workflow",
        "pattern": "remember brain gbrain entity observation partner cooperative history note memory",
        "tip": (
            "For memory questions, search Brain first, inspect the entity if a "
            "slug is returned, then add observations only when the user provides "
            "new factual evidence."
        ),
    },
    {
        "title": "Evidence-first spatial insight workflow",
        "pattern": (
            "housing infrastructure environment city settlement building buildings "
            "road roads drainage runoff erosion h3 hex spatial risk impact damage "
            "priority zone basemap satellite"
        ),
        "tip": (
            "For housing, infrastructure, or environment insight, treat H3/hex as "
            "an internal analysis unit. Gather evidence from buildings, roads, "
            "drainage, terrain, rain/flood, or uploaded raster context before "
            "making claims, and explain the evidence behind the map."
        ),
    },
    {
        "title": "Pipeline proof workflow",
        "pattern": (
            "dagster pipeline flowing flow fresh freshness proof evidence posthog "
            "telemetry satellite weather h3 raster cache cron"
        ),
        "tip": (
            "Before saying scheduled satellite, weather, H3, or raster data is "
            "fresh/flowing, inspect pipeline evidence or telemetry. If no fresh "
            "evidence exists, say that plainly and run the smallest live check."
        ),
    },
]


def life_harness_enabled() -> bool:
    """Return whether the runtime harness should adapt Hermes/Sage calls."""

    return os.environ.get("MUNDI_AGENT_HARNESS", "1").strip().lower() not in _DISABLED_VALUES


def _tokens(text: str) -> set[str]:
    return set(re.findall(r"[a-z0-9]+", text.lower()))


def retrieve_life_harness_skills(user_text: str, *, top_k: int = 2) -> list[dict[str, str]]:
    """H5: retrieve compact procedural skills for the current task."""

    query = _tokens(user_text)
    if not query:
        return []
    scored: list[tuple[int, dict[str, str]]] = []
    for skill in _H5_SKILLS:
        score = len(query & _tokens(skill["pattern"]))
        if score:
            scored.append((score, skill))
    scored.sort(key=lambda item: (-item[0], item[1]["title"]))
    return [skill for _, skill in scored[:top_k]]


def apply_life_harness_system_prompt(system_prompt: str, user_text: str = "") -> str:
    """Append concise H2/H3/H4/H5 operating rules to the system prompt."""

    if not life_harness_enabled() or "<RuntimeHarness>" in system_prompt:
        return system_prompt
    skills = retrieve_life_harness_skills(user_text)
    skill_block = ""
    if skills:
        tips = "\n".join(f"- {skill['title']}: {skill['tip']}" for skill in skills)
        skill_block = "\nH5 retrieved procedural skills:\n" + tips
    return (
        system_prompt.rstrip()
        + "\n\n<RuntimeHarness>\n"
        + "Use the runtime harness rules:\n"
        + "H2: Before calling a tool, verify every required argument is present.\n"
        + "H3: Treat each tool description as the exact environment contract.\n"
        + "H4: Do not repeat the same failing or non-progressing tool call; use the result, choose the next tool, or ask one concise question.\n"
        + "H5: For agriculture work, plan in this order: locate the field, inspect available map/data layers, run the smallest relevant tool, then give risk and next action.\n"
        + skill_block
        + "</RuntimeHarness>"
    )


def apply_life_harness_tool_contracts(tools: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """H3: make the tool contract explicit inside every tool description.

    Tool descriptions are sent with every LLM tool-call request, so this keeps
    required-field guidance close to the decision point for models like
    Nemotron that may otherwise emit free-form or partial tool calls.
    """

    if not life_harness_enabled():
        return tools

    for tool in tools:
        fn = tool.get("function")
        if not isinstance(fn, dict):
            continue
        description = str(fn.get("description") or "").rstrip()
        if _CONTRACT_MARKER in description:
            continue
        params = fn.get("parameters")
        required: list[str] = []
        if isinstance(params, dict) and isinstance(params.get("required"), list):
            required = [str(name) for name in params["required"]]
        required_text = ", ".join(required) if required else "none"
        fn["description"] = (
            description
            + "\n\n"
            + f"{_CONTRACT_MARKER} pass exactly one JSON object. "
            + f"Required arguments: {required_text}. "
            + "If a required value is unknown, ask the user instead of guessing."
        ).strip()
    return tools


def _schema_for_tool(
    tools: list[dict[str, Any]],
    tool_name: str,
) -> dict[str, Any] | None:
    for tool in tools:
        fn = tool.get("function")
        if not isinstance(fn, dict) or fn.get("name") != tool_name:
            continue
        params = fn.get("parameters")
        return params if isinstance(params, dict) else None
    return None


def validate_life_harness_tool_args(
    tool_name: str,
    tool_args: Any,
    tools: list[dict[str, Any]],
) -> dict[str, Any] | None:
    """H2: validate a tool call before the environment executes it."""

    if not life_harness_enabled():
        return None

    schema = _schema_for_tool(tools, tool_name)
    if schema is None:
        return None

    if not isinstance(tool_args, dict):
        return {
            "status": "error",
            "harness_layer": "H2",
            "error": (
                f"Invalid arguments for {tool_name}: expected one JSON object. "
                "Call the tool again with an object that matches the schema."
            ),
        }

    required = [str(name) for name in schema.get("required", []) if isinstance(name, str)]
    missing = [name for name in required if name not in tool_args or tool_args[name] is None]
    if missing:
        return {
            "status": "error",
            "harness_layer": "H2",
            "error": (
                f"Missing required arguments for {tool_name}: {', '.join(missing)}. "
                "Call the tool again with all required arguments."
            ),
            "missing_required_args": missing,
        }

    properties = schema.get("properties")
    if schema.get("additionalProperties") is False and isinstance(properties, dict):
        extras = sorted(str(name) for name in tool_args if name not in properties)
        if extras:
            return {
                "status": "error",
                "harness_layer": "H2",
                "error": (
                    f"Unexpected arguments for {tool_name}: {', '.join(extras)}. "
                    "Call the tool again using only schema-defined arguments."
                ),
                "unexpected_args": extras,
            }

    return None


def life_harness_tool_signature(tool_name: str, tool_args: Any) -> str:
    """Return a stable signature for H4 repeat-call detection."""

    try:
        args_text = json.dumps(tool_args, sort_keys=True, separators=(",", ":"), default=str)
    except TypeError:
        args_text = str(tool_args)
    return f"{tool_name}:{args_text}"


def repeated_life_harness_tool_error(
    recent_signatures: list[str],
    *,
    repeat_after: int = 3,
) -> dict[str, Any] | None:
    """H4: detect repeated identical tool calls that are unlikely to progress."""

    if not life_harness_enabled() or len(recent_signatures) < repeat_after:
        return None
    window = recent_signatures[-repeat_after:]
    if len(set(window)) != 1:
        return None
    return {
        "status": "error",
        "harness_layer": "H4",
        "error": (
            "The same tool call has repeated without progress. Do not call it "
            "again with identical arguments; use the prior result, choose the "
            "next tool, or ask the user one concise clarification question."
        ),
    }
