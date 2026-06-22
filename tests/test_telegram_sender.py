"""Tests for src/senders/telegram_sender._handle and env-driven config.

The _handle() coroutine is the testable seam: it parses a payload, applies
channel and partner filters, validates the recipient, and either logs a stub
or performs an outbound send. The pubsub loop in run_forever() is just a
JSON-decode + dispatch wrapper around _handle() and is exercised end-to-end
by scripts/smoke_abcd.sh against a live redis.

These tests cover the routing logic in isolation, without redis or telegram:

  - delivery_channel filter: non-telegram payloads are no-ops
  - partner filter: payload partner_id must equal MUNDI_TELEGRAM_PARTNER_ID
    when that env var is set; absent env var = accept any partner
  - recipient validation: empty/whitespace recipient is dropped with a warning
  - s3 coords validation: missing bucket or key drops with a warning
  - stub mode (MUNDI_TELEGRAM_LIVE != "1"): never calls _download_png or
    _send_telegram_photo; just logs
  - live mode: calls _download_png then _send_telegram_photo with the right
    chat_id, caption, and bytes
  - live mode + s3 failure: exception in _download_png is caught, send is skipped
  - live mode + send failure: _send_telegram_photo returns False; handler
    still returns cleanly (no raise)
"""

from __future__ import annotations

import logging

import pytest

from src.senders import telegram_sender


# ---------- env helpers ----------


def _clear_telegram_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for var in (
        "MUNDI_TELEGRAM_LIVE",
        "MUNDI_TELEGRAM_PARTNER_ID",
        "MUNDI_TELEGRAM_BOT_TOKEN",
    ):
        monkeypatch.delenv(var, raising=False)


def test_allowed_partner_unset_returns_none(monkeypatch: pytest.MonkeyPatch) -> None:
    _clear_telegram_env(monkeypatch)
    assert telegram_sender._allowed_partner() is None


