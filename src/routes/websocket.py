import asyncio
import json
import logging
import os
import time
import uuid
from collections import defaultdict, deque
from contextlib import asynccontextmanager, suppress
from datetime import datetime, timezone
from typing import Any, Dict, Tuple

import asyncpg
from fastapi import APIRouter, WebSocket, WebSocketDisconnect, Depends
from anyio import EndOfStream
from pydantic import BaseModel, ConfigDict

from src.dependencies.session import UserContext, verify_websocket
from src.database.models import Conversation, MundiChatCompletionMessage
from src.structures import (
    get_async_db_connection,
    convert_mundi_message_to_sanitized,
    _build_postgres_url,
)

logger = logging.getLogger(__name__)


class ConversationRelatedPayload(BaseModel):
    conversation_id: int


class ChatCompletionReferenceNotificationPayload(ConversationRelatedPayload):
    id: int
    map_id: str


class EphemeralNotificationPayload(ConversationRelatedPayload):
    ephemeral: bool
    action_id: str
    layer_id: str | None
    action: str
    timestamp: datetime
    completed_at: datetime | None
    status: str
    bounds: list[float] | None
    updates: dict[str, Any]

    model_config = ConfigDict(
        json_encoders={datetime: lambda v: v.isoformat() if v else None}
    )


class EphemeralErrorNotificationPayload(ConversationRelatedPayload):
    ephemeral: bool
    action_id: str
    error_message: str
    timestamp: datetime
    status: str

    model_config = ConfigDict(
        json_encoders={datetime: lambda v: v.isoformat() if v else None}
    )


# Create router
router = APIRouter()

# Subscriber registry for WebSocket notifications by conversation_id
# conversation_id -> set[asyncio.Queue]
subscribers_by_conversation = defaultdict(set)
subscribers_lock = asyncio.Lock()

# Track recently disconnected users and their missed messages per conversation
# (user_id, conversation_id) -> {"disconnect_time": float, "missed_messages": deque[(timestamp, payload)]}
recently_disconnected_users: Dict[Tuple[str, int], Dict[str, Any]] = {}
DISCONNECT_TTL = 30.0  # Keep disconnected user data for 30 seconds
MAX_MISSED_MESSAGES = 100  # Limit buffer size per user per conversation

CHAT_CH = "chat_completion_messages_notify"
REDIS_WS_CHANNEL = "ws:ephemeral"  # Redis Pub/Sub channel for cross-worker ephemeral messages
chat_q: asyncio.Queue[str] = asyncio.Queue()
# Initialize listener tasks at module level
_listener_task: asyncio.Task | None = None
_redis_sub_task: asyncio.Task | None = None


# ---------------------------------------------------------------------------
# Redis Pub/Sub for cross-worker WebSocket message routing
# ---------------------------------------------------------------------------

async def _get_redis_pubsub():
    """Create a Redis Pub/Sub connection for subscribing."""
    try:
        from redis.asyncio import Redis as AsyncRedis
        redis = AsyncRedis(
            host=os.environ.get("REDIS_HOST", "localhost"),
            port=int(os.environ.get("REDIS_PORT", 6379)),
            decode_responses=True,
        )
        return redis
    except Exception:
        logger.debug("Redis not available for Pub/Sub", exc_info=True)
        return None


async def _publish_to_redis(payload_json: str) -> bool:
    """Publish a message to Redis Pub/Sub for cross-worker distribution.

    Returns True if published successfully, False otherwise.
    """
    try:
        from redis.asyncio import Redis as AsyncRedis
        redis = AsyncRedis(
            host=os.environ.get("REDIS_HOST", "localhost"),
            port=int(os.environ.get("REDIS_PORT", 6379)),
            decode_responses=True,
        )
        try:
            await redis.publish(REDIS_WS_CHANNEL, payload_json)
            return True
        finally:
            await redis.aclose()
    except Exception:
        logger.debug("Failed to publish to Redis Pub/Sub (falling back to local)", exc_info=True)
        return False


