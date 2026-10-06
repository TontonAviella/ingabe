"""Tests for the legacy tool shim — the bridge between /internal/tool-call
and the inline elif handlers in message_routes.py.

Each test names the contract it pins down. The shim's invariant is "never
raises, always returns a JSON-serializable dict the LLM can read."
"""
from __future__ import annotations

import json
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import numpy as np
import pytest

from src.services import isdasoil_service
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
async def test_registry_includes_core_legacy_names():
    """Whitelist sanity: every non-Pydantic tool Sage can call must be in
    LEGACY_HANDLERS so /internal/tool-call's whitelist check accepts it;
    a missing name returns 404 and the Hermes turn silently breaks."""
    must_have_names = {
        # Hardcoded in message_routes.py (no tools.json or pydantic schema)
        "new_layer_from_postgis", "set_layer_style", "add_layer_to_map",
        "reverse_geocode_coordinates",
        # tools.json schemas
        "get_forecast", "get_field_health", "get_ndvi_stats", "search_brain",
        "get_insurance_intelligence",
    }
    missing = must_have_names - set(LEGACY_HANDLERS.keys())
    assert not missing, (
        f"LEGACY_HANDLERS is missing {len(missing)} tool name(s): {sorted(missing)}. "
        f"/internal/tool-call would return 404 when the Hermes plugin invokes them."
    )


@pytest.mark.asyncio
async def test_retired_tools_are_not_registered():
    """Tools retired from the MVP (2026-10-05) must not be callable through
    Hermes either: SQL/QGIS tools for GIS teams, and tools whose answers were
    misleading (crop guesses, a crop confirmation that saved nothing)."""
    retired = {
        "query_duckdb_sql", "query_postgis_database", "zonal_statistics",
        "identify_parcel_crop", "confirm_crop_prediction", "get_crop_classifications",
        "get_emissions_stats", "create_management_zones", "create_prescription_map",
        "create_soil_sampling_plan", "query_rwanda_zonal_stats",
        "native_buffer", "qgis_clip", "gdal_warpreproject",
    }
    assert not retired & set(LEGACY_HANDLERS.keys())


@pytest.mark.asyncio
async def test_all_legacy_handlers_are_real_functions():
    """Every registered handler is a real `_handle_<name>` function."""
    stub_names = [
        name for name, fn in LEGACY_HANDLERS.items()
        if not getattr(fn, "__name__", "").startswith("_handle_")
    ]
    assert stub_names == [], f"These tools are not real handlers: {stub_names}."


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


@pytest.mark.asyncio
async def test_internal_rwanda_connection_only_reads_rwanda_tables():
    """The internal Rwanda connection has no user scope; the shim must refuse
    other tables (they hold every user's rows), as the chat loop does."""
    from src.routes.message_routes import RWANDA_INTERNAL_CONNECTION_NAME, _rwanda_internal_conn_id

    ctx = _make_ctx({
        "postgis_connection_id": _rwanda_internal_conn_id("PTESTBBBBBBB"),
        "query": "SELECT id, geom FROM user_mundiai_maps LIMIT 5",
        "layer_name": "maps",
    })
    ctx.conn.fetchrow = AsyncMock(return_value={
        "connection_uri": "postgresql://internal", "connection_name": RWANDA_INTERNAL_CONNECTION_NAME,
    })
    result = await execute_legacy_tool("new_layer_from_postgis", ctx)
    assert result["status"] == "error"
    assert "user_mundiai_maps" in result["error"]


@pytest.mark.asyncio
async def test_get_soil_properties_gives_sage_the_likely_range(monkeypatch):
    """Sage reads the soil result as the service returns it: phosphorus comes with
    its likely range, not the old "10.59 ± 0.13 ppm", and the result says what the
    range means. Raw band means read at Cyampirita on 2026-10-06."""
    monkeypatch.setattr(isdasoil_service, "_read_point",
                        lambda url, lon, lat, buffer_m=150.0: np.array([24.5, 23.53, 1.21, 1.19]))
    result = await execute_legacy_tool("get_soil_properties", _make_ctx({
        "longitude": 30.4245, "latitude": -1.6969, "properties": ["phosphorous_extractable"],
    }))
    phosphorus = result["properties"]["phosphorous_extractable"]
    assert (phosphorus["value"], phosphorus["likely_range"]) == (10.59, [9.27, 12.08])
    assert "uncertainty" not in phosphorus
    assert "68%" in result["spread_note"]
    json.dumps(result)
