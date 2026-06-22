from __future__ import annotations

import asyncio
import time
import duckdb
import hashlib
import json
import re
import os
import tempfile
from contextlib import asynccontextmanager
from fastapi import HTTPException, status

from src.fs_lru import FileCache, layer_cache
from src.structures import get_async_read_connection
from src.utils import get_async_s3_client, get_bucket_name

DUCKDB_RESERVED_KEYWORDS = {
    "select",
    "from",
    "where",
    "table",
    "group",
    "order",
    "insert",
    "update",
    "delete",
    "join",
    "on",
    "into",
    "and",
    "or",
    "not",
    "as",
    "by",
    "limit",
    "offset",
    "union",
    "distinct",
    "case",
    "when",
    "then",
    "else",
    "end",
    "create",
    "drop",
    "alter",
    "null",
    "is",
    "in",
    "like",
    "having",
}


def _geoparquet_cache() -> FileCache:
    cache_dir = os.environ.get(
        "GEOPARQUET_CACHE_DIR",
        os.environ.get("LAYER_CACHE_DIR", "/cache"),
    )
    max_size = int(os.environ.get("GEOPARQUET_CACHE_MAX_BYTES", 512 * 1024 * 1024))
    global _GEOPARQUET_CACHE_SINGLETON
    try:
        return _GEOPARQUET_CACHE_SINGLETON
    except NameError:
        _GEOPARQUET_CACHE_SINGLETON = FileCache(cache_dir=cache_dir, max_size=max_size)
        return _GEOPARQUET_CACHE_SINGLETON


