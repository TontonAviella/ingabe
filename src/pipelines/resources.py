"""Dagster resources wrapping existing Mundi.ai clients.

These resources provide Dagster-compatible interfaces to the Postgres and
DuckDB clients used by the Rwanda pre-compute assets.
"""

import os
from contextlib import contextmanager
from typing import Any

import asyncpg
import duckdb
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


class DuckDBResource(ConfigurableResource):
    """Dagster resource for DuckDB operations.

    DuckDB is used for analytical queries and data transformations.
    Each operation gets a fresh in-memory or persistent database connection.
    """

    database_path: str = Field(default=":memory:", description="DuckDB database path")
    read_only: bool = Field(default=False, description="Open database in read-only mode")

    @contextmanager
    def get_connection(self):
        """Get a DuckDB connection."""
        conn = duckdb.connect(database=self.database_path, read_only=self.read_only)
        try:
            # Configure DuckDB for S3 access — each SET must be a separate
            # execute() call because DuckDB only supports prepared parameters
            # on the last statement in a batch.
            s3_endpoint = os.environ.get("S3_ENDPOINT_URL", "minio:9000").replace("http://", "")
            s3_key = os.environ.get("S3_ACCESS_KEY_ID", "s3user")
            s3_secret = os.environ.get("S3_SECRET_ACCESS_KEY", "backup123")
            s3_region = os.environ.get("S3_DEFAULT_REGION", "us-east-1")
            conn.execute(f"SET s3_endpoint = '{s3_endpoint}'")
            conn.execute(f"SET s3_access_key_id = '{s3_key}'")
            conn.execute(f"SET s3_secret_access_key = '{s3_secret}'")
            conn.execute(f"SET s3_region = '{s3_region}'")
            yield conn
        finally:
            conn.close()

    def query(self, sql: str) -> Any:
        """Execute a query and return results."""
        with self.get_connection() as conn:
            return conn.execute(sql).fetchall()
