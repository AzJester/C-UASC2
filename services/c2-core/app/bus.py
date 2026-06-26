"""Pub/sub bus wrapper (NATS).

Thin async wrapper over NATS that is deliberately *resilient to broker loss*: if the
bus is unreachable the C2 node keeps serving REST and degrades gracefully rather
than crashing (DDIL behavior, docs/06). Publishing while disconnected returns False
so callers can audit the gap and reconcile on reconnect.

The canonical schemas are transport-independent (ADR-0001); this wrapper could be
swapped for a DDS/MQTT binding without touching the data model.
"""
from __future__ import annotations

import asyncio
import logging
from typing import Awaitable, Callable, Optional

log = logging.getLogger("c2.bus")

try:  # nats-py is optional so the module imports cleanly in test environments.
    import nats  # type: ignore

    _HAVE_NATS = True
except Exception:  # pragma: no cover - exercised only when dependency absent
    nats = None  # type: ignore
    _HAVE_NATS = False


class Bus:
    def __init__(self, url: str) -> None:
        self._url = url
        self._nc = None
        self._connected = False

    @property
    def connected(self) -> bool:
        return self._connected

    async def connect(self, connect_timeout: float = 2.0) -> bool:
        """Best-effort, fast-failing connect. Never raises; returns connection state.

        The initial attempt is bounded so a missing broker degrades the node in
        seconds rather than blocking startup (DDIL). Once connected, nats-py handles
        reconnection on its own (allow_reconnect, infinite attempts).
        """
        if not _HAVE_NATS:
            log.warning("nats-py not installed; bus running in disconnected mode")
            return False
        try:
            self._nc = await asyncio.wait_for(
                nats.connect(
                    self._url,
                    connect_timeout=connect_timeout,
                    allow_reconnect=True,
                    max_reconnect_attempts=-1,  # reconnect forever once established (DDIL)
                    reconnect_time_wait=2,
                ),
                timeout=connect_timeout + 1.0,
            )
            self._connected = True
            log.info("connected to bus at %s", self._url)
            return True
        except Exception as exc:  # noqa: BLE001
            log.warning("bus unreachable at %s; continuing degraded: %s", self._url, exc)
            return False

    async def publish(self, subject: str, data: bytes) -> bool:
        """Publish; return False (not raise) when the bus is unavailable."""
        if not self._connected or self._nc is None:
            log.warning("publish to %s dropped: bus disconnected", subject)
            return False
        try:
            await self._nc.publish(subject, data)
            return True
        except Exception as exc:  # noqa: BLE001
            log.warning("publish to %s failed: %s", subject, exc)
            self._connected = False
            return False

    async def subscribe(
        self, subject: str, handler: Callable[[str, bytes], Awaitable[None]]
    ) -> None:
        if not self._connected or self._nc is None:
            log.warning("subscribe to %s skipped: bus disconnected", subject)
            return

        async def _cb(msg) -> None:  # type: ignore[no-untyped-def]
            try:
                await handler(msg.subject, msg.data)
            except Exception as exc:  # noqa: BLE001
                log.exception("handler error on %s: %s", msg.subject, exc)

        await self._nc.subscribe(subject, cb=_cb)
        log.info("subscribed to %s", subject)

    async def close(self) -> None:
        if self._nc is not None:
            try:
                await self._nc.drain()
            except Exception:  # noqa: BLE001
                pass
        self._connected = False
