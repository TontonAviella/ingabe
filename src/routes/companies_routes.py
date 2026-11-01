"""Companies page API (/api/admin/companies): Ingabe staff add partner companies and invite their admins.

Each company is a WorkOS organization. Staff add it here with a first admin's
email and its industry; WorkOS emails the invitation; from then on the company's
own admins manage their people on /settings/organization. A partner company's
people work only in its industry (src/services/industry.py); Ingabe's own
company (tier 'internal') sees every industry. Staff are listed in
PLATFORM_ADMIN_EMAILS. People who asked to join (src/services/access_requests.py) are approved here, with an
invitation into a company, or declined. Deeper troubleshooting (sign-in logs, branding, sign-in
methods, paid SSO) stays in the WorkOS dashboard.
"""

from __future__ import annotations

import asyncio
from typing import Any, Literal

from fastapi import APIRouter, Depends, HTTPException, Request, status
from pydantic import BaseModel

from src.database.pool import async_conn, get_async_db_connection
from src.dependencies.session import UserContext, provision_organization, verify_session_required
from src.services import access_requests, industry, workos_auth

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
    found = await asyncio.to_thread(workos_auth.companies)
    async with get_async_db_connection() as conn:
        rows = await conn.fetch("SELECT workos_org_id, industry, tier FROM organizations WHERE workos_org_id = ANY($1)",
                                [c["id"] for c in found])
    by_org = {r["workos_org_id"]: r for r in rows}
    for c in found:
        row = by_org.get(c["id"])
        # "all": Ingabe's own company; None: a partner company not given an industry yet.
        c["industry"] = "all" if row and row["tier"] == "internal" else (row["industry"] if row else None)
    return {"companies": found, "industries": [{"key": k, "label": v["label"]} for k, v in industry.INDUSTRIES.items()]}


class NewCompany(BaseModel):
    name: str
    admin_email: str
    industry: str


async def _set_company_industry(workos_org_id: str, name: str, value: str) -> None:
    async with get_async_db_connection() as conn:
        org_id = await provision_organization(conn, workos_org_id, name)
        await industry.save_company_industry(conn, org_id, value)


@router.post("")
async def add_company(body: NewCompany, request: Request, session: UserContext = Depends(verify_session_required)):
    """Create the company (or reuse one with this exact name) and email its first admin."""
    _require_staff(request)
    if not body.name.strip():
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Enter the company name")
    if "@" not in body.admin_email:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Enter the admin's email address")
    if body.industry not in industry.INDUSTRIES:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Choose the company's industry")
    try:
        created = await asyncio.to_thread(workos_auth.create_partner, body.name, body.admin_email)
    except ValueError as e:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(e))
    # Set before anyone signs in, so the company's people only ever see this industry. A company that already existed
    # (same name: the form was used to invite another admin) keeps its industry: that is changed on its own row.
    async with get_async_db_connection() as conn:
        org_id = await provision_organization(conn, created["organization_id"], created["name"])
        current = await industry.company_industry(conn, org_id)
        if created["created"] or current is None:
            await industry.save_company_industry(conn, org_id, body.industry)
            current = body.industry
    return {**created, "industry": current, "industry_kept": current != body.industry}


async def _company_name(organization_id: str) -> str:
    """The company's name from WorkOS; 404 when WorkOS has no such company, 502 when it did not answer."""
    try:
        return await asyncio.to_thread(workos_auth.organization_name, organization_id)
    except Exception as e:  # noqa: BLE001 - WorkOS SDK errors: only its 404 means the company does not exist
        if getattr(e, "status_code", None) == 404:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="No such company") from e
        raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail=f"WorkOS did not answer: {e}") from e


class CompanyIndustry(BaseModel):
    industry: str


@router.put("/{organization_id}/industry")
async def set_company_industry(organization_id: str, body: CompanyIndustry, request: Request,
                               session: UserContext = Depends(verify_session_required)):
    """Change a partner company's industry; its people then see only that one."""
    _require_staff(request)
    if body.industry not in industry.INDUSTRIES:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Choose one of the industries")
    name = await _company_name(organization_id)
    await _set_company_industry(organization_id, name, body.industry)
    return {"organization_id": organization_id, "industry": body.industry}


class AdminInvite(BaseModel):
    email: str


@router.post("/{organization_id}/admins")
async def invite_admin(organization_id: str, body: AdminInvite, request: Request,
                       session: UserContext = Depends(verify_session_required)):
    """Invite another admin to a company (e.g. the first one left, or never answered)."""
    _require_staff(request)
    if "@" not in body.email:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Enter an email address")
    try:
        return await asyncio.to_thread(workos_auth.invite, organization_id, body.email.strip(), "admin", None)
    except ValueError as e:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(e))


@router.post("/invitations/{invitation_id}/resend")
async def resend(invitation_id: str, request: Request, session: UserContext = Depends(verify_session_required)):
    _require_staff(request)
    try:
        return await asyncio.to_thread(workos_auth.resend_invitation, invitation_id)
    except ValueError as e:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(e))


@router.get("/access-requests")
async def list_access_requests(request: Request, session: UserContext = Depends(verify_session_required)):
    """People who asked to join: open ones first (longest waiting first), then those decided lately."""
    _require_staff(request)
    async with async_conn("access_requests.list") as conn:
        return {"requests": await access_requests.listing(conn)}


class Approval(BaseModel):
    organization_id: str
    role: Literal["member", "admin"] = "member"
    already_invited: bool = False  # the Add company form just invited them as its first admin


@router.post("/access-requests/{request_id}/approve")
async def approve_access_request(request_id: int, body: Approval, request: Request,
                                 session: UserContext = Depends(verify_session_required)):
    """Invite the person into a company (WorkOS emails the invitation) and close the request."""
    ws_session = _require_staff(request)
    async with async_conn("access_requests.approve") as conn:
        open_request = await access_requests.get_open(conn, request_id)
        if open_request is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="This request is no longer open")
        company = await _company_name(body.organization_id)
        try:
            if not body.already_invited:
                await asyncio.to_thread(workos_auth.invite, body.organization_id, open_request["email"], body.role,
                                        ws_session.user_id)
        except ValueError as e:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(e)) from e
        decided = await access_requests.decide(conn, request_id, "approved", ws_session.email,
                                               f"Invited to {company} as {body.role}")
    if decided is None:  # someone else decided it while the invitation went out
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Someone else just decided this request")
    return decided


@router.post("/access-requests/{request_id}/decline")
async def decline_access_request(request_id: int, request: Request,
                                 session: UserContext = Depends(verify_session_required)):
    """Close the request without an invitation (the person is not told automatically)."""
    ws_session = _require_staff(request)
    async with async_conn("access_requests.decline") as conn:
        decided = await access_requests.decide(conn, request_id, "declined", ws_session.email, "Declined")
    if decided is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="This request is no longer open")
    return decided
