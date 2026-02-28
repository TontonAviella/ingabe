import fcntl
import os
import tempfile
import threading
from collections import OrderedDict
import asyncio
from contextlib import asynccontextmanager
from src.structures import get_async_read_connection
from src.utils import get_async_s3_client, get_bucket_name
from src.database.models import LAYER_TYPE_POSTGIS


class FileCache:
    def __init__(self, cache_dir, max_size):
        os.makedirs(cache_dir, exist_ok=True)
        self.cache_dir, self.max_size = cache_dir, max_size
        self.cache = OrderedDict()  # key -> file size
        self.locked_keys = set()
        self.total = 0
        self._lock_dir = os.path.join(cache_dir, ".locks")
        self._mu = threading.Lock()  # protects cache, total, locked_keys
        os.makedirs(self._lock_dir, exist_ok=True)
        for fn in os.listdir(cache_dir):
            if fn == ".locks":
                continue
            path = os.path.join(cache_dir, fn)
            if os.path.isfile(path):
                size = os.path.getsize(path)
                self.cache[fn] = size
                self.total += size

    def _file_lock(self, key):
        """Acquire a file-based lock for cross-process safety."""
        lock_path = os.path.join(self._lock_dir, f"{key}.lock")
        lock_fd = open(lock_path, "w")
        try:
            fcntl.flock(lock_fd, fcntl.LOCK_EX)
        except Exception:
            lock_fd.close()
            raise
        return lock_fd

    def _file_unlock(self, lock_fd):
        """Release a file-based lock."""
        try:
            fcntl.flock(lock_fd, fcntl.LOCK_UN)
        finally:
            lock_fd.close()

    def _discover_from_fs(self, key, path):
        """Register a filesystem-resident key in the in-memory index.

        Must be called while holding self._mu.  Guards against duplicate
        accounting by checking whether the key is already tracked.
        """
        if key not in self.cache:
            if os.path.isfile(path):
                size = os.path.getsize(path)
                self.cache[key] = size
                self.total += size
                return True
        return key in self.cache

    def _evict(self):
        # Caller must hold self._mu
        while self.total > self.max_size:
            for key in list(self.cache.keys()):
                if key not in self.locked_keys:
                    size = self.cache.pop(key)
                    path = os.path.join(self.cache_dir, key)
                    try:
                        os.remove(path)
                    except FileNotFoundError:
                        pass
                    self.total -= size
                    break
            else:
                break

    def set(self, key, data: bytes):
        lock_fd = self._file_lock(key)
        try:
            path = os.path.join(self.cache_dir, key)
            # Write to temp file then atomically rename to avoid partial reads
            tmp_fd, tmp_path = tempfile.mkstemp(dir=self.cache_dir, prefix=f".{key}.")
            try:
                os.write(tmp_fd, data)
                os.close(tmp_fd)
                os.rename(tmp_path, path)
            except Exception:
                try:
                    os.close(tmp_fd)
                except OSError:
                    pass
                try:
                    os.unlink(tmp_path)
                except OSError:
                    pass
                raise
            size = os.path.getsize(path)
            with self._mu:
                if key in self.cache:
                    self.total -= self.cache.pop(key)
                self.cache[key] = size
                self.total += size
                self._evict()
        finally:
            self._file_unlock(lock_fd)

    def get(self, key) -> bytes:
        path = os.path.join(self.cache_dir, key)
        with self._mu:
            if not self._discover_from_fs(key, path):
                raise KeyError(f"Key {key} not found in cache")
            self.cache.move_to_end(key)
        with open(path, "rb") as f:
            return f.read()

    def has(self, key) -> bool:
        path = os.path.join(self.cache_dir, key)
        with self._mu:
            if key in self.cache:
                return True
            return self._discover_from_fs(key, path)

    def get_path(self, key) -> str:
        path = os.path.join(self.cache_dir, key)
        with self._mu:
            if not self._discover_from_fs(key, path):
                raise KeyError(f"Key {key} not found in cache")
            self.cache.move_to_end(key)
        return path

    def lock(self, key):
        with self._mu:
            self.locked_keys.add(key)

    def unlock(self, key):
        with self._mu:
            self.locked_keys.discard(key)

    def invalidate(self, key):
        """Remove a specific key from the cache (both in-memory index and disk)."""
        lock_fd = self._file_lock(key)
        try:
            with self._mu:
                if key in self.cache:
                    self.total -= self.cache.pop(key)
            path = os.path.join(self.cache_dir, key)
            try:
                os.remove(path)
            except FileNotFoundError:
                pass
        finally:
            self._file_unlock(lock_fd)


