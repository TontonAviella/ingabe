"""Result checks for Sage: make a failed tool result correctable.

A tool error tells the model what went wrong but rarely the fact it needs to
fix it, so it retries blind or gives up. In local chat history (Feb-Jul 2026,
one user, ~10 conversations: a small sample) PostGIS layer and query calls
failed on guessed column names and on unknown connection ids, and geocoding
missed Rwandan cell names. For each known failure kind this adds
``error_kind``, a ``next_step`` instruction and the facts to act on (the
project's real connection ids, the referenced tables' real columns).
Unrecognized errors and successful results pass through unchanged.

Both Sage paths call ``check_tool_result`` where a tool result is handed back
to the model: the chat loop when it stores a tool message, and the Hermes
tool-call route. Facts come from a ``ResultFacts`` object, so the checks are
testable without a database; ``PostgresResultFacts`` is the real one.
"""
from __future__ import annotations

import json
import logging
import re
from collections.abc import Callable, Mapping
from contextlib import AbstractAsyncContextManager
from typing import Any, Protocol

logger = logging.getLogger(__name__)

KIND_CONNECTION = "connection_not_found"
KIND_COLUMN = "unknown_column"
KIND_GEOCODE = "geocode_miss"
KIND_ROW_LIMIT = "row_limit"

_ERROR_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    (KIND_CONNECTION, re.compile(r"PostGIS connection '[^']*' not found")),
    (KIND_COLUMN, re.compile(r'column "?([\w.]+)"? does not exist', re.IGNORECASE)),
    (KIND_GEOCODE, re.compile(r"^No results for ")),
    (KIND_ROW_LIMIT, re.compile(r"must include a LIMIT clause")),
)
# FROM/JOIN targets in a SQL query: optional schema, optional double quotes.
_TABLE_REF = re.compile(r'\b(?:from|join)\s+((?:"?\w+"?\.)?"?\w+"?)', re.IGNORECASE)
_MAX_TABLES = 5
_MAX_COLUMNS = 80
_MAX_CONNECTIONS = 20


class ResultFacts(Protocol):
    """Where the facts for a repair come from."""

    async def connections(self) -> list[dict[str, str]]:
        """The project's PostGIS connections as ``{"id", "name"}``."""
        ...

    async def table_columns(self, connection_id: str, tables: list[str]) -> dict[str, list[str]]:
        """Column names per referenced table, in table order."""
        ...


def _as_dict(result: Any) -> dict[str, Any] | None:
    if isinstance(result, Mapping):
        return dict(result)
    if isinstance(result, str):
        try:
            parsed = json.loads(result)
        except ValueError:
            return None
        return parsed if isinstance(parsed, dict) else None
    return None


def classify_tool_error(result: Any) -> str | None:
    """The known failure kind of a tool result, or None."""
    parsed = _as_dict(result)
    if not parsed or "next_step" in parsed:
        return None
    error = str(parsed.get("error") or "")
    if not error:
        return None
    for kind, pattern in _ERROR_PATTERNS:
        if pattern.search(error):
            return kind
    return None


def referenced_tables(sql: Any) -> list[str]:
    """Tables a query reads, as written (``schema.table`` or ``table``)."""
    found: list[str] = []
    for match in _TABLE_REF.finditer(str(sql or "")):
        name = match.group(1).replace('"', "")
        if name.lower() not in {"lateral", "unnest", "generate_series"} and name not in found:
            found.append(name)
    return found[:_MAX_TABLES]


