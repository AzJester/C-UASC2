"""Edge materiel + scenario simulator.

Drives the live demo against the government-owned bus interface so the whole flow
is exercisable without real hardware:

  * SCENARIO  flies moving UAS tracks (singles + a swarm) inbound toward a defended
              asset, fused from multiple sensors, plus a friendly that must never be
              engageable. Track quality rises with sensor coverage.
  * SENSOR    on a CUE/DWELL/SLEW SensorTask, raises the referenced track's quality
              (Imperative 4: remote tasking improves the picture on demand).
  * EFFECTOR  on an EngagementOrder, performs a (simulated) hardware-interlock check,
              reports ACCEPTED -> ACTIVE -> COMPLETE, and removes the defeated track
              (Imperative 5).

Reference fixture only. The "interlock" stands in for real weapons-safety hardware
(docs/05 §4): the last gate lives in the effector, below the network.
"""
from __future__ import annotations

import asyncio
import json
import logging
import math
import os
import random
from datetime import datetime, timezone
from uuid import uuid4

import nats

logging.basicConfig(level=logging.INFO, format="%(asctime)s sensor-sim %(levelname)s %(message)s")
log = logging.getLogger("sim")

NATS_URL = os.environ.get("NATS_URL", "nats://localhost:4222")
REGION = os.environ.get("SIM_REGION", "region-1")
SENSOR_ID = os.environ.get("SIM_SENSOR_ID", "SEN-RAD-01")
EFFECTOR_ID = os.environ.get("SIM_EFFECTOR_ID", "EFF-EW-01")
TICK = 0.5            # seconds between scenario updates
TTL = 6.0            # track time-to-live; defeated tracks age out this fast

# Defended asset; tracks are positioned in a local meters frame around it and
# converted to WGS84 on publish so the web COP can render them (matches the UI).
ASSET = {"lat": 34.20, "lon": -118.20}
M_PER_DEG_LAT = 111320.0
M_PER_DEG_LON = 111320.0 * math.cos(math.radians(ASSET["lat"]))

# Sensor coverage (local meters), mirrors the web COP layout (joint laydown).
SENSORS = [
    {"id": "SEN-RAD-01", "x": 0, "y": -150, "range": 6000},
    {"id": "SEN-RF-02", "x": 1700, "y": 1300, "range": 5200},
    {"id": "SEN-EO-03", "x": -1800, "y": 1000, "range": 3600},
    {"id": "SEN-SHIP-04", "x": 4800, "y": -1200, "range": 6500},
    {"id": "SEN-MADIS-05", "x": -200, "y": -2600, "range": 4500},
]

TYPES = ["MULTIROTOR", "UAS_GROUP_1", "UAS_GROUP_2", "FIXED_WING"]
BLUE_IDS = {"FRIEND", "ASSUMED_FRIEND", "NEUTRAL"}   # friendly force (Blue) picture


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _latlon(x: float, y: float, alt: float = 120.0) -> dict:
    return {
        "lat": ASSET["lat"] + y / M_PER_DEG_LAT,
        "lon": ASSET["lon"] + x / M_PER_DEG_LON,
        "altMeters": alt,
        "frame": "WGS84",
    }


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


