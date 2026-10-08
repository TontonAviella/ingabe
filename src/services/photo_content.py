"""One optimised photo per content: an upload whose bytes match an earlier upload shares that optimised photo, and
with it everything kept for the photo (plots, the crop survey, bare spots, every model answer about its pictures).

Plots, surveys and spots are stored under the optimised photo's key (`photo_plots.photo_key`), so pointing a new
layer at an existing optimised photo is all it takes for nothing to run twice. The index lives in object storage,
not in a table, so it works across partners: a layer only ever points at a photo whose bytes its owner uploaded.

Index entry: photo_content/v1/<sha256 of the uploaded file>-<target CRS>.json -> {"cog_key", "cog_srs"}.
The optimised photo depends on the bytes and the target CRS, so both are in the key.
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
from typing import Any, Optional

logger = logging.getLogger(__name__)

_INDEX_PREFIX = "photo_content/v1"
_CHUNK = 8 * 1024 * 1024


def file_sha256(path: str) -> str:
    """SHA-256 of a file, read in 8 MB pieces (a 5 GB orthophoto takes about 10 s)."""
    digest = hashlib.sha256()
    with open(path, "rb") as f:
        while chunk := f.read(_CHUNK):
            digest.update(chunk)
    return digest.hexdigest()


def index_key(sha256: str, target_srs: str) -> str:
    srs = re.sub(r"[^a-z0-9]+", "_", (target_srs or "native").lower()).strip("_")
    return f"{_INDEX_PREFIX}/{sha256}-{srs}.json"


async def find(s3: Any, bucket: str, sha256: str, target_srs: str) -> Optional[dict[str, Any]]:
    """The optimised photo made earlier from the same bytes and target CRS, if it is still stored; else None.
    Storage trouble returns None: the photo is then optimised again, as if never seen."""
    try:
        response = await s3.get_object(Bucket=bucket, Key=index_key(sha256, target_srs))
        async with response["Body"] as body:
            entry = json.loads(await body.read())
        await s3.head_object(Bucket=bucket, Key=entry["cog_key"])
        return entry
    except Exception as exc:  # noqa: BLE001 - missing entry, missing photo or storage trouble: optimise again
        if type(exc).__name__ not in ("NoSuchKey", "ClientError"):
            logger.warning("photo content index read failed for %s", sha256[:12], exc_info=True)
        return None


async def remember(s3: Any, bucket: str, sha256: str, target_srs: str, cog_key: str, cog_srs: Optional[str]) -> None:
    """Record which optimised photo these bytes made; failures only cost a future re-optimisation."""
    try:
        await s3.put_object(Bucket=bucket, Key=index_key(sha256, target_srs),
                            Body=json.dumps({"cog_key": cog_key, "cog_srs": cog_srs}).encode(),
                            ContentType="application/json")
    except Exception:  # noqa: BLE001
        logger.warning("photo content index write failed for %s", sha256[:12], exc_info=True)
