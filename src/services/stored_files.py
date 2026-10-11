"""Files checked by their sha256: the hash of a file on disk, and model weights kept in object storage that are
fetched once into a local cache and refused when they do not match.

One home for both, so every model the app runs is checked the same way (crop fingerprints, the example finder)
and an uploaded file is identified by the same hash everywhere.
"""

from __future__ import annotations

import hashlib
import os
from pathlib import Path
from typing import Any, Union

_CHUNK = 8 * 1024 * 1024


def file_sha256(path: Union[str, Path]) -> str:
    """SHA-256 of a file, read in 8 MB pieces (a 5 GB orthophoto takes about 10 s)."""
    digest = hashlib.sha256()
    with open(path, "rb") as f:
        while chunk := f.read(_CHUNK):
            digest.update(chunk)
    return digest.hexdigest()


def ensure_local_copy(s3: Any, bucket: str, key: str, sha256: str, path: str, what: str) -> str:
    """The local path of `key` (e.g. model weights), fetched from object storage once and checked by sha256.

    A file already at `path` is kept when its sha256 matches; otherwise it is fetched again. A fetched file that
    does not match is deleted and RuntimeError is raised, naming `what`. `s3` is a synchronous boto3 client:
    call this from a thread in async code.
    """
    if not (os.path.exists(path) and file_sha256(path) == sha256):
        os.makedirs(os.path.dirname(path), exist_ok=True)
        partial = path + ".part"
        s3.download_file(bucket, key, partial)
        if file_sha256(partial) != sha256:
            os.remove(partial)
            raise RuntimeError(f"the {what} in storage ({key}) does not match its sha256")
        os.replace(partial, path)
    return path
