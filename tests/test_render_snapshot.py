"""Tests for src.tools.render_snapshot.

Two seams worth pinning:

  1. `RenderMapSnapshotArgs` — the LLM-facing validator. The model_validator is
     what catches malformed bbox / pixel sizes / missing recipients BEFORE we
     burn compute on a render. Bugs here either silently accept garbage (bad)
     or reject valid inputs (worse — Sage looks broken to the partner).

  2. `render_map_snapshot(args, meta)` — the tool body. We pin the orchestration
     contract:
       - happy path → S3 put + Redis publish with full payload, status=success
       - render failure → no S3 put, no publish, status=error
       - upload failure → no publish, status=error
       - publish failure → status=partial (we already wrote S3, sender just
         won't see it; deliberate so we don't double-charge S3 on retry)
       - partner_id propagation from meta.session.get_org_id()

All heavy deps (renderer, S3 client, redis) are mocked at module attribute
level. No network, no postgres.
"""

from __future__ import annotations

import json
import logging
from types import SimpleNamespace
from typing import Any

import pytest

from src.tools import render_snapshot as rs
from src.tools.pyd import IngabeToolCallMetaArgs


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


# ---------- RenderMapSnapshotArgs validation ----------


def _valid_args(**overrides) -> dict:
    base = {
        "bbox": "29.44,-1.72,29.68,-1.50",
        "width": 1024,
        "height": 600,
        "caption": "NDVI smoke",
        "delivery_channel": "browser",
        "recipient": "",
    }
    base.update(overrides)
    return base


def test_args_valid_browser_no_recipient() -> None:
    args = rs.RenderMapSnapshotArgs(**_valid_args())
    assert args.delivery_channel == "browser"
    assert args.recipient == ""


def test_args_valid_telegram_with_recipient() -> None:
    args = rs.RenderMapSnapshotArgs(
        **_valid_args(delivery_channel="telegram", recipient="12345")
    )
    assert args.delivery_channel == "telegram"
    assert args.recipient == "12345"


def test_args_valid_email_with_recipient() -> None:
    args = rs.RenderMapSnapshotArgs(
        **_valid_args(delivery_channel="email", recipient="ops@example.com")
    )
    assert args.delivery_channel == "email"
    assert args.recipient == "ops@example.com"


def test_args_valid_whatsapp_with_recipient() -> None:
    args = rs.RenderMapSnapshotArgs(
        **_valid_args(delivery_channel="whatsapp", recipient="+250788123456")
    )
    assert args.delivery_channel == "whatsapp"
    assert args.recipient == "+250788123456"


def test_args_bbox_wrong_field_count() -> None:
    with pytest.raises(ValueError, match="west,south,east,north"):
        rs.RenderMapSnapshotArgs(**_valid_args(bbox="1,2,3"))


def test_args_bbox_non_numeric() -> None:
    with pytest.raises(ValueError, match="numbers"):
        rs.RenderMapSnapshotArgs(**_valid_args(bbox="a,b,c,d"))


def test_args_bbox_west_ge_east() -> None:
    with pytest.raises(ValueError, match="west<east"):
        rs.RenderMapSnapshotArgs(**_valid_args(bbox="30,-2,29,-1"))


def test_args_bbox_south_ge_north() -> None:
    with pytest.raises(ValueError, match="west<east"):
        rs.RenderMapSnapshotArgs(**_valid_args(bbox="29,-1,30,-2"))


def test_args_bbox_out_of_wgs84_range() -> None:
    with pytest.raises(ValueError, match="WGS84"):
        rs.RenderMapSnapshotArgs(**_valid_args(bbox="29,-91,30,1"))


@pytest.mark.parametrize("dim", ["width", "height"])
def test_args_pixel_dim_too_small(dim: str) -> None:
    with pytest.raises(ValueError, match=f"{dim} must be"):
        rs.RenderMapSnapshotArgs(**_valid_args(**{dim: 32}))


@pytest.mark.parametrize("dim", ["width", "height"])
def test_args_pixel_dim_too_large(dim: str) -> None:
    with pytest.raises(ValueError, match=f"{dim} must be"):
        rs.RenderMapSnapshotArgs(**_valid_args(**{dim: 8192}))


@pytest.mark.parametrize("ch", ["telegram", "whatsapp", "email"])
def test_args_non_browser_requires_recipient(ch: str) -> None:
    with pytest.raises(ValueError, match=f"recipient required.*{ch}"):
        rs.RenderMapSnapshotArgs(
            **_valid_args(delivery_channel=ch, recipient="   ")
        )


def test_args_invalid_delivery_channel() -> None:
    with pytest.raises(ValueError):
        rs.RenderMapSnapshotArgs(**_valid_args(delivery_channel="sms"))


