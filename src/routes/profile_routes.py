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
    user_id, org_id = session.get_user_id(), session.get_org_id()
    own = await industry.industry_of(conn, user_id)
    company = None
    if org_id:
        name = await conn.fetchval("SELECT name FROM organizations WHERE id::text = $1", org_id)
        if name is not None:
            company = {
                "name": name,
                "industry": await industry.company_industry(conn, org_id),
                "can_set": session.get_org_role() in industry.COMPANY_ADMIN_ROLES,
            }
    return {
        # What new projects get: the company's industry once it has one, else the user's own (None: the app asks).
        "industry": (company and company["industry"]) or own,
        "source": "company" if company and company["industry"] else ("you" if own else None),
        "company": company,
        # Only an account row can keep a choice (the legacy single-user mode has none): the app asks only then.
        "can_choose": bool(user_id) and await conn.fetchval("SELECT 1 FROM users WHERE internal_uuid = $1", user_id) is not None,
        "options": [{"key": k, **v} for k, v in industry.INDUSTRIES.items()],
    }


@router.get("/industry")
async def get_industry(session: UserContext = Depends(verify_session_required)):
    """The industry this user works in; null until they choose (the app then asks)."""
    async with get_async_db_connection() as conn:
        return await _industry_payload(conn, session)


@router.put("/industry")
async def put_industry(body: IndustryUpdate, session: UserContext = Depends(verify_session_required)):
    """Save an industry: agriculture, power_grid or telecom. A company's owners and admins set it for the whole
    company; other members of a company that has chosen cannot override it; anyone else saves their own."""
    async with get_async_db_connection() as conn:
        org_id = session.get_org_id()
        company_admin = bool(org_id) and session.get_org_role() in industry.COMPANY_ADMIN_ROLES
        try:
            industry.check_industry(body.industry)
            if not company_admin and await industry.company_industry(conn, org_id):
                raise HTTPException(status_code=status.HTTP_403_FORBIDDEN,
                                    detail="Your company's industry is set by its owners and admins.")
            if company_admin:
                await industry.save_company_industry(conn, org_id, body.industry)  # type: ignore[arg-type]
            saved = await industry.save_industry(conn, session.get_user_id(), body.industry)
        except ValueError as e:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(e))
        if not saved:
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="saving an industry needs a signed-in account")
        return await _industry_payload(conn, session)
