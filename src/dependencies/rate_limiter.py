"""Application-level rate limiting using slowapi + Redis.

Three tiers:
  - expensive:  LLM chat, AI endpoints           (default: 20/minute)
  - heavy:      file uploads, QGIS processing     (default: 10/minute)
  - general:    all other /api/* routes            (default: 120/minute)

Keyed by authenticated user ID when available, falls back to client IP.
Configurable via environment variables.

Disabled entirely when RATE_LIMIT_ENABLED=false (useful for local dev / tests).
"""

import logging
import os

from fastapi import Request
from slowapi import Limiter
from slowapi.errors import RateLimitExceeded
from slowapi.util import get_remote_address
from starlette.responses import JSONResponse

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Key function: prefer user ID from auth, fall back to IP
# ---------------------------------------------------------------------------

def _get_rate_limit_key(request: Request) -> str:
    """Extract a rate-limit key from the request.

    Uses the authenticated user ID stored by auth middleware when available,
    otherwise falls back to the client IP address.
    """
    # Auth dependencies store user context in request.state
    user_ctx = getattr(request.state, "user_context", None)
    if user_ctx is not None:
        try:
            return f"user:{user_ctx.get_user_id()}"
        except Exception:
            pass
    return get_remote_address(request)


# ---------------------------------------------------------------------------
# Rate limits from env (with sensible defaults)
# ---------------------------------------------------------------------------

RATE_LIMIT_ENABLED = os.environ.get("RATE_LIMIT_ENABLED", "true").lower() != "false"

_EXPENSIVE_LIMIT = os.environ.get("RATE_LIMIT_EXPENSIVE", "20/minute")
_HEAVY_LIMIT = os.environ.get("RATE_LIMIT_HEAVY", "10/minute")
_GENERAL_LIMIT = os.environ.get("RATE_LIMIT_GENERAL", "120/minute")

# ---------------------------------------------------------------------------
# Redis storage URI
# ---------------------------------------------------------------------------

_redis_host = os.environ.get("REDIS_HOST", "localhost")
_redis_port = os.environ.get("REDIS_PORT", "6379")
_REDIS_URI = f"redis://{_redis_host}:{_redis_port}"

# ---------------------------------------------------------------------------
# Limiter instance
# ---------------------------------------------------------------------------

limiter = Limiter(
    key_func=_get_rate_limit_key,
    default_limits=[_GENERAL_LIMIT],
    storage_uri=_REDIS_URI,
    enabled=RATE_LIMIT_ENABLED,
    strategy="fixed-window",
)

# ---------------------------------------------------------------------------
# Decorators for route-level use
# ---------------------------------------------------------------------------

expensive_limit = limiter.limit(_EXPENSIVE_LIMIT)
heavy_limit = limiter.limit(_HEAVY_LIMIT)
general_limit = limiter.limit(_GENERAL_LIMIT)

# ---------------------------------------------------------------------------
# 429 error handler
# ---------------------------------------------------------------------------

async def rate_limit_exceeded_handler(request: Request, exc: RateLimitExceeded) -> JSONResponse:
    """Return a JSON 429 response with Retry-After header."""
    retry_after = exc.detail.split("per")[0].strip() if exc.detail else "60"
    logger.warning(
        "Rate limit exceeded: %s %s key=%s detail=%s",
        request.method,
        request.url.path,
        _get_rate_limit_key(request),
        exc.detail,
    )
    return JSONResponse(
        status_code=429,
        content={
            "detail": "Too many requests. Please slow down.",
            "retry_after": retry_after,
        },
        headers={"Retry-After": retry_after},
    )
