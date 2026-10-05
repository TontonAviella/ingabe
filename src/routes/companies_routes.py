"""Companies page API (/api/admin/companies): Ingabe staff add partner companies and invite their admins.

Each company is a WorkOS organization. Staff add it here with a first admin's
email; WorkOS emails the invitation; from then on the company's own admins
manage their people on /settings/organization. Staff are listed in
PLATFORM_ADMIN_EMAILS. Deeper troubleshooting (sign-in logs, branding, sign-in
methods, paid SSO) stays in the WorkOS dashboard.
"""

from __future__ import annotations

import asyncio
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request, status
from pydantic import BaseModel

from src.dependencies.session import UserContext, verify_session_required
from src.services import workos_auth

router = APIRouter()


def _require_staff(request: Request) -> Any:
    ws_session = getattr(request.state, "workos_session", None)
    if not workos_auth.enabled() or ws_session is None:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Sign in required")
    if not workos_auth.is_platform_staff(ws_session.email):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Only Ingabe staff can manage companies")
    return ws_session


@router.get("")
async def list_companies(request: Request, session: UserContext = Depends(verify_session_required)):
    _require_staff(request)
    return {"companies": await asyncio.to_thread(workos_auth.companies)}


class NewCompany(BaseModel):
    name: str
    admin_email: str


@router.post("")
async def add_company(body: NewCompany, request: Request, session: UserContext = Depends(verify_session_required)):
    """Create the company (or reuse one with this exact name) and email its first admin."""
    _require_staff(request)
    if not body.name.strip():
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Enter the company name")
    if "@" not in body.admin_email:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Enter the admin's email address")
    return await asyncio.to_thread(workos_auth.create_partner, body.name, body.admin_email)


class AdminInvite(BaseModel):
    email: str


@router.post("/{organization_id}/admins")
async def invite_admin(organization_id: str, body: AdminInvite, request: Request,
                       session: UserContext = Depends(verify_session_required)):
    """Invite another admin to a company (e.g. the first one left, or never answered)."""
    _require_staff(request)
    if "@" not in body.email:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Enter an email address")
    return await asyncio.to_thread(workos_auth.invite, organization_id, body.email.strip(), "admin", None)


@router.post("/invitations/{invitation_id}/resend")
async def resend(invitation_id: str, request: Request, session: UserContext = Depends(verify_session_required)):
    _require_staff(request)
    return await asyncio.to_thread(workos_auth.resend_invitation, invitation_id)
