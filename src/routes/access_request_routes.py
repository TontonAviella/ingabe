"""The Request access form's endpoint (/api/access-requests): anyone, signed in or not, asks to join Ingabe.

Rules in src/services/access_requests.py. Staff see and decide requests on the Companies page
(src/routes/companies_routes.py). The answer is the same whether or not the address already has an account, so the
form cannot be used to learn who is on Ingabe. A hidden "website" field catches simple bots, and one address on the
internet (for IPv6, one /64 network: _limit_subject) may ask PER_IP_PER_HOUR times an hour.
"""

from __future__ import annotations

import asyncio
import ipaddress
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
_WINDOW_SECONDS = 3600
_COUNT_KEPT_SECONDS = 3700  # a little past the hour it counts
_REDIS_TIMEOUT_SECONDS = 2.0  # a Redis that hangs must not hold a public request: past this, it goes through
_warned_no_redis = False


class AccessRequestIn(BaseModel):
    email: str
    name: Optional[str] = None
    organization: Optional[str] = None
    reason: Optional[str] = None
    website: Optional[str] = None  # hidden from people: only a bot fills it in


def _client_ip(request: Request) -> str:
    # Behind Cloudflare the visitor's address is in this header; locally it is the socket's.
    return request.headers.get("cf-connecting-ip") or (request.client.host if request.client else "unknown")


def _limit_subject(ip: str) -> str:
    """Who the limit counts: an IPv4 address, or the /64 network an IPv6 address is in.

    One IPv6 client normally holds a whole /64, and one address has many spellings ("2001:db8::1",
    "2001:0db8:0:0:0:0:0:1"), so counting the raw text let one client ask without limit (2026-11-08). An IPv4 address
    written as IPv6 ("::ffff:198.51.100.7") is that IPv4 address. Anything that is not one address counts as one
    "unknown" subject, so made-up values cannot open new counts.
    """
    try:
        address = ipaddress.ip_address(ip.strip())
    except ValueError:
        # Behind Cloudflare the header is always one address: this means a proxy or header changed.
        logger.warning("Access request limit: %r is not one address; counted with every other such value", ip[:64])
        return "unknown"
    if isinstance(address, ipaddress.IPv6Address):
        if address.ipv4_mapped is not None:
            return str(address.ipv4_mapped)
        # From the number, not the text: a zone such as "%eth0" is not part of the network.
        return str(ipaddress.IPv6Network((int(address) >> 64 << 64, 64)))
    return str(address)


def _limit_key(subject: str, now: float) -> str:
    return f"access_request:{subject}:{int(now // _WINDOW_SECONDS)}"


async def _count(r, key: str) -> int:
    # The count and its expiry are made together: an INCR whose EXPIRE failed counted forever (2026-11-08).
    async with r.pipeline(transaction=True) as pipe:
        pipe.set(key, 0, ex=_COUNT_KEPT_SECONDS, nx=True)
        pipe.incr(key)
        _, n = await pipe.execute()
    return n


async def _within_limit(ip: str) -> bool:
    global _warned_no_redis
    r = get_async_redis()
    if r is None:
        if not _warned_no_redis:
            _warned_no_redis = True
            logger.warning("Access request limit is off: no Redis")
        return True
    key = _limit_key(_limit_subject(ip), time.time())
    try:
        n = await asyncio.wait_for(_count(r, key), timeout=_REDIS_TIMEOUT_SECONDS)
        return n <= PER_IP_PER_HOUR
    except Exception:  # noqa: BLE001 - the limit is a guard: a Redis hiccup must not lose a person's request
        logger.warning("Access request limit not checked (Redis)", exc_info=True)
        return True


@router.post("", status_code=status.HTTP_202_ACCEPTED)
async def request_access(body: AccessRequestIn, request: Request):
    """Keep the request and push Ingabe staff; the same answer for every address."""
    if body.website:  # a bot: answered like everyone, kept nowhere
        return {"received": True}
    try:  # checked before the limit counts: a typo must not use up one of the hour's tries
        sub = access_requests.clean_submission(body.email, body.name, body.organization, body.reason)
    except ValueError as e:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(e)) from e
    if not await _within_limit(_client_ip(request)):
        raise HTTPException(status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                            detail="Too many requests from here just now. Please try again in an hour.")
    async with async_conn("access_requests.record") as conn:  # no user: the person is not signed in
        kept = await access_requests.record(conn, sub, "form")
        await access_requests.notify(conn, kept)
    return {"received": True}
