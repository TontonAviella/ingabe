from __future__ import annotations

import asyncio
import time
import duckdb
import json
import re
import os
from fastapi import HTTPException, status

from src.fs_lru import layer_cache

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
    cache = layer_cache()
    # Acquire cached geopackage path in async context
    async with cache.layer_filename(layer_id) as gpkg_path:

        def query_func():
            con = duckdb.connect(":memory:")
            con.execute("SET memory_limit='256MB';")
            con.execute("SET threads=1;")
            con.install_extension("spatial")
            con.load_extension("spatial")

            try:
                # Create table from cached geopackage file
                con.execute(f"""
                    CREATE OR REPLACE TABLE {layer_id} AS
                    SELECT * FROM ST_Read('{gpkg_path}');
                """)

                cursor = con.execute(sql_query)
                headers = [col[0] for col in cursor.description]
                rows = cursor.fetchall()[:max_n_rows]
                result_json = json.loads(json.dumps(rows))

                return {
                    "status": "success",
                    "duration_ms": 1000 * (time.time() - start_time),
                    "result": result_json,
                    "headers": headers,
                    "row_count": len(rows),
                    "query": sql_query,
                }
            finally:
                con.close()

        loop = asyncio.get_running_loop()
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