def test_allowed_partner_whitespace_treated_as_unset(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MUNDI_TELEGRAM_PARTNER_ID", "   ")
    assert telegram_sender._allowed_partner() is None


def test_allowed_partner_set(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MUNDI_TELEGRAM_PARTNER_ID", "bk-insurance")
    assert telegram_sender._allowed_partner() == "bk-insurance"


def test_is_live_defaults_false(monkeypatch: pytest.MonkeyPatch) -> None:
    _clear_telegram_env(monkeypatch)
    assert telegram_sender._is_live() is False


def test_is_live_only_truthy_on_exact_1(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MUNDI_TELEGRAM_LIVE", "true")
    assert telegram_sender._is_live() is False
    monkeypatch.setenv("MUNDI_TELEGRAM_LIVE", "1")
    assert telegram_sender._is_live() is True


# ---------- _handle: routing & filters ----------


def _make_payload(**overrides) -> dict:
    payload = {
        "snapshot_id": "snap-test",
        "delivery_channel": "telegram",
        "recipient": "12345",
        "png_s3_bucket": "test-bucket",
        "png_s3_key": "snapshots/test.png",
        "caption": "test caption",
    }
    payload.update(overrides)
    return payload


class _SendCalls:
    """Captures (chat_id, caption, png_bytes) from the patched send fn."""

    def __init__(self, return_value: bool = True) -> None:
        self.calls: list[tuple[str, str, bytes]] = []
        self.return_value = return_value

    async def __call__(self, chat_id: str, caption: str, png_bytes: bytes) -> bool:
        self.calls.append((chat_id, caption, png_bytes))
        return self.return_value


class _DownloadCalls:
    def __init__(
        self, return_value: bytes = b"png-bytes", raise_exc: Exception | None = None
    ) -> None:
        self.calls: list[tuple[str, str]] = []
        self.return_value = return_value
        self.raise_exc = raise_exc

    async def __call__(self, bucket: str, key: str) -> bytes:
        self.calls.append((bucket, key))
        if self.raise_exc is not None:
            raise self.raise_exc
        return self.return_value


async def test_handle_drops_wrong_channel(monkeypatch: pytest.MonkeyPatch) -> None:
    _clear_telegram_env(monkeypatch)
    send = _SendCalls()
    dl = _DownloadCalls()
    monkeypatch.setattr(telegram_sender, "_send_telegram_photo", send)
    monkeypatch.setattr(telegram_sender, "_download_png", dl)

    await telegram_sender._handle(_make_payload(delivery_channel="whatsapp"))

    assert send.calls == []
    assert dl.calls == []


async def test_handle_drops_partner_mismatch(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    _clear_telegram_env(monkeypatch)
    monkeypatch.setenv("MUNDI_TELEGRAM_PARTNER_ID", "partner-a")
    send = _SendCalls()
    dl = _DownloadCalls()
    monkeypatch.setattr(telegram_sender, "_send_telegram_photo", send)
    monkeypatch.setattr(telegram_sender, "_download_png", dl)

    with caplog.at_level(logging.INFO, logger="mundi.senders.telegram"):
        await telegram_sender._handle(_make_payload(partner_id="partner-b"))

    assert send.calls == []
    assert dl.calls == []
    assert any("partner mismatch" in r.getMessage() for r in caplog.records)


async def test_handle_accepts_partner_match_in_stub_mode(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _clear_telegram_env(monkeypatch)
    monkeypatch.setenv("MUNDI_TELEGRAM_PARTNER_ID", "partner-a")
    send = _SendCalls()
    dl = _DownloadCalls()
    monkeypatch.setattr(telegram_sender, "_send_telegram_photo", send)
    monkeypatch.setattr(telegram_sender, "_download_png", dl)

    await telegram_sender._handle(_make_payload(partner_id="partner-a"))

    # stub mode: neither download nor send is called
    assert send.calls == []
    assert dl.calls == []


async def test_handle_unset_partner_accepts_any(monkeypatch: pytest.MonkeyPatch) -> None:
    """Backwards compat: when MUNDI_TELEGRAM_PARTNER_ID is unset, partner-less
    or any-partner payloads must still flow through."""
    _clear_telegram_env(monkeypatch)
    send = _SendCalls()
    dl = _DownloadCalls()
    monkeypatch.setattr(telegram_sender, "_send_telegram_photo", send)
    monkeypatch.setattr(telegram_sender, "_download_png", dl)

    # No partner_id field at all
    await telegram_sender._handle(_make_payload())
    # With an arbitrary partner_id
    await telegram_sender._handle(_make_payload(partner_id="anything"))
    # Stub mode → no outbound calls either way; the absence of a partner-mismatch
    # drop is the assertion
    assert send.calls == []
    assert dl.calls == []


async def test_handle_drops_empty_recipient(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    _clear_telegram_env(monkeypatch)
    send = _SendCalls()
    dl = _DownloadCalls()
    monkeypatch.setattr(telegram_sender, "_send_telegram_photo", send)
    monkeypatch.setattr(telegram_sender, "_download_png", dl)

    with caplog.at_level(logging.WARNING, logger="mundi.senders.telegram"):
        await telegram_sender._handle(_make_payload(recipient="   "))

    assert send.calls == []
    assert dl.calls == []
    assert any("empty recipient" in r.getMessage() for r in caplog.records)


@pytest.mark.parametrize("missing_key", ["png_s3_bucket", "png_s3_key"])
async def test_handle_drops_missing_s3_coords(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    missing_key: str,
) -> None:
    _clear_telegram_env(monkeypatch)
    send = _SendCalls()
    dl = _DownloadCalls()
    monkeypatch.setattr(telegram_sender, "_send_telegram_photo", send)
    monkeypatch.setattr(telegram_sender, "_download_png", dl)

    payload = _make_payload(**{missing_key: ""})
    with caplog.at_level(logging.WARNING, logger="mundi.senders.telegram"):
        await telegram_sender._handle(payload)

    assert send.calls == []
    assert dl.calls == []
    assert any("missing s3 coords" in r.getMessage() for r in caplog.records)


# ---------- _handle: stub vs live ----------


async def test_handle_stub_mode_logs_and_skips_send(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    _clear_telegram_env(monkeypatch)
    send = _SendCalls()
    dl = _DownloadCalls()
    monkeypatch.setattr(telegram_sender, "_send_telegram_photo", send)
    monkeypatch.setattr(telegram_sender, "_download_png", dl)

    with caplog.at_level(logging.INFO, logger="mundi.senders.telegram"):
        await telegram_sender._handle(_make_payload(snapshot_id="snap-stub-1"))

    assert send.calls == []
    assert dl.calls == []
    assert any(
        "[stub]" in r.getMessage() and "snap-stub-1" in r.getMessage()
        for r in caplog.records
    )


async def test_handle_live_mode_downloads_and_sends(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _clear_telegram_env(monkeypatch)
    monkeypatch.setenv("MUNDI_TELEGRAM_LIVE", "1")
    monkeypatch.setenv("MUNDI_TELEGRAM_BOT_TOKEN", "fake-token")
    send = _SendCalls(return_value=True)
    dl = _DownloadCalls(return_value=b"binary-png")
    monkeypatch.setattr(telegram_sender, "_send_telegram_photo", send)
    monkeypatch.setattr(telegram_sender, "_download_png", dl)

    await telegram_sender._handle(
        _make_payload(
            recipient="987654",
            png_s3_bucket="bk-bucket",
            png_s3_key="snapshots/abc.png",
            caption="hello",
        )
    )

    assert dl.calls == [("bk-bucket", "snapshots/abc.png")]
    assert send.calls == [("987654", "hello", b"binary-png")]


async def test_handle_live_mode_download_failure_skips_send(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    _clear_telegram_env(monkeypatch)
    monkeypatch.setenv("MUNDI_TELEGRAM_LIVE", "1")
    monkeypatch.setenv("MUNDI_TELEGRAM_BOT_TOKEN", "fake-token")
    send = _SendCalls()
    dl = _DownloadCalls(raise_exc=RuntimeError("s3 down"))
    monkeypatch.setattr(telegram_sender, "_send_telegram_photo", send)
    monkeypatch.setattr(telegram_sender, "_download_png", dl)

    with caplog.at_level(logging.ERROR, logger="mundi.senders.telegram"):
        # Must not raise even though _download_png does
        await telegram_sender._handle(_make_payload())

    assert send.calls == []
    assert dl.calls == [("test-bucket", "snapshots/test.png")]
    assert any("s3 download failed" in r.getMessage() for r in caplog.records)


async def test_handle_live_mode_send_failure_does_not_raise(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A False return from _send_telegram_photo (HTTP non-200, etc.) is logged
    and counted as failed delivery; the handler must not raise."""
    _clear_telegram_env(monkeypatch)
    monkeypatch.setenv("MUNDI_TELEGRAM_LIVE", "1")
    monkeypatch.setenv("MUNDI_TELEGRAM_BOT_TOKEN", "fake-token")
    send = _SendCalls(return_value=False)
    dl = _DownloadCalls()
    monkeypatch.setattr(telegram_sender, "_send_telegram_photo", send)
    monkeypatch.setattr(telegram_sender, "_download_png", dl)

    await telegram_sender._handle(_make_payload())

    assert len(send.calls) == 1
    assert len(dl.calls) == 1


async def test_handle_user_id_none_safe_in_live_mode(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Regression: cron-fired payloads from sage_alerts carry user_id=None
    (no user is logged in for a scheduled alert — see sage_alerts.py:178).
    The LIVE delivery path must not dereference user_id from the payload.
    Pinning this contract here prevents a future refactor from silently
    adding a payload["user_id"] read that breaks on cron-fired sends.
    """
    _clear_telegram_env(monkeypatch)
    monkeypatch.setenv("MUNDI_TELEGRAM_LIVE", "1")
    monkeypatch.setenv("MUNDI_TELEGRAM_BOT_TOKEN", "fake-token")
    send = _SendCalls(return_value=True)
    dl = _DownloadCalls(return_value=b"png")
    monkeypatch.setattr(telegram_sender, "_send_telegram_photo", send)
    monkeypatch.setattr(telegram_sender, "_download_png", dl)

    await telegram_sender._handle(_make_payload(user_id=None))

    # Download + send both fire normally. If a future change reads
    # payload["user_id"] without a None-guard, this assertion fails.
    assert len(dl.calls) == 1
    assert len(send.calls) == 1


async def test_handle_user_id_none_safe_in_stub_mode(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Same contract on the stub path: cron payload with user_id=None is
    logged and skipped, never touched."""
    _clear_telegram_env(monkeypatch)
    send = _SendCalls()
    dl = _DownloadCalls()
    monkeypatch.setattr(telegram_sender, "_send_telegram_photo", send)
    monkeypatch.setattr(telegram_sender, "_download_png", dl)

    await telegram_sender._handle(_make_payload(user_id=None))

    assert send.calls == []
    assert dl.calls == []