class Track:
    _seq = 1000

    def __init__(self, identity: str, x: float, y: float, speed: float, tq: float, cls: str,
                 ox: float = 0.0, oy: float = 0.0, orad: float = 700.0, ospeed: float = 0.25,
                 platform: str = None, service: str = None, alt: float = 120.0):
        Track._seq += 1
        self.id = f"TRK-{Track._seq}"
        self.identity = identity
        self.cls = cls
        self.platform = platform
        self.service = service
        self.altMeters = alt
        self.x, self.y = x, y
        self.speed = speed
        self.heading = math.atan2(-y, -x)  # toward asset
        self.tq = tq
        self.contrib: list[str] = []
        self.ox, self.oy, self.orad, self.ospeed = ox, oy, orad, ospeed
        self.orbit = 0.0
        self.alive = True

    def is_blue(self) -> bool:
        return self.identity in BLUE_IDS

    def step(self, dt: float) -> None:
        if self.is_blue():
            # Blue force air: loiter on a patrol box, never dive the asset.
            self.orbit += dt * self.ospeed
            self.x = self.ox + math.cos(self.orbit) * self.orad
            self.y = self.oy + math.sin(self.orbit) * self.orad
        else:
            desired = math.atan2(-self.y, -self.x)
            err = ((desired - self.heading + math.pi * 3) % (math.tau)) - math.pi
            self.heading += max(-0.5 * dt, min(0.5 * dt, err))
            self.x += math.cos(self.heading) * self.speed * dt
            self.y += math.sin(self.heading) * self.speed * dt
        # fusion: track quality from sensor coverage
        contrib = [s["id"] for s in SENSORS if math.hypot(self.x - s["x"], self.y - s["y"]) <= s["range"]]
        self.contrib = contrib
        if not self.is_blue():
            target = min(11 if len(contrib) >= 2 else 7, 3 + len(contrib) * 3)
            if self.tq < target:
                self.tq = min(target, self.tq + dt * 1.2)
            elif not contrib and self.tq > 2:
                self.tq -= dt * 0.5

    def range_to_asset(self) -> float:
        return math.hypot(self.x, self.y)

    def payload(self) -> dict:
        p = {
            "trackId": self.id,
            "kinematics": {
                "position": _latlon(self.x, self.y, self.altMeters),
                "velocity": {
                    "speedMps": round(self.speed, 1),
                    "courseDeg": round((math.degrees(self.heading) + 360) % 360, 1),
                    "verticalRateMps": 0.0,
                },
            },
            "covariance": {"horizontalMeters": 40.0 if self.tq < 9 else 10.0, "verticalMeters": 25.0},
            "trackQuality": int(self.tq),
            "identity": self.identity,
            "classificationType": self.cls,
            "contributingSensors": self.contrib or [SENSOR_ID],
            "timeObserved": _now(),
            "timeToLiveSeconds": TTL,
        }
        if self.platform:
            p["platform"] = self.platform
        if self.service:
            p["service"] = self.service
        return p


class Scenario:
    BLUE_ROSTER = [
        # identity, ox, oy, orad, ospeed, cls, speed, platform, service, alt
        ("FRIEND", 3300, -900, 600, 0.28, "ROTARY", 70, "MH-60R", "USN", 300),
        ("FRIEND", 1900, 2500, 1500, 0.20, "FIXED_WING", 180, "F/A-18E", "USN", 6000),
        ("FRIEND", -2600, -1500, 700, 0.22, "ROTARY", 120, "MV-22B", "USMC", 900),
        ("FRIEND", -1200, 1500, 500, 0.30, "ROTARY", 80, "AH-1Z", "USMC", 150),
        ("ASSUMED_FRIEND", -2700, 2000, 900, 0.18, "UAS_GROUP_3", 40, "RQ-21A", "USMC", 1500),
        ("FRIEND", 300, 2800, 600, 0.30, "UAS_GROUP_3", 45, "RQ-7B", "USA", 2400),
        ("FRIEND", -2300, 300, 550, 0.26, "ROTARY", 75, "UH-60M", "USA", 250),
        ("FRIEND", 2700, -2500, 1300, 0.14, "FIXED_WING", 90, "MQ-9", "USAF", 7600),
    ]

    def __init__(self):
        self.tracks: dict[str, Track] = {}
        for r in self.BLUE_ROSTER:
            self._spawn_blue(*r)
        self._spawn_hostile(bearing=-0.6, r=4200, tq=5, cls="MULTIROTOR")
        self._spawn_wave(6)
        self._wave_timer = 0.0

    def _spawn_blue(self, identity, ox, oy, orad, ospeed, cls, speed, platform=None, service="USA", alt=300):
        t = Track(identity, ox + orad, oy, speed, 13, cls, ox=ox, oy=oy, orad=orad, ospeed=ospeed,
                  platform=platform, service=service, alt=alt)
        self.tracks[t.id] = t

    def _spawn_hostile(self, bearing=None, r=None, tq=4, cls=None, speed=None):
        bearing = bearing if bearing is not None else random.uniform(0, math.tau)
        r = r if r is not None else random.uniform(4600, 5200)
        t = Track(
            "HOSTILE",
            math.cos(bearing) * r,
            math.sin(bearing) * r,
            speed if speed is not None else random.uniform(26, 38),
            tq,
            cls or random.choice(TYPES),
            alt=random.uniform(60, 400),
        )
        self.tracks[t.id] = t
        return t

    def _spawn_wave(self, n: int):
        b = random.uniform(0, math.tau)
        for i in range(n):
            self._spawn_hostile(bearing=b + (i - n / 2) * 0.09, r=4900 + (i % 3) * 160, tq=4, cls="UAS_GROUP_1")
        log.info("SWARM inbound: %d contacts", n)

    def tick(self, dt: float):
        self._wave_timer += dt
        # periodic re-seed so the live demo always has activity
        hostiles = [t for t in self.tracks.values() if t.identity == "HOSTILE"]
        if self._wave_timer > 30 and len(hostiles) < 10:
            self._wave_timer = 0.0
            self._spawn_wave(random.randint(2, 4))
        for t in list(self.tracks.values()):
            t.step(dt)
            if not t.is_blue() and t.range_to_asset() < 250:
                log.info("LEAKER: %s reached defended asset", t.id)
                self.tracks.pop(t.id, None)

    def task(self, track_id: str) -> tuple[int, int] | None:
        t = self.tracks.get(track_id)
        if not t:
            return None
        before = int(t.tq)
        t.tq = min(15, t.tq + 6)
        return before, int(t.tq)

    def neutralize(self, track_id: str):
        self.tracks.pop(track_id, None)


