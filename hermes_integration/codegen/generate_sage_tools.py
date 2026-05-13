#!/usr/bin/env python3
"""Codegen: read mundi.ai's tools.json + pydantic_tools.py → emit Hermes registrations.

Outputs `hermes_integration/plugins/ingabe-sage/generated_tools.py` containing
schemas + stub handlers for every Sage tool. The stub handlers return a
"not yet wired" JSON error so the LLM gets a clean error instead of crashing.
Wiring real handlers happens incrementally in Phase 2 (when Hermes runs
in-process inside mundi.ai's FastAPI).

Why stubs first: registering all 60 tool SCHEMAS lets us see how Nemotron
reasons about Sage's full tool surface (which tools it tries to call, in
what order, with what args) without needing to plumb each handler through.
Tells us where to prioritize real wiring.

Usage:
    python hermes_integration/codegen/generate_sage_tools.py

Reads from:
    /Users/macbook/Ingabe/mundi.ai/src/geoprocessing/tools.json
    /Users/macbook/Ingabe/mundi.ai/src/dependencies/pydantic_tools.py (count only)

Writes:
    hermes_integration/plugins/ingabe-sage/generated_tools.py
"""
from __future__ import annotations

import json
import re
from pathlib import Path

MUNDI_ROOT = Path("/Users/macbook/Ingabe/mundi.ai")
TOOLS_JSON = MUNDI_ROOT / "src" / "geoprocessing" / "tools.json"
PYDANTIC_REGISTRY_FILE = MUNDI_ROOT / "src" / "dependencies" / "pydantic_tools.py"
# Output of `docker exec mundi-app python /app/dump_pydantic_schemas.py`.
# Captured separately because importing Pydantic ArgModels requires mundi.ai's
# full venv (asyncpg, pgvector, qdrant-client, etc.) which we don't recreate
# in the Hermes plugin env. Refresh via:
#     docker cp dump_pydantic_schemas.py mundi-app:/app/
#     docker exec mundi-app python /app/dump_pydantic_schemas.py > \
#       hermes_integration/codegen/sage_pydantic_schemas.json
PYDANTIC_SCHEMAS_JSON = Path(__file__).parent / "sage_pydantic_schemas.json"
OUTPUT = (
    Path(__file__).parent.parent
    / "plugins" / "ingabe-sage" / "generated_tools.py"
)

# Tools that we KNOW are pure-IO (can run without mundi.ai in-process services).
# These get a TODO marker in the stub so we prioritize wiring them first.
EASY_WINS = {
    "search_location",       # Nominatim — already wired in tools.py
    # Add more as we identify them
}


def _emoji_for(tool_name: str) -> str:
    """Best-effort emoji classification for the tools list display."""
    n = tool_name.lower()
    if "raster" in n or "ndvi" in n or "spectral" in n: return "🛰️"
    if "vector" in n or "geom" in n or "polygon" in n: return "📐"
    if "buffer" in n or "intersect" in n or "union" in n: return "⊕"
    if "layer" in n or "display" in n: return "🗺️"
    if "warp" in n or "reproject" in n or "transform" in n: return "🔄"
    if "extract" in n or "clip" in n: return "✂️"
    if "soil" in n or "moisture" in n or "weather" in n: return "💧"
    if "alos" in n or "sar" in n or "cygnss" in n: return "📡"
    if "insurance" in n or "trigger" in n: return "🛡️"
    if "search" in n or "find" in n or "query" in n: return "🔍"
    return "🔧"


def _slugify(s: str) -> str:
    """Python identifier-safe."""
    return re.sub(r"[^a-zA-Z0-9_]", "_", s)


def _truncate_desc(desc: str, limit: int = 4000) -> str:
    desc = (desc or "").strip()
    if len(desc) > limit:
        desc = desc[:limit - 3] + "..."
    return desc


