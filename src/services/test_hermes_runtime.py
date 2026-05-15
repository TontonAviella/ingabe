"""Unit tests for hermes_runtime helpers.

Covers the bits that don't need a live ACP connection: the session-cache
lookup logic, the cancellation watchdog, and the flag parser. Full-loop
integration is exercised separately by the prod smoke test (TCP probe of
the bridge — see project_hermes_phase2_validated memory).
"""
from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.services.hermes_runtime import (
    CANCEL_POLL_INTERVAL_SECONDS,
    SESSION_REDIS_KEY,
    SESSION_TTL_SECONDS,
    _cancel_watchdog,
    _resume_or_create_session,
    hermes_is_enabled,
)


# ---------------------------------------------------------------------------
# hermes_is_enabled — truthy/falsy parsing
# ---------------------------------------------------------------------------


def test_hermes_is_enabled_default_off(monkeypatch):
    """Unset env defaults to OFF — Sage stays on the hand-rolled loop."""
    monkeypatch.delenv("MUNDI_USE_HERMES", raising=False)
    assert hermes_is_enabled() is False


def test_hermes_is_enabled_truthy_values(monkeypatch):
    """1/true/yes (case-insensitive) all flip the flag on."""
    for v in ("1", "true", "TRUE", "True", "yes", "YES"):
        monkeypatch.setenv("MUNDI_USE_HERMES", v)
        assert hermes_is_enabled() is True, f"expected True for {v!r}"


def test_hermes_is_enabled_falsy_values(monkeypatch):
    """Anything else stays off — no surprise truthiness."""
    for v in ("0", "false", "no", "off", "", "enabled"):
        monkeypatch.setenv("MUNDI_USE_HERMES", v)
        assert hermes_is_enabled() is False, f"expected False for {v!r}"


# ---------------------------------------------------------------------------
# _resume_or_create_session — Redis cache + load_session/new_session branch
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_resume_uses_cached_session_id():
    """Cached id in Redis → load_session called with that id, no new_session."""
    cached_id = "sess_abc123"
    conv_id = "conv_xyz"

    conn = MagicMock()
    conn.load_session = AsyncMock(return_value=None)
    conn.new_session = AsyncMock()

    fake_redis = MagicMock()
    fake_redis.get.return_value = cached_id

    with patch("src.dependencies.redis_client.get_redis_client", return_value=fake_redis):
        # acp module not used inside the helper anymore — pass a marker
        result = await _resume_or_create_session(conn, object(), conv_id)

    assert result == cached_id
    conn.load_session.assert_awaited_once_with(
        cwd="/tmp", session_id=cached_id, mcp_servers=[],
    )
    conn.new_session.assert_not_awaited()
    # TTL refreshed on hit so active conversations don't expire mid-day
    fake_redis.expire.assert_called_once_with(
        SESSION_REDIS_KEY.format(conversation_id=conv_id),
        SESSION_TTL_SECONDS,
    )


@pytest.mark.asyncio
async def test_no_cache_falls_through_to_new_session():
    """Empty Redis → new_session called, id written to cache."""
    new_id = "sess_fresh999"
    conv_id = "conv_xyz"

    conn = MagicMock()
    conn.load_session = AsyncMock()
    new_session_resp = MagicMock()
    new_session_resp.session_id = new_id
    conn.new_session = AsyncMock(return_value=new_session_resp)

    fake_redis = MagicMock()
    fake_redis.get.return_value = None

    with patch("src.dependencies.redis_client.get_redis_client", return_value=fake_redis):
        result = await _resume_or_create_session(conn, object(), conv_id)

    assert result == new_id
    conn.load_session.assert_not_awaited()
    conn.new_session.assert_awaited_once_with(cwd="/tmp", mcp_servers=[])
    fake_redis.set.assert_called_once_with(
        SESSION_REDIS_KEY.format(conversation_id=conv_id),
        new_id,
        ex=SESSION_TTL_SECONDS,
    )


@pytest.mark.asyncio
async def test_failed_load_session_falls_back_to_new():
    """Hermes restarted → load_session raises → graceful fallback to new_session.

    User gets a response (loses context), better than crashing the turn.
    """
    stale_id = "sess_stale_pre_restart"
    new_id = "sess_post_restart"
    conv_id = "conv_xyz"

    conn = MagicMock()
    conn.load_session = AsyncMock(side_effect=RuntimeError("session expired"))
    new_session_resp = MagicMock()
    new_session_resp.session_id = new_id
    conn.new_session = AsyncMock(return_value=new_session_resp)

    fake_redis = MagicMock()
    fake_redis.get.return_value = stale_id

    with patch("src.dependencies.redis_client.get_redis_client", return_value=fake_redis):
        result = await _resume_or_create_session(conn, object(), conv_id)

    assert result == new_id
    conn.load_session.assert_awaited_once()
    conn.new_session.assert_awaited_once()


