"""Example finder on a drone orthophoto layer: mark a few objects in a window of the photo, the finder finds the
rest. An adapter only: the rules, the settings and the jobs live in src/services/example_finder.py, the picture
in src/services/example_finder_pictures.py. Switch EXAMPLE_FINDER (off: both routes answer 404)."""

from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field

from src.database.models import LAYER_TYPE_RASTER, MapLayer
from src.dependencies.dag import edit_layer, get_layer
from src.dependencies.rate_limiter import heavy_limit
from src.dependencies.redis_client import get_async_redis
from src.dependencies.session import UserContext, verify_session_required
from src.services import example_finder, example_finder_pictures, photo_context
from src.structures import async_read_conn
from src.utils import get_async_s3_client, get_bucket_name

logger = logging.getLogger(__name__)


def _finder_switched_on() -> None:
    if not example_finder.finder_enabled():
        raise HTTPException(404, "Not Found")


def _limit_per_user(request: Request, session: UserContext = Depends(verify_session_required)) -> UserContext:
    """The app's rate limiter keys on request.state.user_context when it is set (per user, not per address)."""
    request.state.user_context = session
    return session


router = APIRouter(dependencies=[Depends(_finder_switched_on)])


class FinderWindow(BaseModel):
    bbox: list[float] = Field(description="west, south, east, north in `crs`")
    crs: str = Field(default=example_finder_pictures.LONLAT,
                     description="'lonlat' (EPSG:4326) or 'layer' (the photo's own coordinates)")


class FinderRequest(BaseModel):
    target: str = Field(description="a kind of object from the measured table, e.g. 'tower_dish', 'maize_seedling'")
    window: FinderWindow
    marks: list[list[float]] = Field(description="1-20 boxes (west, south, east, north) around objects to find")
    lookalike_marks: list[list[float]] = Field(default_factory=list,
                                               description="boxes around things that look alike but are not wanted")


async def _photo_project(layer: MapLayer, session: UserContext) -> tuple[dict[str, Any], str]:
    metadata = layer.metadata_dict or {}
    if layer.type != LAYER_TYPE_RASTER:
        raise HTTPException(400, "The example finder works on drone photos")
    if not photo_context.is_photo_ready(metadata):
        raise HTTPException(409, "The photo is still being processed")
    async with async_read_conn("example_finder.project", user_id=session.get_user_id()) as conn:
        project_id = await photo_context.project_of(conn, layer.layer_id)
    if project_id is None:
        raise HTTPException(409, "This photo is not in a project, so its finds have nowhere to be kept")
    return metadata, project_id


@router.post("/layer/{layer_id}/finder", operation_id="start_example_finder")
@heavy_limit
async def start_example_finder(
    request: Request,
    body: FinderRequest,
    layer: MapLayer = Depends(edit_layer),
    session: UserContext = Depends(_limit_per_user),
) -> dict[str, Any]:
    """Find the objects like the marked ones in a window of the photo. Returns the job: "done" with the finds when
    this project asked the same before, otherwise "queued" or "running" (ask GET .../finder/{job_id}). 429 when this
    user already has the most jobs waiting, 503 when the whole app has."""
    metadata, project_id = await _photo_project(layer, session)
    s3, bucket = await get_async_s3_client(), get_bucket_name()
    source = example_finder_pictures.OrthophotoWindow(
        cog_url=await photo_context.cog_url(s3, bucket, metadata), bbox=body.window.bbox, crs=body.window.crs)
    try:
        job = await example_finder.start(get_async_redis(), s3, bucket, project_id, session.get_user_id(), source,
                                         body.marks, body.lookalike_marks, body.target)
    except example_finder_pictures.FinderInputError as e:
        raise HTTPException(422, str(e)) from None
    except example_finder.FinderBusy as e:
        raise HTTPException(503 if e.whole_app else 429, str(e)) from None
    except example_finder_pictures.PictureReadError as e:
        logger.warning("example finder on layer %s: %s", layer.layer_id, e)
        raise HTTPException(502, "The photo could not be read; try again") from None
    return job.as_dict()


@router.get("/layer/{layer_id}/finder/{job_id}", operation_id="get_example_finder_job")
async def get_example_finder_job(
    job_id: str,
    layer: MapLayer = Depends(get_layer),
    session: UserContext = Depends(verify_session_required),
) -> dict[str, Any]:
    """A finder job on this photo: its status, and the finds when it is done."""
    _, project_id = await _photo_project(layer, session)
    job = await example_finder.status(get_async_redis(), await get_async_s3_client(), get_bucket_name(), project_id,
                                      job_id)
    if job is None:
        raise HTTPException(404, "No such finder job on this photo (it may have stopped: start it again)")
    return job.as_dict()