async def check_tool_result(
    tool_name: str,
    args: Any,
    result: Any,
    facts: ResultFacts,
    *,
    admin_boundary_tool: str,
) -> dict[str, Any] | None:
    """The result with repair guidance added, or None when there is none to add.

    ``admin_boundary_tool`` is the tool that resolves Rwandan admin units,
    suggested when geocoding misses."""
    kind = classify_tool_error(result)
    if kind is None:
        return None
    out = _as_dict(result) or {}
    out["error_kind"] = kind
    args = args if isinstance(args, Mapping) else {}
    try:
        if kind == KIND_CONNECTION:
            available = (await facts.connections())[:_MAX_CONNECTIONS]
            out["available_connections"] = available
            out["next_step"] = (
                "Call the tool again with one of available_connections' ids exactly as "
                "listed; never invent an id or reuse one from another conversation."
                if available else
                "This project has no PostGIS connection. Tell the user and ask them to "
                "add one; do not invent a connection id."
            )
        elif kind == KIND_COLUMN:
            column = _ERROR_PATTERNS[1][1].search(str(out.get("error")))
            missing = column.group(1) if column else "that column"
            tables = referenced_tables(args.get("query"))
            connection_id = str(args.get("postgis_connection_id") or "")
            columns = (
                await facts.table_columns(connection_id, tables) if tables and connection_id else {}
            )
            if columns:
                out["table_columns"] = columns
                out["next_step"] = (
                    f'Column "{missing}" does not exist. Rewrite the query using only '
                    "the columns listed in table_columns, then call the tool again."
                )
            else:
                out["next_step"] = (
                    f'Column "{missing}" does not exist. Look the table\'s columns up '
                    "first (information_schema.columns, with a LIMIT) instead of guessing."
                )
        elif kind == KIND_GEOCODE:
            out["next_step"] = (
                "The geocoder has no entry for this place. If it is a Rwandan province, "
                f"district, sector, cell or village, use {admin_boundary_tool} instead; "
                "otherwise try a broader place name, or ask the user where it is."
            )
        elif kind == KIND_ROW_LIMIT:
            out["next_step"] = "Add LIMIT n with n below 1000 to the query and call the tool again."
    except Exception:
        logger.warning("result check for %s (%s) could not gather facts", tool_name, kind, exc_info=True)
        out.setdefault("next_step", "Do not repeat the same call; fix the cause in `error` or ask the user.")
    return out


async def apply_result_checks(
    tool_name: str,
    args: Any,
    result: Any,
    *,
    open_conn: Callable[[], AbstractAsyncContextManager[Any]],
    project_id: str,
    user_id: str,
    connection_manager: Any,
    admin_boundary_tool: str,
) -> dict[str, Any] | None:
    """``check_tool_result`` with facts from the database; None leaves the
    result as it is. ``open_conn`` opens an RLS-scoped app connection, and
    only when the result is a known, repairable failure."""
    if classify_tool_error(result) is None:
        return None
    try:
        async with open_conn() as conn:
            facts = PostgresResultFacts(
                conn, project_id=project_id, user_id=user_id, connection_manager=connection_manager
            )
            return await check_tool_result(
                tool_name, args, result, facts, admin_boundary_tool=admin_boundary_tool
            )
    except Exception:
        logger.warning("result check for %s failed; passing the result through", tool_name, exc_info=True)
        return None


class PostgresResultFacts:
    """Facts from the app database and the project's PostGIS connections."""

    def __init__(self, conn: Any, *, project_id: str, user_id: str, connection_manager: Any) -> None:
        # ``conn``: an app-database connection with the caller's RLS scope.
        self._conn = conn
        self._project_id = project_id
        self._user_id = user_id
        self._connection_manager = connection_manager

    async def connections(self) -> list[dict[str, str]]:
        # Never the connection URI: it holds credentials.
        rows = await self._conn.fetch(
            """
            SELECT id, connection_name FROM project_postgres_connections
            WHERE project_id = $1 AND soft_deleted_at IS NULL
            ORDER BY created_at DESC LIMIT $2
            """,
            self._project_id,
            _MAX_CONNECTIONS,
        )
        return [{"id": row["id"], "name": row["connection_name"] or ""} for row in rows]

    async def table_columns(self, connection_id: str, tables: list[str]) -> dict[str, list[str]]:
        # Same access rule the PostGIS tools apply before connecting.
        allowed = await self._conn.fetchval(
            """
            SELECT 1 FROM project_postgres_connections
            WHERE id = $1 AND (user_id = $2 OR project_id = $3) AND soft_deleted_at IS NULL
            """,
            connection_id,
            self._user_id,
            self._project_id,
        )
        if not allowed:
            return {}
        qualified = [t for t in tables if "." in t]
        bare = [t for t in tables if "." not in t]
        pg = await self._connection_manager.connect_to_postgres(connection_id, timeout=5)
        try:
            rows = await pg.fetch(
                """
                SELECT table_schema, table_name, column_name
                FROM information_schema.columns
                WHERE (table_schema || '.' || table_name) = ANY($1::text[])
                   OR (table_name = ANY($2::text[])
                       AND table_schema NOT IN ('pg_catalog', 'information_schema'))
                ORDER BY table_schema, table_name, ordinal_position
                """,
                qualified,
                bare,
            )
        finally:
            await pg.close()
        columns: dict[str, list[str]] = {}
        for row in rows:
            key = f"{row['table_schema']}.{row['table_name']}"
            names = columns.setdefault(key, [])
            if len(names) < _MAX_COLUMNS:
                names.append(row["column_name"])
        return columns
