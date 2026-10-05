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
    HERMES_INGABE_TOOLSETS,
    hermes_is_enabled,
    hermes_result_failure_reason,
    select_hermes_toolsets,
)

pytestmark = pytest.mark.filterwarnings("ignore::DeprecationWarning")


# ---------------------------------------------------------------------------
# hermes_is_enabled — truthy/falsy parsing
# ---------------------------------------------------------------------------


def test_hermes_is_enabled_defaults_off_even_with_credentials(monkeypatch):
    monkeypatch.delenv("MUNDI_USE_HERMES", raising=False)
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    monkeypatch.setenv("OPENROUTER_API_KEY", "test-key")
    assert hermes_is_enabled() is False


def test_hermes_is_enabled_auto_without_openrouter_key_is_off(monkeypatch):
    monkeypatch.setenv("MUNDI_USE_HERMES", "auto")
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    monkeypatch.setenv("OPENAI_MODEL", "ollama:gemma4")
    assert hermes_is_enabled() is False


def test_hermes_is_enabled_auto_with_openrouter_key(monkeypatch):
    monkeypatch.setenv("MUNDI_USE_HERMES", "auto")
    monkeypatch.setenv("OPENROUTER_API_KEY", "test-key")
    assert hermes_is_enabled() is True


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


def test_hermes_toolsets_exclude_general_host_capabilities():
    configured = [
        "terminal",
        "file",
        "browser",
        "web",
        "skills",
        *HERMES_INGABE_TOOLSETS,
    ]

    selected = select_hermes_toolsets(
        configured,
        "analyze this drone orthophoto and add the result to the map",
    )

    assert selected == [
        "ingabe-sage-core",
        "ingabe-sage-map-view",
        "ingabe-sage-raster-vision",
    ]
    assert not {"terminal", "file", "browser", "web", "skills"} & set(selected)


def test_hermes_toolsets_keep_small_talk_on_core_only():
    selected = select_hermes_toolsets(list(HERMES_INGABE_TOOLSETS), "hello Sage")

    assert selected == ["ingabe-sage-core"]


def test_hermes_result_rejects_empty_and_incomplete_turns():
    assert hermes_result_failure_reason(
        {"completed": False, "turn_exit_reason": "empty_response_exhausted"},
        "(empty)",
    ) == "empty_response_exhausted"
    assert hermes_result_failure_reason(
        {"completed": False, "turn_exit_reason": "iteration_limit"},
        "Partial answer",
    ) == "iteration_limit"


def test_hermes_result_accepts_completed_text():
    assert hermes_result_failure_reason(
        {"completed": True, "turn_exit_reason": "text_response"},
        "The map layer is ready.",
    ) is None


@pytest.mark.parametrize(
    ("result", "text", "expected"),
    [
        ({"completed": True}, "API call failed after 3 retries: HTTP 402", "unusable_response"),
        ({"completed": True}, "upstream HTTP 503", "unusable_response"),
        ({"completed": True}, "tiles load in HTTP 5ms", None),
        ({"completed": True}, "empty", None),
        ({"completed": True}, "(empty)", "unusable_response"),
        (None, "(empty)", "unusable_response"),
        ({"completed": False}, "Partial", "unusable_response"),
    ],
)
def test_hermes_result_failure_reason_table(result, text, expected):
    assert hermes_result_failure_reason(result, text) == expected