class LayerCache:
    def __init__(self):
        self.file_cache = FileCache(
            cache_dir="/cache", max_size=1024 * 1024 * 128
        )  # 128 MiB

    @asynccontextmanager
    async def layer_filename(self, layer_id: str):
        cache_key = f"{layer_id}.gpkg"

        await self.bytes_for_layer(layer_id, "GeoPackage")

        self.file_cache.lock(cache_key)
        try:
            yield self.file_cache.get_path(cache_key)
        finally:
            self.file_cache.unlock(cache_key)

    def invalidate_layer(self, layer_id: str) -> bool:
        """Remove a layer's cached GPKG file from disk + memory index.

        Returns True if the key existed in the cache.
        """
        cache_key = f"{layer_id}.gpkg"
        existed = self.file_cache.has(cache_key)
        if existed:
            self.file_cache.invalidate(cache_key)
        return existed

    async def bytes_for_layer(self, layer_id: str, format: str = "GeoPackage") -> bytes:
        cache_key = f"{layer_id}.gpkg"

        try:
            return self.file_cache.get(cache_key)
        except (KeyError, FileNotFoundError):
            # not cached yet or missing file, proceed to fetch
            pass

        async with get_async_read_connection() as conn:
            layer = await conn.fetchrow(
                """
                SELECT layer_id, name, type, metadata, bounds, geometry_type,
                    created_on, last_edited, feature_count, s3_key, remote_url
                FROM map_layers
                WHERE layer_id = $1
                """,
                layer_id,
            )

            if not layer:
                raise KeyError(f"Layer {layer_id} not found")

            if layer["type"] == LAYER_TYPE_POSTGIS:
                raise KeyError(
                    f"PostGIS layer {layer_id} cannot be pulled as individual vector file"
                )

            # Check if the layer is associated with any maps via the layers array
            # Order by created_on DESC to get the most recently created map first
            await conn.fetch(
                """
                SELECT id, title, description, owner_uuid
                FROM user_mundiai_maps
                WHERE $1 = ANY(layers) AND soft_deleted_at IS NULL
                ORDER BY created_on DESC
                """,
                layer_id,
            )

            # Handle remote sources
            if layer["remote_url"]:
                # Remote URL: use vsicurl with ogr2ogr
                ogr_source = f"/vsicurl/{layer['remote_url']}"

                with tempfile.TemporaryDirectory() as temp_dir:
                    cached_output_gpkg = os.path.join(temp_dir, f"{layer_id}.gpkg")

                    if format != "GeoPackage":
                        raise TypeError("only GeoPackage supported in bytes_for_layer")

                    # Use ogr2ogr to convert remote source to GPKG
                    ogr_cmd = [
                        "ogr2ogr",
                        "-f",
                        "GPKG",
                        cached_output_gpkg,
                        ogr_source,
                    ]
                    process = await asyncio.create_subprocess_exec(*ogr_cmd)
                    await process.wait()
                    if process.returncode != 0:
                        raise Exception(
                            f"ogr2ogr command failed with exit code {process.returncode}"
                        )

                    with open(cached_output_gpkg, "rb") as f:
                        data = f.read()
                    self.file_cache.set(cache_key, data)

            else:
                # S3 storage: original approach
                bucket_name = get_bucket_name()

                with tempfile.TemporaryDirectory() as temp_dir:
                    s3_key = layer["s3_key"]
                    file_extension = os.path.splitext(s3_key)[1]

                    local_input_file = os.path.join(
                        temp_dir, f"{layer_id}_input{file_extension}"
                    )

                    s3 = await get_async_s3_client()
                    await s3.download_file(bucket_name, s3_key, local_input_file)

                    cached_output_gpkg = os.path.join(temp_dir, f"{layer_id}.gpkg")

                    if format != "GeoPackage":
                        raise TypeError("only GeoPackage supported in bytes_for_layer")

                    if file_extension.lower() == ".gpkg":
                        with (
                            open(local_input_file, "rb") as src,
                            open(cached_output_gpkg, "wb") as dst,
                        ):
                            dst.write(src.read())
                    else:
                        ogr_cmd = [
                            "ogr2ogr",
                            "-f",
                            "GPKG",
                            cached_output_gpkg,
                            local_input_file,
                        ]
                        process = await asyncio.create_subprocess_exec(*ogr_cmd)
                        await process.wait()
                        if process.returncode != 0:
                            raise Exception(
                                f"ogr2ogr command failed with exit code {process.returncode}"
                            )

                    with open(cached_output_gpkg, "rb") as f:
                        data = f.read()
                    self.file_cache.set(cache_key, data)

        return self.file_cache.get(cache_key)


cache_singleton = LayerCache()


def layer_cache() -> LayerCache:
    return cache_singleton
