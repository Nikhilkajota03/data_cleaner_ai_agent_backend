"""
Event Streaming and Pub/Sub Broadcaster.

Provides a unified event broadcasting mechanism for Server-Sent Events (SSE).
Combines Redis Pub/Sub for real-time node transitions with a Redis List buffer
for reliable event replay when clients connect asynchronously.
Includes an in-memory fallback buffer when Redis is unavailable.
"""

import asyncio
from collections import defaultdict, deque
from datetime import datetime, timezone
import json
import logging
from typing import Any, AsyncGenerator, Optional

import redis.asyncio as redis

from backend.app.core.config import settings

logger = logging.getLogger(__name__)

# In-memory fallback event history: thread_id -> deque of event dicts
_MEMORY_EVENT_BUFFER: dict[str, deque[dict[str, Any]]] = defaultdict(lambda: deque(maxlen=200))
_MEMORY_SUBSCRIBERS: dict[str, list[asyncio.Queue]] = defaultdict(list)


class ThreadEventBroadcaster:
    """Broadcaster for multi-agent pipeline events with Redis & in-memory support."""

    @classmethod
    async def get_redis_client(cls) -> Optional[redis.Redis]:
        """Returns an async Redis client or None if unreachable."""
        try:
            client = redis.from_url(settings.REDIS_URL, decode_responses=True)
            await client.ping()
            return client
        except Exception as e:
            logger.debug(f"Redis unavailable for event broadcasting: {e}")
            return None

    @classmethod
    async def publish_event(
        cls,
        thread_id: str,
        event_type: str,
        data: dict[str, Any],
    ) -> None:
        """
        Publishes a pipeline event to Redis list (for replay) and Redis channel (for live listeners).
        Also preserves it in the memory fallback buffer.
        """
        event_payload = {
            "thread_id": thread_id,
            "event": event_type,
            "data": data,
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }
        serialized = json.dumps(event_payload)

        # 1. Update in-memory fallback
        _MEMORY_EVENT_BUFFER[thread_id].append(event_payload)
        for q in _MEMORY_SUBSCRIBERS.get(thread_id, []):
            try:
                q.put_nowait(event_payload)
            except asyncio.QueueFull:
                pass

        # 2. Update Redis if connected
        redis_client = await cls.get_redis_client()
        if redis_client:
            try:
                list_key = f"pipeline:events:{thread_id}"
                channel_key = f"pipeline:channel:{thread_id}"
                async with redis_client.pipeline(transaction=True) as pipe:
                    pipe.rpush(list_key, serialized)
                    pipe.expire(list_key, settings.REDIS_SESSION_TTL_SECONDS)
                    pipe.publish(channel_key, serialized)
                    await pipe.execute()
            except Exception as ex:
                logger.warning(f"Failed to publish event to Redis: {ex}")
            finally:
                await redis_client.aclose()

    @classmethod
    async def get_event_history(cls, thread_id: str) -> list[dict[str, Any]]:
        """Retrieves previously recorded events for a given thread_id."""
        redis_client = await cls.get_redis_client()
        if redis_client:
            try:
                list_key = f"pipeline:events:{thread_id}"
                raw_events = await redis_client.lrange(list_key, 0, -1)
                if raw_events:
                    return [json.loads(evt) for evt in raw_events]
            except Exception as ex:
                logger.warning(f"Failed to fetch event history from Redis: {ex}")
            finally:
                await redis_client.aclose()

        # Fallback to memory
        return list(_MEMORY_EVENT_BUFFER.get(thread_id, []))

    @classmethod
    async def stream_events(
        cls,
        thread_id: str,
        timeout_seconds: float = 60.0,
    ) -> AsyncGenerator[dict[str, Any], None]:
        """
        Yields Server-Sent Events (SSE) for a given thread.
        First replays historical events, then listens for new live events
        until a terminal event (hitl_paused, completed, failed, rejected) occurs.
        """
        seen_events: set[str] = set()

        # 1. Replay historical events
        history = await cls.get_event_history(thread_id)
        last_event_type = ""
        for item in history:
            unique_id = f"{item.get('event')}_{item.get('timestamp')}"
            if unique_id not in seen_events:
                seen_events.add(unique_id)
                last_event_type = item.get("event", "")
                yield {
                    "event": item.get("event", "message"),
                    "data": json.dumps(item.get("data", {})),
                }

        # Terminal conditions: if already paused or completed, finish replay
        if last_event_type in ("hitl_paused", "completed", "failed", "rejected"):
            return

        # 2. Live subscription (Try Redis pub/sub, fall back to memory queue)
        redis_client = await cls.get_redis_client()
        if redis_client:
            pubsub = redis_client.pubsub()
            channel_key = f"pipeline:channel:{thread_id}"
            await pubsub.subscribe(channel_key)
            try:
                start_time = asyncio.get_event_loop().time()
                while True:
                    elapsed = asyncio.get_event_loop().time() - start_time
                    if elapsed > timeout_seconds:
                        break

                    message = await pubsub.get_message(
                        ignore_subscribe_messages=True,
                        timeout=1.0,
                    )
                    if message and message.get("type") == "message":
                        raw_data = message.get("data")
                        if isinstance(raw_data, str):
                            parsed = json.loads(raw_data)
                            event_type = parsed.get("event", "message")
                            yield {
                                "event": event_type,
                                "data": json.dumps(parsed.get("data", {})),
                            }
                            if event_type in ("hitl_paused", "completed", "failed", "rejected"):
                                break
                    await asyncio.sleep(0.1)
            finally:
                await pubsub.unsubscribe(channel_key)
                await pubsub.aclose()
                await redis_client.aclose()
        else:
            # Memory subscription fallback
            local_queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
            _MEMORY_SUBSCRIBERS[thread_id].append(local_queue)
            try:
                start_time = asyncio.get_event_loop().time()
                while True:
                    elapsed = asyncio.get_event_loop().time() - start_time
                    if elapsed > timeout_seconds:
                        break
                    try:
                        parsed = await asyncio.wait_for(local_queue.get(), timeout=1.0)
                        event_type = parsed.get("event", "message")
                        yield {
                            "event": event_type,
                            "data": json.dumps(parsed.get("data", {})),
                        }
                        if event_type in ("hitl_paused", "completed", "failed", "rejected"):
                            break
                    except asyncio.TimeoutError:
                        continue
            finally:
                if local_queue in _MEMORY_SUBSCRIBERS.get(thread_id, []):
                    _MEMORY_SUBSCRIBERS[thread_id].remove(local_queue)
