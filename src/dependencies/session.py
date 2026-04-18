"""Clerk-based authentication for Ingabe.

Verifies Clerk JWTs via JWKS, auto-provisions users in the local DB,
and provides FastAPI dependency functions that return a UserContext
with the real user UUID.

Backwards-compatible: when CLERK_SECRET_KEY is not set, falls back to
the legacy single-user "edit" / "view_only" mode so self-hosted and
test environments keep working without Clerk.
"""

from __future__ import annotations

import logging
import os
import time
import uuid
from abc import ABC, abstractmethod
from typing import Optional

import jwt
from fastapi import HTTPException, Request, WebSocket, status
from fastapi.exceptions import WebSocketException
from fastapi.security import HTTPBearer

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# JWKS cache — fetched once, refreshed every 6 hours
# ---------------------------------------------------------------------------
_jwks_cache: dict | None = None
_jwks_fetched_at: float = 0.0
_JWKS_TTL = 6 * 3600  # seconds


def _get_clerk_jwks_url() -> str | None:
    """Build the JWKS URL from CLERK_ISSUER or CLERK_FRONTEND_API."""
    issuer = os.environ.get("CLERK_ISSUER")
    if issuer:
        return f"{issuer.rstrip('/')}/.well-known/jwks.json"
    frontend_api = os.environ.get("CLERK_FRONTEND_API")
    if frontend_api:
        return f"https://{frontend_api}/.well-known/jwks.json"
    return None


def _fetch_jwks() -> dict:
    """Fetch JWKS from Clerk (cached with TTL)."""
    global _jwks_cache, _jwks_fetched_at

    now = time.time()
    if _jwks_cache and (now - _jwks_fetched_at) < _JWKS_TTL:
        return _jwks_cache

    import requests

    url = _get_clerk_jwks_url()
    if not url:
        raise RuntimeError(
            "Cannot fetch JWKS: set CLERK_ISSUER or CLERK_FRONTEND_API"
        )

    resp = requests.get(url, timeout=5)
    resp.raise_for_status()
    _jwks_cache = resp.json()
    _jwks_fetched_at = now
    logger.info("Refreshed Clerk JWKS from %s", url)
    return _jwks_cache


def _get_signing_key(token: str) -> jwt.algorithms.RSAAlgorithm:
    """Find the RSA public key matching the token's kid."""
    jwks = _fetch_jwks()
    unverified = jwt.get_unverified_header(token)
    kid = unverified.get("kid")

    for key_data in jwks.get("keys", []):
        if key_data.get("kid") == kid:
            return jwt.algorithms.RSAAlgorithm.from_jwk(key_data)

    # kid not found — force refresh and retry once
    global _jwks_fetched_at
    _jwks_fetched_at = 0.0
    jwks = _fetch_jwks()
    for key_data in jwks.get("keys", []):
        if key_data.get("kid") == kid:
            return jwt.algorithms.RSAAlgorithm.from_jwk(key_data)

    raise jwt.InvalidTokenError(f"No matching key found for kid={kid}")


# ---------------------------------------------------------------------------
# User context — abstract base + two concrete implementations
# ---------------------------------------------------------------------------


class UserContext(ABC):
    @abstractmethod
    def get_user_id(self) -> str:
        """Return the internal UUID string for this user."""
        pass

    def get_clerk_id(self) -> str | None:
        """Return the Clerk user ID (e.g. 'user_2NNE...'), or None for legacy mode."""
        return None

    def get_email(self) -> str | None:
        """Return the user's email from the JWT, or None."""
        return None

    def get_org_id(self) -> str | None:
        """Return the internal org UUID string, or None if no org context."""
        return None

    def get_org_role(self) -> str | None:
        """Return the user's role within the active org (owner/admin/member)."""
        return None


class ClerkUserContext(UserContext):
    """Real user authenticated via Clerk JWT."""

    def __init__(
        self,
        internal_uuid: str,
        clerk_id: str,
        email: str | None = None,
        org_id: str | None = None,
        org_role: str | None = None,
    ):
        self._uuid = internal_uuid
        self._clerk_id = clerk_id
        self._email = email
        self._org_id = org_id
        self._org_role = org_role

    def get_user_id(self) -> str:
        return self._uuid

    def get_clerk_id(self) -> str | None:
        return self._clerk_id

    def get_email(self) -> str | None:
        return self._email

    def get_org_id(self) -> str | None:
        return self._org_id

    def get_org_role(self) -> str | None:
        return self._org_role