async def _redis_pubsub_listener():
    """Subscribe to Redis Pub/Sub and distribute ephemeral messages to local WebSocket queues.

    Each uvicorn worker runs this task independently. When an ephemeral message
    is published to Redis by any worker, all workers receive it and distribute
    to their local WebSocket connections.
    """
    reconnect_delay = 1
    max_reconnect_delay = 30

    while True:
        redis = None
        pubsub = None
        try:
            redis = await _get_redis_pubsub()
            if redis is None:
                await asyncio.sleep(10)
                continue

            pubsub = redis.pubsub()
            await pubsub.subscribe(REDIS_WS_CHANNEL)
            logger.info("Redis Pub/Sub subscriber connected to channel: %s", REDIS_WS_CHANNEL)
            reconnect_delay = 1

            async for message in pubsub.listen():
                if message["type"] != "message":
                    continue
                payload_json = message["data"]
                try:
                    await _distribute_from_json(payload_json)
                except Exception:
                    logger.exception("Error distributing Redis Pub/Sub message")

        except asyncio.CancelledError:
            raise
        except Exception as e:
            logger.warning("Redis Pub/Sub subscriber error: %s", e)
        finally:
            if pubsub is not None:
                with suppress(Exception):
                    await pubsub.unsubscribe(REDIS_WS_CHANNEL)
                    await pubsub.aclose()
            if redis is not None:
                with suppress(Exception):
                    await redis.aclose()

        logger.info("Redis Pub/Sub reconnecting in %ds...", reconnect_delay)
        await asyncio.sleep(reconnect_delay)
        reconnect_delay = min(reconnect_delay * 2, max_reconnect_delay)


# ---------------------------------------------------------------------------
# Local distribution helpers
# ---------------------------------------------------------------------------

def _parse_payload(payload_dict: dict) -> ConversationRelatedPayload:
    """Parse a dict into the appropriate payload model."""
    if payload_dict.get("ephemeral"):
        if "error_message" in payload_dict:
            return EphemeralErrorNotificationPayload(**payload_dict)
        return EphemeralNotificationPayload(**payload_dict)
    return ChatCompletionReferenceNotificationPayload(**payload_dict)


async def _distribute_to_local(conversation_id: int, parsed_payload: ConversationRelatedPayload):
    """Distribute a parsed payload to local in-memory subscriber queues
    and buffer for recently disconnected users.
    """
    now = time.time()

    # Store messages for recently disconnected users who might reconnect
    users_to_remove = []
    for (
        user_id,
        disconnected_conversation_id,
    ), user_data in recently_disconnected_users.items():
        if now - user_data["disconnect_time"] > DISCONNECT_TTL:
            users_to_remove.append((user_id, disconnected_conversation_id))
            continue

        if disconnected_conversation_id == conversation_id:
            missed_messages = user_data["missed_messages"]
            missed_messages.append((now, parsed_payload))
            while len(missed_messages) > MAX_MISSED_MESSAGES:
                missed_messages.popleft()

    for user_key in users_to_remove:
        del recently_disconnected_users[user_key]

    # Broadcast to live subscribers on this worker
    async with subscribers_lock:
        queues = list(subscribers_by_conversation.get(conversation_id, []))
    for q in queues:
        q.put_nowait(parsed_payload)


async def _distribute_from_json(payload_json: str):
    """Parse JSON and distribute to local subscribers."""
    payload_dict = json.loads(payload_json)
    parsed_payload = _parse_payload(payload_dict)
    assert parsed_payload.conversation_id, "conversation_id is required"
    await _distribute_to_local(parsed_payload.conversation_id, parsed_payload)


async def _publish_and_distribute(payload: ConversationRelatedPayload):
    """Publish an ephemeral payload via Redis Pub/Sub for cross-worker delivery.

    Falls back to local-only distribution if Redis is unavailable.
    """
    payload_json = payload.model_dump_json()
    published = await _publish_to_redis(payload_json)
    if not published:
        # Redis unavailable — distribute locally only (single-worker fallback)
        await _distribute_to_local(payload.conversation_id, payload)


# ---------------------------------------------------------------------------
# Startup / lifecycle
# ---------------------------------------------------------------------------

def start_chat_listener():
    global _listener_task

    if _listener_task is None or _listener_task.done():
        dsn = _build_postgres_url()
        _listener_task = asyncio.create_task(_chat_pg_listener(dsn=dsn))

    return _listener_task


def start_redis_subscriber():
    global _redis_sub_task

    if _redis_sub_task is None or _redis_sub_task.done():
        _redis_sub_task = asyncio.create_task(_redis_pubsub_listener())

    return _redis_sub_task


@router.on_event("startup")
async def startup_listener():
    global _listener_task, _redis_sub_task

    # Cancel and await previous listener task if exists
    if _listener_task is not None and not _listener_task.done():
        _listener_task.cancel()
        try:
            await _listener_task
        except asyncio.CancelledError:
            pass

    # Start PG listener
    start_chat_listener()
    # Start Redis Pub/Sub subscriber for cross-worker ephemeral messages
    start_redis_subscriber()
    # Start cleanup task for recently disconnected users
    asyncio.create_task(cleanup_recently_disconnected_users())


