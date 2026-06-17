"""Telegram sender for Sage map snapshots.

Subscribes to 'mundi:render_snapshot', filters on delivery_channel='telegram',
downloads the PNG from MinIO, and posts to the Telegram Bot API.

Week-1 mode: log-only stub. Set MUNDI_TELEGRAM_LIVE=1 and provide
MUNDI_TELEGRAM_BOT_TOKEN to actually send. Partner-scoped delivery is
enforced before send: a sender configured for partner X drops payloads
for any other partner_id even if they appear on the shared channel.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import signal
from typing import Optional

logger = logging.getLogger("mundi.senders.telegram")

CHANNEL = "mundi:render_snapshot"


async def _get_redis():
    from redis.asyncio import Redis as AsyncRedis

    return AsyncRedis(
        host=os.environ.get("REDIS_HOST", "localhost"),
        port=int(os.environ.get("REDIS_PORT", 6379)),
        decode_responses=True,
    )


def _allowed_partner() -> Optional[str]:
    val = os.environ.get("MUNDI_TELEGRAM_PARTNER_ID", "").strip()
    return val or None


def _is_live() -> bool:
    return os.environ.get("MUNDI_TELEGRAM_LIVE", "0") == "1"


def _bot_token() -> Optional[str]:
    val = os.environ.get("MUNDI_TELEGRAM_BOT_TOKEN", "").strip()
    return val or None


async def _download_png(bucket: str, key: str) -> bytes:
    from src.utils import get_async_s3_client

    s3 = await get_async_s3_client()
    obj = await s3.get_object(Bucket=bucket, Key=key)
    body = obj["Body"]
    try:
        data = await body.read()
    finally:
        try:
            body.close()
        except Exception:
            pass
    return data


async def _send_telegram_photo(
    chat_id: str, caption: str, png_bytes: bytes
) -> bool:
    token = _bot_token()
    if not token:
        logger.error("MUNDI_TELEGRAM_BOT_TOKEN is not set; cannot send")
        return False
    try:
        import httpx

        url = f"https://api.telegram.org/bot{token}/sendPhoto"
        files = {"photo": ("snapshot.png", png_bytes, "image/png")}
        data = {"chat_id": chat_id, "caption": caption}
        async with httpx.AsyncClient(timeout=30.0) as client:
            resp = await client.post(url, data=data, files=files)
        if resp.status_code != 200:
            logger.error(
                "Telegram sendPhoto failed: status=%s body=%s",
                resp.status_code,
                resp.text[:500],
            )
            return False
        return True
    except Exception:
        logger.exception("Telegram sendPhoto raised")
        return False


async def _handle(payload: dict) -> None:
    snapshot_id = payload.get("snapshot_id")
    channel = payload.get("delivery_channel")
    if channel != "telegram":
        return

    allowed = _allowed_partner()
    payload_partner = payload.get("partner_id")
    if allowed is not None and payload_partner != allowed:
        logger.info(
            "telegram_sender: dropping snap=%s; partner mismatch payload=%s allowed=%s",
            snapshot_id,
            payload_partner,
            allowed,
        )
        return

    recipient = (payload.get("recipient") or "").strip()
    if not recipient:
        logger.warning("telegram_sender: snap=%s has empty recipient", snapshot_id)
        return

    bucket = payload.get("png_s3_bucket")
    key = payload.get("png_s3_key")
    if not bucket or not key:
        logger.warning(
            "telegram_sender: snap=%s missing s3 coords (bucket=%s key=%s)",
            snapshot_id,
            bucket,
            key,
        )
        return

    caption = payload.get("caption") or ""

    if not _is_live():
        logger.info(
            "telegram_sender [stub]: would send snap=%s to chat=%s caption=%r s3=%s/%s",
            snapshot_id,
            recipient,
            caption,
            bucket,
            key,
        )
        return

    try:
        png = await _download_png(bucket, key)
    except Exception:
        logger.exception("telegram_sender: snap=%s s3 download failed", snapshot_id)
        return

    ok = await _send_telegram_photo(recipient, caption, png)
    logger.info(
        "telegram_sender: snap=%s delivered=%s chat=%s bytes=%d",
        snapshot_id,
        ok,
        recipient,
        len(png),
    )


async def run_forever() -> None:
    logging.basicConfig(
        level=os.environ.get("LOG_LEVEL", "INFO"),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    logger.info(
        "telegram_sender starting (live=%s partner_filter=%s)",
        _is_live(),
        _allowed_partner() or "<any>",
    )

    stop = asyncio.Event()

    def _on_signal(*_a):
        logger.info("telegram_sender: signal received, shutting down")
        stop.set()

    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, _on_signal)
        except NotImplementedError:
            pass

    reconnect_delay = 1.0
    while not stop.is_set():
        redis = None
        pubsub = None
        try:
            redis = await _get_redis()
            pubsub = redis.pubsub()
            await pubsub.subscribe(CHANNEL)
            logger.info("telegram_sender subscribed to %s", CHANNEL)
            reconnect_delay = 1.0

            while not stop.is_set():
                msg = await pubsub.get_message(
                    ignore_subscribe_messages=True, timeout=1.0
                )
                if msg is None:
                    continue
                data = msg.get("data")
                if not data:
                    continue
                try:
                    payload = json.loads(data)
                except (TypeError, ValueError):
                    logger.warning("telegram_sender: non-JSON message dropped")
                    continue
                try:
                    await _handle(payload)
                except Exception:
                    logger.exception("telegram_sender: handler raised")
        except Exception:
            logger.exception(
                "telegram_sender: pubsub loop crashed; reconnecting in %.1fs",
                reconnect_delay,
            )
            await asyncio.sleep(reconnect_delay)
            reconnect_delay = min(reconnect_delay * 2, 30.0)
        finally:
            try:
                if pubsub is not None:
                    await pubsub.unsubscribe(CHANNEL)
                    await pubsub.aclose()
            except Exception:
                pass
            try:
                if redis is not None:
                    await redis.aclose()
            except Exception:
                pass

    logger.info("telegram_sender stopped")


if __name__ == "__main__":
    asyncio.run(run_forever())
