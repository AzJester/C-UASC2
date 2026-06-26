"""Edge materiel simulator.

Simulates an edge node's materiel against the government-owned bus interface so the
end-to-end flow is demonstrable without real hardware:

  * SENSOR  publishes fused tracks to cuas.track.fused.<region>, and on receiving a
            CUE/DWELL SensorTask raises that track's quality (Imperative 4: tasking
            improves track quality on demand).
  * EFFECTOR subscribes to cuas.engagement.order.<id>, performs a (simulated)
            hardware-interlock check, then reports ACCEPTED -> ACTIVE -> COMPLETE on
            cuas.engagement.status.<engagementId> (Imperative 5).

This is a reference fixture. The "interlock" here is a stand-in for real
weapons-safety hardware (docs/05 §4): the last gate lives in the effector.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
from datetime import datetime, timezone
from uuid import uuid4

import nats

logging.basicConfig(level=logging.INFO, format="%(asctime)s sensor-sim %(levelname)s %(message)s")
log = logging.getLogger("sim")

NATS_URL = os.environ.get("NATS_URL", "nats://localhost:4222")
REGION = os.environ.get("SIM_REGION", "region-1")
SENSOR_ID = os.environ.get("SIM_SENSOR_ID", "SEN-RAD-01")
EFFECTOR_ID = os.environ.get("SIM_EFFECTOR_ID", "EFF-EW-01")

# Two tracks: a hostile multirotor (detectable but low TQ until tasked) and a
# friendly that must never be engageable regardless of TQ.
TRACKS: dict[str, dict] = {
    "TRK-1001": {
        "trackId": "TRK-1001",
        "kinematics": {
            "position": {"lat": 34.2000, "lon": -118.2000, "altMeters": 120.0, "frame": "WGS84"},
            "velocity": {"speedMps": 14.0, "courseDeg": 270.0, "verticalRateMps": 0.0},
        },
        "covariance": {"horizontalMeters": 40.0, "verticalMeters": 25.0},
        "trackQuality": 6,
        "identity": "HOSTILE",
        "classificationType": "MULTIROTOR",
        "contributingSensors": [SENSOR_ID],
        "timeToLiveSeconds": 30.0,
    },
    "TRK-2001": {
        "trackId": "TRK-2001",
        "kinematics": {
            "position": {"lat": 34.2100, "lon": -118.2100, "altMeters": 200.0, "frame": "WGS84"},
            "velocity": {"speedMps": 30.0, "courseDeg": 90.0, "verticalRateMps": 0.0},
        },
        "covariance": {"horizontalMeters": 15.0, "verticalMeters": 10.0},
        "trackQuality": 13,
        "identity": "FRIEND",
        "classificationType": "ROTARY",
        "contributingSensors": [SENSOR_ID],
        "timeToLiveSeconds": 30.0,
    },
}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _envelope(message_type: str, component: str, node_id: str, payload: dict) -> bytes:
    return json.dumps(
        {
            "messageId": str(uuid4()),
            "schemaVersion": "1.0.0",
            "messageType": message_type,
            "source": {"nodeId": node_id, "componentType": component},
            "classification": "UNCLASSIFIED",
            "timeCreated": _now(),
            "payload": payload,
        }
    ).encode()


async def _publish_tracks(nc) -> None:
    """Publish the current tracks once per second."""
    while True:
        for track in TRACKS.values():
            track["timeObserved"] = _now()
            await nc.publish(
                f"cuas.track.fused.{REGION}",
                _envelope("Track", "fusion", SENSOR_ID, track),
            )
        await asyncio.sleep(1.0)


async def _handle_task(msg) -> None:
    """On CUE/DWELL, raise the referenced track's quality (tasking -> better TQ)."""
    try:
        env = json.loads(msg.data)
        task = env.get("payload", env)
    except Exception as exc:  # noqa: BLE001
        log.warning("bad task message: %s", exc)
        return
    track_id = task.get("trackId")
    task_type = task.get("taskType")
    if task_type in ("CUE", "DWELL", "SLEW") and track_id in TRACKS:
        before = TRACKS[track_id]["trackQuality"]
        # Cross-cue/dwell tightens the estimate -> higher TQ, capped at 15.
        TRACKS[track_id]["trackQuality"] = min(15, before + 6)
        TRACKS[track_id]["covariance"] = {"horizontalMeters": 8.0, "verticalMeters": 5.0}
        if SENSOR_ID not in TRACKS[track_id]["contributingSensors"]:
            TRACKS[track_id]["contributingSensors"].append(SENSOR_ID)
        log.info(
            "%s on %s: TQ %d -> %d", task_type, track_id, before, TRACKS[track_id]["trackQuality"]
        )


async def _handle_order(nc, msg) -> None:
    """Simulated effector: interlock check, then ACCEPTED -> ACTIVE -> COMPLETE."""
    try:
        env = json.loads(msg.data)
        order = env.get("payload", env)
    except Exception as exc:  # noqa: BLE001
        log.warning("bad order message: %s", exc)
        return

    engagement_id = order.get("engagementId", "UNKNOWN")
    track_id = order.get("trackId")

    async def report(state: str, reason: str = "OK", detail: str = "") -> None:
        payload = {
            "engagementId": engagement_id,
            "effectorId": EFFECTOR_ID,
            "trackId": track_id,
            "state": state,
            "reasonCode": reason,
            "detail": detail,
            "timeReported": _now(),
        }
        await nc.publish(
            f"cuas.engagement.status.{engagement_id}",
            _envelope("EngagementStatus", "effector", EFFECTOR_ID, payload),
        )
        log.info("engagement %s -> %s (%s)", engagement_id, state, reason)

    # Gate 4: hardware interlock. The order must carry an authority token; absent
    # one, a real effector refuses to fire. This is the last gate, below the network.
    if not order.get("authorityToken"):
        await report("FAILED", "INTERLOCK_BLOCKED", "no authority token")
        return

    await report("ACCEPTED")
    await asyncio.sleep(0.4)
    await report("ACTIVE")
    await asyncio.sleep(0.8)
    await report("COMPLETE")


async def main() -> None:
    nc = await nats.connect(NATS_URL, max_reconnect_attempts=-1, reconnect_time_wait=2)
    log.info("connected to %s; sensor=%s effector=%s region=%s", NATS_URL, SENSOR_ID, EFFECTOR_ID, REGION)

    async def _order_cb(msg) -> None:
        # nats-py requires a coroutine function as the callback (not a lambda that
        # merely returns one), so bind nc through a closure.
        await _handle_order(nc, msg)

    await nc.subscribe(f"cuas.sensor.task.{SENSOR_ID}", cb=_handle_task)
    await nc.subscribe(f"cuas.engagement.order.{EFFECTOR_ID}", cb=_order_cb)

    await _publish_tracks(nc)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