class LegacyUserContext(UserContext):
    """Backwards-compatible single-user context for self-hosted / test."""

    _LEGACY_UUID = "00000000-0000-0000-0000-000000000000"

    def get_user_id(self) -> str:
        return self._LEGACY_UUID


# Keep old name for backwards compatibility in tests
EditOrReadOnlyUserContext = LegacyUserContext


# ---------------------------------------------------------------------------
# JWT verification
# ---------------------------------------------------------------------------

_bearer_scheme = HTTPBearer(auto_error=False)


def _is_clerk_enabled() -> bool:
    return bool(os.environ.get("CLERK_SECRET_KEY"))


def _decode_clerk_jwt(token: str) -> dict:
    """Verify and decode a Clerk JWT. Raises on failure."""
    key = _get_signing_key(token)
    issuer = os.environ.get("CLERK_ISSUER")

    decode_opts = {
        "algorithms": ["RS256"],
        "options": {"verify_exp": True, "verify_nbf": True},
    }
    if issuer:
        decode_opts["issuer"] = issuer

    return jwt.decode(token, key, **decode_opts)


async def _get_or_create_user(clerk_id: str, email: str | None) -> str:
    """Look up or auto-provision a user row, returning the internal UUID.

    Uses UUID5 (deterministic from clerk_id) so the same Clerk user always
    gets the same UUID — no race conditions even under concurrent requests.
    """
    from src.structures import async_conn

    # Deterministic UUID from Clerk ID
    internal_uuid = str(uuid.uuid5(uuid.NAMESPACE_URL, f"clerk:{clerk_id}"))

    async with async_conn("clerk_user_provision") as conn:
        existing = await conn.fetchval(
            "SELECT internal_uuid FROM users WHERE clerk_id = $1",
            clerk_id,
        )
        if existing:
            return str(existing)

        # Insert new user (ON CONFLICT for race-condition safety)
        await conn.execute(
            """
            INSERT INTO users (internal_uuid, clerk_id, email, created_at)
            VALUES ($1, $2, $3, CURRENT_TIMESTAMP)
            ON CONFLICT (clerk_id) DO UPDATE SET email = EXCLUDED.email
            """,
            internal_uuid,
            clerk_id,
            email,
        )
        logger.info("Provisioned new user clerk_id=%s uuid=%s", clerk_id, internal_uuid)
        return internal_uuid


# ---------------------------------------------------------------------------
# FastAPI dependencies — drop-in replacements for existing code
# ---------------------------------------------------------------------------

def _extract_token_from_request(request: Request) -> str | None:
    """Extract Bearer token from Authorization header."""
    auth_header = request.headers.get("authorization", "")
    if auth_header.startswith("Bearer "):
        return auth_header[7:]
    return None


async def _resolve_clerk_org(clerk_org_id: str) -> str | None:
    """Map a Clerk org_id (org_xxx) to our internal organizations.id UUID."""
    from src.structures import async_conn

    async with async_conn("resolve_clerk_org") as conn:
        row = await conn.fetchval(
            "SELECT id FROM organizations WHERE clerk_org_id = $1",
            clerk_org_id,
        )
        return str(row) if row else None


async def _authenticate_clerk(token: str) -> ClerkUserContext:
    """Verify Clerk JWT and return a ClerkUserContext."""
    try:
        claims = _decode_clerk_jwt(token)
    except jwt.ExpiredSignatureError:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Token expired")
    except jwt.InvalidTokenError as e:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail=f"Invalid token: {e}")

    clerk_id = claims.get("sub")
    if not clerk_id:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Token missing sub claim")

    email = claims.get("email")
    internal_uuid = await _get_or_create_user(clerk_id, email)

    org_id: str | None = None
    org_role: str | None = None
    clerk_org_id = claims.get("org_id")
    if clerk_org_id:
        org_id = await _resolve_clerk_org(clerk_org_id)
        org_role = claims.get("org_role")
        if org_id:
            logger.info("Resolved clerk org %s → %s", clerk_org_id, org_id)
        else:
            logger.warning("Clerk org %s not found in organizations table", clerk_org_id)

    return ClerkUserContext(internal_uuid, clerk_id, email, org_id, org_role)


