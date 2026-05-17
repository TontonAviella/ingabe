"""Tests for the legacy tool shim — the bridge between /internal/tool-call
and the inline elif handlers in message_routes.py.

Each test names the contract it pins down. The shim's invariant is "never
raises, always returns a JSON-serializable dict the LLM can read."
"""
from __future__ import annotations

import json
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from src.services.legacy_tool_shim import (
    LEGACY_HANDLERS,
    LegacyToolContext,
    execute_legacy_tool,
)


def _make_ctx(arguments: dict[str, Any] | None = None) -> LegacyToolContext:
    """Build a context for tests. The conn is an AsyncMock with sensible
    async-method defaults so handlers that hit the DB return None (=
    "no row found", = "owner not found", = error path) without raising
    a `TypeError: object MagicMock can't be used in 'await' expression`.

    Tests that need specific DB return values override `ctx.conn.fetchrow`
    etc. with their own AsyncMock(return_value=...).
    """
    conn = MagicMock()
    conn.fetchrow = AsyncMock(return_value=None)
    conn.fetchval = AsyncMock(return_value=None)
    conn.fetch = AsyncMock(return_value=[])
    conn.execute = AsyncMock(return_value=None)
    return LegacyToolContext(
        user_id="user-test-aaa",
        partner_id="partner-test-bbb",
        conversation_id=42,
        map_id="MTESTAAAAAAA",
        project_id="PTESTBBBBBBB",
        conn=conn,
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
    whether to apologize, retry, or give up. We pick a tool we know is
    still on the not-yet-extracted list (a QGIS-processing tool — they're
    all still stubs since the qgis-processing sidecar dispatch hasn't
    been ported yet)."""
    result = await execute_legacy_tool("native_buffer", _make_ctx({}))
    assert isinstance(result, dict)
    assert result["status"] == "not_yet_extracted"
    assert result["tool_name"] == "native_buffer"
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
async def test_add_layer_to_map_rejects_missing_args():
    """add_layer_to_map needs both layer_id AND new_name. Missing either
    returns a structured error without raising or touching the DB."""
    result = await execute_legacy_tool("add_layer_to_map", _make_ctx({}))
    assert result["status"] == "error"
    assert "Missing required parameters" in result["error"]

    result2 = await execute_legacy_tool("add_layer_to_map", _make_ctx({
        "layer_id": "Labcd1234abcd",  # missing new_name
    }))
    assert result2["status"] == "error"
    assert "Missing required parameters" in result2["error"]


@pytest.mark.asyncio
async def test_set_layer_style_rejects_missing_args():
    """set_layer_style needs both layer_id AND maplibre_json_layers_str."""
    result = await execute_legacy_tool("set_layer_style", _make_ctx({}))
    assert result["status"] == "error"
    assert "Missing required parameters" in result["error"]


@pytest.mark.asyncio
async def test_set_layer_style_rejects_invalid_json():
    """The maplibre_json_layers_str argument must be valid JSON. If the
    LLM emits garbage, fail fast with a status=error result instead of
    propagating the JSONDecodeError as a 500."""
    result = await execute_legacy_tool("set_layer_style", _make_ctx({
        "layer_id": "Labcd1234abcd",
        "maplibre_json_layers_str": "not actually json {{{",
    }))
    assert result["status"] == "error"
    assert "Invalid JSON format" in result["error"]
    assert result["layer_id"] == "Labcd1234abcd"


@pytest.mark.asyncio
async def test_zonal_statistics_rejects_missing_args():
    """Both raster_layer_id and zones_layer_id are required."""
    result = await execute_legacy_tool("zonal_statistics", _make_ctx({}))
    assert result["status"] == "error"
    assert "Missing required parameters" in result["error"]


@pytest.mark.asyncio
async def test_reverse_geocode_requires_coords():
    """lat and lon are required. Missing either should return a clean error
    before opening any DB connection."""
    result = await execute_legacy_tool("reverse_geocode_coordinates", _make_ctx({}))
    assert result["status"] == "error"
    assert "lat and lon are required" in result["error"]

    result2 = await execute_legacy_tool(
        "reverse_geocode_coordinates", _make_ctx({"lat": -1.9})
    )
    assert result2["status"] == "error"


@pytest.mark.asyncio
async def test_query_postgis_database_requires_limit_clause():
    """query_postgis_database hard-blocks queries without an explicit LIMIT
    clause. Prevents accidental million-row pulls that would OOM the worker
    OR flood the LLM context window."""
    # Need to set up the connection lookup to succeed first. Mock the conn
    # to return a result for the connection_uri check.
    from unittest.mock import AsyncMock
    ctx = _make_ctx({
        "postgis_connection_id": "C00000000001",
        "sql_query": "SELECT * FROM districts",  # no LIMIT
    })
    ctx.conn.fetchrow = AsyncMock(return_value={"connection_uri": "postgresql://..."})

    result = await execute_legacy_tool("query_postgis_database", ctx)
    assert result["status"] == "error"
    assert "LIMIT clause" in result["error"]


@pytest.mark.asyncio
async def test_query_postgis_database_caps_limit_at_1000():
    """LIMIT > 1000 should be rejected as a guard against runaway queries."""
    from unittest.mock import AsyncMock
    ctx = _make_ctx({
        "postgis_connection_id": "C00000000001",
        "sql_query": "SELECT * FROM districts LIMIT 5000",
    })
    ctx.conn.fetchrow = AsyncMock(return_value={"connection_uri": "postgresql://..."})

    result = await execute_legacy_tool("query_postgis_database", ctx)
    assert result["status"] == "error"
    assert "exceeds maximum allowed limit" in result["error"]


@pytest.mark.asyncio
async def test_query_postgis_database_rejects_missing_args():
    """Both postgis_connection_id and sql_query are required."""
    result = await execute_legacy_tool("query_postgis_database", _make_ctx({}))
    assert result["status"] == "error"
    assert "Missing required parameters" in result["error"]


@pytest.mark.asyncio
async def test_new_layer_from_postgis_rejects_missing_args():
    """The real handler (extracted from message_routes.py:1977-2462) must
    validate its 3 required args before touching the DB. Pins the
    fail-fast behavior: missing postgis_connection_id, query, or
    layer_name should return a tool_result with status=error WITHOUT
    raising — same contract as the Pydantic-handler path."""
    # No args at all → missing postgis_connection_id should fire first.
    result = await execute_legacy_tool("new_layer_from_postgis", _make_ctx({}))
    assert isinstance(result, dict)
    assert result["status"] == "error"
    assert "Missing required parameters" in result["error"]

    # Only postgis_connection_id, missing query → same error path.
    result2 = await execute_legacy_tool("new_layer_from_postgis", _make_ctx({
        "postgis_connection_id": "C00000000001",
    }))
    assert result2["status"] == "error"
    assert "Missing required parameters" in result2["error"]
