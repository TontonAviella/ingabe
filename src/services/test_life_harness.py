from src.services.life_harness import (
    NEMOTRON_SUPER3_MODEL,
    apply_life_harness_system_prompt,
    apply_life_harness_tool_contracts,
    life_harness_tool_signature,
    repeated_life_harness_tool_error,
    retrieve_life_harness_skills,
    validate_life_harness_tool_args,
)


def _tool_schema():
    return [
        {
            "type": "function",
            "function": {
                "name": "report_field_status",
                "description": "Report field risk status.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "field_name": {"type": "string"},
                        "risk": {"type": "string"},
                        "action": {"type": "string"},
                    },
                    "required": ["field_name", "risk", "action"],
                    "additionalProperties": False,
                },
            },
        }
    ]


def test_nemotron_super3_model_string_matches_hosted_provider():
    assert NEMOTRON_SUPER3_MODEL == "nvidia/nemotron-3-super-120b-a12b:free"


def test_h3_appends_required_argument_contract():
    tools = apply_life_harness_tool_contracts(_tool_schema())

    description = tools[0]["function"]["description"]

    assert "Runtime harness contract:" in description
    assert "field_name, risk, action" in description
    assert "ask the user instead of guessing" in description


def test_h2_blocks_missing_required_arguments():
    error = validate_life_harness_tool_args(
        "report_field_status",
        {"action": "scout drainage today"},
        _tool_schema(),
    )

    assert error is not None
    assert error["status"] == "error"
    assert error["harness_layer"] == "H2"
    assert error["missing_required_args"] == ["field_name", "risk"]


def test_h2_allows_empty_string_when_schema_requires_key_presence():
    error = validate_life_harness_tool_args(
        "report_field_status",
        {"field_name": "", "risk": "", "action": ""},
        _tool_schema(),
    )

    assert error is None


def test_h2_blocks_unexpected_arguments_when_schema_is_strict():
    error = validate_life_harness_tool_args(
        "report_field_status",
        {
            "field_name": "Kabarama",
            "risk": "rain risk high",
            "action": "scout drainage today",
            "extra": "do not pass this",
        },
        _tool_schema(),
    )

    assert error is not None
    assert error["harness_layer"] == "H2"
    assert error["unexpected_args"] == ["extra"]


def test_h4_blocks_third_identical_tool_call():
    signature = life_harness_tool_signature(
        "report_field_status",
        {
            "field_name": "Kabarama",
            "risk": "rain risk high",
            "action": "scout drainage today",
        },
    )

    assert repeated_life_harness_tool_error([signature, signature]) is None
    error = repeated_life_harness_tool_error([signature, signature, signature])

    assert error is not None
    assert error["harness_layer"] == "H4"
    assert "same tool call" in error["error"]


def test_system_prompt_gets_harness_rules_once():
    prompt = apply_life_harness_system_prompt(
        "Base prompt",
        "What damage should heavy rain cause near this field?",
    )
    prompt_again = apply_life_harness_system_prompt(prompt)

    assert "<RuntimeHarness>" in prompt
    assert "H2:" in prompt
    assert "Rain impact workflow" in prompt
    assert prompt_again == prompt


def test_h5_retrieves_task_relevant_skills():
    skills = retrieve_life_harness_skills(
        "Use the drone TIFF and admin cells to show crop stress by village",
        top_k=2,
    )

    titles = {skill["title"] for skill in skills}
    assert "Drone raster workflow" in titles
    assert "Admin plus H3 workflow" in titles