@pytest.mark.parametrize(
    ("ch", "recipient", "message"),
    [
        ("email", "not-an-email", "valid email"),
        ("telegram", "bad", "Telegram chat id"),
        ("whatsapp", "local-phone", "E.164 phone number"),
    ],
)
def test_args_non_browser_validates_recipient_format(ch: str, recipient: str, message: str) -> None:
    with pytest.raises(ValueError, match=message):
        rs.RenderMapSnapshotArgs(
            **_valid_args(delivery_channel=ch, recipient=recipient)
        )


# ---------- render_map_snapshot orchestration ----------


def _make_meta(*, org_id: str | None = "bk-insurance") -> IngabeToolCallMetaArgs:
    """Build a meta with a session whose get_org_id returns the supplied value.

    Pass org_id=None to simulate a session without a tenant binding; pass
    `_NoSession` to drop the session attr entirely.
    """
    if org_id is _NoSession:
        session = None
    else:
        session = SimpleNamespace(get_org_id=lambda oid=org_id: oid)
    return IngabeToolCallMetaArgs(
        user_uuid="u-1",
        conversation_id=42,
        map_id="map-xyz",
        project_id="proj-1",
        session=session,
    )


_NoSession = object()  # sentinel


class _FakeS3OK:
    def __init__(self) -> None:
        self.puts: list[dict] = []

    async def put_object(self, **kwargs) -> None:
        self.puts.append(kwargs)


class _FakeS3Raises:
    async def put_object(self, **kwargs) -> None:
        raise RuntimeError("s3 down")


class _FakeRedisOK:
    def __init__(self) -> None:
        self.published: list[tuple[str, str]] = []

    async def publish(self, channel: str, message: str) -> int:
        self.published.append((channel, message))
        return 1

    async def aclose(self) -> None:
        pass


class _FakeRedisRaisesPublish:
    async def publish(self, channel: str, message: str) -> int:
        raise RuntimeError("redis publish failed")

    async def aclose(self) -> None:
        pass


def _patch_renderer(monkeypatch: pytest.MonkeyPatch, png_bytes: bytes = b"PNG") -> None:
    """Patch get_map_style_internal + render_map_internal in the module that
    render_snapshot.render_map_snapshot imports them from.

    The imports happen inside the function body (lazy), so we patch the
    target modules' attributes."""
    import src.services.map_service as ms
    import src.dependencies.base_map as bm

    async def fake_get_style(
        map_id,
        base_map,
        only_show_inline_sources=True,
        inline_s3_endpoint_url=None,
    ):
        return {"version": 8, "sources": {}, "layers": []}

    async def fake_render(**kwargs):
        return SimpleNamespace(body=png_bytes), {}

    def fake_provider():
        return SimpleNamespace(name="osm")

    monkeypatch.setattr(ms, "get_map_style_internal", fake_get_style, raising=False)
    monkeypatch.setattr(ms, "render_map_internal", fake_render, raising=False)
    monkeypatch.setattr(bm, "get_base_map_provider", fake_provider, raising=False)


def _patch_s3_and_redis(
    monkeypatch: pytest.MonkeyPatch,
    *,
    s3: Any | None = None,
    redis: Any | None = None,
    bucket: str = "test-bucket",
) -> tuple[Any, Any]:
    """Patch get_async_s3_client + get_bucket_name on the render_snapshot module,
    plus monkeypatch the redis.asyncio.Redis class used inside _publish_snapshot."""
    s3 = s3 if s3 is not None else _FakeS3OK()
    redis = redis if redis is not None else _FakeRedisOK()

    async def fake_s3_client(*_a, **_kw):
        return s3

    monkeypatch.setattr(rs, "get_async_s3_client", fake_s3_client)
    monkeypatch.setattr(rs, "get_bucket_name", lambda: bucket)

    # Patch the Redis CLASS used inside _publish_snapshot. The class is imported
    # lazily inside the function so patching the upstream module works.
    import redis.asyncio as redis_async

    monkeypatch.setattr(redis_async, "Redis", lambda **_kw: redis)
    return s3, redis


