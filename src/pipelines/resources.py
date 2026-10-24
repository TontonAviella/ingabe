"""Dagster resources wrapping existing Mundi.ai clients.

These resources provide Dagster-compatible interfaces to the Postgres client used by the Rwanda pre-compute
assets (every cache table lives in PostgreSQL; the DuckDB resource was removed 2026-10-25).
"""

import os
from contextlib import contextmanager
from typing import Any

import asyncpg
from dagster import ConfigurableResource
from pydantic import Field


class PostgresResource(ConfigurableResource):
    """Dagster resource for PostgreSQL operations.

    Wraps the existing asyncpg connection configuration.
    Provides both sync and async connection methods.
    """

    host: str = Field(description="PostgreSQL host")
    port: int = Field(default=5432, description="PostgreSQL port")
    database: str = Field(description="Database name")
    user: str = Field(description="Database user")
    password: str = Field(description="Database password")

    @classmethod
    def from_env(cls) -> "PostgresResource":
        """Create resource from environment variables."""
        return cls(
            host=os.environ.get("POSTGRES_HOST", "postgresdb"),
            port=int(os.environ.get("POSTGRES_PORT", "5432")),
            database=os.environ.get("POSTGRES_DB", "mundidb"),
            user=os.environ.get("POSTGRES_USER", "mundiuser"),
            password=os.environ.get("POSTGRES_PASSWORD", "gdalpassword"),
        )

    def get_connection_string(self) -> str:
        """Get PostgreSQL connection string."""
        return f"postgresql://{self.user}:{self.password}@{self.host}:{self.port}/{self.database}"

    @contextmanager
    def get_sync_connection(self):
        """Get a synchronous psycopg2 connection (for Dagster ops)."""
        import psycopg2

        conn = psycopg2.connect(
            host=self.host,
            port=self.port,
            database=self.database,
            user=self.user,
            password=self.password,
        )
        try:
            yield conn
        finally:
            conn.close()

    async def get_async_connection(self) -> asyncpg.Connection:
        """Get an async connection (for wrapping async code)."""
        return await asyncpg.connect(
            host=self.host,
            port=self.port,
            database=self.database,
            user=self.user,
            password=self.password,
        )

    def execute_query(self, query: str, params: tuple = ()) -> list[tuple]:
        """Execute a query and return results (sync version)."""
        with self.get_sync_connection() as conn:
            with conn.cursor() as cur:
                cur.execute(query, params)
                if cur.description:
                    return cur.fetchall()
                return []