SCN = Scenario()


async def _publish_tracks(nc):
    while True:
        for t in SCN.tracks.values():
            await nc.publish(f"cuas.track.fused.{REGION}", _envelope("Track", "fusion", SENSOR_ID, t.payload()))
        await asyncio.sleep(TICK)


async def _run_scenario():
    while True:
        SCN.tick(TICK)
        await asyncio.sleep(TICK)


async def _handle_task(msg):
    try:
        task = json.loads(msg.data).get("payload", {})
    except Exception as exc:  # noqa: BLE001
        log.warning("bad task message: %s", exc)
        return
    if task.get("taskType") in ("CUE", "DWELL", "SLEW"):
        res = SCN.task(task.get("trackId"))
        if res:
            log.info("%s on %s: TQ %d -> %d", task.get("taskType"), task.get("trackId"), res[0], res[1])


async def _handle_order(nc, msg):
    try:
        order = json.loads(msg.data).get("payload", {})
    except Exception as exc:  # noqa: BLE001
        log.warning("bad order message: %s", exc)
        return
    engagement_id = order.get("engagementId", "UNKNOWN")
    track_id = order.get("trackId")
    eff_id = order.get("effectorId", EFFECTOR_ID)

    async def report(state, reason="OK", detail=""):
        payload = {
            "engagementId": engagement_id,
            "effectorId": eff_id,
            "trackId": track_id,
            "state": state,
            "reasonCode": reason,
            "detail": detail,
            "timeReported": _now(),
        }
        await nc.publish(f"cuas.engagement.status.{engagement_id}", _envelope("EngagementStatus", "effector", eff_id, payload))
        log.info("engagement %s -> %s (%s)", engagement_id, state, reason)

    # Gate 4: hardware interlock. No authority token => a real effector refuses.
    if not order.get("authorityToken"):
        await report("FAILED", "INTERLOCK_BLOCKED", "no authority token")
        return
    await report("ACCEPTED")
    await asyncio.sleep(0.4)
    await report("ACTIVE")
    await asyncio.sleep(0.9)
    await report("COMPLETE")
    SCN.neutralize(track_id)   # battle damage: drop the defeated track from the COP


async def main():
    nc = await nats.connect(NATS_URL, max_reconnect_attempts=-1, reconnect_time_wait=2)
    log.info("connected to %s; sensor=%s effector=%s region=%s", NATS_URL, SENSOR_ID, EFFECTOR_ID, REGION)

    async def _order_cb(msg):
        await _handle_order(nc, msg)

    # Subscribe to all sensor tasks / engagement orders so the simulator responds
    # for any sensor or effector the C2 node tasks or pairs (distributed pairing).
    await nc.subscribe("cuas.sensor.task.>", cb=_handle_task)
    await nc.subscribe("cuas.engagement.order.>", cb=_order_cb)

    await asyncio.gather(_publish_tracks(nc), _run_scenario())


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
