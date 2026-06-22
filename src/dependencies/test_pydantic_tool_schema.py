from src.dependencies.pydantic_tools import get_pydantic_tool_calls
from src.tools.pyd import tool_from


def test_all_pydantic_tool_arg_models_are_strict_schema_compatible():
    """Every registered Pydantic tool must build a strict OpenAI tool schema."""
    registry = get_pydantic_tool_calls()

    for tool_name, (fn, arg_model, *_rest) in registry.items():
        schema = tool_from(fn, arg_model)
        params = schema["function"]["parameters"]
        properties = params.get("properties") or {}

        assert params.get("additionalProperties") is False, tool_name
        assert sorted(params.get("required") or []) == sorted(properties.keys()), tool_name