def main() -> int:
    tools_json = json.loads(TOOLS_JSON.read_text())
    if PYDANTIC_SCHEMAS_JSON.exists():
        pyd_schemas = json.loads(PYDANTIC_SCHEMAS_JSON.read_text())
    else:
        pyd_schemas = {}
        print(
            f"WARN: {PYDANTIC_SCHEMAS_JSON} missing. Re-run dump_pydantic_schemas.py "
            "inside mundi-app container to regenerate."
        )

    # Build a merged dict {tool_name: schema}. tools.json wins ties (it's the
    # canonical source for tools that exist in both). pyd_schemas are added
    # only for tools NOT already in tools.json — matches the runtime merge
    # logic in mundi.ai's message_routes.py.
    tools_json_names = {
        entry.get("function", {}).get("name")
        for entry in tools_json
        if entry.get("function", {}).get("name")
    }
    only_pyd_names = sorted(set(pyd_schemas.keys()) - tools_json_names)
    overlap_names = sorted(set(pyd_schemas.keys()) & tools_json_names)

    lines: list[str] = [
        '"""Auto-generated Hermes tool registrations from mundi.ai\'s tool surface.',
        '',
        'DO NOT EDIT BY HAND. Regenerate via:',
        '    python hermes_integration/codegen/generate_sage_tools.py',
        '',
        'Sources:',
        f'  - src/geoprocessing/tools.json ({len(tools_json)} GDAL/QGIS schemas)',
        f'  - sage_pydantic_schemas.json ({len(pyd_schemas)} Pydantic-derived schemas,',
        f'    of which {len(only_pyd_names)} are unique to Pydantic and {len(overlap_names)}',
        '    overlap with tools.json — overlap silently dropped, tools.json wins)',
        '',
        'Handlers are currently stubs (return "not yet wired" JSON). Phase 2 of',
        'the Hermes migration will replace each stub with a real handler that',
        'imports from mundi.ai\'s src/tools/ when Hermes runs in-process.',
        '"""',
        "from __future__ import annotations",
        "",
        "import json",
        "from typing import Any, Dict",
        "",
        "",
        "GENERATED_SCHEMAS: Dict[str, Dict[str, Any]] = {",
    ]

    # Tier 1: tools.json (canonical GDAL/QGIS tools)
    for entry in tools_json:
        fn = entry.get("function", {})
        name = fn.get("name")
        if not name:
            continue
        lines.append(f"    {name!r}: {{")
        lines.append(f"        'name': {name!r},")
        lines.append(f"        'description': {_truncate_desc(fn.get('description', ''))!r},")
        params = fn.get("parameters", {"type": "object", "properties": {}})
        lines.append(f"        'parameters': {params!r},")
        lines.append("    },")

    # Tier 2: Pydantic-derived schemas not already present
    for name in only_pyd_names:
        schema = pyd_schemas[name]
        lines.append(f"    {name!r}: {{")
        lines.append(f"        'name': {name!r},")
        lines.append(f"        'description': {_truncate_desc(schema.get('description', ''))!r},")
        lines.append(f"        'parameters': {schema.get('parameters', {})!r},")
        lines.append("    },")

    lines.append("}")
    lines.append("")
    lines.append("")
    lines.append("def _make_stub_handler(tool_name: str):")
    lines.append('    """Stub handler returning a structured \'not yet wired\' error.')
    lines.append("")
    lines.append("    Returning args_received per tool call lets us see EXACTLY which")
    lines.append("    Sage tools the LLM tries to call with what args. Reconnaissance")
    lines.append("    for Phase 2 wiring priority.")
    lines.append('    """')
    lines.append("    def _stub(args, **kw):")
    lines.append("        return json.dumps({")
    lines.append("            'status': 'not_yet_wired_in_hermes',")
    lines.append("            'tool_name': tool_name,")
    lines.append("            'args_received': args,")
    lines.append("            'task_id': kw.get('task_id'),")
    lines.append("            'note': (")
    lines.append("                'This Sage tool is REGISTERED with Hermes but not yet '")
    lines.append("                'EXECUTABLE. Phase 2 of the Hermes migration ('")
    lines.append("                'in-process integration with mundi.ai) wires the real '")
    lines.append("                'handler. For now, Hermes can see the tool surface but '")
    lines.append("                'cannot dispatch the actual geoprocessing.'")
    lines.append("            ),")
    lines.append("        })")
    lines.append("    return _stub")
    lines.append("")

    OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    OUTPUT.write_text("\n".join(lines))

    print(f"Wrote {OUTPUT}")
    print(f"  tools.json (canonical):    {len(tools_json)}")
    print(f"  pydantic-only schemas:     {len(only_pyd_names)}")
    print(f"  overlap (tools.json wins): {len(overlap_names)}: {overlap_names}")
    print(f"  TOTAL in GENERATED_SCHEMAS: {len(tools_json) + len(only_pyd_names)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
