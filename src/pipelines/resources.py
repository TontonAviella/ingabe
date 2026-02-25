"""Dagster resources wrapping existing Mundi.ai clients.

These resources provide Dagster-compatible interfaces to the existing
S3, Postgres, DuckDB, and Redis clients used throughout the application.
They bridge the sync Dagster world with the async application code.
"""

import asyncio
import os
from contextlib import contextmanager
from typing import Any

import asyncpg
import boto3
import duckdb
import redis
from dagster import ConfigurableResource
from pydantic import Field


class S3Resource(ConfigurableResource):
    """Dagster resource for S3/MinIO operations.

    Wraps the existing boto3 client configuration from src/utils.py.
    """

    endpoint_url: str = Field(description="S3 endpoint URL")
    access_key_id: str = Field(description="S3 access key ID")
    secret_access_key: str = Field(description="S3 secret access key")
    region_name: str = Field(default="us-east-1", description="S3 region")
    bucket_name: str = Field(description="Default S3 bucket name")

    @classmethod
    def from_env(cls) -> "S3Resource":
        """Create resource from environment variables."""
        return cls(
            endpoint_url=os.environ.get("S3_ENDPOINT_URL", "http://minio:9000"),
            access_key_id=os.environ.get("S3_ACCESS_KEY_ID", "s3user"),
            secret_access_key=os.environ.get("S3_SECRET_ACCESS_KEY", "backup123"),
            region_name=os.environ.get("S3_DEFAULT_REGION", "us-east-1"),
            bucket_name=os.environ.get("S3_BUCKET", "test-bucket"),
        )

    @contextmanager
    def get_client(self):
        """Get a boto3 S3 client."""
        config = boto3.session.Config(signature_version="s3v4")
        client = boto3.client(
            "s3",
            endpoint_url=self.endpoint_url,
            aws_access_key_id=self.access_key_id,
            aws_secret_access_key=self.secret_access_key,
            region_name=self.region_name,
            config=config,
        )
        try:
            yield client
        finally:
            pass  # boto3 clients don't need explicit cleanup

    def list_objects(self, prefix: str, max_keys: int = 1000) -> list[dict[str, Any]]:
        """List objects in the bucket with given prefix."""
        with self.get_client() as client:
            response = client.list_objects_v2(
                Bucket=self.bucket_name,
                Prefix=prefix,
                MaxKeys=max_keys,
            )
            return response.get("Contents", [])

    def get_object_metadata(self, key: str) -> dict[str, Any]:
        """Get metadata for an object."""
        with self.get_client() as client:
            return client.head_object(Bucket=self.bucket_name, Key=key)


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


class RedisResource(ConfigurableResource):
    """Dagster resource for Redis operations.

    Wraps the existing Redis client configuration for caching.
    """

    host: str = Field(description="Redis host")
    port: int = Field(default=6379, description="Redis port")
    db: int = Field(default=0, description="Redis database number")

    @classmethod
    def from_env(cls) -> "RedisResource":
        """Create resource from environment variables."""
        return cls(
            host=os.environ.get("REDIS_HOST", "redis"),
            port=int(os.environ.get("REDIS_PORT", "6379")),
        )

    @contextmanager
    def get_client(self):
        """Get a Redis client."""
        client = redis.Redis(host=self.host, port=self.port, db=self.db)
        try:
            yield client
        finally:
            client.close()

    def get(self, key: str) -> bytes | None:
        """Get a value from Redis."""
        with self.get_client() as client:
            return client.get(key)

    def set(self, key: str, value: str | bytes, ex: int | None = None) -> bool:
        """Set a value in Redis with optional expiration."""
        with self.get_client() as client:
            return client.set(key, value, ex=ex)

    def delete(self, *keys: str) -> int:
        """Delete keys from Redis."""
        with self.get_client() as client:
            return client.delete(*keys)


# Helper function to run async code in Dagster ops
def run_async(coro):
    """Run an async coroutine in a sync Dagster op.

    Since Dagster ops are synchronous, this helper allows wrapping
    async functions from the existing codebase.
    """
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        # No event loop running, create a new one
        return asyncio.run(coro)
    else:
        # Event loop already running (shouldn't happen in Dagster)
        # Use run_until_complete as fallback
        return loop.run_until_complete(coro)
