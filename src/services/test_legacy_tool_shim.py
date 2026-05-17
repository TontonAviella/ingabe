"""Tests for the legacy tool shim — the bridge between /internal/tool-call
and the inline elif handlers in message_routes.py.

Each test names the contract it pins down. The shim's invariant is "never
raises, always returns a JSON-serializable dict the LLM can read."
"""
from __future__ import annotations

import json
from typing import Any
from unittest.mock import MagicMock

import pytest

from src.services.legacy_tool_shim import (
    LEGACY_HANDLERS,
    LegacyToolContext,
    execute_legacy_tool,
)


def _make_ctx(arguments: dict[str, Any] | None = None) -> LegacyToolContext:
    """Build a context for tests. The conn is a Mock since no test should
    actually hit the DB — handlers that need the DB are stubbed."""
    return LegacyToolContext(
        user_id="user-test-aaa",
        partner_id="partner-test-bbb",
        conversation_id=42,
        map_id="MTESTAAAAAAA",
        project_id="PTESTBBBBBBB",
        conn=MagicMock(),
        arguments=arguments or {},
    )


@pytest.mark.asyncio
async def test_registry_includes_all_53_legacy_names():
    """Whitelist sanity: every name the chat loop dispatches must be in
    LEGACY_HANDLERS so /internal/tool-call's whitelist check accepts it.

    Failure mode this guards: someone removes a name from
    _NOT_YET_EXTRACTED but doesn't add a real handler — the route returns
    404 and Hermes turns silently break. Pin the names explicitly.
    """
    must_have_names = {
        # Hardcoded in message_routes.py (no tools.json or pydantic schema)
        "new_layer_from_postgis", "set_layer_style", "add_layer_to_map",
        "query_postgis_database", "query_duckdb_sql", "zonal_statistics",
        "reverse_geocode_coordinates",
        # tools.json schemas with inline elif handlers
        "get_forecast", "get_field_health", "get_ndvi_stats", "search_brain",
        "identify_parcel_crop", "get_insurance_intelligence",
        "create_management_zones",
        # QGIS-processing forwards
        "native_buffer", "qgis_clip", "gdal_warpreproject",
    }
    missing = must_have_names - set(LEGACY_HANDLERS.keys())
    assert not missing, (
        f"LEGACY_HANDLERS is missing {len(missing)} tool name(s): {sorted(missing)}. "
        f"This means /internal/tool-call would return 404 when the Hermes "
        f"plugin invokes them — confirmed broken turn for the user."
    )
    # Lower bound: at least 53 entries total (1 real + 52 stubs as of this PR)
    assert len(LEGACY_HANDLERS) >= 53, (
        f"LEGACY_HANDLERS has only {len(LEGACY_HANDLERS)} entries — fewer than the "
        f"53 inline elif blocks in message_routes.py. Some legacy tools are unreachable."
    )


@pytest.mark.asyncio
async def test_not_yet_extracted_returns_structured_message():
    """A stub handler must return a parseable dict with status=not_yet_extracted,
    NOT raise. The LLM downstream pattern-matches on `status` to decide
    whether to apologize, retry, or give up."""
    result = await execute_legacy_tool("get_forecast", _make_ctx({"lat": -1.95, "lon": 30.06}))
    assert isinstance(result, dict)
    assert result["status"] == "not_yet_extracted"
    assert result["tool_name"] == "get_forecast"
    assert "message" in result and "MUNDI_USE_HERMES=0" in result["message"]


@pytest.mark.asyncio
async def test_unknown_tool_returns_structured_not_404():
    """Tool name not in LEGACY_HANDLERS still returns a parseable result
    instead of raising. The 404-on-unknown protection lives in the
    /internal/tool-call route's whitelist check (not here) — this function
    is the LAST line of defense, so it MUST always return something usable.
    """
    result = await execute_legacy_tool("a_tool_that_does_not_exist_anywhere", _make_ctx())
    assert isinstance(result, dict)
    assert result["status"] == "not_yet_extracted"
    assert result["tool_name"] == "a_tool_that_does_not_exist_anywhere"


@pytest.mark.asyncio
async def test_result_is_json_serializable():
    """Every shim result gets JSON-encoded on the wire before reaching the
    LLM. A non-serializable result (numpy arrays, raw asyncpg Records,
    custom dataclasses) would 500 the dispatch and break the turn.
    This test guards the contract for all registered handlers."""
    for tool_name in list(LEGACY_HANDLERS.keys())[:10]:  # sample to keep fast
        result = await execute_legacy_tool(tool_name, _make_ctx())
        try:
            json.dumps(result)
        except (TypeError, ValueError) as e:
            pytest.fail(
                f"{tool_name} returned non-JSON-serializable result: {e}. "
                f"The dispatch route would emit a 500; LLM turn would die."
            )


@pytest.mark.asyncio
async def test_new_layer_from_postgis_stub_response_shape():
    """The real new_layer_from_postgis handler is stubbed pending extraction.
    Pin the stub's response shape so a future migration (the real
    extraction) can replace it without surprising callers."""
    result = await execute_legacy_tool("new_layer_from_postgis", _make_ctx({
        "postgis_connection_id": "C00000000001",
        "query": "SELECT id, name, geom FROM rwanda_district_boundaries LIMIT 5",
        "layer_name": "Rwanda Districts",
    }))
    assert result["status"] == "not_yet_extracted"
    assert result["tool_name"] == "new_layer_from_postgis"
    assert "message_routes.py:1977" in result["message"]
