"""Deterministic DDIL behavior for the bus wrapper (docs/06).

The C2 node must degrade, not crash, when the broker is unreachable. These tests
point the bus at a dead port so the behavior is independent of any broker that may
be running in the environment.
"""
import asyncio

import app.bus as bus_module
from app.bus import Bus, PublishOutcome

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


def test_publish_reports_success_only_after_broker_flush():
    class FakeNats:
        def __init__(self):
            self.published = False
            self.flushed = False

        async def publish(self, subject, data):
            self.published = subject == "cuas.test" and data == b"payload"

        async def flush(self, timeout):
            assert timeout == 1.0
            self.flushed = True

    bus = Bus(DEAD_URL)
    bus._nc = FakeNats()  # transport seam for deterministic acknowledgement test
    bus._connected = True
    assert asyncio.run(bus.publish("cuas.test", b"payload")) is True
    assert bus._nc.published is True
    assert bus._nc.flushed is True


def test_publish_flush_failure_marks_bus_disconnected():
    class FailedFlushNats:
        async def publish(self, subject, data):
            return None

        async def flush(self, timeout):
            raise TimeoutError("no broker acknowledgement")

    bus = Bus(DEAD_URL)
    bus._nc = FailedFlushNats()
    bus._connected = True
    assert asyncio.run(bus.publish("cuas.test", b"payload")) is False
    assert bus.connected is False


def test_command_publish_distinguishes_not_sent_from_ambiguous_delivery():
    disconnected = Bus(DEAD_URL)
    assert (
        asyncio.run(disconnected.publish_outcome("cuas.command", b"payload"))
        is PublishOutcome.NOT_SENT
    )

    class AmbiguousNats:
        async def publish(self, subject, data):
            return None

        async def flush(self, timeout):
            raise TimeoutError("bytes may already be at a subscriber")

    ambiguous = Bus(DEAD_URL)
    ambiguous._nc = AmbiguousNats()
    ambiguous._connected = True
    assert (
        asyncio.run(ambiguous.publish_outcome("cuas.command", b"payload"))
        is PublishOutcome.DELIVERY_UNKNOWN
    )


def test_nats_callbacks_keep_connection_state_truthful(monkeypatch):
    captured = {}

    class FakeConnection:
        is_connected = True
        is_reconnecting = False

    class FakeNatsModule:
        async def connect(self, url, **kwargs):
            captured.update(kwargs)
            return FakeConnection()

    monkeypatch.setattr(bus_module, "_HAVE_NATS", True)
    monkeypatch.setattr(bus_module, "nats", FakeNatsModule())
    bus = Bus("nats://example:4222")
    assert asyncio.run(bus.connect()) is True
    assert bus.connected is True
    asyncio.run(captured["disconnected_cb"]())
    assert bus.connected is False
    asyncio.run(captured["reconnected_cb"]())
    assert bus.connected is True
    asyncio.run(captured["closed_cb"]())
    assert bus.connected is False


def test_duplicate_subscription_is_not_registered_twice():
    class FakeNats:
        def __init__(self):
            self.calls = 0

        async def subscribe(self, subject, cb):
            self.calls += 1

    async def handler(subject, data):
        return None

    bus = Bus(DEAD_URL)
    bus._nc = FakeNats()
    bus._connected = True
    asyncio.run(bus.subscribe("cuas.test", handler))
    asyncio.run(bus.subscribe("cuas.test", handler))
    assert bus._nc.calls == 1
