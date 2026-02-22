"""Unit tests for tool_helpers.py — result builders, arg parsing, safe execution."""

import pytest
from unittest.mock import MagicMock

from openai.types.chat import ChatCompletionMessageToolCall
from openai.types.chat.chat_completion_message_tool_call import Function

from src.geoprocessing.tool_helpers import (
    execute_tool,
    parse_tool_args,
    require_args,
    tool_error,
    tool_success,
)


# ---------------------------------------------------------------------------
# Result builders
# ---------------------------------------------------------------------------


class TestToolSuccess:
    def test_default(self):
        result = tool_success()
        assert result == {"status": "success", "message": "Success"}

    def test_custom_message(self):
        result = tool_success("Layer created")
        assert result["message"] == "Layer created"

    def test_extra_fields(self):
        result = tool_success("ok", layer_id="L001", count=5)
        assert result["layer_id"] == "L001"
        assert result["count"] == 5
        assert result["status"] == "success"


class TestToolError:
    def test_default(self):
        result = tool_error("Something broke")
        assert result == {"status": "error", "error": "Something broke"}

    def test_extra_fields(self):
        result = tool_error("timeout", code=504)
        assert result["code"] == 504
        assert result["status"] == "error"


# ---------------------------------------------------------------------------
# Argument parsing
# ---------------------------------------------------------------------------


class TestParseToolArgs:
    def _make_tool_call(self, arguments: str) -> ChatCompletionMessageToolCall:
        return ChatCompletionMessageToolCall(
            id="call_1",
            type="function",
            function=Function(name="test_tool", arguments=arguments),
        )

    def test_valid_json(self):
        tc = self._make_tool_call('{"layer_id": "L001", "distance": 100}')
        args = parse_tool_args(tc)
        assert args == {"layer_id": "L001", "distance": 100}

    def test_empty_json(self):
        tc = self._make_tool_call("{}")
        assert parse_tool_args(tc) == {}

    def test_invalid_json_returns_empty(self):
        tc = self._make_tool_call("not json at all")
        assert parse_tool_args(tc) == {}

    def test_none_arguments_returns_empty(self):
        tc = MagicMock()
        tc.function.arguments = None
        assert parse_tool_args(tc) == {}


class TestRequireArgs:
    def test_all_present(self):
        assert require_args({"a": 1, "b": "x"}, "a", "b") is None

    def test_missing_single(self):
        err = require_args({"a": 1}, "a", "b")
        assert err is not None
        assert "b" in err

    def test_missing_multiple(self):
        err = require_args({}, "x", "y")
        assert "x" in err and "y" in err

    def test_empty_string_counts_as_missing(self):
        err = require_args({"a": ""}, "a")
        assert err is not None
        assert "a" in err

    def test_zero_counts_as_missing(self):
        """Falsy values like 0 are treated as missing by the current logic."""
        err = require_args({"a": 0}, "a")
        assert err is not None

    def test_no_keys_always_passes(self):
        assert require_args({}) is None


# ---------------------------------------------------------------------------
# Safe execution wrapper
# ---------------------------------------------------------------------------


class TestExecuteTool:
    @pytest.mark.anyio
    async def test_success_returns_dict(self):
        async def good_fn():
            return {"status": "success", "data": 42}

        result = await execute_tool(good_fn, tool_name="test")
        assert result["status"] == "success"
        assert result["data"] == 42

    @pytest.mark.anyio
    async def test_non_dict_wrapped(self):
        async def string_fn():
            return "hello"

        result = await execute_tool(string_fn, tool_name="test")
        assert result["status"] == "success"
        assert result["message"] == "hello"

    @pytest.mark.anyio
    async def test_exception_caught(self):
        async def bad_fn():
            raise ValueError("boom")

        result = await execute_tool(bad_fn, tool_name="exploder")
        assert result["status"] == "error"
        assert "exploder" in result["error"]
        assert "boom" in result["error"]

    @pytest.mark.anyio
    async def test_passes_args_and_kwargs(self):
        async def fn_with_args(a, b, key=None):
            return {"sum": a + b, "key": key}

        result = await execute_tool(fn_with_args, 1, 2, key="val", tool_name="adder")
        assert result["sum"] == 3
        assert result["key"] == "val"
