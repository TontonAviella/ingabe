"""A stand-in for the WorkOS API for sign-in tests, used through the real SDK.

Access tokens are real RS256 JWTs checked against a real JWKS, and cookies are
sealed with the SDK's own code on a realistic secret, so tests exercise the
library itself (CODING_STANDARDS.md, lesson of 2026-10-05). Only the network is
replaced: the SDK client's HTTP transport and PyJWKClient's JWKS download.

Refresh tokens are strictly single-use here. (On 2026-10-06 WorkOS accepted one
reuse half a second later, so a short grace window may exist; the code must
not depend on it.) ``clock_ahead`` is how far WorkOS' clock runs ahead of ours.
"""

from __future__ import annotations

import json
import secrets
import time
import uuid
from typing import Any, Optional

import httpx
import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa

from src.services import workos_auth

USER = {
    "object": "user", "id": "user_01TEST", "email": "agronome@bk.rw", "email_verified": True,
    "first_name": "Aline", "last_name": "Uwase", "profile_picture_url": None, "external_id": None,
    "last_sign_in_at": "2026-10-06T08:00:00.000Z", "created_at": "2026-01-10T08:00:00.000Z",
    "updated_at": "2026-10-06T08:00:00.000Z", "metadata": {}, "locale": None,
}


class FakeWorkOS:
    def __init__(self, cookie_secret: str) -> None:
        self.cookie_password = workos_auth.cookie_key(cookie_secret)
        self.clock_ahead = 0.0
        self.refresh_calls = 0
        self.down = False  # True: every API call fails to connect
        self._key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        self._kid = f"kid_{uuid.uuid4().hex}"  # unique: the SDK's JWKS client is cached across tests
        self._usable: dict[str, bool] = {}  # refresh token -> not yet exchanged

    # -- what WorkOS hands out ------------------------------------------------

    def jwks(self) -> dict[str, Any]:
        jwk = jwt.algorithms.RSAAlgorithm.to_jwk(self._key.public_key(), as_dict=True)
        return {"keys": [{**jwk, "kid": self._kid, "alg": "RS256", "use": "sig"}]}

    def access_token(self, issued_at: Optional[float] = None, lifetime: int = 300) -> str:
        iat = int(time.time() + self.clock_ahead if issued_at is None else issued_at)
        claims = {"iss": "https://api.workos.com", "sub": USER["id"], "sid": "session_01TEST",
                  "jti": uuid.uuid4().hex, "org_id": "org_01TEST", "role": "admin",
                  "permissions": [], "iat": iat, "exp": iat + lifetime}
        return jwt.encode(claims, self._key, algorithm="RS256", headers={"kid": self._kid})

    def refresh_token(self) -> str:
        token = secrets.token_urlsafe(18)
        self._usable[token] = True
        return token

    def cookie(self, expired: bool = True) -> str:
        """A session cookie as /auth/callback sets it; by default its 5-minute access token has expired."""
        from workos.session import seal_session_from_auth_response  # lazy: WorkOS SDK

        issued_at = time.time() - 3600 if expired else None
        return seal_session_from_auth_response(
            access_token=self.access_token(issued_at), refresh_token=self.refresh_token(),
            user=USER, cookie_password=self.cookie_password)

    def unspent(self, refresh_token: str) -> bool:
        return self._usable.get(refresh_token, False)

    def tokens_in(self, cookie: str) -> dict[str, Any]:
        from workos.session import unseal_data  # lazy: WorkOS SDK

        return unseal_data(cookie, self.cookie_password)

    # -- the API --------------------------------------------------------------

    def handle(self, request: httpx.Request) -> httpx.Response:
        if self.down:
            raise httpx.ConnectError("Network is unreachable", request=request)
        body = json.loads(request.content or b"{}")
        if request.url.path != "/user_management/authenticate" or body.get("grant_type") != "refresh_token":
            return httpx.Response(404, json={"message": "not faked"})
        self.refresh_calls += 1
        if not self._usable.get(body.get("refresh_token", "")):
            return httpx.Response(400, json={"error": "invalid_grant",
                                             "error_description": "Refresh token already exchanged."})
        self._usable[body["refresh_token"]] = False
        return httpx.Response(200, json={
            "user": USER, "organization_id": "org_01TEST", "authentication_method": "GoogleOAuth",
            "access_token": self.access_token(), "refresh_token": self.refresh_token()})


@pytest.fixture(name="fake_workos")
def fake_workos_fixture(monkeypatch):
    """WorkOS as the sign-in provider, with the real SDK client talking to a FakeWorkOS."""
    import workos._base_client  # lazy: WorkOS SDK

    secret = secrets.token_hex(32)  # 64 hex characters, like the real WORKOS_COOKIE_PASSWORD
    for k, v in {"AUTH_PROVIDER": "workos", "WORKOS_API_KEY": "sk_test_fake", "WORKOS_CLIENT_ID": "client_fake",
                 "WORKOS_COOKIE_PASSWORD": secret}.items():
        monkeypatch.setenv(k, v)
    fake = FakeWorkOS(secret)
    monkeypatch.setattr(jwt.PyJWKClient, "fetch_data", lambda self: fake.jwks())
    monkeypatch.setattr(workos._base_client.time, "sleep", lambda _seconds: None)  # the SDK's retry backoff
    workos_auth._client.cache_clear()
    client = workos_auth._client()
    client._client = httpx.Client(transport=httpx.MockTransport(fake.handle))
    yield fake
    workos_auth._client.cache_clear()
