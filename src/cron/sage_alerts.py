"""Proactive Sage alert cron worker.

Wakes every TICK_INTERVAL_SEC, scans `alert_subscriptions` for due rows,
and for each: renders the configured map+bbox, uploads the PNG to MinIO,
and publishes a payload on the 'mundi:render_snapshot' Redis channel —
the same payload schema that `render_map_snapshot` (the chat tool) emits,
so existing WhatsApp/Telegram senders pick it up unchanged.

Why no LLM in the loop:
- Recurring partner reports (BK Insurance Monday-morning NDVI digest, etc.)
  are deterministic. A cron + parameterised caption is auditable; an LLM
  in the path adds latency, cost, and a failure mode that doesn't help.
- Sage's interactive runtime (process_chat_interaction_task) stays for
  ad-hoc partner questions; this worker is for the recurring reports those
  partners said they wanted on a schedule.

Run:
    python -m src.cron.sage_alerts

Stub-mode default: senders are stubbed by their own MUNDI_*_LIVE=0 gates,
so this worker can run safely without external API credentials — it'll
render, upload, publish, and the senders will log "would send".

Telemetry: each fire emits an OTel span 'pattern_e.proactive_fire' with
subscription_id, partner_id, delivery_channel, render_ms, publish_ok.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import signal
import time
import uuid
from datetime import datetime, timedelta, timezone
from typing import Optional

logger = logging.getLogger("mundi.cron.sage_alerts")

RENDER_SNAPSHOT_CHANNEL = "mundi:render_snapshot"
SNAPSHOT_S3_PREFIX = "snapshots"
SNAPSHOT_TTL_SEC = 86400

TICK_INTERVAL_SEC = int(os.environ.get("MUNDI_SAGE_ALERTS_TICK_SEC", "30"))
MAX_PER_TICK = int(os.environ.get("MUNDI_SAGE_ALERTS_MAX_PER_TICK", "20"))


def _postgres_password() -> str:
    password = os.environ.get("POSTGRES_PASSWORD")
    if not password:
        raise RuntimeError("POSTGRES_PASSWORD must be set for sage_alerts")
    return password


async def _get_pool():
    import asyncpg

    return await asyncpg.create_pool(
        host=os.environ.get("POSTGRES_HOST", "postgres"),
        port=int(os.environ.get("POSTGRES_PORT", "5432")),
        database=os.environ.get("POSTGRES_DB", "mundi"),
        user=os.environ.get("POSTGRES_USER", "mundiuser"),
        password=_postgres_password(),
        min_size=1,
        max_size=4,
    )


async def _get_redis():
    from redis.asyncio import Redis as AsyncRedis

    return AsyncRedis(
        host=os.environ.get("REDIS_HOST", "redis"),
        port=int(os.environ.get("REDIS_PORT", "6379")),
        decode_responses=True,
    )


async def _render_snapshot_png(
    map_id: str, bbox: str, width: int, height: int
) -> bytes:
    """Render the map to PNG bytes via the same path the chat tool uses."""
    from src.dependencies.base_map import get_base_map_provider
    from src.services.map_service import get_map_style_internal, render_map_internal

    base_map = get_base_map_provider()
    style_json = await get_map_style_internal(
        map_id,
        base_map,
        only_show_inline_sources=True,
        inline_s3_endpoint_url=os.environ.get("S3_ENDPOINT_URL"),
    )
    style_json_str = style_json if isinstance(style_json, str) else json.dumps(style_json)
    render_response, _ = await render_map_internal(
        map_id=map_id,
        bbox=bbox,
        width=width,
        height=height,
        renderer="mbgl",
        bgcolor="#ffffff",
        style_json=style_json_str,
    )
    png_bytes: bytes = getattr(render_response, "body", b"") or b""
    if not png_bytes:
        raise RuntimeError("renderer produced empty output")
    return png_bytes


async def _upload_png(map_id: str, snapshot_id: str, png_bytes: bytes) -> tuple[str, str]:
    """Upload to MinIO, return (bucket, key). Same prefix as the chat-tool snapshots
    so the 24h lifecycle policy applies."""
    from src.utils import get_async_s3_client, get_bucket_name

    bucket = get_bucket_name()
    key = f"{SNAPSHOT_S3_PREFIX}/{map_id}/{snapshot_id}.png"
    s3 = await get_async_s3_client()
    await s3.put_object(
        Bucket=bucket,
        Key=key,
        Body=png_bytes,
        ContentType="image/png",
    )
    return bucket, key


async def _publish(redis, payload: dict) -> bool:
    try:
        await redis.publish(RENDER_SNAPSHOT_CHANNEL, json.dumps(payload))
        return True
    except Exception:
        logger.exception("sage_alerts: redis publish failed")
        return False


def _render_caption(template: str, *, fire_ts: datetime, partner_id: str) -> str:
    try:
        return template.format(
            fire_ts_utc=fire_ts.strftime("%Y-%m-%d %H:%M UTC"),
            partner_id=partner_id,
            date=fire_ts.strftime("%Y-%m-%d"),
        )
    except (KeyError, IndexError, ValueError):
        logger.warning("sage_alerts: caption template error, using template raw: %r", template)
        return template


async def _fire_subscription(pool, redis, row) -> None:
    from src.cron.cron_expr import next_fire_after

    sub_id = row["id"]
    partner_id = row["partner_id"]
    fire_ts = datetime.now(timezone.utc)
    snapshot_id = uuid.uuid4().hex

    span = _open_span(
        "pattern_e.proactive_fire",
        subscription_id=sub_id,
        partner_id=partner_id,
        delivery_channel=row["delivery_channel"],
    )

    render_start = time.monotonic()
    render_ok = False
    publish_ok = False
    try:
        try:
            png_bytes = await _render_snapshot_png(
                row["map_id"], row["bbox"], row["width"], row["height"]
            )
            render_ok = True
        except Exception:
            logger.exception(
                "sage_alerts: render failed sub=%s map=%s", sub_id, row["map_id"]
            )
            return

        bucket, key = await _upload_png(row["map_id"], snapshot_id, png_bytes)

        caption = _render_caption(
            row["caption_template"], fire_ts=fire_ts, partner_id=partner_id
        )
        bbox_floats = [float(p.strip()) for p in row["bbox"].split(",")]
        payload = {
            "snapshot_id": snapshot_id,
            "map_id": row["map_id"],
            "user_id": None,
            "partner_id": partner_id,
            "conversation_id": None,
            "png_s3_bucket": bucket,
            "png_s3_key": key,
            "caption": caption,
            "bbox": bbox_floats,
            "width": row["width"],
            "height": row["height"],
            "delivery_channel": row["delivery_channel"],
            "recipient": row["recipient"],
            "created_at": fire_ts.isoformat(),
            "ttl_sec": SNAPSHOT_TTL_SEC,
            "source": "sage_alerts_cron",
            "subscription_id": sub_id,
        }
        publish_ok = await _publish(redis, payload)

        logger.info(
            "sage_alerts: fired sub=%s partner=%s channel=%s recipient=%s "
            "snap=%s render_ms=%d publish=%s",
            sub_id,
            partner_id,
            row["delivery_channel"],
            row["recipient"],
            snapshot_id,
            int((time.monotonic() - render_start) * 1000),
            publish_ok,
        )
    finally:
        # Advance next_fire_at regardless of success — skip-not-spam.
        # If cron_expr is malformed and next_fire_after raises, fall back to
        # fire_ts + TICK_INTERVAL_SEC so the row doesn't hot-loop every tick.
        try:
            next_fire = next_fire_after(row["cron_expr"], fire_ts)
        except Exception:
            logger.exception(
                "sage_alerts: bad cron_expr=%r sub=%s; advancing by %ds",
                row.get("cron_expr"),
                sub_id,
                TICK_INTERVAL_SEC,
            )
            next_fire = fire_ts + timedelta(seconds=TICK_INTERVAL_SEC)
        try:
            async with pool.acquire() as conn:
                await conn.execute(
                    """
                    UPDATE alert_subscriptions
                    SET last_fired_at = $1, next_fire_at = $2
                    WHERE id = $3
                    """,
                    fire_ts,
                    next_fire,
                    sub_id,
                )
        except Exception:
            logger.exception(
                "sage_alerts: failed to advance next_fire_at for sub=%s", sub_id
            )

        if span is not None:
            span.set_attribute("pattern_e.render_ok", render_ok)
            span.set_attribute("pattern_e.publish_ok", publish_ok)
            span.set_attribute(
                "pattern_e.render_ms", int((time.monotonic() - render_start) * 1000)
            )
            span.end()


def _open_span(name: str, **attrs):
    try:
        from opentelemetry import trace

        tracer = trace.get_tracer("mundi.cron.sage_alerts")
        span = tracer.start_span(name)
        for k, v in attrs.items():
            try:
                span.set_attribute(k, v if v is not None else "")
            except Exception:
                pass
        return span
    except Exception:
        return None


async def _tick(pool, redis) -> int:
    async with pool.acquire() as conn:
        # Bootstrap any rows with NULL next_fire_at so they fire on the next tick.
        await conn.execute(
            """
            UPDATE alert_subscriptions
            SET next_fire_at = now()
            WHERE enabled = true AND next_fire_at IS NULL
            """
        )
        rows = await conn.fetch(
            """
            SELECT id, partner_id, map_id, bbox, width, height,
                   delivery_channel, recipient, caption_template, cron_expr
            FROM alert_subscriptions
            WHERE enabled = true AND next_fire_at <= now()
            ORDER BY next_fire_at ASC
            LIMIT $1
            """,
            MAX_PER_TICK,
        )
    for row in rows:
        try:
            await _fire_subscription(pool, redis, row)
        except Exception:
            logger.exception("sage_alerts: subscription handler raised sub=%s", row["id"])
    return len(rows)


async def run_forever() -> None:
    logging.basicConfig(
        level=os.environ.get("LOG_LEVEL", "INFO"),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    logger.info("sage_alerts starting (tick=%ds, max_per_tick=%d)", TICK_INTERVAL_SEC, MAX_PER_TICK)

    stop = asyncio.Event()

    def _on_signal(*_a):
        logger.info("sage_alerts: signal received, shutting down")
        stop.set()

    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, _on_signal)
        except NotImplementedError:
            pass

    pool: Optional[object] = None
    redis = None
    reconnect_delay = 1.0
    while not stop.is_set():
        try:
            if pool is None:
                pool = await _get_pool()
            if redis is None:
                redis = await _get_redis()
            reconnect_delay = 1.0

            fired = await _tick(pool, redis)
            if fired:
                logger.info("sage_alerts: tick fired %d subscription(s)", fired)

            try:
                await asyncio.wait_for(stop.wait(), timeout=TICK_INTERVAL_SEC)
            except asyncio.TimeoutError:
                pass
        except Exception:
            logger.exception(
                "sage_alerts: tick loop crashed; reconnecting in %.1fs", reconnect_delay
            )
            try:
                if pool is not None:
                    await pool.close()
            except Exception:
                pass
            try:
                if redis is not None:
                    await redis.aclose()
            except Exception:
                pass
            pool = None
            redis = None
            await asyncio.sleep(reconnect_delay)
            reconnect_delay = min(reconnect_delay * 2, 30.0)

    try:
        if pool is not None:
            await pool.close()
    except Exception:
        pass
    try:
        if redis is not None:
            await redis.aclose()
    except Exception:
        pass
    logger.info("sage_alerts stopped")


if __name__ == "__main__":
    asyncio.run(run_forever())
