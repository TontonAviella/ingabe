"""Async connection pool management for the primary (write) and read-replica databases.

All runtime database access should use the helpers exported here:

* ``get_async_db_connection()`` — write connection
* ``get_async_read_connection()`` — read-only connection (replica or primary)
* ``async_conn(span_name)`` — write connection with OpenTelemetry span
* ``async_read_conn(span_name)`` — read connection with OpenTelemetry span
"""

from __future__ import annotations

import asyncio
import os
import sys
from typing import Optional

import asyncpg
from opentelemetry import trace

IS_RUNNING_PYTEST = "pytest" in sys.modules or "PYTEST_CURRENT_TEST" in os.environ

_async_connection_pool: Optional[asyncpg.Pool] = None
_async_pool_lock = asyncio.Lock()

_async_read_pool: Optional[asyncpg.Pool] = None
_async_read_pool_lock = asyncio.Lock()

tracer = trace.get_tracer(__name__)


# ---------------------------------------------------------------------------
# DSN builder
# ---------------------------------------------------------------------------

def _build_postgres_url(host: Optional[str] = None, port: Optional[str] = None) -> str:
    """Build a PostgreSQL DSN from environment variables."""
    user = os.environ["POSTGRES_USER"]
    password = os.environ["POSTGRES_PASSWORD"]
    h = host or os.environ["POSTGRES_HOST"]
    p = port or os.environ.get("POSTGRES_PORT", "5432")
    db = os.environ["POSTGRES_DB"]
    return f"postgresql://{user}:{password}@{h}:{p}/{db}"


# ---------------------------------------------------------------------------
# Pool getters (double-checked locking)
# ---------------------------------------------------------------------------

async def _get_async_connection_pool() -> asyncpg.Pool:
    global _async_connection_pool
    if _async_connection_pool is None:
        async with _async_pool_lock:
            if _async_connection_pool is None:
                _async_connection_pool = await asyncpg.create_pool(
                    dsn=_build_postgres_url(),
                    min_size=1,
                    max_size=10,
                )
    return _async_connection_pool  # type: ignore[return-value]


async def _get_async_read_pool() -> asyncpg.Pool:
    """Return the read-replica pool, or the primary pool when no replica is configured."""
    read_host = os.environ.get("POSTGRES_READ_HOST")
    if not read_host:
        return await _get_async_connection_pool()

    global _async_read_pool
    if _async_read_pool is None:
        async with _async_read_pool_lock:
            if _async_read_pool is None:
                read_port = os.environ.get(
                    "POSTGRES_READ_PORT",
                    os.environ.get("POSTGRES_PORT", "5432"),
                )
                _async_read_pool = await asyncpg.create_pool(
                    dsn=_build_postgres_url(host=read_host, port=read_port),
                    min_size=1,
                    max_size=10,
                )
    return _async_read_pool  # type: ignore[return-value]


# ---------------------------------------------------------------------------
# Connection context managers
# ---------------------------------------------------------------------------

class AsyncDatabaseConnection:
    """Context-manager that yields an *exclusive* asyncpg connection.

    Set ``readonly=True`` to route to the read replica when configured.
    """

    def __init__(self, span_name: Optional[str] = None, readonly: bool = False):
        self.conn: Optional[asyncpg.Connection] = None
        self.span: Optional[trace.Span] = None
        self.span_name: Optional[str] = span_name
        self.readonly: bool = readonly

    async def __aenter__(self) -> asyncpg.Connection:
        current_span = trace.get_current_span()
        if current_span.is_recording():
            self.span = tracer.start_span(self.span_name or "asyncpg")

        if IS_RUNNING_PYTEST:
            self.conn = await asyncpg.connect(_build_postgres_url())
        else:
            if self.readonly:
                pool = await _get_async_read_pool()
            else:
                pool = await _get_async_connection_pool()
            self.conn = await pool.acquire()
        return self.conn

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        if self.conn is not None:
            if IS_RUNNING_PYTEST:
                await self.conn.close()
            else:
                if self.readonly:
                    pool = await _get_async_read_pool()
                else:
                    pool = await _get_async_connection_pool()
                await pool.release(self.conn)
        if self.span:
            self.span.end()


# ---------------------------------------------------------------------------
# Public convenience helpers
# ---------------------------------------------------------------------------

def get_async_db_connection() -> AsyncDatabaseConnection:
    """Return a write connection to the primary database."""
    return AsyncDatabaseConnection()


def get_async_read_connection() -> AsyncDatabaseConnection:
    """Return a read-only connection routed to the replica (or primary)."""
    return AsyncDatabaseConnection(readonly=True)


def async_conn(span_name: Optional[str] = None) -> AsyncDatabaseConnection:
    """Write connection with OpenTelemetry span."""
    return AsyncDatabaseConnection(f"pg {span_name}")


def async_read_conn(span_name: Optional[str] = None) -> AsyncDatabaseConnection:
    """Read-only connection with OpenTelemetry span, routed to the replica."""
    return AsyncDatabaseConnection(f"pg:ro {span_name}", readonly=True)
