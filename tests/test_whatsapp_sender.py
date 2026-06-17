"""Tests for src/senders/whatsapp_sender._handle and env-driven config.

WhatsApp is a first-class peer to telegram (Rwanda primary channel), so this
mirrors test_telegram_sender.py one-for-one with two WhatsApp-specific
properties added:

  - recipient E.164 normalization: leading "+" is stripped before send
  - live-mode plumbing requires both MUNDI_WHATSAPP_ACCESS_TOKEN and
    MUNDI_WHATSAPP_PHONE_NUMBER_ID (covered by _send_whatsapp_image, not
    _handle directly — the handler does not pre-validate creds since that's
    the send path's job)
"""

from __future__ import annotations

import logging

import pytest

from src.senders import whatsapp_sender


# ---------- env helpers ----------


def _clear_whatsapp_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for var in (
        "MUNDI_WHATSAPP_LIVE",
        "MUNDI_WHATSAPP_PARTNER_ID",
        "MUNDI_WHATSAPP_ACCESS_TOKEN",
        "MUNDI_WHATSAPP_PHONE_NUMBER_ID",
    ):
        monkeypatch.delenv(var, raising=False)


def test_allowed_partner_unset_returns_none(monkeypatch: pytest.MonkeyPatch) -> None:
    _clear_whatsapp_env(monkeypatch)
    assert whatsapp_sender._allowed_partner() is None


