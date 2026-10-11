"""Tower work on a photo inspection: the photos, a person's marks, the example finder's suggestions and their review,
and the tower question cards. An adapter only: the rules live in src/services/tower_inspection.py and
src/services/tower_cards.py (TOWERS_RULES.md).

Every route answers 404 while TOWERS is off (or PHOTO_INSPECTIONS, which serves the photos), for a layer that is not
a photo inspection, and in a project that is not Telecom Towers. Reads need view rights on the layer (get_layer),
writes edit rights (edit_layer), as the other layer routes. Boxes are [x0, y0, x1, y1] in the photo's own pixels,
as it is shown (EXIF orientation applied)."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Optional

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field

from src.database.models import MapLayer
from src.dependencies.dag import edit_layer, get_layer
from src.dependencies.rate_limiter import heavy_limit
from src.dependencies.redis_client import get_async_redis
from src.dependencies.session import UserContext, verify_session_required
from src.services import (
    example_finder,
    example_finder_pictures,
    industry,
    photo_context,
    photo_inspections,
    tower_cards,
    tower_inspection,
)
from src.services.photo_inspections import PhotoFacts
from src.structures import async_read_conn
from src.utils import get_async_s3_client, get_bucket_name

logger = logging.getLogger(__name__)


def _towers_switched_on() -> None:
    if not tower_inspection.enabled() or not photo_inspections.enabled():
        raise HTTPException(404, "Not Found")


router = APIRouter(dependencies=[Depends(_towers_switched_on)])


@dataclass(frozen=True)
class _Inspection:
    layer: MapLayer
    project_id: str
    photos: tuple[PhotoFacts, ...]


async def _inspection(layer: MapLayer, session: UserContext) -> _Inspection:
    """The layer as a tower inspection: a photo inspection in a Telecom Towers project with its photos' facts kept.
    Runs after the rights check (get_layer / edit_layer), so it tells a stranger nothing."""
    prefix = photo_inspections.stored_prefix(layer.metadata_dict or {}, layer.layer_id)
    if prefix is None:
        raise HTTPException(404, "This layer is not a photo inspection")
    async with async_read_conn("towers.project", user_id=session.get_user_id()) as conn:
        try:
            project_id = tower_inspection.work_project(await photo_context.projects_of(conn, layer.layer_id))
        except tower_inspection.TowerConflict as e:
            raise HTTPException(409, str(e)) from None
        project_industry = await industry.industry_of_project(conn, project_id)
    if project_id is None or not industry.available("tower_inspection", project_industry):
        raise HTTPException(404, "Tower work is for photo inspections in Telecom Towers projects")
    photos = await tower_inspection.inspection_photos(await get_async_s3_client(), get_bucket_name(), prefix)
    if photos is None:
        raise HTTPException(409, "This inspection was uploaded before its photo facts were kept; upload the photos "
                                 "again to work on them")
    return _Inspection(layer=layer, project_id=project_id, photos=photos)


async def _viewed(layer: MapLayer = Depends(get_layer),
                  session: UserContext = Depends(verify_session_required)) -> _Inspection:
    return await _inspection(layer, session)


async def _edited(layer: MapLayer = Depends(edit_layer),
                  session: UserContext = Depends(verify_session_required)) -> _Inspection:
    return await _inspection(layer, session)


def _photo(inspection: _Inspection, sha256: str) -> PhotoFacts:
    photo = tower_inspection.photo_named(inspection.photos, sha256)
    if photo is None:
        raise HTTPException(404, "No such photo in this inspection")
    return photo


def _limit_per_user(request: Request, session: UserContext = Depends(verify_session_required)) -> UserContext:
    """The app's rate limiter keys on request.state.user_context when it is set (per user, not per address)."""
    request.state.user_context = session
    return session


@router.get("/towers/layer/{layer_id}/photos", operation_id="get_tower_photos")
async def get_tower_photos(inspection: _Inspection = Depends(_viewed)) -> dict[str, Any]:
    """The inspection's photos: where each opens, its size as shown, and its own facts."""
    layer_id = inspection.layer.layer_id
    photos = []
    for p in inspection.photos:
        width, height = p.upright_size
        photos.append({"sha256": p.sha256, "url": f"/api/layer/{layer_id}/photos/{p.sha256}", "width": width,
                       "height": height, "taken_at": p.taken_at, "lat": p.lat, "lon": p.lon,
                       "relative_altitude_m": p.relative_altitude_m, "gimbal_pitch": p.gimbal_pitch,
                       "gimbal_yaw": p.gimbal_yaw})
    return {"photos": photos}


@router.get("/towers/layer/{layer_id}/photos/{sha256}/work", operation_id="get_tower_photo_work")
async def get_tower_photo_work(sha256: str, inspection: _Inspection = Depends(_viewed)) -> dict[str, Any]:
    """Every part's work on one photo: marks, look-alike marks, the finder job, its suggestions, the review, what
    is counted by you and how many suggestions wait."""
    works = await tower_inspection.photo_work(get_async_redis(), await get_async_s3_client(), get_bucket_name(),
                                              inspection.project_id, inspection.layer.layer_id,
                                              _photo(inspection, sha256))
    return {"parts": {part: work.as_dict() for part, work in works.items()}}


class MarksBody(BaseModel):
    part: str
    marks: list[list[float]] = Field(description="0-20 boxes around the part, in the photo's pixels")
    lookalike_marks: list[list[float]] = Field(default_factory=list,
                                               description="boxes around things that look alike but are not wanted")


