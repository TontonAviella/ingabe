"""Server-side PostHog analytics helpers.

The backend should never fail because analytics is unavailable. These helpers
therefore lazy-load the SDK, sanitize payloads, and swallow capture errors.
"""

from __future__ import annotations

import importlib
import logging
import os
import time
from collections.abc import Mapping
from typing import Any

logger = logging.getLogger(__name__)

_POSTHOG_MODULE: Any | None = None
_POSTHOG_READY = False
_POSTHOG_DISABLED_REASON: str | None = None

_SENSITIVE_EXACT_KEYS = {
    "authorization",
    "body",
    "content",
    "cookie",
    "message",
    "messages",
    "prompt",
}
_SENSITIVE_KEY_PARTS = (
    "api_key",
    "auth_token",
    "bearer",
    "connection_uri",
    "email",
    "etag",
    "file_name",
    "filename",
    "password",
    "s3_key",
    "secret",
    "session_token",
    "token",
    "upload_id",
    "uri",
    "url",
)
_MAX_PROPERTIES = 80
_MAX_STRING_LENGTH = 240


def _truthy_env(name: str, default: str = "0") -> bool:
    return os.environ.get(name, default).strip().lower() in {"1", "true", "yes", "on"}


def _posthog_key() -> str:
    return (
        os.environ.get("POSTHOG_API_KEY", "").strip()
        or os.environ.get("POSTHOG_PROJECT_API_KEY", "").strip()
        or os.environ.get("VITE_POSTHOG_KEY", "").strip()
    )


def _posthog_host() -> str:
    return (
        os.environ.get("POSTHOG_HOST", "").strip()
        or os.environ.get("VITE_POSTHOG_HOST", "").strip()
        or "https://us.i.posthog.com"
    )


def _load_posthog() -> Any | None:
    global _POSTHOG_DISABLED_REASON, _POSTHOG_MODULE, _POSTHOG_READY

    if _POSTHOG_READY:
        return _POSTHOG_MODULE

    if _POSTHOG_DISABLED_REASON:
        return None

    if _truthy_env("POSTHOG_BACKEND_DISABLED"):
        _POSTHOG_DISABLED_REASON = "disabled_by_env"
        return None

    api_key = _posthog_key()
    if not api_key:
        _POSTHOG_DISABLED_REASON = "missing_api_key"
        return None

    try:
        posthog = importlib.import_module("posthog")
    except Exception as exc:
        _POSTHOG_DISABLED_REASON = "sdk_unavailable"
        logger.debug("PostHog backend analytics disabled: %s", exc)
        return None

    try:
        posthog.api_key = api_key
        posthog.host = _posthog_host()
        posthog.disable_geoip = True
        posthog.privacy_mode = True
        posthog.sync_mode = _truthy_env("POSTHOG_BACKEND_SYNC")
        posthog.on_error = lambda exc: logger.debug("PostHog capture failed: %s", exc)
        _POSTHOG_MODULE = posthog
        _POSTHOG_READY = True
        return posthog
    except Exception as exc:
        _POSTHOG_DISABLED_REASON = "configuration_failed"
        logger.debug("PostHog backend analytics configuration failed: %s", exc)
        return None


def _is_sensitive_key(key: str) -> bool:
    lowered = key.strip().lower()
    return lowered in _SENSITIVE_EXACT_KEYS or any(
        marker in lowered for marker in _SENSITIVE_KEY_PARTS
    )


def _safe_value(value: Any) -> Any:
    if value is None or isinstance(value, bool):
        return value
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return round(value, 3)
    if isinstance(value, str):
        return value[:_MAX_STRING_LENGTH]
    if isinstance(value, (list, tuple, set)):
        return {"count": len(value)}
    if isinstance(value, Mapping):
        return {"keys": len(value)}
    return str(type(value).__name__)


def sanitize_properties(properties: Mapping[str, Any] | None) -> dict[str, Any]:
    """Return a small primitive-only property dict safe for product analytics."""
    sanitized: dict[str, Any] = {
        "source": "backend",
        "service": "ingabe",
    }
    if not properties:
        return sanitized

    for raw_key, raw_value in list(properties.items())[:_MAX_PROPERTIES]:
        key = str(raw_key)[:80]
        if not key or _is_sensitive_key(key):
            continue
        sanitized[key] = _safe_value(raw_value)
    return sanitized


def capture_backend_event(
    event: str,
    *,
    distinct_id: str | None = None,
    properties: Mapping[str, Any] | None = None,
    groups: Mapping[str, str] | None = None,
) -> bool:
    """Capture a PostHog event if backend analytics is configured."""
    posthog = _load_posthog()
    if posthog is None:
        return False

    safe_distinct_id = str(distinct_id or "backend-system")
    safe_properties = sanitize_properties(properties)
    safe_groups = {
        str(key): str(value)
        for key, value in (groups or {}).items()
        if key and value and not _is_sensitive_key(str(key))
    } or None

    try:
        kwargs: dict[str, Any] = {
            "distinct_id": safe_distinct_id,
            "event": event,
            "properties": safe_properties,
        }
        if safe_groups:
            kwargs["groups"] = safe_groups
        posthog.capture(**kwargs)
        return True
    except Exception as exc:
        logger.debug("PostHog backend capture failed for %s: %s", event, exc)
        return False


def capture_for_session(
    event: str,
    session: Any,
    properties: Mapping[str, Any] | None = None,
) -> bool:
    """Capture with a UserContext-like object when available."""
    distinct_id: str | None = None
    org_id: str | None = None
    try:
        distinct_id = str(session.get_user_id())
    except Exception:
        distinct_id = None
    try:
        org_id = session.get_org_id()
    except Exception:
        org_id = None

    return capture_backend_event(
        event,
        distinct_id=distinct_id,
        properties=properties,
        groups={"organization": str(org_id)} if org_id else None,
    )


def elapsed_ms(started_at: float) -> int:
    return int((time.monotonic() - started_at) * 1000)


def route_template(request: Any) -> str:
    """Return a low-cardinality path template for analytics."""
    try:
        route = request.scope.get("route")
        if route is not None and getattr(route, "path", None):
            return str(route.path)
    except Exception:
        pass
    try:
        return str(request.url.path)
    except Exception:
        return "unknown"


def reset_posthog_for_tests() -> None:
    global _POSTHOG_DISABLED_REASON, _POSTHOG_MODULE, _POSTHOG_READY
    _POSTHOG_MODULE = None
    _POSTHOG_READY = False
    _POSTHOG_DISABLED_REASON = None
