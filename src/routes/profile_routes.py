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
from src.services import industry, insurance_engine, reader_roles
from src.database.pool import get_async_db_connection

router = APIRouter()


class AudienceUpdate(BaseModel):
    audience: Optional[str] = None  # None clears the user's choice


async def _audience_payload(conn, session: UserContext) -> dict:
    """The user's role in the industry they work in now (src/services/reader_roles.py), where it comes from, and the
    industry's roles. Reports are written in the role's voice (insurance_engine)."""
    user_id, org_id = session.get_user_id(), session.get_org_id()
    working_in = await industry.current_industry(conn, user_id, org_id) or "agriculture"
    saved, source = await reader_roles.saved_role(conn, user_id, org_id)
    role = reader_roles.for_industry(working_in, saved)
    return {
        "audience": role,
        "source": source if saved == role else "default",
        "industry": working_in,
        "options": [{"key": o["id"], "label": o["label"]} for o in reader_roles.options(working_in)],
    }


@router.get("/report-audience")
async def get_report_audience(session: UserContext = Depends(verify_session_required)):
    """The view Sage's reports use for this user, and where that comes from."""
    async with get_async_db_connection() as conn:
        return await _audience_payload(conn, session)


@router.put("/report-audience")
async def put_report_audience(body: AudienceUpdate, session: UserContext = Depends(verify_session_required)):
    """Save the user's own role (any industry's, reader_roles), or clear it."""
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
    current, source = await industry.current_industry_and_source(conn, user_id, org_id)
    locked = await industry.locked_industry(conn, org_id) is not None
    company = None
    if org_id:
        name = await conn.fetchval("SELECT name FROM organizations WHERE id::text = $1", org_id)
        if name is not None:
            # The company the user acts for, and its industry: the default for members who never chose their own.
            company = {"name": name, "industry": await industry.company_industry(conn, org_id)}
    return {
        # The industry the user works in now: their home lists its projects and new projects take it (None: the
        # app asks).
        "industry": current,
        "source": source,
        "company": company,
        # A partner company's people work only in its industry: the app shows no other industry and no switch.
        "locked": locked,
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
    """Save the user's own industry: agriculture, power_grid or telecom. Refused for a partner company that has an
    industry (its people work only in it). For the first person of a partner company without one, the choice becomes
    the company's industry for everyone (src/services/industry.py)."""
    async with get_async_db_connection() as conn:
        org_id = session.get_org_id()
        locked = await industry.locked_industry(conn, org_id)
        if locked == body.industry:  # already where the company works (e.g. a second person's first choice)
            return await _industry_payload(conn, session)
        if locked:
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=_locked_message(locked))
        # Only a person's first choice can give an unclaimed company its industry: a later switch (a tab, a shared
        # map from another industry) must never move a whole company.
        first_choice = await industry.industry_of(conn, session.get_user_id()) is None
        try:
            saved = await industry.save_industry(conn, session.get_user_id(), body.industry)
        except ValueError as e:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(e))
        if not saved:
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="saving an industry needs a signed-in account")
        if first_choice and not await industry.claim_company_industry(conn, org_id, body.industry):
            won = await industry.locked_industry(conn, org_id)
            if won and won != body.industry:  # a colleague chose first, a moment ago
                raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=_locked_message(won))
        return await _industry_payload(conn, session)


def _locked_message(locked: str) -> str:
    return f"Your company works in {industry.INDUSTRIES[locked]['label']}. Ask Ingabe if that needs to change."