class ReviewFix(BaseModel):
    id: str
    box: list[float]


class ReviewBody(BaseModel):
    part: str
    kept: list[str] = Field(default_factory=list)
    rejected: list[str] = Field(default_factory=list)
    fixed: list[ReviewFix] = Field(default_factory=list)
    added: list[list[float]] = Field(default_factory=list)


class FindBody(BaseModel):
    part: str


def _refused(e: Exception) -> HTTPException:
    """422 for a request that cannot be done as asked (the reason in data terms), 409 for work not ready for it."""
    return HTTPException(409 if isinstance(e, tower_inspection.TowerConflict) else 422, str(e))


@router.put("/towers/layer/{layer_id}/photos/{sha256}/marks", operation_id="put_tower_marks")
async def put_tower_marks(sha256: str, body: MarksBody, inspection: _Inspection = Depends(_edited)) -> dict[str, Any]:
    """Save a part's marks on the photo; changed marks drop the part's finder job and review. Returns the part's
    work."""
    try:
        work = await tower_inspection.set_marks(get_async_redis(), await get_async_s3_client(), get_bucket_name(),
                                                inspection.project_id, inspection.layer.layer_id,
                                                _photo(inspection, sha256), body.part, body.marks,
                                                body.lookalike_marks)
    except (example_finder_pictures.FinderInputError, tower_inspection.TowerConflict) as e:
        raise _refused(e) from None
    return work.as_dict()


@router.post("/towers/layer/{layer_id}/photos/{sha256}/find", operation_id="find_tower_parts")
@heavy_limit
async def find_tower_parts(
    request: Request,
    sha256: str,
    body: FindBody,
    inspection: _Inspection = Depends(_edited),
    session: UserContext = Depends(_limit_per_user),
) -> dict[str, Any]:
    """Ask the example finder for the rest of the part on the photo, from its marks. 422 with the reason when the
    photo or the marks are unlike the ones the finder was measured on; 429 when this user already has the most
    finder jobs waiting, 503 when the whole app has; 404 while the example finder is switched off."""
    if not example_finder.finder_enabled():
        raise HTTPException(404, "The example finder is switched off")
    s3 = await get_async_s3_client()
    try:
        job = await tower_inspection.start_find(get_async_redis(), s3, get_bucket_name(), inspection.project_id,
                                                session.get_user_id(), inspection.layer.layer_id,
                                                inspection.layer.metadata_dict or {}, _photo(inspection, sha256),
                                                body.part)
    except (example_finder_pictures.FinderInputError, tower_inspection.TowerConflict) as e:
        raise _refused(e) from None
    except example_finder.FinderBusy as e:
        raise HTTPException(503 if e.whole_app else 429, str(e)) from None
    except example_finder_pictures.PictureReadError as e:
        logger.warning("tower finder on layer %s photo %s: %s", inspection.layer.layer_id, sha256, e)
        raise HTTPException(502, "The photo could not be read; try again") from None
    return {"job_id": job.job_id, "status": job.status}


@router.put("/towers/layer/{layer_id}/photos/{sha256}/review", operation_id="put_tower_review")
async def put_tower_review(sha256: str, body: ReviewBody,
                           inspection: _Inspection = Depends(_edited)) -> dict[str, Any]:
    """Save the review of a part's suggestions on the photo (it replaces the last one). Returns the part's work;
    409 before the finder has finished."""
    try:
        work = await tower_inspection.set_review(
            get_async_redis(), await get_async_s3_client(), get_bucket_name(), inspection.project_id,
            inspection.layer.layer_id, _photo(inspection, sha256), body.part, body.kept, body.rejected,
            [f.model_dump() for f in body.fixed], body.added)
    except (example_finder_pictures.FinderInputError, tower_inspection.TowerConflict) as e:
        raise _refused(e) from None
    return work.as_dict()


async def _here(inspection: _Inspection, session: UserContext) -> tower_cards.Here:
    async with async_read_conn("towers.cards", user_id=session.get_user_id()) as conn:
        return await tower_cards.load_here(conn, get_async_redis(), await get_async_s3_client(), get_bucket_name(),
                                           inspection.project_id, inspection.layer.layer_id,
                                           inspection.layer.name or inspection.layer.layer_id, inspection.photos)


def _check_audience(audience: Optional[str]) -> str:
    try:
        return tower_cards.check_audience(audience)
    except tower_cards.UnknownAudience as e:
        raise HTTPException(422, str(e)) from None


@router.get("/towers/layer/{layer_id}/cards", operation_id="get_tower_cards")
async def get_tower_cards(audience: Optional[str] = None, inspection: _Inspection = Depends(_viewed),
                          session: UserContext = Depends(verify_session_required)) -> dict[str, Any]:
    """The tower question cards for this inspection and reader (owner or technician), the most useful first."""
    reader = _check_audience(audience)
    return tower_cards.deck(await _here(inspection, session), reader)


@router.get("/towers/layer/{layer_id}/cards/{card_id}", operation_id="get_tower_card_answer")
async def get_tower_card_answer(card_id: str, audience: Optional[str] = None,
                                inspection: _Inspection = Depends(_viewed),
                                session: UserContext = Depends(verify_session_required)) -> dict[str, Any]:
    """One card's answer, in the drone cards' answer shape: what, why, what to do, how sure, facts, photos."""
    reader = _check_audience(audience)
    if card_id not in tower_cards.CARDS:
        raise HTTPException(404, f"No card {card_id}")
    return tower_cards.answer_card(card_id, await _here(inspection, session), reader)
