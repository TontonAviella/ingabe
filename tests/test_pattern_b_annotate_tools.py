from __future__ import annotations

import json

from scripts import pattern_b_annotate_tools as pattern_b


def test_pattern_b_skips_non_dict_function_entries(tmp_path, monkeypatch) -> None:
    tools_path = tmp_path / "tools.json"
    tools_path.write_text(
        json.dumps(
            [
                {"type": "function", "function": "not-a-dict"},
                {
                    "type": "function",
                    "function": {
                        "name": "get_field_health",
                        "description": "health",
                    },
                },
            ]
        )
    )
    monkeypatch.setattr(pattern_b, "TOOLS_PATH", tools_path)
    monkeypatch.setattr(
        pattern_b,
        "ANNOTATIONS",
        {"get_field_health": " WHEN TO USE: test."},
    )

    assert pattern_b.main() == 0

    data = json.loads(tools_path.read_text())
    assert data[0]["function"] == "not-a-dict"
    assert "WHEN TO USE: test." in data[1]["function"]["description"]
