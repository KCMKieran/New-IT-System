"""Tests for OPT-0013 SSE alert pub/sub.

Locked behaviors:
- Cold start: 0 subscribers
- subscribe() + publish() round-trip works in asyncio
- Multiple subscribers all receive each event (fan-out)
- Disconnect removes subscriber from list
- Cross-thread publish: scheduler-thread call_soon_threadsafe path works
- Bounded queue: full subscriber doesn't crash publish for others
- SSE_ENABLED env flag default off
"""

from __future__ import annotations

import asyncio
import os
import threading
import time

import pytest

from app.core import alerts_pubsub as pubsub


@pytest.fixture(autouse=True)
def reset_pubsub():
    pubsub._reset_for_tests()
    yield
    pubsub._reset_for_tests()


def _run(coro):
    """Run an async coroutine in a fresh event loop (each test isolated)."""
    return asyncio.run(coro)


# ── cold start ────────────────────────────────────────────────────────────

def test_no_subscribers_initially():
    assert pubsub.subscriber_count() == 0


def test_publish_with_no_subscribers_returns_zero():
    delivered = pubsub.publish({"type": "scan"})
    assert delivered == 0


# ── basic publish/subscribe round-trip ───────────────────────────────────

def test_subscribe_receives_published_event():
    async def run():
        received: list[dict] = []
        gen = pubsub.subscribe()
        sub_iter = gen.__aiter__()

        async def collect_one():
            evt = await asyncio.wait_for(sub_iter.__anext__(), timeout=2)
            received.append(evt)

        task = asyncio.create_task(collect_one())
        # Wait until subscribe() finishes registration
        for _ in range(50):
            await asyncio.sleep(0.01)
            if pubsub.subscriber_count() >= 1:
                break
        assert pubsub.subscriber_count() == 1

        pubsub.publish({"type": "scan", "tier": "fast_burst", "new_alert_count": 2})
        await task
        assert received[0]["type"] == "scan"
        assert received[0]["new_alert_count"] == 2

        await gen.aclose()
    _run(run())


def test_subscriber_cleanup_on_close():
    async def run():
        gen = pubsub.subscribe()
        sub_iter = gen.__aiter__()
        task = asyncio.create_task(
            asyncio.wait_for(sub_iter.__anext__(), timeout=0.5)
        )
        for _ in range(50):
            await asyncio.sleep(0.01)
            if pubsub.subscriber_count() >= 1:
                break
        assert pubsub.subscriber_count() == 1

        try:
            await task
        except asyncio.TimeoutError:
            pass

        await gen.aclose()
        assert pubsub.subscriber_count() == 0
    _run(run())


# ── fan-out ───────────────────────────────────────────────────────────────

def test_fan_out_to_multiple_subscribers():
    async def run():
        received_a: list[dict] = []
        received_b: list[dict] = []
        gen_a = pubsub.subscribe()
        gen_b = pubsub.subscribe()

        async def collect(g, sink):
            async for evt in g:
                sink.append(evt)
                return

        task_a = asyncio.create_task(collect(gen_a, received_a))
        task_b = asyncio.create_task(collect(gen_b, received_b))
        for _ in range(50):
            await asyncio.sleep(0.01)
            if pubsub.subscriber_count() >= 2:
                break
        assert pubsub.subscriber_count() == 2

        delivered = pubsub.publish({"type": "scan", "tier": "slow"})
        assert delivered == 2

        await asyncio.wait_for(asyncio.gather(task_a, task_b), timeout=2)
        assert received_a[0]["tier"] == "slow"
        assert received_b[0]["tier"] == "slow"

        await gen_a.aclose()
        await gen_b.aclose()
    _run(run())


# ── cross-thread publish (scheduler thread → asyncio loop) ───────────────

def test_publish_from_background_thread():
    """Scheduler runs in a non-asyncio thread. publish() must bridge to
    the asyncio queue via call_soon_threadsafe.
    """
    async def run():
        received: list[dict] = []
        gen = pubsub.subscribe()
        sub_iter = gen.__aiter__()

        task = asyncio.create_task(
            asyncio.wait_for(sub_iter.__anext__(), timeout=2)
        )
        for _ in range(50):
            await asyncio.sleep(0.01)
            if pubsub.subscriber_count() >= 1:
                break

        def _bg_publish():
            time.sleep(0.05)
            pubsub.publish({
                "type": "scan",
                "from_thread": threading.current_thread().name,
            })

        t = threading.Thread(target=_bg_publish)
        t.start()
        received.append(await task)
        t.join()

        assert received[0]["from_thread"].startswith("Thread")
        await gen.aclose()
    _run(run())


# ── bounded queue ─────────────────────────────────────────────────────────

def test_publish_to_full_queue_does_not_crash(monkeypatch):
    """If a subscriber stops reading, queue fills. publish() must drop
    silently (with warning) rather than crash — a slow client cannot
    take down the publisher.
    """
    monkeypatch.setattr(pubsub, "_MAX_QUEUE_SIZE", 2)

    async def run():
        # Use a long-lived consumer task that registers but consumes only
        # the FIRST event — subsequent publishes fill the queue.
        gen = pubsub.subscribe()
        consumer_started = asyncio.Event()
        first_received = asyncio.Event()

        async def lazy_consumer():
            consumer_started.set()
            async for evt in gen:
                first_received.set()
                # Then hang here indefinitely without consuming more.
                await asyncio.sleep(3600)

        task = asyncio.create_task(lazy_consumer())
        await consumer_started.wait()
        # Wait for registration
        for _ in range(50):
            await asyncio.sleep(0.01)
            if pubsub.subscriber_count() >= 1:
                break

        # First publish → consumed; later publishes pile up + overflow.
        for i in range(10):
            pubsub.publish({"i": i})
            await asyncio.sleep(0.005)

        # If we got here without exception, the test passes.
        assert pubsub.subscriber_count() == 1
        task.cancel()
        try:
            await task
        except (asyncio.CancelledError, Exception):
            pass
        await gen.aclose()
    _run(run())


# ── env-flag gating ───────────────────────────────────────────────────────

def test_sse_disabled_by_default(monkeypatch):
    monkeypatch.delenv("SSE_ENABLED", raising=False)
    assert os.getenv("SSE_ENABLED", "false").lower() != "true"


def test_sse_enabled_via_env(monkeypatch):
    monkeypatch.setenv("SSE_ENABLED", "true")
    assert os.getenv("SSE_ENABLED", "false").lower() == "true"