async def _chat_pg_listener(dsn: str):
    """PostgreSQL LISTEN connection with reconnection, health checks, and lifecycle management."""
    reconnect_delay = 1
    max_reconnect_delay = 60

    while True:
        conn = None
        try:
            # Connect with timeout
            conn = await asyncio.wait_for(asyncpg.connect(dsn), timeout=10)

            # Add listener with existing callback
            await conn.add_listener(
                CHAT_CH,
                lambda _conn, _pid, _channel, payload: asyncio.create_task(
                    _broadcast_payload(payload)
                ),
            )

            logger.info("PostgreSQL listener connected to channel: %s", CHAT_CH)
            reconnect_delay = 1  # Reset on successful connection

            # Health check loop - periodic ping to detect dead connections
            while True:
                await asyncio.sleep(60)
                try:
                    await conn.execute("SELECT 1")
                except asyncpg.PostgresError:
                    logger.warning("Listener health check failed, reconnecting")
                    break

        except asyncio.CancelledError:
            # Always propagate cancellation
            raise
        except asyncpg.PostgresError as e:
            logger.error("PostgreSQL listener error: %s", e, exc_info=True)
        except asyncio.TimeoutError:
            logger.error("PostgreSQL listener connection timeout")
        except Exception as e:
            logger.exception("Unexpected listener error: %s", e)
        finally:
            if conn is not None:
                try:
                    await asyncio.wait_for(conn.close(), timeout=5)
                except asyncio.TimeoutError:
                    logger.error("Timeout closing listener connection")
                except Exception:
                    logger.debug("Error closing listener connection", exc_info=True)

        # Reconnect with exponential backoff
        logger.info("Listener reconnecting in %ds...", reconnect_delay)
        await asyncio.sleep(reconnect_delay)
        reconnect_delay = min(reconnect_delay * 2, max_reconnect_delay)


async def cleanup_recently_disconnected_users():
    """Periodically clean up expired disconnected users"""
    while True:
        try:
            await asyncio.sleep(60)  # Run cleanup every minute
            now = time.time()

            # Clean up users who disconnected too long ago
            users_to_remove = []
            for (
                user_id,
                conversation_id,
            ), user_data in recently_disconnected_users.items():
                if now - user_data["disconnect_time"] > DISCONNECT_TTL:
                    users_to_remove.append((user_id, conversation_id))

            # Remove expired users
            for user_key in users_to_remove:
                del recently_disconnected_users[user_key]

        except Exception:
            logger.exception("Error in cleanup_recently_disconnected_users")


async def get_websocket_conversation(
    conversation_id: int, user_context: UserContext
) -> Conversation | None:
    """Get conversation for WebSocket with proper authentication"""
    user_id = user_context.get_user_id()

    async with get_async_db_connection() as conn:
        conversation = await conn.fetchrow(
            """
            SELECT id, project_id, owner_uuid, title, created_at, updated_at, soft_deleted_at
            FROM conversations
            WHERE id = $1 AND owner_uuid = $2 AND soft_deleted_at IS NULL
            """,
            conversation_id,
            user_id,
        )
        if not conversation:
            return None

        return Conversation(
            id=conversation["id"],
            project_id=conversation["project_id"],
            owner_uuid=conversation["owner_uuid"],
            title=conversation["title"],
            created_at=conversation["created_at"],
            updated_at=conversation["updated_at"],
            soft_deleted_at=conversation["soft_deleted_at"],
        )