def test_allowed_partner_whitespace_treated_as_unset(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MUNDI_WHATSAPP_PARTNER_ID", "   ")
    assert whatsapp_sender._allowed_partner() is None


def test_allowed_partner_set(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MUNDI_WHATSAPP_PARTNER_ID", "bk-insurance")
    assert whatsapp_sender._allowed_partner() == "bk-insurance"


def test_is_live_defaults_false(monkeypatch: pytest.MonkeyPatch) -> None:
    _clear_whatsapp_env(monkeypatch)
    assert whatsapp_sender._is_live() is False


def test_is_live_only_truthy_on_exact_1(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MUNDI_WHATSAPP_LIVE", "true")
    assert whatsapp_sender._is_live() is False
    monkeypatch.setenv("MUNDI_WHATSAPP_LIVE", "1")
    assert whatsapp_sender._is_live() is True


# ---------- _handle: routing & filters ----------


def _make_payload(**overrides) -> dict:
    payload = {
        "snapshot_id": "snap-test",
        "delivery_channel": "whatsapp",
        "recipient": "250788123456",
        "png_s3_bucket": "test-bucket",
        "png_s3_key": "snapshots/test.png",
        "caption": "test caption",
    }
    payload.update(overrides)
    return payload


class _SendCalls:
    def __init__(self, return_value: bool = True) -> None:
        self.calls: list[tuple[str, str, bytes]] = []
        self.return_value = return_value

    async def __call__(self, to: str, caption: str, png_bytes: bytes) -> bool:
        self.calls.append((to, caption, png_bytes))
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
    _clear_whatsapp_env(monkeypatch)
    send = _SendCalls()
    dl = _DownloadCalls()
    monkeypatch.setattr(whatsapp_sender, "_send_whatsapp_image", send)
    monkeypatch.setattr(whatsapp_sender, "_download_png", dl)

    await whatsapp_sender._handle(_make_payload(delivery_channel="telegram"))

    assert send.calls == []
    assert dl.calls == []


async def test_handle_drops_partner_mismatch(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    _clear_whatsapp_env(monkeypatch)
    monkeypatch.setenv("MUNDI_WHATSAPP_PARTNER_ID", "partner-a")
    send = _SendCalls()
    dl = _DownloadCalls()
    monkeypatch.setattr(whatsapp_sender, "_send_whatsapp_image", send)
    monkeypatch.setattr(whatsapp_sender, "_download_png", dl)

    with caplog.at_level(logging.INFO, logger="mundi.senders.whatsapp"):
        await whatsapp_sender._handle(_make_payload(partner_id="partner-b"))

    assert send.calls == []
    assert dl.calls == []
    assert any("partner mismatch" in r.getMessage() for r in caplog.records)


async def test_handle_accepts_partner_match_in_stub_mode(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _clear_whatsapp_env(monkeypatch)
    monkeypatch.setenv("MUNDI_WHATSAPP_PARTNER_ID", "partner-a")
    send = _SendCalls()
    dl = _DownloadCalls()
    monkeypatch.setattr(whatsapp_sender, "_send_whatsapp_image", send)
    monkeypatch.setattr(whatsapp_sender, "_download_png", dl)

    await whatsapp_sender._handle(_make_payload(partner_id="partner-a"))

    assert send.calls == []
    assert dl.calls == []


async def test_handle_unset_partner_accepts_any(monkeypatch: pytest.MonkeyPatch) -> None:
    _clear_whatsapp_env(monkeypatch)
    send = _SendCalls()
    dl = _DownloadCalls()
    monkeypatch.setattr(whatsapp_sender, "_send_whatsapp_image", send)
    monkeypatch.setattr(whatsapp_sender, "_download_png", dl)

    await whatsapp_sender._handle(_make_payload())
    await whatsapp_sender._handle(_make_payload(partner_id="anything"))
    assert send.calls == []
    assert dl.calls == []


async def test_handle_drops_empty_recipient(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    _clear_whatsapp_env(monkeypatch)
    send = _SendCalls()
    dl = _DownloadCalls()
    monkeypatch.setattr(whatsapp_sender, "_send_whatsapp_image", send)
    monkeypatch.setattr(whatsapp_sender, "_download_png", dl)

    with caplog.at_level(logging.WARNING, logger="mundi.senders.whatsapp"):
        await whatsapp_sender._handle(_make_payload(recipient="   "))

    assert send.calls == []
    assert dl.calls == []
    assert any("empty recipient" in r.getMessage() for r in caplog.records)


async def test_handle_recipient_strips_leading_plus(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """E.164 normalization: '+250788...' must become '250788...' before send.
    The Meta Graph API rejects numbers that include the leading '+'."""
    _clear_whatsapp_env(monkeypatch)
    monkeypatch.setenv("MUNDI_WHATSAPP_LIVE", "1")
    monkeypatch.setenv("MUNDI_WHATSAPP_ACCESS_TOKEN", "fake")
    monkeypatch.setenv("MUNDI_WHATSAPP_PHONE_NUMBER_ID", "1234567890")
    send = _SendCalls(return_value=True)
    dl = _DownloadCalls()
    monkeypatch.setattr(whatsapp_sender, "_send_whatsapp_image", send)
    monkeypatch.setattr(whatsapp_sender, "_download_png", dl)

    await whatsapp_sender._handle(_make_payload(recipient=" +250788123456 "))

    assert len(send.calls) == 1
    to, _caption, _png = send.calls[0]
    assert to == "250788123456"


async def test_handle_recipient_plus_only_after_trim_is_dropped(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """A recipient that is just '+' (or '+   ') after strip+lstrip is empty
    and must be dropped."""
    _clear_whatsapp_env(monkeypatch)
    send = _SendCalls()
    dl = _DownloadCalls()
    monkeypatch.setattr(whatsapp_sender, "_send_whatsapp_image", send)
    monkeypatch.setattr(whatsapp_sender, "_download_png", dl)

    with caplog.at_level(logging.WARNING, logger="mundi.senders.whatsapp"):
        await whatsapp_sender._handle(_make_payload(recipient="+"))

    assert send.calls == []
    assert dl.calls == []
    assert any("empty recipient" in r.getMessage() for r in caplog.records)


@pytest.mark.parametrize("missing_key", ["png_s3_bucket", "png_s3_key"])
async def test_handle_drops_missing_s3_coords(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    missing_key: str,
) -> None:
    _clear_whatsapp_env(monkeypatch)
    send = _SendCalls()
    dl = _DownloadCalls()
    monkeypatch.setattr(whatsapp_sender, "_send_whatsapp_image", send)
    monkeypatch.setattr(whatsapp_sender, "_download_png", dl)

    payload = _make_payload(**{missing_key: ""})
    with caplog.at_level(logging.WARNING, logger="mundi.senders.whatsapp"):
        await whatsapp_sender._handle(payload)

    assert send.calls == []
    assert dl.calls == []
    assert any("missing s3 coords" in r.getMessage() for r in caplog.records)


# ---------- _handle: stub vs live ----------


async def test_handle_stub_mode_logs_and_skips_send(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    _clear_whatsapp_env(monkeypatch)
    send = _SendCalls()
    dl = _DownloadCalls()
    monkeypatch.setattr(whatsapp_sender, "_send_whatsapp_image", send)
    monkeypatch.setattr(whatsapp_sender, "_download_png", dl)

    with caplog.at_level(logging.INFO, logger="mundi.senders.whatsapp"):
        await whatsapp_sender._handle(_make_payload(snapshot_id="snap-stub-1"))

    assert send.calls == []
    assert dl.calls == []
    assert any(
        "[stub]" in r.getMessage() and "snap-stub-1" in r.getMessage()
        for r in caplog.records
    )


async def test_handle_live_mode_downloads_and_sends(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _clear_whatsapp_env(monkeypatch)
    monkeypatch.setenv("MUNDI_WHATSAPP_LIVE", "1")
    monkeypatch.setenv("MUNDI_WHATSAPP_ACCESS_TOKEN", "fake")
    monkeypatch.setenv("MUNDI_WHATSAPP_PHONE_NUMBER_ID", "1234567890")
    send = _SendCalls(return_value=True)
    dl = _DownloadCalls(return_value=b"binary-png")
    monkeypatch.setattr(whatsapp_sender, "_send_whatsapp_image", send)
    monkeypatch.setattr(whatsapp_sender, "_download_png", dl)

    await whatsapp_sender._handle(
        _make_payload(
            recipient="250788000000",
            png_s3_bucket="bk-bucket",
            png_s3_key="snapshots/abc.png",
            caption="hello",
        )
    )

    assert dl.calls == [("bk-bucket", "snapshots/abc.png")]
    assert send.calls == [("250788000000", "hello", b"binary-png")]


async def test_handle_live_mode_download_failure_skips_send(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    _clear_whatsapp_env(monkeypatch)
    monkeypatch.setenv("MUNDI_WHATSAPP_LIVE", "1")
    monkeypatch.setenv("MUNDI_WHATSAPP_ACCESS_TOKEN", "fake")
    monkeypatch.setenv("MUNDI_WHATSAPP_PHONE_NUMBER_ID", "1234567890")
    send = _SendCalls()
    dl = _DownloadCalls(raise_exc=RuntimeError("s3 down"))
    monkeypatch.setattr(whatsapp_sender, "_send_whatsapp_image", send)
    monkeypatch.setattr(whatsapp_sender, "_download_png", dl)

    with caplog.at_level(logging.ERROR, logger="mundi.senders.whatsapp"):
        await whatsapp_sender._handle(_make_payload())

    assert send.calls == []
    assert dl.calls == [("test-bucket", "snapshots/test.png")]
    assert any("s3 download failed" in r.getMessage() for r in caplog.records)


async def test_handle_live_mode_send_failure_does_not_raise(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _clear_whatsapp_env(monkeypatch)
    monkeypatch.setenv("MUNDI_WHATSAPP_LIVE", "1")
    monkeypatch.setenv("MUNDI_WHATSAPP_ACCESS_TOKEN", "fake")
    monkeypatch.setenv("MUNDI_WHATSAPP_PHONE_NUMBER_ID", "1234567890")
    send = _SendCalls(return_value=False)
    dl = _DownloadCalls()
    monkeypatch.setattr(whatsapp_sender, "_send_whatsapp_image", send)
    monkeypatch.setattr(whatsapp_sender, "_download_png", dl)

    await whatsapp_sender._handle(_make_payload())

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
    _clear_whatsapp_env(monkeypatch)
    monkeypatch.setenv("MUNDI_WHATSAPP_LIVE", "1")
    monkeypatch.setenv("MUNDI_WHATSAPP_ACCESS_TOKEN", "fake")
    monkeypatch.setenv("MUNDI_WHATSAPP_PHONE_NUMBER_ID", "1234567890")
    send = _SendCalls(return_value=True)
    dl = _DownloadCalls(return_value=b"png")
    monkeypatch.setattr(whatsapp_sender, "_send_whatsapp_image", send)
    monkeypatch.setattr(whatsapp_sender, "_download_png", dl)

    await whatsapp_sender._handle(_make_payload(user_id=None))

    # Download + send both fire normally. If a future change reads
    # payload["user_id"] without a None-guard, this assertion fails.
    assert len(dl.calls) == 1
    assert len(send.calls) == 1


async def test_handle_user_id_none_safe_in_stub_mode(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Same contract on the stub path: cron payload with user_id=None is
    logged and skipped, never touched."""
    _clear_whatsapp_env(monkeypatch)
    send = _SendCalls()
    dl = _DownloadCalls()
    monkeypatch.setattr(whatsapp_sender, "_send_whatsapp_image", send)
    monkeypatch.setattr(whatsapp_sender, "_download_png", dl)

    await whatsapp_sender._handle(_make_payload(user_id=None))

    assert send.calls == []
    assert dl.calls == []
