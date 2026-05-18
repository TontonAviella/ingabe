"""Unit tests for hermes_runtime helpers.

After the in-process AIAgent pivot, Hermes owns session lifecycle (via
`session_id=conv-<id>` passed to AIAgent), so the ACP-era helpers
(_cancel_watchdog, _resume_or_create_session, SESSION_REDIS_KEY,
SESSION_TTL_SECONDS) no longer exist and their tests were removed.
Remaining coverage: env-flag parsing and the cancel-poll budget. Full
end-to-end coverage lives in the prod smoke test — see
project_hermes_phase2_validated memory.
"""
from __future__ import annotations

import pytest

from src.services.hermes_runtime import (
    CANCEL_POLL_INTERVAL_SECONDS,
    hermes_is_enabled,
)

pytestmark = pytest.mark.filterwarnings("ignore::DeprecationWarning")


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
# Cancel poll budget — UX-facing invariant
# ---------------------------------------------------------------------------


def test_cancel_poll_interval_under_2s():
    """User-facing cancel button should feel responsive."""
    assert CANCEL_POLL_INTERVAL_SECONDS <= 2.0
