"""In-memory pub/sub for OPT-0013 SSE alert push.

Scheduler (a background thread) publishes; FastAPI handlers (asyncio)
subscribe. Bridges the thread → asyncio boundary via
``loop.call_soon_threadsafe`` — never call queue.put_nowait directly from
the scheduler thread, asyncio.Queue isn't thread-safe.

Scope: single-process. If we ever go multi-worker (uvicorn --workers >1),
switch to Redis pub/sub; the public publish/subscribe API can stay.
"""

from __future__ import annotations

import asyncio
import logging
import threading
from typing import Any, AsyncIterator

logger = logging.getLogger(__name__)

# Each subscriber gets its own queue + the event loop that owns it. The
# main FastAPI loop is captured per-subscribe (so we'd work even if a
# test creates an ad-hoc loop), not at module import.
_subscribers: list[tuple[asyncio.Queue, asyncio.AbstractEventLoop]] = []
_lock = threading.Lock()

# Cap per subscriber so a slow client can't OOM the server.
_MAX_QUEUE_SIZE = 100


def publish(event: dict[str, Any]) -> int:
    """Fan out one event to every subscriber. Safe to call from any thread.

    Returns the number of subscribers that received it. Drops events on
    queues that are already full (logged at WARNING — that subscriber is
    too slow / disconnected and will be GC'd on its next iteration).
    """
    with _lock:
        snapshot = list(_subscribers)

    delivered = 0
    for q, loop in snapshot:
        if loop.is_closed():
            continue
        try:
            loop.call_soon_threadsafe(_safe_put, q, event)
            delivered += 1
        except RuntimeError:
            # Loop shut down between snapshot and call; the dead subscriber
            # will get cleaned up when its handler exits its await loop.
            continue
    return delivered


def _safe_put(q: asyncio.Queue, event: dict[str, Any]) -> None:
    try:
        q.put_nowait(event)
    except asyncio.QueueFull:
        logger.warning(
            "alerts_pubsub: subscriber queue full (size=%d), dropping event",
            q.qsize(),
        )


async def subscribe() -> AsyncIterator[dict[str, Any]]:
    """Async generator yielding events as they arrive. Caller MUST iterate
    in a context that can clean up on disconnect (the SSE handler does
    this via try/finally).
    """
    loop = asyncio.get_running_loop()
    q: asyncio.Queue = asyncio.Queue(maxsize=_MAX_QUEUE_SIZE)
    entry = (q, loop)
    with _lock:
        _subscribers.append(entry)
    try:
        while True:
            event = await q.get()
            yield event
    finally:
        with _lock:
            try:
                _subscribers.remove(entry)
            except ValueError:
                pass


def subscriber_count() -> int:
    """Number of active subscribers — used by tests + admin endpoints."""
    with _lock:
        return len(_subscribers)


def _reset_for_tests() -> None:
    """Test-only hook to clear the subscriber list."""
    with _lock:
        _subscribers.clear()
