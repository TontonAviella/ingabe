import os
from typing import List
from fastapi import HTTPException, status, Request
from urllib.parse import urlparse


async def require_auth(
    request: Request,
) -> List[str]:
    allowed_origins_env = os.environ.get("MUNDI_EMBED_ALLOWED_ORIGINS")
    if not allowed_origins_env:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Not found",
        )

    allowed_origins = [
        origin.strip() for origin in allowed_origins_env.split(",") if origin.strip()
    ]
    if not allowed_origins:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Not found",
        )

    origin = request.headers.get("origin", "")
    referer = request.headers.get("referer", "")

    origin_allowed = False
    if origin and origin in allowed_origins:
        origin_allowed = True
    elif referer:
        referer_origin = f"{urlparse(referer).scheme}://{urlparse(referer).netloc}"
        if referer_origin in allowed_origins:
            origin_allowed = True

    if not origin_allowed:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Not found",
        )

    return allowed_origins
