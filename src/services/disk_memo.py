"""Answers that never change, kept on disk so an app restart does not lose them.

A satellite value already published (a WaPOR dekad at a point, a month of DE Africa NDVI anomaly over an
area) is fixed for good, yet it takes seconds to read from the remote file. The services that read them
keep each answer in memory, which every restart empties: every deploy restarts the app, and the first
insurance report after one took ~10 s instead of ~1.5 s (measured in mundi-app on 2026-10-26, 3 runs
each). This is the copy on disk beneath those memory caches.

Only values that will never change may be kept here: the caller decides, and keys must name the exact
source (URL, coordinates, area). A value that may change slowly (a place's position on OpenStreetMap) is
kept with the time it was read, and its caller decides when it is too old (search_place). Values are JSON. A disk that cannot be read or written is logged and
treated as a miss, so the answer is read from its source as before.

The file lives on the ingabe-cache volume (DISK_MEMO_PATH), which outlives containers.
"""

from __future__ import annotations

import json
import logging
import os
import sqlite3
import threading
from typing import Any

logger = logging.getLogger(__name__)

_DEFAULT_PATH = "/tmp/ingabe_cache/memo.sqlite3"
_ready: set[str] = set()  # paths whose table exists, created once per process
_ready_lock = threading.Lock()


def _path() -> str:
    return os.environ.get("DISK_MEMO_PATH") or _DEFAULT_PATH


def _connect() -> sqlite3.Connection:
    path = _path()
    conn = sqlite3.connect(path, timeout=5)
    with _ready_lock:
        if path not in _ready:
            conn.execute("PRAGMA journal_mode=WAL")  # the app and the Dagster daemon share the file
            conn.execute("CREATE TABLE IF NOT EXISTS memo (namespace TEXT, key TEXT, value TEXT, "
                         "PRIMARY KEY (namespace, key))")
            conn.commit()
            _ready.add(path)
    return conn


def get(namespace: str, key: str) -> tuple[bool, Any]:
    """(True, value) when kept, else (False, None). A stored None is a value (e.g. NODATA), not a miss."""
    try:
        conn = _connect()
        try:
            row = conn.execute("SELECT value FROM memo WHERE namespace = ? AND key = ?", (namespace, key)).fetchone()
        finally:
            conn.close()
    except (sqlite3.Error, OSError):
        logger.warning("disk memo: could not read %s %s; reading from the source", namespace, key, exc_info=True)
        return False, None
    return (False, None) if row is None else (True, json.loads(row[0]))


def put(namespace: str, key: str, value: Any) -> None:
    """Keep value (JSON-serialisable) under (namespace, key) for good."""
    try:
        conn = _connect()
        try:
            conn.execute("INSERT OR REPLACE INTO memo (namespace, key, value) VALUES (?, ?, ?)",
                         (namespace, key, json.dumps(value)))
            conn.commit()
        finally:
            conn.close()
    except (sqlite3.Error, OSError):
        logger.warning("disk memo: could not keep %s %s; it will be read from the source again",
                       namespace, key, exc_info=True)
