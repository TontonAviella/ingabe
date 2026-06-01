import json
import logging
import os
import uuid
from datetime import datetime, timezone
from typing import Any, Dict, Literal, Optional

from pydantic import BaseModel, Field, model_validator

from src.tools.pyd import IngabeToolCallMetaArgs
from src.utils import get_async_s3_client, get_bucket_name

logger = logging.getLogger(__name__)

RENDER_SNAPSHOT_CHANNEL = "mundi:render_snapshot"
SNAPSHOT_S3_PREFIX = "snapshots"
SNAPSHOT_TTL_SEC = 86400


class RenderMapSnapshotArgs(BaseModel):
    bbox: str = Field(
        ...,
        description="Bounding box 'west,south,east,north' in WGS84, e.g. '29.44,-1.72,29.68,-1.50'",
    )
    width: int = Field(
        ...,
        description="Image width in pixels. Use 1024 for chat attachments, 1600 for reports.",
    )
    height: int = Field(
        ...,
        description="Image height in pixels. Use 600 for chat, 1000 for reports.",
    )
    caption: str = Field(
        ...,
        description="One-line caption shown to the recipient, e.g. 'NDVI Cyampirita 2026-05-12, mean=0.62'.",
    )
    delivery_channel: Literal["browser", "telegram", "whatsapp", "email"] = Field(
        ...,
        description=(
            "Where to send the snapshot. 'browser' = the user's current map view. "
            "'telegram'/'whatsapp'/'email' route to an out-of-band sender; "
            "recipient must be set for those."
        ),
    )
    recipient: str = Field(
        ...,
        description=(
            "Destination for non-browser channels: Telegram chat_id, WhatsApp E.164 "
            "number, or email address. Pass an empty string for 'browser'."
        ),
    )

    @model_validator(mode="after")
    def _validate(self) -> "RenderMapSnapshotArgs":
        parts = [p.strip() for p in self.bbox.split(",")]
        if len(parts) != 4:
            raise ValueError("bbox must be 'west,south,east,north'")
        try:
            w, s, e, n = (float(p) for p in parts)
        except ValueError:
            raise ValueError("bbox values must be numbers")
        if w >= e or s >= n:
            raise ValueError("Invalid bbox: west<east and south<north required")
        if not (
            -180 <= w <= 180 and -180 <= e <= 180 and -90 <= s <= 90 and -90 <= n <= 90
        ):
            raise ValueError("bbox out of WGS84 range")
        if not 64 <= self.width <= 4096:
            raise ValueError("width must be 64..4096")
        if not 64 <= self.height <= 4096:
            raise ValueError("height must be 64..4096")
        if self.delivery_channel != "browser" and not self.recipient.strip():
            raise ValueError(
                f"recipient required for delivery_channel={self.delivery_channel}"
            )
        return self


async def render_map_snapshot(
    args: RenderMapSnapshotArgs, meta: IngabeToolCallMetaArgs
) -> Dict[str, Any]:
    """Render a static PNG snapshot of the current map and dispatch it to a delivery channel.

    WHEN TO USE: the user wants a snapshot they can SHARE outside the browser —
    Telegram message, WhatsApp message, email attachment — or wants a permalink
    PNG of the current view. Also use for cron-fired reports to insurance partners.

    WHEN NOT TO USE: the user wants to keep working in the browser (they already
    see the map). Numeric/tabular results — use the specific stats tool. Adding a
    NEW data layer on the map — use display_satellite_layer or compute_spectral_index.

    Delivery model: renders PNG, uploads to MinIO under snapshots/{map_id}/, then
    publishes a payload on the 'mundi:render_snapshot' Redis channel. A separate
    sender process consumes the channel, filters on delivery_channel and partner_id,
    and performs the actual send. Returns once the payload is published; does NOT
    block on the sender. Snapshot URLs are valid for 24 hours."""
    from src.dependencies.base_map import get_base_map_provider
    from src.services.map_service import get_map_style_internal, render_map_internal

    partner_id: Optional[str] = None
    try:
        if meta.session is not None and hasattr(meta.session, "get_org_id"):
            partner_id = meta.session.get_org_id()
    except Exception:
        partner_id = None

    base_map = get_base_map_provider()

    try:
        style_json = await get_map_style_internal(
            meta.map_id, base_map, only_show_inline_sources=True
        )
    except Exception as e:
        logger.exception("render_map_snapshot: style fetch failed map=%s", meta.map_id)
        return {"status": "error", "error": f"style fetch failed: {e}"}

    style_json_str = style_json if isinstance(style_json, str) else json.dumps(style_json)

    try:
        render_response, _ = await render_map_internal(
            map_id=meta.map_id,
            bbox=args.bbox,
            width=args.width,
            height=args.height,
            renderer="mbgl",
            bgcolor="#ffffff",
            style_json=style_json_str,
        )
    except Exception as e:
        logger.exception("render_map_snapshot: render failed map=%s", meta.map_id)
        return {"status": "error", "error": f"render failed: {e}"}

    png_bytes: bytes = getattr(render_response, "body", b"") or b""
    if not png_bytes:
        return {"status": "error", "error": "renderer produced empty output"}

    snapshot_id = uuid.uuid4().hex
    s3_key = f"{SNAPSHOT_S3_PREFIX}/{meta.map_id}/{snapshot_id}.png"
    bucket = get_bucket_name()

    try:
        s3 = await get_async_s3_client()
        await s3.put_object(
            Bucket=bucket,
            Key=s3_key,
            Body=png_bytes,
            ContentType="image/png",
        )
    except Exception as e:
        logger.exception("render_map_snapshot: s3 put failed key=%s", s3_key)
        return {"status": "error", "error": f"upload failed: {e}"}

    bbox_floats = [float(p.strip()) for p in args.bbox.split(",")]

    payload = {
        "snapshot_id": snapshot_id,
        "map_id": meta.map_id,
        "user_id": meta.user_uuid,
        "partner_id": partner_id,
        "conversation_id": meta.conversation_id,
        "png_s3_bucket": bucket,
        "png_s3_key": s3_key,
        "caption": args.caption,
        "bbox": bbox_floats,
        "width": args.width,
        "height": args.height,
        "delivery_channel": args.delivery_channel,
        "recipient": args.recipient,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "ttl_sec": SNAPSHOT_TTL_SEC,
    }

    publish_ok = await _publish_snapshot(payload)
    if not publish_ok:
        logger.warning(
            "render_map_snapshot: pubsub publish failed map=%s snap=%s — "
            "payload stored, sender will not see it",
            meta.map_id,
            snapshot_id,
        )

    return {
        "status": "success" if publish_ok else "partial",
        "snapshot_id": snapshot_id,
        "png_s3_key": s3_key,
        "delivery_channel": args.delivery_channel,
        "published": publish_ok,
        "size_bytes": len(png_bytes),
    }


async def _publish_snapshot(payload: Dict[str, Any]) -> bool:
    try:
        from redis.asyncio import Redis as AsyncRedis

        redis = AsyncRedis(
            host=os.environ.get("REDIS_HOST", "localhost"),
            port=int(os.environ.get("REDIS_PORT", 6379)),
            decode_responses=True,
        )
        try:
            await redis.publish(RENDER_SNAPSHOT_CHANNEL, json.dumps(payload))
            return True
        finally:
            await redis.aclose()
    except Exception:
        logger.debug("render_map_snapshot: redis publish failed", exc_info=True)
        return False