@pytest.mark.anyio
async def test_render_map_snapshot_happy_path(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_renderer(monkeypatch, png_bytes=b"PNGDATA")
    s3, redis = _patch_s3_and_redis(monkeypatch)

    args = rs.RenderMapSnapshotArgs(**_valid_args(
        delivery_channel="telegram", recipient="12345"
    ))
    meta = _make_meta(org_id="bk-insurance")

    result = await rs.render_map_snapshot(args, meta)

    assert result["status"] == "success"
    assert result["published"] is True
    assert result["delivery_channel"] == "telegram"
    assert result["size_bytes"] == len(b"PNGDATA")

    # S3 put once, under snapshots/<map_id>/<snap>.png
    assert len(s3.puts) == 1
    put = s3.puts[0]
    assert put["Bucket"] == "test-bucket"
    assert put["Key"].startswith("snapshots/map-xyz/")
    assert put["Key"].endswith(".png")
    assert put["Body"] == b"PNGDATA"
    assert put["ContentType"] == "image/png"

    # Redis publish once, with full payload
    assert len(redis.published) == 1
    channel, msg = redis.published[0]
    assert channel == rs.RENDER_SNAPSHOT_CHANNEL
    payload = json.loads(msg)
    assert payload["partner_id"] == "bk-insurance"
    assert payload["delivery_channel"] == "telegram"
    assert payload["recipient"] == "12345"
    assert payload["bbox"] == [29.44, -1.72, 29.68, -1.50]
    assert payload["map_id"] == "map-xyz"
    assert payload["user_id"] == "u-1"
    assert payload["conversation_id"] == 42
    assert payload["ttl_sec"] == rs.SNAPSHOT_TTL_SEC


@pytest.mark.anyio
async def test_render_map_snapshot_render_failure_returns_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import src.services.map_service as ms
    import src.dependencies.base_map as bm

    async def fake_get_style(*_a, **_kw):
        return {"version": 8, "sources": {}, "layers": []}

    async def fake_render(**_kw):
        raise RuntimeError("mbgl crashed")

    monkeypatch.setattr(ms, "get_map_style_internal", fake_get_style, raising=False)
    monkeypatch.setattr(ms, "render_map_internal", fake_render, raising=False)
    monkeypatch.setattr(bm, "get_base_map_provider", lambda: SimpleNamespace(), raising=False)

    s3, redis = _patch_s3_and_redis(monkeypatch)

    result = await rs.render_map_snapshot(
        rs.RenderMapSnapshotArgs(**_valid_args()), _make_meta()
    )

    assert result["status"] == "error"
    assert "render failed" in result["error"]
    assert s3.puts == []
    assert redis.published == []


@pytest.mark.anyio
async def test_render_map_snapshot_empty_png_returns_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_renderer(monkeypatch, png_bytes=b"")
    s3, redis = _patch_s3_and_redis(monkeypatch)

    result = await rs.render_map_snapshot(
        rs.RenderMapSnapshotArgs(**_valid_args()), _make_meta()
    )

    assert result["status"] == "error"
    assert "empty" in result["error"]
    assert s3.puts == []
    assert redis.published == []


@pytest.mark.anyio
async def test_render_map_snapshot_s3_failure_returns_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_renderer(monkeypatch)
    s3, redis = _patch_s3_and_redis(monkeypatch, s3=_FakeS3Raises())

    result = await rs.render_map_snapshot(
        rs.RenderMapSnapshotArgs(**_valid_args()), _make_meta()
    )

    assert result["status"] == "error"
    assert "upload failed" in result["error"]
    # publish must not happen if S3 put failed
    assert redis.published == []


@pytest.mark.anyio
async def test_render_map_snapshot_publish_failure_returns_partial(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """Publish failure with successful S3 put = 'partial'. The PNG is stored
    (sender can be replayed if needed) but the sender will not see it."""
    _patch_renderer(monkeypatch)
    s3, redis = _patch_s3_and_redis(monkeypatch, redis=_FakeRedisRaisesPublish())

    with caplog.at_level(logging.WARNING, logger="src.tools.render_snapshot"):
        result = await rs.render_map_snapshot(
            rs.RenderMapSnapshotArgs(**_valid_args()), _make_meta()
        )

    assert result["status"] == "partial"
    assert result["published"] is False
    assert len(s3.puts) == 1  # S3 put still happened


@pytest.mark.anyio
async def test_render_map_snapshot_partner_id_none_when_no_session(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_renderer(monkeypatch)
    s3, redis = _patch_s3_and_redis(monkeypatch)

    meta = _make_meta(org_id=_NoSession)  # session=None
    result = await rs.render_map_snapshot(
        rs.RenderMapSnapshotArgs(**_valid_args()), meta
    )

    assert result["status"] == "success"
    payload = json.loads(redis.published[0][1])
    assert payload["partner_id"] is None


@pytest.mark.anyio
async def test_render_map_snapshot_partner_id_none_when_get_org_id_raises(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A broken session must not break the snapshot — partner_id falls to None."""
    _patch_renderer(monkeypatch)
    s3, redis = _patch_s3_and_redis(monkeypatch)

    def broken():
        raise RuntimeError("session expired")

    meta = IngabeToolCallMetaArgs(
        user_uuid="u-1",
        conversation_id=1,
        map_id="map-xyz",
        project_id="p-1",
        session=SimpleNamespace(get_org_id=broken),
    )

    result = await rs.render_map_snapshot(
        rs.RenderMapSnapshotArgs(**_valid_args()), meta
    )

    assert result["status"] == "success"
    payload = json.loads(redis.published[0][1])
    assert payload["partner_id"] is None
