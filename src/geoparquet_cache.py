"""Local cache of uploaded layers' GeoParquet files.

The layer describer reads a layer's GeoParquet copy with DuckDB; this module
downloads it from object storage once and keeps it in a size-bounded cache.
(It was src/duckdb.py, a name that shadowed the duckdb package under pytest.)
"""

from __future__ import annotations

import hashlib
import os
import tempfile
from contextlib import asynccontextmanager

from src.fs_lru import FileCache
from src.utils import get_async_s3_client, get_bucket_name


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
async def geoparquet_layer_filename(layer_id: str, geoparquet_key: str):
    cache = _geoparquet_cache()
    cache_key = await _ensure_geoparquet_cached(layer_id, geoparquet_key)
    cache.lock(cache_key)
    try:
        yield cache.get_path(cache_key)
    finally:
        cache.unlock(cache_key)
