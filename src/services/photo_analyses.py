"""One analysis of each photo of an inspection (SAM 3 outlines today: trees around a site, conductors on a line), run
once per photo in the background and kept in storage under the analysis' own prefix (never beside the photo work
records, which tower_inspection lists by prefix). A result is reused only by the same method."""

from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import dataclass
from typing import Any, Callable, Sequence

import numpy as np
from redis.exceptions import LockError

from src.services import hosted_detector, photo_inspections, stored_files
from src.services.example_finder_pictures import InspectionPhoto, PictureReadError
from src.services.photo_inspections import PhotoFacts

logger = logging.getLogger(__name__)

LOCK_TTL_S = 120


@dataclass(frozen=True)
class PhotoAnalysis:
    name: str  # in the lock and the log
    method: str  # changes when the method does: results of another method are not reused
    store_prefix: str
    select: Callable[[Sequence[PhotoFacts]], list[PhotoFacts]]  # the photos it applies to
    analyse: Callable[[np.ndarray], dict[str, Any]]  # blocking (a Roboflow call); the result without its method
    enabled: Callable[[], bool]  # it can run here


def _key(job: PhotoAnalysis, project_id: str, layer_id: str, sha256: str) -> str:
    return f"{job.store_prefix}/{project_id}/{layer_id}/{sha256}.json"


async def load(job: PhotoAnalysis, s3: Any, bucket: str, project_id: str, layer_id: str,
               photos: Sequence[PhotoFacts]) -> dict[str, dict[str, Any]]:
    """The stored results of this method for the photos it applies to, by photo."""
    out: dict[str, dict[str, Any]] = {}
    for p in job.select(photos):
        data = await stored_files.load_json(s3, bucket, _key(job, project_id, layer_id, p.sha256))
        if data is not None and data.get("method") == job.method:
            out[p.sha256] = data
    return out


async def _analyse_photo(job: PhotoAnalysis, s3: Any, bucket: str, project_id: str, layer_id: str,
                         layer_metadata: dict[str, Any], photo: PhotoFacts) -> None:
    view = photo_inspections.view_of(photo)
    if view is None:  # a photo whose view is unknown is not read (as the scan does)
        return
    picture = await InspectionPhoto(s3=s3, bucket=bucket, layer_id=layer_id, layer_metadata=layer_metadata,
                                    sha256=photo.sha256, view=view).read()
    result = await asyncio.to_thread(job.analyse, picture.pixels)
    await s3.put_object(Bucket=bucket, Key=_key(job, project_id, layer_id, photo.sha256),
                        Body=json.dumps({"method": job.method, **result}).encode(), ContentType="application/json")


_tasks: set[asyncio.Task] = set()


async def ensure(job: PhotoAnalysis, redis: Any, s3: Any, bucket: str, project_id: str, layer_id: str,
                 layer_metadata: dict[str, Any], photos: Sequence[PhotoFacts]) -> bool:
    """Analyse, in the background, every photo it applies to that has no result yet; one run per inspection across
    workers. Returns whether a run goes on now. Nothing runs while it cannot run here."""
    if not job.enabled() or not job.select(photos):
        return False
    lock = redis.lock(f"towers:v1:{project_id}:{layer_id}:{job.name}", timeout=LOCK_TTL_S, thread_local=False)
    if not await lock.acquire(blocking=False):
        return True

    async def run() -> None:
        try:
            done = await load(job, s3, bucket, project_id, layer_id, photos)
            for photo in job.select(photos):
                if photo.sha256 in done:
                    continue
                try:
                    await _analyse_photo(job, s3, bucket, project_id, layer_id, layer_metadata, photo)
                except (hosted_detector.DetectorUnavailable, PictureReadError) as e:
                    logger.info("%s of %s left %s: %s", job.name, layer_id, photo.filename, e)
                try:
                    await lock.reacquire()
                except LockError:
                    logger.warning("%s of %s lost its lock", job.name, layer_id)
                    return
        except Exception:
            logger.exception("%s of %s stopped", job.name, layer_id)
        finally:
            try:
                await lock.release()
            except LockError:
                pass

    task = asyncio.get_running_loop().create_task(run())
    _tasks.add(task)
    task.add_done_callback(_tasks.discard)
    return True
