# Copyright (C) 2025 Ingabe Ltd.
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU Affero General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU Affero General Public License for more details.
#
# You should have received a copy of the GNU Affero General Public License
# along with this program.  If not, see <http://www.gnu.org/licenses/>.
"""The signed-in user's own settings: which view of reports they get, and which industry they work in."""

from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel

from src.dependencies.session import UserContext, verify_session_required
from src.services import industry, insurance_engine
from src.database.pool import get_async_db_connection

router = APIRouter()


class AudienceUpdate(BaseModel):
    audience: Optional[str] = None  # None clears the user's choice


async def _audience_payload(conn, session: UserContext) -> dict:
    audience, source = await insurance_engine.audience_setting(conn, session.get_user_id(), session.get_org_id())
    return {
        "audience": audience,
        "source": source,
        "options": [{"key": k, "label": v} for k, v in insurance_engine.AUDIENCE_LABELS.items()],
    }


@router.get("/report-audience")
async def get_report_audience(session: UserContext = Depends(verify_session_required)):
    """The view Sage's reports use for this user, and where that comes from."""
    async with get_async_db_connection() as conn:
        return await _audience_payload(conn, session)


@router.put("/report-audience")
async def put_report_audience(body: AudienceUpdate, session: UserContext = Depends(verify_session_required)):
    """Save the user's own report view (farmer, insurance, agronomist, scientist), or clear it."""
    async with get_async_db_connection() as conn:
        try:
            saved = await insurance_engine.save_user_audience(conn, session.get_user_id(), body.audience)
        except ValueError as e:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(e))
        if not saved:
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="saving a role needs a signed-in account")
        return await _audience_payload(conn, session)


class IndustryUpdate(BaseModel):
    industry: str


async def _industry_payload(conn, session: UserContext) -> dict:
    return {
        "industry": await industry.industry_of(conn, session.get_user_id()),
        "options": [{"key": k, **v} for k, v in industry.INDUSTRIES.items()],
    }


@router.get("/industry")
async def get_industry(session: UserContext = Depends(verify_session_required)):
    """The industry this user works in; null until they choose (the app then asks)."""
    async with get_async_db_connection() as conn:
        return await _industry_payload(conn, session)


@router.put("/industry")
async def put_industry(body: IndustryUpdate, session: UserContext = Depends(verify_session_required)):
    """Save the user's industry: agriculture, power_grid or telecom."""
    async with get_async_db_connection() as conn:
        try:
            saved = await industry.save_industry(conn, session.get_user_id(), body.industry)
        except ValueError as e:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(e))
        if not saved:
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="saving an industry needs a signed-in account")
        return await _industry_payload(conn, session)
