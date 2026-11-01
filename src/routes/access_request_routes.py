"""The Request access form's endpoint (/api/access-requests): anyone, signed in or not, asks to join Ingabe.

Rules in src/services/access_requests.py. Staff see and decide requests on the Companies page
(src/routes/companies_routes.py). The answer is the same whether or not the address already has an account, so the
form cannot be used to learn who is on Ingabe. A hidden "website" field catches simple bots, and one address on the
internet may ask PER_IP_PER_HOUR times an hour.
"""

from __future__ import annotations

import logging
import time
from typing import Optional

from fastapi import APIRouter, HTTPException, Request, status
from pydantic import BaseModel

from src.database.pool import async_conn
from src.dependencies.redis_client import get_async_redis
from src.services import access_requests

logger = logging.getLogger(__name__)

router = APIRouter()

PER_IP_PER_HOUR = 5


class AccessRequestIn(BaseModel):
    email: str
    name: Optional[str] = None
    organization: Optional[str] = None
    reason: Optional[str] = None
    website: Optional[str] = None  # hidden from people: only a bot fills it in


def _client_ip(request: Request) -> str:
    # Behind Cloudflare the visitor's address is in this header; locally it is the socket's.
    return request.headers.get("cf-connecting-ip") or (request.client.host if request.client else "unknown")


async def _within_limit(ip: str) -> bool:
    r = get_async_redis()
    if r is None:
        return True
    key = f"access_request:{ip}:{int(time.time() // 3600)}"
    try:
        n = await r.incr(key)
        if n == 1:
            await r.expire(key, 3700)
        return n <= PER_IP_PER_HOUR
    except Exception:  # noqa: BLE001 - the limit is a guard: a Redis hiccup must not lose a person's request
        logger.warning("Access request limit not checked (Redis)", exc_info=True)
        return True


@router.post("", status_code=status.HTTP_202_ACCEPTED)
async def request_access(body: AccessRequestIn, request: Request):
    """Keep the request and push Ingabe staff; the same answer for every address."""
    if body.website:  # a bot: answered like everyone, kept nowhere
        return {"received": True}
    if not await _within_limit(_client_ip(request)):
        raise HTTPException(status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                            detail="Too many requests from here just now. Please try again in an hour.")
    try:
        sub = access_requests.clean_submission(body.email, body.name, body.organization, body.reason)
    except ValueError as e:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(e)) from e
    async with async_conn("access_requests.record") as conn:  # no user: the person is not signed in
        kept = await access_requests.record(conn, sub, "form")
        await access_requests.notify(conn, kept)
    return {"received": True}
