"""Tests for src.cron.sage_alerts — the proactive snapshot cron worker.

The worker has two real seams worth testing in isolation:

  1. `_render_caption(template, fire_ts, partner_id)` — Pure-ish: format() with
     a fixed schema, must not raise on partial/extra keys, must format the
     fire_ts in the documented format.

  2. `_fire_subscription(pool, redis, row)` and `_tick(pool, redis)` — Render+
     upload+publish orchestration. We don't test the renderer or S3 here
     (they're tested by their own modules / smoke); we pin:

       - publish payload carries every field the senders need
       - render failure short-circuits and DOES NOT publish, but STILL advances
         next_fire_at (skip-not-spam)
       - publish failure still advances next_fire_at
       - _tick fetches enabled+due rows and dispatches each

Everything is mocked at module attribute level (monkeypatch.setattr on
src.cron.sage_alerts.<name>). No redis, no postgres, no S3, no renderer.
"""

from __future__ import annotations

import asyncio
import json
import logging
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from typing import Any

import pytest

from src.cron import sage_alerts


def test_postgres_password_required(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("POSTGRES_PASSWORD", raising=False)
    with pytest.raises(RuntimeError, match="POSTGRES_PASSWORD"):
        sage_alerts._postgres_password()


def test_postgres_password_returns_configured_secret(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("POSTGRES_PASSWORD", "secret")
    assert sage_alerts._postgres_password() == "secret"


# ---------- _render_caption ----------


def test_render_caption_basic() -> None:
    out = sage_alerts._render_caption(
        "{partner_id} NDVI for {date}",
        fire_ts=datetime(2026, 5, 12, 6, 0, tzinfo=timezone.utc),
        partner_id="bk-insurance",
    )
    assert out == "bk-insurance NDVI for 2026-05-12"


def test_render_caption_fire_ts_utc_format() -> None:
    out = sage_alerts._render_caption(
        "snap at {fire_ts_utc}",
        fire_ts=datetime(2026, 5, 12, 6, 30, tzinfo=timezone.utc),
        partner_id="any",
    )
    assert out == "snap at 2026-05-12 06:30 UTC"


def test_render_caption_unknown_key_returns_raw(caplog: pytest.LogCaptureFixture) -> None:
    template = "hello {nope}"
    with caplog.at_level(logging.WARNING, logger="mundi.cron.sage_alerts"):
        out = sage_alerts._render_caption(
            template,
            fire_ts=datetime(2026, 5, 12, tzinfo=timezone.utc),
            partner_id="x",
        )
    assert out == template
    assert any("caption template error" in r.getMessage() for r in caplog.records)


def test_render_caption_bad_format_spec_returns_raw(
    caplog: pytest.LogCaptureFixture,
) -> None:
    template = "snap {date:z}"
    with caplog.at_level(logging.WARNING, logger="mundi.cron.sage_alerts"):
        out = sage_alerts._render_caption(
            template,
            fire_ts=datetime(2026, 5, 12, tzinfo=timezone.utc),
            partner_id="x",
        )
    assert out == template
    assert any("caption template error" in r.getMessage() for r in caplog.records)


def test_render_caption_no_placeholders_passes_through() -> None:
    out = sage_alerts._render_caption(
        "Weekly digest",
        fire_ts=datetime(2026, 5, 12, tzinfo=timezone.utc),
        partner_id="bk",
    )
    assert out == "Weekly digest"


# ---------- _publish ----------


class _FakeRedisOK:
    def __init__(self) -> None:
        self.published: list[tuple[str, str]] = []

    async def publish(self, channel: str, message: str) -> int:
        self.published.append((channel, message))
        return 1


class _FakeRedisRaises:
    async def publish(self, channel: str, message: str) -> int:
        raise RuntimeError("redis down")


async def test_publish_success() -> None:
    redis = _FakeRedisOK()
    ok = await sage_alerts._publish(redis, {"snapshot_id": "abc"})
    assert ok is True
    assert len(redis.published) == 1
    channel, msg = redis.published[0]
    assert channel == sage_alerts.RENDER_SNAPSHOT_CHANNEL
    assert json.loads(msg) == {"snapshot_id": "abc"}


async def test_publish_failure_returns_false(caplog: pytest.LogCaptureFixture) -> None:
    redis = _FakeRedisRaises()
    with caplog.at_level(logging.ERROR, logger="mundi.cron.sage_alerts"):
        ok = await sage_alerts._publish(redis, {"snapshot_id": "abc"})
    assert ok is False
    assert any("redis publish failed" in r.getMessage() for r in caplog.records)


# ---------- _fire_subscription ----------


class _FakeConn:
    def __init__(self) -> None:
        self.executes: list[tuple[str, tuple]] = []
        self.fetches: list[tuple[str, tuple]] = []
        self.fetch_result: list[dict] = []

    async def execute(self, sql: str, *args) -> None:
        self.executes.append((sql, args))

    async def fetch(self, sql: str, *args):
        self.fetches.append((sql, args))
        return self.fetch_result


class _FakePool:
    def __init__(self, conn: _FakeConn) -> None:
        self._conn = conn

    def acquire(self):
        conn = self._conn

        @asynccontextmanager
        async def _cm():
            yield conn

        return _cm()

    async def close(self) -> None:
        pass


def _make_row(**overrides) -> dict:
    row = {
        "id": "sub-1",
        "partner_id": "bk-insurance",
        "map_id": "map-xyz",
        "bbox": "29.0,-2.0,30.0,-1.0",
        "width": 1024,
        "height": 600,
        "delivery_channel": "telegram",
        "recipient": "12345",
        "caption_template": "{partner_id} NDVI {date}",
        "cron_expr": "0 6 * * 1",
    }
    row.update(overrides)
    return row


async def test_fire_subscription_happy_path_publishes_full_payload(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    conn = _FakeConn()
    pool = _FakePool(conn)
    redis = _FakeRedisOK()

    async def fake_render(map_id, bbox, width, height):
        return b"\x89PNG-fake-bytes"

    upload_calls: list[tuple[str, str, bytes]] = []

    async def fake_upload(map_id, snapshot_id, png_bytes):
        upload_calls.append((map_id, snapshot_id, png_bytes))
        return "test-bucket", f"snapshots/{map_id}/{snapshot_id}.png"

    monkeypatch.setattr(sage_alerts, "_render_snapshot_png", fake_render)
    monkeypatch.setattr(sage_alerts, "_upload_png", fake_upload)

    await sage_alerts._fire_subscription(pool, redis, _make_row())

    # 1) Published once
    assert len(redis.published) == 1
    channel, msg = redis.published[0]
    assert channel == sage_alerts.RENDER_SNAPSHOT_CHANNEL
    payload = json.loads(msg)

    # 2) Payload schema matches what senders consume
    for required in (
        "snapshot_id",
        "map_id",
        "partner_id",
        "png_s3_bucket",
        "png_s3_key",
        "caption",
        "bbox",
        "width",
        "height",
        "delivery_channel",
        "recipient",
        "created_at",
        "ttl_sec",
        "source",
        "subscription_id",
    ):
        assert required in payload, f"payload missing {required}"

    assert payload["source"] == "sage_alerts_cron"
    assert payload["subscription_id"] == "sub-1"
    assert payload["delivery_channel"] == "telegram"
    assert payload["recipient"] == "12345"
    assert payload["partner_id"] == "bk-insurance"
    assert payload["bbox"] == [29.0, -2.0, 30.0, -1.0]
    assert payload["ttl_sec"] == sage_alerts.SNAPSHOT_TTL_SEC
    assert payload["caption"].startswith("bk-insurance NDVI ")

    # 3) Upload called with the rendered bytes
    assert len(upload_calls) == 1
    assert upload_calls[0][2] == b"\x89PNG-fake-bytes"

    # 4) next_fire_at advanced (UPDATE issued)
    assert len(conn.executes) == 1
    sql, args = conn.executes[0]
    assert "UPDATE alert_subscriptions" in sql
    assert args[2] == "sub-1"  # WHERE id = sub-1


async def test_fire_subscription_render_failure_no_publish_but_advances(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    conn = _FakeConn()
    pool = _FakePool(conn)
    redis = _FakeRedisOK()

    async def fake_render(*_a, **_kw):
        raise RuntimeError("renderer down")

    async def fake_upload(*_a, **_kw):
        pytest.fail("upload must not be called when render fails")

    monkeypatch.setattr(sage_alerts, "_render_snapshot_png", fake_render)
    monkeypatch.setattr(sage_alerts, "_upload_png", fake_upload)

    with caplog.at_level(logging.ERROR, logger="mundi.cron.sage_alerts"):
        await sage_alerts._fire_subscription(pool, redis, _make_row())

    # No publish on render failure
    assert redis.published == []
    # But next_fire_at IS advanced — skip-not-spam
    assert len(conn.executes) == 1
    assert "UPDATE alert_subscriptions" in conn.executes[0][0]
    assert any("render failed" in r.getMessage() for r in caplog.records)


async def test_fire_subscription_publish_failure_still_advances(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    conn = _FakeConn()
    pool = _FakePool(conn)
    redis = _FakeRedisRaises()

    async def fake_render(*_a, **_kw):
        return b"png"

    async def fake_upload(*_a, **_kw):
        return "bucket", "key"

    monkeypatch.setattr(sage_alerts, "_render_snapshot_png", fake_render)
    monkeypatch.setattr(sage_alerts, "_upload_png", fake_upload)

    await sage_alerts._fire_subscription(pool, redis, _make_row())

    # next_fire_at advanced even though publish raised
    assert len(conn.executes) == 1
    assert "UPDATE alert_subscriptions" in conn.executes[0][0]


async def test_fire_subscription_bad_cron_expr_advances_by_tick_interval(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """A malformed cron_expr must NOT hot-loop: next_fire_at is bumped by
    TICK_INTERVAL_SEC so the row doesn't fire again until at least the next tick.
    """
    from src.cron import cron_expr as cron_expr_mod

    conn = _FakeConn()
    pool = _FakePool(conn)
    redis = _FakeRedisOK()

    async def fake_render(*_a, **_kw):
        return b"png"

    async def fake_upload(*_a, **_kw):
        return "bucket", "key"

    def bad_next_fire_after(expr: str, now: datetime) -> datetime:
        raise ValueError(f"unparseable cron: {expr!r}")

    monkeypatch.setattr(sage_alerts, "_render_snapshot_png", fake_render)
    monkeypatch.setattr(sage_alerts, "_upload_png", fake_upload)
    monkeypatch.setattr(cron_expr_mod, "next_fire_after", bad_next_fire_after)

    with caplog.at_level(logging.ERROR, logger="mundi.cron.sage_alerts"):
        await sage_alerts._fire_subscription(
            pool, redis, _make_row(cron_expr="not a cron expr")
        )

    # The UPDATE still ran — row was advanced, not left hot-loopable.
    assert len(conn.executes) == 1
    sql, args = conn.executes[0]
    assert "UPDATE alert_subscriptions" in sql
    fire_ts, next_fire, sub_id = args
    assert sub_id == "sub-1"
    # next_fire is strictly in the future relative to fire_ts by at least
    # TICK_INTERVAL_SEC, which is the invariant that prevents hot-looping.
    delta = (next_fire - fire_ts).total_seconds()
    assert delta >= sage_alerts.TICK_INTERVAL_SEC
    # And the bad-cron warning was logged.
    assert any(
        "bad cron_expr" in r.getMessage() and "not a cron expr" in r.getMessage()
        for r in caplog.records
    )


# ---------- _tick ----------


async def test_tick_bootstraps_then_fetches_then_fires(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    conn = _FakeConn()
    conn.fetch_result = [_make_row(id="sub-a"), _make_row(id="sub-b")]
    pool = _FakePool(conn)
    redis = _FakeRedisOK()

    fired: list[str] = []

    async def fake_fire(_pool, _redis, row):
        fired.append(row["id"])

    monkeypatch.setattr(sage_alerts, "_fire_subscription", fake_fire)

    count = await sage_alerts._tick(pool, redis)

    assert count == 2
    assert fired == ["sub-a", "sub-b"]
    # Bootstrap UPDATE for NULL next_fire_at should have run
    assert any("next_fire_at IS NULL" in sql for sql, _ in conn.executes)
    # And the SELECT for due rows
    assert len(conn.fetches) == 1
    fetch_sql = conn.fetches[0][0]
    assert "next_fire_at <= now()" in fetch_sql
    assert "enabled = true" in fetch_sql


async def test_tick_swallows_subscription_handler_errors(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """One bad subscription must not kill the tick — the other rows still fire."""
    conn = _FakeConn()
    conn.fetch_result = [_make_row(id="bad"), _make_row(id="good")]
    pool = _FakePool(conn)
    redis = _FakeRedisOK()

    fired: list[str] = []

    async def fake_fire(_pool, _redis, row):
        if row["id"] == "bad":
            raise RuntimeError("boom")
        fired.append(row["id"])

    monkeypatch.setattr(sage_alerts, "_fire_subscription", fake_fire)

    with caplog.at_level(logging.ERROR, logger="mundi.cron.sage_alerts"):
        count = await sage_alerts._tick(pool, redis)

    assert count == 2  # both rows were considered
    assert fired == ["good"]  # only the good one ran to completion
    assert any("subscription handler raised" in r.getMessage() for r in caplog.records)


async def test_tick_zero_rows_returns_zero() -> None:
    conn = _FakeConn()
    conn.fetch_result = []
    pool = _FakePool(conn)
    redis = _FakeRedisOK()

    count = await sage_alerts._tick(pool, redis)
    assert count == 0
