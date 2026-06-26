"""Deterministic DDIL behavior for the bus wrapper (docs/06).

The C2 node must degrade, not crash, when the broker is unreachable. These tests
point the bus at a dead port so the behavior is independent of any broker that may
be running in the environment.
"""
import asyncio

from app.bus import Bus

DEAD_URL = "nats://127.0.0.1:14222"  # nothing listening here


def test_connect_to_dead_broker_returns_false_fast():
    bus = Bus(DEAD_URL)
    connected = asyncio.run(bus.connect(connect_timeout=1.0))
    assert connected is False
    assert bus.connected is False


def test_publish_while_disconnected_returns_false_not_raises():
    bus = Bus(DEAD_URL)
    # No connect() call: publishing must be a safe no-op that reports failure.
    result = asyncio.run(bus.publish("cuas.track.fused.region-1", b"{}"))
    assert result is False


def test_subscribe_while_disconnected_is_noop():
    bus = Bus(DEAD_URL)

    async def handler(subject, data):  # pragma: no cover - never invoked
        raise AssertionError("handler should not run when disconnected")

    # Should not raise even though there is no connection.
    asyncio.run(bus.subscribe("cuas.track.fused.>", handler))
