"""Photo-set upload handler: a ZIP of photos, or one photo with no map position, becomes one photo inspection.

What a photo inspection is (which files are photos, the limits, the facts read from each photo, where originals
are stored, the camera points) belongs to src/services/photo_inspections.py. This handler unpacks the upload,
stores each original photo, and hands a GeoJSON of camera points to the vector path, so the inspection is a
normal vector layer whose metadata says where its photos are.
"""

import asyncio
import json
import logging
import os
import shutil
import tempfile
import zipfile

from fastapi import HTTPException, status

from src.services import photo_inspections
from src.upload.archive import ArchiveTooLarge, extract_zip
from src.upload.base import BaseUploadHandler, HandlerResult, UploadContext
from src.upload.handlers.vector_handler import VectorUploadHandler
from src.upload.keys import upload_key
from src.utils import get_async_s3_client, s3_op

logger = logging.getLogger(__name__)

# Photos sent to object storage at the same time.
_UPLOAD_CONCURRENCY = 8


class PhotoSetUploadHandler(BaseUploadHandler):
    """Handles photo inspections (a ZIP of JPG/PNG photos, or a single photo GDAL cannot place on the map).

    Preprocessing unpacks the photos, reads their facts, stores the originals under the inspection's prefix and
    writes the camera points as GeoJSON. Layer creation is the vector path's. An upload that creates no layer
    removes the stored originals (discard); once the layer exists, the uploaded file itself is not kept.
    """

    async def preprocess(self, ctx: UploadContext) -> HandlerResult:
        temp_dir = tempfile.mkdtemp(prefix="photo-inspection-")
        try:
            return await self._inspection(ctx, temp_dir)
        except ArchiveTooLarge as e:
            shutil.rmtree(temp_dir, ignore_errors=True)
            raise HTTPException(status_code=status.HTTP_413_CONTENT_TOO_LARGE, detail=str(e))
        except zipfile.BadZipFile:
            shutil.rmtree(temp_dir, ignore_errors=True)
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="The file is not a readable ZIP")
        except (photo_inspections.UnreadablePhoto, photo_inspections.NoPhotoPosition) as e:
            shutil.rmtree(temp_dir, ignore_errors=True)
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(e))
        except BaseException:
            shutil.rmtree(temp_dir, ignore_errors=True)
            raise

    async def create_layers(self, ctx: UploadContext, result: HandlerResult) -> HandlerResult:
        """The camera points become a vector layer like any other GeoJSON."""
        return await VectorUploadHandler().create_layers(ctx, result)

    async def discard(self, ctx: UploadContext) -> None:
        """The upload created no layer: remove the originals it stored (unless a layer with this id exists, as when
        a retried upload fails because the first one finished)."""
        prefix = photo_inspections.storage_prefix(ctx.project_id, ctx.layer_id)
        try:
            removed = await photo_inspections.forget_if_unused(ctx.conn, await get_async_s3_client(),
                                                               ctx.bucket_name, prefix, ctx.layer_id)
        except Exception:  # noqa: BLE001 - the upload already failed with its own error; a leftover is only storage
            logger.warning("could not remove the stored photos of failed inspection %s", ctx.layer_id, exc_info=True)
            return
        if removed:
            logger.info("failed photo inspection %s: removed %d stored photos", ctx.layer_id, len(removed))

    async def _inspection(self, ctx: UploadContext, temp_dir: str) -> HandlerResult:
        photos_dir = os.path.join(temp_dir, "photos")
        os.makedirs(photos_dir)
        is_archive = ctx.file_ext == ".zip"
        photos = await _unpacked_photos(ctx.temp_file_path, photos_dir) if is_archive else [_single_photo(ctx)]
        facts = await asyncio.to_thread(photo_inspections.read_photos, photos)
        photo_inspections.check_placeable(facts)

        prefix = photo_inspections.storage_prefix(ctx.project_id, ctx.layer_id)
        await _store_originals([path for path, _ in photos], facts, prefix, ctx.bucket_name)
        shutil.rmtree(photos_dir, ignore_errors=True)  # stored; only the camera points are needed from here

        geojson_path = os.path.join(temp_dir, "cameras.geojson")
        with open(geojson_path, "w", encoding="utf-8") as f:
            json.dump(photo_inspections.camera_points(facts, prefix), f)

        ctx.metadata_dict.update({
            "original_format": "photo_zip" if is_archive else "photo",
            "photo_inspection": photo_inspections.summary(facts, prefix),
        })
        logger.info("photo inspection %s: %d photos, %d placed", ctx.layer_id, len(facts),
                    sum(1 for f in facts if f.has_position))
        return HandlerResult(
            layer_type="vector",
            updated_temp_file_path=geojson_path,
            updated_file_ext=".geojson",
            updated_s3_key=upload_key(ctx.user_id, ctx.project_id, ctx.layer_id, ".geojson"),
            temp_dir_to_cleanup=temp_dir,
            brain_pages_per_feature=False,
            keep_uploaded_file=False,
        )


async def _unpacked_photos(zip_path: str, photos_dir: str) -> list[tuple[str, str]]:
    """(path, name inside the ZIP) of each photo in the archive, sorted by name; only photos are unpacked, within
    the inspection's limits."""
    written = await asyncio.to_thread(
        extract_zip, zip_path, photos_dir,
        max_entries=photo_inspections.MAX_PHOTOS,
        max_total_bytes=photo_inspections.MAX_UNPACKED_BYTES,
        only=photo_inspections.is_photo_name,
    )
    root = os.path.realpath(photos_dir)
    photos = sorted({(path, os.path.relpath(path, root)) for path in written}, key=lambda p: p[1])
    if not photos:
        raise photo_inspections.UnreadablePhoto("The ZIP holds no photos that could be unpacked")
    return photos


def _single_photo(ctx: UploadContext) -> tuple[str, str]:
    """(path, filename) of a photo uploaded on its own."""
    original = os.path.basename(ctx.metadata_dict.get("original_filename") or "")
    name = original if photo_inspections.is_photo_name(original) else f"{ctx.file_basename}{ctx.file_ext}"
    return ctx.temp_file_path, name


async def _store_originals(paths: list[str], facts: list[photo_inspections.PhotoFacts], prefix: str,
                           bucket: str) -> None:
    """Upload each original photo (streamed from disk, never read whole into memory) to its key under `prefix`;
    identical photos are stored once. Every upload has finished before a failure is raised, so removing what was
    stored (discard) does not race an upload still running."""
    s3 = await get_async_s3_client()
    limit = asyncio.Semaphore(_UPLOAD_CONCURRENCY)
    by_key = {photo_inspections.photo_key(prefix, f): path for path, f in zip(paths, facts, strict=True)}

    async def put(key: str, path: str) -> None:
        async with limit:
            await s3_op(
                s3.upload_file(path, bucket, key, ExtraArgs={"ContentType": photo_inspections.content_type(key)}),
                "upload", f"inspection photo {key}",
            )

    outcomes = await asyncio.gather(*(put(key, path) for key, path in by_key.items()), return_exceptions=True)
    failure = next((o for o in outcomes if isinstance(o, BaseException)), None)
    if failure is not None:
        raise failure
