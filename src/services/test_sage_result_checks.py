"""Sage result checks: failed tool results gain the facts to fix them."""
from __future__ import annotations

import json
from contextlib import asynccontextmanager

import pytest

from src.services import sage_result_checks as rc

ADMIN_TOOL = "show_admin_boundary"


class FakeFacts:
    def __init__(self, connections=None, columns=None, broken: bool = False) -> None:
        self._connections = connections or []
        self._columns = columns or {}
        self._broken = broken
        self.column_requests: list[tuple[str, list[str]]] = []

    async def connections(self):
        if self._broken:
            raise RuntimeError("db down")
        return self._connections

    async def table_columns(self, connection_id, tables):
        self.column_requests.append((connection_id, tables))
        return self._columns


def _err(text: str) -> str:
    return json.dumps({"status": "error", "error": text})


@pytest.mark.parametrize(
    ("result", "kind"),
    [
        (_err("PostGIS connection 'Cv3y6K4T6Y9V' not found or you do not have access to it."), rc.KIND_CONNECTION),
        (_err('Query validation failed: column "cell" does not exist'), rc.KIND_COLUMN),
        ({"status": "error", "error": "No results for 'Karushuga, Rwanda'"}, rc.KIND_GEOCODE),
        (_err("Query must include a LIMIT clause with a value less than 1000"), rc.KIND_ROW_LIMIT),
        (_err("name 'datetime' is not defined"), None),
        (json.dumps({"status": "success", "rows": []}), None),
        ("not json", None),
        ({"status": "error", "error": "No results for 'x'", "next_step": "already guided"}, None),
    ],
)
def test_classify_tool_error(result, kind) -> None:
    assert rc.classify_tool_error(result) == kind


def test_referenced_tables_reads_from_and_join_targets() -> None:
    sql = 'SELECT c.name FROM rwanda.cells c JOIN "admin"."sectors" s ON s.id = c.sector_id, LATERAL x'
    assert rc.referenced_tables(sql) == ["rwanda.cells", "admin.sectors"]
    assert rc.referenced_tables(None) == []


@pytest.mark.asyncio
async def test_unknown_connection_lists_the_projects_real_connections() -> None:
    facts = FakeFacts(connections=[{"id": "CRwAAAA", "name": "Rwanda boundaries"}])
    out = await rc.check_tool_result(
        "new_layer_from_postgis", {"postgis_connection_id": "Cv3y6K4T6Y9V"},
        _err("PostGIS connection 'Cv3y6K4T6Y9V' not found or you do not have access to it."), facts, admin_boundary_tool=ADMIN_TOOL,
    )
    assert out["error_kind"] == rc.KIND_CONNECTION
    assert out["available_connections"] == [{"id": "CRwAAAA", "name": "Rwanda boundaries"}]
    assert "exactly as listed" in out["next_step"]
    assert out["status"] == "error"  # still a failure; only guidance is added


@pytest.mark.asyncio
async def test_no_connections_tells_the_model_not_to_invent_one() -> None:
    out = await rc.check_tool_result(
        "query_postgis_database", {}, _err("PostGIS connection 'X' not found"), FakeFacts(), admin_boundary_tool=ADMIN_TOOL,
    )
    assert out["available_connections"] == []
    assert "no PostGIS connection" in out["next_step"]


@pytest.mark.asyncio
async def test_unknown_column_returns_the_tables_real_columns() -> None:
    facts = FakeFacts(columns={"rwanda.cells": ["cell_name", "sector_name", "geom"]})
    args = {"postgis_connection_id": "CRwAAAA", "query": "SELECT cell, geom FROM rwanda.cells LIMIT 10"}
    out = await rc.check_tool_result(
        "new_layer_from_postgis", args, _err('Query validation failed: column "cell" does not exist'), facts, admin_boundary_tool=ADMIN_TOOL,
    )
    assert facts.column_requests == [("CRwAAAA", ["rwanda.cells"])]
    assert out["table_columns"] == {"rwanda.cells": ["cell_name", "sector_name", "geom"]}
    assert 'Column "cell" does not exist' in out["next_step"]


@pytest.mark.asyncio
async def test_unknown_column_without_a_query_asks_to_look_columns_up() -> None:
    facts = FakeFacts()
    out = await rc.check_tool_result(
        "new_layer_from_postgis", {}, _err('column "id" does not exist'), facts, admin_boundary_tool=ADMIN_TOOL,
    )
    assert facts.column_requests == []
    assert "information_schema.columns" in out["next_step"]


@pytest.mark.asyncio
async def test_geocode_miss_points_to_the_admin_boundary_tool() -> None:
    out = await rc.check_tool_result(
        "search_location", {"query": "Karushuga"}, _err("No results for 'Karushuga, Rwanda'"), FakeFacts(), admin_boundary_tool=ADMIN_TOOL,
    )
    assert ADMIN_TOOL in out["next_step"]


@pytest.mark.asyncio
async def test_missing_facts_still_give_a_next_step() -> None:
    out = await rc.check_tool_result(
        "query_postgis_database", {}, _err("PostGIS connection 'X' not found"), FakeFacts(broken=True), admin_boundary_tool=ADMIN_TOOL,
    )
    assert out["error_kind"] == rc.KIND_CONNECTION
    assert out["next_step"]


@pytest.mark.asyncio
async def test_apply_result_checks_leaves_other_results_alone_without_touching_the_db() -> None:
    def no_db():
        raise AssertionError("must not open a connection for a result it will not change")

    for result in (json.dumps({"status": "success"}), _err("some other failure")):
        assert await rc.apply_result_checks(
            "t", {}, result, open_conn=no_db, project_id="P", user_id="U", connection_manager=None,
            admin_boundary_tool=ADMIN_TOOL,
        ) is None


@pytest.mark.asyncio
async def test_apply_result_checks_passes_through_when_the_db_is_unavailable() -> None:
    @asynccontextmanager
    async def broken():
        raise RuntimeError("pool exhausted")
        yield

    assert await rc.apply_result_checks(
        "t", {}, _err("PostGIS connection 'X' not found"), open_conn=broken,
        project_id="P", user_id="U", connection_manager=None, admin_boundary_tool=ADMIN_TOOL,
    ) is None


class FakeConn:
    def __init__(self) -> None:
        self.queries: list[str] = []

    async def fetch(self, query, *args):
        self.queries.append(query)
        return [{"id": "CRwAAAA", "connection_name": None}]


@pytest.mark.asyncio
async def test_connection_facts_never_select_the_connection_uri() -> None:
    conn = FakeConn()
    facts = rc.PostgresResultFacts(conn, project_id="P", user_id="U", connection_manager=None)
    assert await facts.connections() == [{"id": "CRwAAAA", "name": ""}]
    assert "connection_uri" not in conn.queries[0]