def verify_session(session_required: bool = True):
    async def _verify_session(request: Request = None) -> Optional[UserContext]:
        # --- Clerk mode: validate token if present ---
        if _is_clerk_enabled():
            token = _extract_token_from_request(request) if request else None
            if token:
                return await _authenticate_clerk(token)
            # No token — block by default to prevent the shared-UUID
            # cross-tenant data leak.  Set CLERK_ALLOW_LEGACY_FALLBACK=true
            # during migration to temporarily restore old behavior.
            if not os.environ.get("CLERK_ALLOW_LEGACY_FALLBACK", "").lower() == "true":
                if session_required:
                    raise HTTPException(
                        status_code=status.HTTP_401_UNAUTHORIZED,
                        detail="Authentication required — Bearer token missing",
                    )
                return None
            logger.warning(
                "Clerk enabled but no Bearer token — legacy fallback allowed by CLERK_ALLOW_LEGACY_FALLBACK"
            )

        # --- Legacy mode (fallback when no Clerk token) ---
        auth_mode = os.environ.get("MUNDI_AUTH_MODE")
        if auth_mode == "edit":
            return LegacyUserContext()
        elif auth_mode == "view_only":
            if session_required:
                raise HTTPException(
                    status_code=status.HTTP_401_UNAUTHORIZED,
                    detail="Authentication required",
                )
            return None

        # Clerk enabled but no token and no legacy mode
        if _is_clerk_enabled() and session_required:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Authentication required",
            )

        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Set CLERK_SECRET_KEY for Clerk auth, or MUNDI_AUTH_MODE for legacy mode",
        )

    return _verify_session


# Convenience functions used as FastAPI Depends() across all routes
async def verify_session_required(request: Request = None) -> Optional[UserContext]:
    return await verify_session(session_required=True)(request)


async def verify_session_optional(request: Request = None) -> Optional[UserContext]:
    return await verify_session(session_required=False)(request)


async def session_user_id(request: Request = None) -> str:
    session = await verify_session_required(request)
    return session.get_user_id()


# ---------------------------------------------------------------------------
# WebSocket authentication
# ---------------------------------------------------------------------------

async def verify_websocket(websocket: WebSocket) -> UserContext:
    """Authenticate WebSocket connections.

    Clerk mode: expects ?token=<jwt> query param.  When no token is
    provided, falls back to MUNDI_AUTH_MODE so routes behind OptionalAuth
    (e.g. ProjectView) can still use the WebSocket in edit mode.
    Legacy mode: allows all in edit mode, denies in view_only.
    """
    if _is_clerk_enabled():
        token = websocket.query_params.get("token")
        if token:
            try:
                claims = _decode_clerk_jwt(token)
            except jwt.ExpiredSignatureError as e:
                logger.warning("WS JWT expired: %s (token_prefix=%s)", e, token[:20])
                raise WebSocketException(code=status.WS_1008_POLICY_VIOLATION)
            except jwt.InvalidTokenError as e:
                logger.warning(
                    "WS JWT decode failed: %s: %s (token_prefix=%s)",
                    type(e).__name__, e, token[:20],
                )
                raise WebSocketException(code=status.WS_1008_POLICY_VIOLATION)
            except Exception as e:
                logger.warning(
                    "WS JWT unexpected error: %s: %s (token_prefix=%s)",
                    type(e).__name__, e, token[:20],
                )
                raise WebSocketException(code=status.WS_1008_POLICY_VIOLATION)

            clerk_id = claims.get("sub")
            if not clerk_id:
                raise WebSocketException(code=status.WS_1008_POLICY_VIOLATION)

            email = claims.get("email")
            internal_uuid = await _get_or_create_user(clerk_id, email)

            org_id: str | None = None
            org_role: str | None = None
            clerk_org_id = claims.get("org_id")
            if clerk_org_id:
                org_id = await _resolve_clerk_org(clerk_org_id)
                org_role = claims.get("org_role")

            return ClerkUserContext(internal_uuid, clerk_id, email, org_id, org_role)
        # No token supplied — block by default to prevent cross-tenant leak.
        logger.warning(
            "WS: Clerk enabled but NO ?token= query param in WebSocket URL (client not sending JWT)"
        )
        if not os.environ.get("CLERK_ALLOW_LEGACY_FALLBACK", "").lower() == "true":
            raise WebSocketException(code=status.WS_1008_POLICY_VIOLATION)
        logger.warning(
            "WS: Clerk enabled but no token — legacy fallback allowed by CLERK_ALLOW_LEGACY_FALLBACK"
        )

    # Legacy / fallback mode
    auth_mode = os.environ.get("MUNDI_AUTH_MODE")
    if auth_mode == "edit":
        return LegacyUserContext()
    elif auth_mode == "view_only":
        raise WebSocketException(code=status.WS_1008_POLICY_VIOLATION)
    else:
        raise WebSocketException(code=status.WS_1011_INTERNAL_ERROR)
