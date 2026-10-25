"""A person's 👍 or 👎 on one of Sage's answers (src/services/sage_answer_ratings.py owns the rules)."""

from typing import Literal, Optional

from fastapi import APIRouter, Depends, HTTPException, Response
from pydantic import BaseModel, Field

from src.database.models import MundiMap
from src.dependencies.dag import get_map
from src.dependencies.session import UserContext, verify_session_required
from src.services import sage_answer_ratings

router = APIRouter()


class AnswerRating(BaseModel):
    rating: Literal["up", "down"]
    comment: Optional[str] = Field(default=None, max_length=sage_answer_ratings.COMMENT_CHARS)


@router.post("/{map_id}/messages/{message_id}/rating", status_code=204, operation_id="rate_sage_answer")
async def rate_sage_answer(
    message_id: int,
    body: AnswerRating,
    map: MundiMap = Depends(get_map),
    session: UserContext = Depends(verify_session_required),
) -> Response:
    saved = await sage_answer_ratings.save(map_id=map.id, message_id=message_id, user_id=session.get_user_id(),
                                           rating=body.rating, comment=body.comment)
    if not saved:
        raise HTTPException(404, "No Sage answer with that id on this map")
    return Response(status_code=204)