@pytest.mark.asyncio
async def test_redis_unavailable_still_creates_session():
    """If Redis is down entirely, we still serve the turn (just without resume).

    Caching is best-effort; never break the user-facing path on it.
    """
    new_id = "sess_no_redis"
    conv_id = "conv_xyz"

    conn = MagicMock()
    new_session_resp = MagicMock()
    new_session_resp.session_id = new_id
    conn.new_session = AsyncMock(return_value=new_session_resp)

    with patch(
        "src.dependencies.redis_client.get_redis_client",
        side_effect=RuntimeError("redis down"),
    ):
        result = await _resume_or_create_session(conn, object(), conv_id)

    assert result == new_id
    conn.new_session.assert_awaited_once()


# ---------------------------------------------------------------------------
# _cancel_watchdog — Redis polling → conn.cancel
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_cancel_watchdog_fires_on_redis_key(monkeypatch):
    """When the cancel key is set, watchdog calls conn.cancel and consumes the key."""
    # Speed up the test by making the poll interval tiny
    monkeypatch.setattr(
        "src.services.hermes_runtime.CANCEL_POLL_INTERVAL_SECONDS", 0.01
    )

    conn = MagicMock()
    conn.cancel = AsyncMock()

    fake_redis = MagicMock()
    # Two reads: first returns nothing, second returns the cancel marker
    fake_redis.get.side_effect = [None, "cancelled"]

    with patch("src.dependencies.redis_client.get_redis_client", return_value=fake_redis):
        await asyncio.wait_for(
            _cancel_watchdog(
                conn, session_id="s1", message_id="m1",
                map_id="M1", conversation_id="C1",
            ),
            timeout=2.0,
        )

    conn.cancel.assert_awaited_once_with(session_id="s1")
    fake_redis.delete.assert_called_once_with("messages:M1:cancelled")


@pytest.mark.asyncio
async def test_cancel_watchdog_handles_cancellation(monkeypatch):
    """When the watchdog itself is cancelled (turn ended cleanly), exit silently."""
    monkeypatch.setattr(
        "src.services.hermes_runtime.CANCEL_POLL_INTERVAL_SECONDS", 0.01
    )

    conn = MagicMock()
    conn.cancel = AsyncMock()

    fake_redis = MagicMock()
    fake_redis.get.return_value = None  # never fires

    with patch("src.dependencies.redis_client.get_redis_client", return_value=fake_redis):
        task = asyncio.create_task(
            _cancel_watchdog(
                conn, session_id="s1", message_id="m1",
                map_id="M1", conversation_id="C1",
            )
        )
        await asyncio.sleep(0.05)
        task.cancel()
        # Should exit without raising
        try:
            await task
        except asyncio.CancelledError:
            pass

    conn.cancel.assert_not_awaited()


@pytest.mark.asyncio
async def test_cancel_watchdog_survives_redis_errors(monkeypatch):
    """Transient Redis failures shouldn't kill the watchdog — keep polling."""
    monkeypatch.setattr(
        "src.services.hermes_runtime.CANCEL_POLL_INTERVAL_SECONDS", 0.01
    )

    conn = MagicMock()
    conn.cancel = AsyncMock()

    fake_redis = MagicMock()
    # First .get raises, second returns the cancel marker — verifies the
    # watchdog kept polling past the error
    fake_redis.get.side_effect = [RuntimeError("blip"), "cancelled"]

    with patch("src.dependencies.redis_client.get_redis_client", return_value=fake_redis):
        await asyncio.wait_for(
            _cancel_watchdog(
                conn, session_id="s1", message_id="m1",
                map_id="M1", conversation_id="C1",
            ),
            timeout=2.0,
        )

    conn.cancel.assert_awaited_once()


# ---------------------------------------------------------------------------
# Module-level constants — sanity checks (lock in the API surface)
# ---------------------------------------------------------------------------


def test_redis_key_format_uses_conversation_id():
    """Lock in the key template so an accidental change shows up in code review."""
    assert SESSION_REDIS_KEY == "hermes:session:{conversation_id}"


def test_session_ttl_is_24h():
    """TTL changes have product implications (lost context); flag any tweak."""
    assert SESSION_TTL_SECONDS == 86400


def test_cancel_poll_interval_under_2s():
    """User-facing cancel button should feel responsive."""
    assert CANCEL_POLL_INTERVAL_SECONDS <= 2.0