@router.websocket("/ws/{conversation_id}/messages/updates")
async def ws_conversation_chat(
    ws: WebSocket,
    conversation_id: int,
    user_context: UserContext = Depends(verify_websocket),
):
    # Auth is now handled by verify_websocket dependency (Clerk JWT or legacy mode)
    user_id = user_context.get_user_id()

    # Check if user owns the conversation
    conversation = await get_websocket_conversation(conversation_id, user_context)
    if not conversation:
        await ws.close(code=4403, reason="Unauthorized")
        return

    await ws.accept()
    queue = asyncio.Queue()
    async with subscribers_lock:
        subscribers_by_conversation[conversation_id].add(queue)

    # Check if this user recently disconnected from this specific conversation and replay their missed messages
    user_conversation_key = (user_id, conversation_id)
    if user_conversation_key in recently_disconnected_users:
        user_data = recently_disconnected_users[user_conversation_key]
        missed_messages = user_data["missed_messages"]

        # Replay all missed messages for this specific user on this specific conversation
        for ts, missed_payload in missed_messages:
            queue.put_nowait(missed_payload)

        # Remove user from recently disconnected since they've reconnected to this conversation
        del recently_disconnected_users[user_conversation_key]
    try:
        while True:
            queue_task = asyncio.create_task(queue.get())
            recv_task = asyncio.create_task(ws.receive())

            done, pending = await asyncio.wait(
                {queue_task, recv_task}, return_when=asyncio.FIRST_COMPLETED
            )

            # client closed
            if recv_task in done:
                try:
                    recv_task.result()
                except (WebSocketDisconnect, EndOfStream):
                    pass

                for task in pending:
                    task.cancel()
                    with suppress(asyncio.CancelledError):
                        await task
                break

            # got a payload
            payload = queue_task.result()
            recv_task.cancel()
            with suppress(asyncio.CancelledError):
                await recv_task

            assert (
                isinstance(payload, ChatCompletionReferenceNotificationPayload)
                or isinstance(payload, EphemeralNotificationPayload)
                or isinstance(payload, EphemeralErrorNotificationPayload)
            )

            # Check if this is an ephemeral message
            if isinstance(
                payload,
                (EphemeralNotificationPayload, EphemeralErrorNotificationPayload),
            ):
                # Send ephemeral message directly without DB lookup
                await ws.send_json(payload.model_dump(mode="json"))
                continue
            # Get the full message from the database using the id from notification
            ccref_notification: ChatCompletionReferenceNotificationPayload = payload
            async with get_async_db_connection() as conn:
                message = await conn.fetchrow(
                    """
                    SELECT * FROM chat_completion_messages
                    WHERE id = $1 AND conversation_id = $2
                    """,
                    ccref_notification.id,
                    ccref_notification.conversation_id,
                )

                if message:
                    msg_dict = dict(message)
                    # Parse message_json ... when using raw asyncpg
                    msg_dict["message_json"] = json.loads(msg_dict["message_json"])
                    cc_message = MundiChatCompletionMessage(**msg_dict)
                    if cc_message.message_json["role"] == "system":
                        continue
                    sanitized_payload = convert_mundi_message_to_sanitized(cc_message)
                    await ws.send_json(sanitized_payload.model_dump(mode="json"))

    except WebSocketDisconnect:
        pass
    except asyncio.CancelledError:
        pass
    except Exception as e:
        logger.error("Unexpected WebSocket error for conversation %s: %s", conversation_id, e, exc_info=True)
    finally:
        # Track this user as recently disconnected from this specific conversation
        user_conversation_key = (user_id, conversation_id)
        recently_disconnected_users[user_conversation_key] = {
            "disconnect_time": time.time(),
            "missed_messages": deque(),
        }

        async with subscribers_lock:
            subscribers_by_conversation[conversation_id].discard(queue)
            if not subscribers_by_conversation[conversation_id]:
                del subscribers_by_conversation[conversation_id]


async def _broadcast_payload(payload: str):
    """Handle PostgreSQL NOTIFY payloads — distribute to local subscribers.

    Each worker has its own PG LISTEN connection, so this already works
    across multiple workers without Redis.
    """
    try:
        await _distribute_from_json(payload)
    except Exception:
        logger.exception("Error broadcasting payload")
        raise


@asynccontextmanager
async def kue_ephemeral_action(
    conversation_id: int,
    action_description: str,
    layer_id: str | None = None,
    update_style_json: bool = False,
    bounds: list[float] | None = None,
):
    """
    Async context manager for ephemeral actions.
    Sends a websocket message with the action when entering,
    and automatically removes it when exiting the context.

    Uses Redis Pub/Sub for cross-worker delivery (falls back to local if Redis unavailable).
    """
    payload = EphemeralNotificationPayload(
        conversation_id=conversation_id,
        ephemeral=True,
        action_id=str(uuid.uuid4()),
        layer_id=layer_id,
        action=action_description,
        timestamp=datetime.now(timezone.utc),
        completed_at=None,
        status="active",
        bounds=bounds,
        updates={
            "style_json": update_style_json,
        },
    )

    # Put it on the event loop to prevent race conditions
    await asyncio.sleep(0.05)

    try:
        # Publish via Redis for cross-worker delivery (falls back to local)
        await _publish_and_distribute(payload)

        # Yield control back to the caller
        yield payload

    finally:
        # Always send the action completed message
        finished_payload = payload.model_copy()
        finished_payload.status = "completed"
        finished_payload.completed_at = datetime.now(timezone.utc)

        # Publish completion via Redis for cross-worker delivery
        await _publish_and_distribute(finished_payload)


async def kue_notify_error(conversation_id: int, error_message: str):
    """
    Send an ephemeral error notification to the client.
    Unlike kue_ephemeral_action, this is not a context manager and sends a single error message.

    Uses Redis Pub/Sub for cross-worker delivery (falls back to local if Redis unavailable).
    """
    payload = EphemeralErrorNotificationPayload(
        conversation_id=conversation_id,
        ephemeral=True,
        action_id=str(uuid.uuid4()),
        error_message=error_message,
        timestamp=datetime.now(timezone.utc),
        status="error",
    )

    # Publish via Redis for cross-worker delivery (falls back to local)
    await _publish_and_distribute(payload)
