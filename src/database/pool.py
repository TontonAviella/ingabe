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
    from urllib.parse import quote_plus

    user = os.environ["POSTGRES_USER"]
    password = quote_plus(os.environ["POSTGRES_PASSWORD"])
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
                _pool_max = int(os.environ.get("DB_POOL_MAX_SIZE", "25"))
                _async_connection_pool = await asyncpg.create_pool(
                    dsn=_build_postgres_url(),
                    min_size=2,
                    max_size=_pool_max,
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
                _read_pool_max = int(os.environ.get("DB_READ_POOL_MAX_SIZE", "25"))
                _async_read_pool = await asyncpg.create_pool(
                    dsn=_build_postgres_url(host=read_host, port=read_port),
                    min_size=2,
                    max_size=_read_pool_max,
                )
    return _async_read_pool  # type: ignore[return-value]


# ---------------------------------------------------------------------------
# Connection context managers
# ---------------------------------------------------------------------------

class AsyncDatabaseConnection:
    """Context-manager that yields an *exclusive* asyncpg connection.

    Set ``readonly=True`` to route to the read replica when configured.
    """

    def __init__(
        self,
        span_name: Optional[str] = None,
        readonly: bool = False,
        user_id: Optional[str] = None,
        partner_id: Optional[str] = None,
        role: Optional[str] = None,
    ):
        self.conn: Optional[asyncpg.Connection] = None
        self._pool: Optional[asyncpg.Pool] = None
        self.span: Optional[trace.Span] = None
        self.span_name: Optional[str] = span_name
        self.readonly: bool = readonly
        self.user_id: Optional[str] = user_id
        self.partner_id: Optional[str] = partner_id
        self.role: Optional[str] = role

    async def __aenter__(self) -> asyncpg.Connection:
        current_span = trace.get_current_span()
        if current_span.is_recording():
            self.span = tracer.start_span(self.span_name or "asyncpg")

        if IS_RUNNING_PYTEST:
            self.conn = await asyncpg.connect(_build_postgres_url())
        else:
            if self.readonly:
                self._pool = await _get_async_read_pool()
            else:
                self._pool = await _get_async_connection_pool()
            self.conn = await self._pool.acquire()

        # Set RLS context so row-level security policies can filter by user.
        # is_local=false (session-level) so settings persist across implicit
        # transactions in asyncpg. With true the value is lost after execute().
        if self.user_id:
            await self.conn.execute(
                "SELECT set_config('app.user_id', $1, false)", self.user_id
            )
        if self.partner_id:
            await self.conn.execute(
                "SELECT set_config('app.partner_id', $1, false)", self.partner_id
            )
        if self.role:
            await self.conn.execute(
                "SELECT set_config('app.role', $1, false)", self.role
            )

        return self.conn

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        if self.conn is not None:
            # Always reset ALL GUCs before returning to pool, regardless of
            # which ones this request set. A previous request may have set
            # partner_id on this connection; leaving it is a data leak.
            if not IS_RUNNING_PYTEST:
                try:
                    await self.conn.execute(
                        "RESET app.user_id; RESET app.partner_id; RESET app.role"
                    )
                except Exception:
                    pass  # Connection may already be broken
            if IS_RUNNING_PYTEST:
                await self.conn.close()
            else:
                await self._pool.release(self.conn)
        if self.span:
            self.span.end()


# ---------------------------------------------------------------------------
# Public convenience helpers
# ---------------------------------------------------------------------------

def get_async_db_connection(
    user_id: Optional[str] = None,
    partner_id: Optional[str] = None,
    role: Optional[str] = None,
) -> AsyncDatabaseConnection:
    """Return a write connection to the primary database."""
    return AsyncDatabaseConnection(
        user_id=user_id, partner_id=partner_id, role=role,
    )


def get_async_read_connection(
    user_id: Optional[str] = None,
    partner_id: Optional[str] = None,
    role: Optional[str] = None,
) -> AsyncDatabaseConnection:
    """Return a read-only connection routed to the replica (or primary)."""
    return AsyncDatabaseConnection(
        readonly=True, user_id=user_id, partner_id=partner_id, role=role,
    )


def async_conn(
    span_name: Optional[str] = None,
    user_id: Optional[str] = None,
    partner_id: Optional[str] = None,
    role: Optional[str] = None,
) -> AsyncDatabaseConnection:
    """Write connection with OpenTelemetry span."""
    return AsyncDatabaseConnection(
        f"pg {span_name}", user_id=user_id, partner_id=partner_id, role=role,
    )


def async_read_conn(
    span_name: Optional[str] = None,
    user_id: Optional[str] = None,
    partner_id: Optional[str] = None,
    role: Optional[str] = None,
) -> AsyncDatabaseConnection:
    """Read-only connection with OpenTelemetry span, routed to the replica."""
    return AsyncDatabaseConnection(
        f"pg:ro {span_name}", readonly=True,
        user_id=user_id, partner_id=partner_id, role=role,
    )


# ---------------------------------------------------------------------------
# Synchronous connection (for thread-pool work in FastAPI endpoints)
# ---------------------------------------------------------------------------

from contextlib import contextmanager


@contextmanager
def get_sync_db_connection(
    user_id: Optional[str] = None,
    partner_id: Optional[str] = None,
    role: Optional[str] = None,
):
    """Yield a synchronous psycopg2 connection using the same env vars as the async pool.

    Intended for ``run_in_executor`` blocks where async connections are unavailable.
    The connection is auto-closed on exit.
    """
    import psycopg2

    conn = psycopg2.connect(dsn=_build_postgres_url())
    try:
        with conn.cursor() as cur:
            if user_id:
                cur.execute("SELECT set_config('app.user_id', %s, false)", (user_id,))
            if partner_id:
                cur.execute("SELECT set_config('app.partner_id', %s, false)", (partner_id,))
            if role:
                cur.execute("SELECT set_config('app.role', %s, false)", (role,))
            conn.commit()
        yield conn
    finally:
        conn.close()