def _sql_literal(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


def _quoted_identifier(name: str) -> str:
    return '"' + name.replace('"', '""') + '"'


def _metadata_dict(raw_metadata) -> dict:
    if isinstance(raw_metadata, dict):
        return raw_metadata
    if isinstance(raw_metadata, str):
        try:
            parsed = json.loads(raw_metadata)
            return parsed if isinstance(parsed, dict) else {}
        except json.JSONDecodeError:
            return {}
    return {}


def _metadata_geoparquet_key(raw_metadata) -> str | None:
    metadata = _metadata_dict(raw_metadata)
    geoparquet_key = metadata.get("geoparquet_key")
    if isinstance(geoparquet_key, str) and geoparquet_key.strip():
        return geoparquet_key
    return None


def _json_default(value):
    if isinstance(value, (bytes, bytearray, memoryview)):
        return bytes(value).hex()
    return str(value)


def quoted_col_for(name: str) -> str:
    if not name:
        return '"{}"'.format(name)

    # If it's not a valid unquoted identifier, quote it
    if (
        not re.match(r"^[a-z_][a-z0-9_]*$", name)  # Valid unquoted SQL identifier
        or name.lower() in DUCKDB_RESERVED_KEYWORDS  # Reserved keyword
        or any(c.isupper() for c in name)  # Mixed/capital case
    ):
        return f'"{name}"'

    return name


async def _layer_metadata(layer_id: str) -> dict:
    async with get_async_read_connection() as conn:
        row = await conn.fetchrow(
            "SELECT metadata FROM map_layers WHERE layer_id = $1",
            layer_id,
        )
    if not row:
        return {}
    return _metadata_dict(row["metadata"])


def _geoparquet_cache_key(layer_id: str, geoparquet_key: str) -> str:
    digest = hashlib.sha256(geoparquet_key.encode("utf-8")).hexdigest()[:16]
    return f"{layer_id}-{digest}.parquet"


async def _ensure_geoparquet_cached(layer_id: str, geoparquet_key: str) -> str:
    cache = _geoparquet_cache()
    cache_key = _geoparquet_cache_key(layer_id, geoparquet_key)
    if not cache.has(cache_key):
        s3 = await get_async_s3_client()
        with tempfile.TemporaryDirectory() as temp_dir:
            local_path = os.path.join(temp_dir, cache_key)
            await s3.download_file(get_bucket_name(), geoparquet_key, local_path)
            cache.set_from_file(cache_key, local_path)
    return cache_key


@asynccontextmanager
async def _geoparquet_layer_filename(layer_id: str, geoparquet_key: str):
    cache = _geoparquet_cache()
    cache_key = await _ensure_geoparquet_cached(layer_id, geoparquet_key)
    cache.lock(cache_key)
    try:
        yield cache.get_path(cache_key)
    finally:
        cache.unlock(cache_key)


def _run_duckdb_query_from_path(
    *,
    sql_query: str,
    layer_id: str,
    analytics_path: str,
    source_format: str,
    start_time: float,
    max_n_rows: int,
) -> dict:
    con = duckdb.connect(":memory:")
    con.execute("SET memory_limit='256MB';")
    con.execute("SET threads=1;")
    con.execute("SET home_directory='/cache';")
    con.install_extension("spatial")
    con.load_extension("spatial")

    try:
        layer_identifier = _quoted_identifier(layer_id)
        path_literal = _sql_literal(analytics_path)
        if source_format == "geoparquet":
            con.execute(f"""
                CREATE OR REPLACE VIEW {layer_identifier} AS
                SELECT * FROM read_parquet({path_literal});
            """)
        else:
            con.execute(f"""
                CREATE OR REPLACE TABLE {layer_identifier} AS
                SELECT * FROM ST_Read({path_literal});
            """)

        cursor = con.execute(sql_query)
        headers = [col[0] for col in cursor.description]
        rows = cursor.fetchall()[:max_n_rows]
        result_json = json.loads(json.dumps(rows, default=_json_default))

        return {
            "status": "success",
            "duration_ms": 1000 * (time.time() - start_time),
            "result": result_json,
            "headers": headers,
            "row_count": len(rows),
            "query": sql_query,
            "source_format": source_format,
        }
    finally:
        con.close()


def get_lakehouse_connection() -> duckdb.DuckDBPyConnection:
    """Create a DuckDB connection with spatial and iceberg extensions loaded.

    Configures S3 credentials for MinIO access. This connection can be used
    for both spatial queries and Iceberg table operations.

    Returns:
        DuckDB connection with spatial and iceberg extensions enabled.
    """
    con = duckdb.connect(":memory:")
    # Cap DuckDB memory to avoid OOM (standard plan=2GB, shared with app+pools)
    con.execute("SET memory_limit='256MB';")
    con.execute("SET threads=1;")
    # Container's appuser has HOME=/home/appuser but no such dir; point DuckDB
    # at /cache (created+chowned in Dockerfile) so install_extension can write.
    con.execute("SET home_directory='/cache';")

    # Load extensions (install is a no-op if already cached on disk from Dockerfile)
    con.install_extension("spatial")
    con.load_extension("spatial")

    con.install_extension("iceberg")
    con.load_extension("iceberg")

    # Configure S3 credentials for MinIO
    s3_endpoint = os.environ.get("S3_ENDPOINT_URL", "http://minio:9000")
    s3_access_key = os.environ.get("S3_ACCESS_KEY_ID", "")
    s3_secret_key = os.environ.get("S3_SECRET_ACCESS_KEY", "")
    s3_region = os.environ.get("S3_DEFAULT_REGION", "us-east-1")

    # DuckDB S3 configuration
    con.execute(f"SET s3_endpoint='{s3_endpoint}';")
    con.execute(f"SET s3_access_key_id='{s3_access_key}';")
    con.execute(f"SET s3_secret_access_key='{s3_secret_key}';")
    con.execute(f"SET s3_region='{s3_region}';")
    con.execute("SET s3_use_ssl=false;")
    con.execute("SET s3_url_style='path';")

    return con


async def execute_duckdb_query(
    sql_query: str, layer_id: str, max_n_rows: int = 25, timeout: int = 30
):
    start_time = time.time()
    metadata = await _layer_metadata(layer_id)
    geoparquet_key = _metadata_geoparquet_key(metadata)
    source_format = "geoparquet" if geoparquet_key else "geopackage"

    path_context = (
        _geoparquet_layer_filename(layer_id, geoparquet_key)
        if geoparquet_key
        else layer_cache().layer_filename(layer_id)
    )

    async with path_context as analytics_path:
        loop = asyncio.get_running_loop()

        def query_func():
            return _run_duckdb_query_from_path(
                sql_query=sql_query,
                layer_id=layer_id,
                analytics_path=analytics_path,
                source_format=source_format,
                start_time=start_time,
                max_n_rows=max_n_rows,
            )

        try:
            return await asyncio.wait_for(
                loop.run_in_executor(None, query_func), timeout=timeout
            )
        except asyncio.TimeoutError:
            raise HTTPException(
                status_code=status.HTTP_504_GATEWAY_TIMEOUT,
                detail=f"DuckDB query timed out after {timeout} seconds",
            )
        except HTTPException:
            raise
        except Exception as e:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"DuckDB query failed: {e}",
            )
