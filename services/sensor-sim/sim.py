"""NATS adapter for the deterministic edge-materiel reference simulator.

The simulation is intentionally notional and non-fielded.  It exercises the
government-owned message contracts, independently verifies scoped authority,
models finite inventory, and reports an explicit engagement/BDA lifecycle.
"""
from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import logging
import math
import os
import time
import urllib.request
from datetime import datetime, timezone
from uuid import uuid4

import nats

from simulation import (
    DEFAULT_SENSORS,
    NOTIONAL_MODEL_NOTICE,
    AssetConfig,
    AuthorityTokenVerifier,
    BusEnvelopeVerifier,
    EffectorModel,
    EngagementSimulator,
    Scenario,
    parse_utc,
)


logging.basicConfig(level=logging.INFO, format="%(asctime)s sensor-sim %(levelname)s %(message)s")
log = logging.getLogger("sim")

NATS_URL = os.environ.get("NATS_URL", "nats://localhost:4222")
REGION = os.environ.get("SIM_REGION", "region-1")
SENSOR_ID = os.environ.get("SIM_SENSOR_ID", "SEN-RAD-01")
EFFECTOR_ID = os.environ.get("SIM_EFFECTOR_ID", "EFF-EW-01")
TICK_SECONDS = float(os.environ.get("SIM_TICK_SECONDS", "0.5"))
TRACK_TTL_SECONDS = float(os.environ.get("SIM_TRACK_TTL_SECONDS", "6.0"))
SEED = int(os.environ.get("SIM_SEED", "4242"))
TIME_SCALE = float(os.environ.get("SIM_EFFECT_TIME_SCALE", "1.0"))
AUTHORITY_SIGNING_KEY = os.environ.get(
    "C2_AUTHORITY_SIGNING_KEY", "cuas-local-reference-key-not-for-production"
)
BUS_SIGNING_KEY = os.environ.get("CUAS_BUS_SIGNING_KEY", AUTHORITY_SIGNING_KEY)


def _load_authoritative_asset() -> tuple[AssetConfig, str]:
    fallback = AssetConfig.from_env(os.environ)
    fallback_node = os.environ.get("SIM_AUTHORITY_ISSUER", "C2-NODE-01")
    url = os.environ.get("C2_SCENARIO_URL")
    if not url:
        return fallback, fallback_node
    attempts = max(1, int(os.environ.get("C2_SCENARIO_ATTEMPTS", "30")))
    for attempt in range(1, attempts + 1):
        try:
            with urllib.request.urlopen(url, timeout=2) as response:  # noqa: S310 - configured local C2 URL
                scenario = json.load(response)
            area = scenario["areaOfOperations"]
            center = area["center"]
            discovered = AssetConfig(
                lat=float(center["lat"]),
                lon=float(center["lon"]),
                label=str(area.get("label") or fallback.label),
            )
            if not -90 <= discovered.lat <= 90 or not -180 <= discovered.lon <= 180:
                raise ValueError("authoritative scenario returned invalid coordinates")
            discovered_node = str(scenario["authoritativeNodeId"])
            if not discovered_node:
                raise ValueError("authoritative scenario omitted authoritativeNodeId")
            log.info(
                "discovered authoritative scenario AO and node %s from %s",
                discovered_node,
                url,
            )
            return discovered, discovered_node
        except Exception as exc:  # noqa: BLE001
            if attempt == attempts:
                raise RuntimeError(
                    f"unable to discover authoritative scenario from {url}"
                ) from exc
            time.sleep(1)
    return fallback, fallback_node  # pragma: no cover


ASSET, DISCOVERED_AUTHORITY_ISSUER = _load_authoritative_asset()
AUTHORITY_ISSUER = os.environ.get(
    "SIM_AUTHORITY_ISSUER", DISCOVERED_AUTHORITY_ISSUER
)
START_TIME = (
    parse_utc(os.environ["SIM_START_TIME"])
    if os.environ.get("SIM_START_TIME")
    else datetime.now(timezone.utc)
)

if TICK_SECONDS <= 0 or TRACK_TTL_SECONDS <= 0:
    raise RuntimeError("SIM_TICK_SECONDS and SIM_TRACK_TTL_SECONDS must be positive")

SCENARIO = Scenario(
    seed=SEED,
    start_time=START_TIME,
    asset=ASSET,
    ttl_seconds=TRACK_TTL_SECONDS,
    enable_organic_air_defense=os.environ.get("SIM_ORGANIC_AIR_DEFENSE", "false").lower() == "true",
)
EFFECTOR = EffectorModel(
    effector_id=EFFECTOR_ID,
    effector_type=os.environ.get("SIM_EFFECTOR_TYPE", "EW_JAMMER"),
    capacity=int(os.environ.get("SIM_EFFECTOR_CAPACITY", "12")),
    remaining=int(
        os.environ.get("SIM_EFFECTOR_REMAINING", os.environ.get("SIM_EFFECTOR_CAPACITY", "12"))
    ),
)
TOKEN_VERIFIER = AuthorityTokenVerifier(AUTHORITY_SIGNING_KEY, issuer=AUTHORITY_ISSUER)
BUS_VERIFIER = BusEnvelopeVerifier(BUS_SIGNING_KEY, AUTHORITY_ISSUER)
ENGAGEMENTS = EngagementSimulator(
    EFFECTOR,
    TOKEN_VERIFIER,
    seed=SEED,
    time_scale=TIME_SCALE,
    max_track_age_seconds=TRACK_TTL_SECONDS,
)


def _envelope(
    message_type: str,
    component: str,
    node_id: str,
    payload: dict,
    *,
    time_created: datetime | None = None,
) -> bytes:
    raw = {
        "messageId": str(uuid4()),
        "schemaVersion": "1.0.0",
        "messageType": message_type,
        "source": {"nodeId": node_id, "componentType": component},
        "classification": "UNCLASSIFIED",
        "timeCreated": (time_created or SCENARIO.clock.now).isoformat(),
        "payload": payload,
    }
    canonical = json.dumps(
        raw,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    ).encode()
    raw["signature"] = (
        "hmac-sha256:"
        + hmac.new(BUS_SIGNING_KEY.encode(), canonical, hashlib.sha256).hexdigest()
    )
    return json.dumps(raw, separators=(",", ":")).encode()


def _sensor_status(sensor) -> dict:
    meters_per_degree_latitude = 111_320.0
    meters_per_degree_longitude = 111_320.0 * math.cos(math.radians(ASSET.lat))
    search_volume = SCENARIO._search_volumes.get(sensor.sensor_id)  # reference status view
    mode = "SEARCH/TRACK"
    if search_volume is not None:
        mode = (
            f"SEARCH {search_volume['centerBearingDeg']:.0f}deg/"
            f"{search_volume['widthDeg']:.0f}deg"
        )
    return {
        "sensorId": sensor.sensor_id,
        "sensorType": "EO_IR" if sensor.modality == "EO_IR" else sensor.modality,
        "vendor": "REFERENCE-SIM",
        "readiness": "READY",
        "mode": mode,
        "taskable": True,
        "coverage": {
            "center": {
                "lat": ASSET.lat + sensor.y / meters_per_degree_latitude,
                "lon": ASSET.lon + sensor.x / meters_per_degree_longitude,
            },
            "rangeMeters": sensor.range_meters,
            "minAltMeters": 0,
            "maxAltMeters": 2000,
        },
        "softwareVersion": "reference-sim-2",
        "timeReported": SCENARIO.clock.now.isoformat(),
    }


async def _publish_tracks(nc) -> None:
    while True:
        for payload in SCENARIO.track_payloads():
            await nc.publish(
                f"cuas.track.fused.{REGION}",
                _envelope("Track", "fusion", SENSOR_ID, payload),
            )
        await asyncio.sleep(TICK_SECONDS)


async def _publish_material_status(nc) -> None:
    while True:
        for sensor in DEFAULT_SENSORS:
            await nc.publish(
                f"cuas.sensor.status.{sensor.sensor_id}",
                _envelope("SensorStatus", "sensor", sensor.sensor_id, _sensor_status(sensor)),
            )
        await nc.publish(
            f"cuas.effector.status.{EFFECTOR_ID}",
            _envelope(
                "EffectorStatus",
                "effector",
                EFFECTOR_ID,
                EFFECTOR.status_payload(ASSET, SCENARIO.clock.now),
            ),
        )
        await asyncio.sleep(2.0)


async def _run_scenario() -> None:
    observed_leakers = 0
    while True:
        SCENARIO.tick(TICK_SECONDS)
        if len(SCENARIO.leakers) > observed_leakers:
            for track_id in SCENARIO.leakers[observed_leakers:]:
                log.info("LEAKER: %s reached the defended asset", track_id)
            observed_leakers = len(SCENARIO.leakers)
        await asyncio.sleep(TICK_SECONDS)


async def _handle_task(msg) -> None:
    try:
        task = BUS_VERIFIER.verify(
            msg.data,
            expected_message_type="SensorTask",
            subject=msg.subject,
            subject_prefix="cuas.sensor.task",
            target_field="sensorId",
        )
    except Exception as exc:  # noqa: BLE001
        log.warning("bad task message: %s", exc)
        return
    expires_at = task.get("expiresAt")
    if expires_at is not None and parse_utc(expires_at) <= datetime.now(timezone.utc):
        log.warning("expired sensor task rejected: %s", task.get("taskId"))
        return
    task_type = task.get("taskType")
    if task_type == "SEARCH":
        volume = task.get("searchVolume") or {
            "centerBearingDeg": task.get("bearingDeg", 0.0),
            "widthDeg": 360.0,
        }
        if SCENARIO.set_search_volume(task.get("sensorId", ""), volume):
            log.info(
                "SEARCH accepted by %s at priority %s: %s",
                task.get("sensorId"),
                task.get("priority"),
                volume,
            )
        else:
            log.warning("SEARCH rejected: invalid sensor or volume")
        return
    if task_type not in {"CUE", "DWELL", "SLEW", "HANDOFF"}:
        log.warning("unsupported sensor task type rejected: %s", task_type)
        return
    sensor_id = task.get("sensorId", SENSOR_ID)
    if task_type == "HANDOFF":
        destination = task.get("handoffToSensorId", "")
        result = (
            (0, 0)
            if SCENARIO.handoff(task.get("trackId", ""), sensor_id, destination)
            else None
        )
        sensor_id = destination
    else:
        result = SCENARIO.task(task.get("trackId", ""), sensor_id)
    if result:
        log.info(
            "%s accepted on %s by %s: TQ remains %d until the next observation",
            task.get("taskType"),
            task.get("trackId"),
            sensor_id,
            result[0],
        )
    else:
        log.warning(
            "%s rejected for %s by %s: track/sensor unavailable or outside coverage",
            task.get("taskType"),
            task.get("trackId"),
            sensor_id,
        )


async def _handle_order(nc, msg) -> None:
    try:
        order = BUS_VERIFIER.verify(
            msg.data,
            expected_message_type="EngagementOrder",
            subject=msg.subject,
            subject_prefix="cuas.engagement.order",
            target_field="effectorId",
        )
    except Exception as exc:  # noqa: BLE001
        log.warning("bad order message: %s", exc)
        return

    async def report(payload: dict) -> None:
        await nc.publish(
            f"cuas.engagement.status.{payload['engagementId']}",
            _envelope("EngagementStatus", "effector", EFFECTOR_ID, payload),
        )
        await nc.publish(
            f"cuas.effector.status.{EFFECTOR_ID}",
            _envelope(
                "EffectorStatus",
                "effector",
                EFFECTOR_ID,
                EFFECTOR.status_payload(ASSET, SCENARIO.clock.now),
            ),
        )
        log.info(
            "engagement %s -> %s sequence=%s terminal=%s (%s)",
            payload["engagementId"],
            payload["state"],
            payload["sequence"],
            payload["terminal"],
            payload["reasonCode"],
        )

    await ENGAGEMENTS.execute(order, SCENARIO, report, asyncio.sleep)


async def _handle_control(msg) -> None:
    try:
        directive = BUS_VERIFIER.verify(
            msg.data,
            expected_message_type="EngagementControlDirective",
            subject=msg.subject,
            subject_prefix="cuas.engagement.control",
            target_field="effectorId",
        )
    except Exception as exc:  # noqa: BLE001
        log.warning("bad engagement-control message: %s", exc)
        return
    result = ENGAGEMENTS.request_abort(directive, SCENARIO.clock.now)
    if result.valid:
        log.warning(
            "engagement %s abort accepted for local effector processing",
            directive.get("engagementId"),
        )
    else:
        log.warning(
            "engagement %s abort rejected: %s",
            directive.get("engagementId"),
            result.reason,
        )


async def main() -> None:
    if AUTHORITY_SIGNING_KEY == "cuas-local-reference-key-not-for-production":
        log.warning("using DEMO-ONLY shared authority key; configure C2_AUTHORITY_SIGNING_KEY outside local reference use")
    log.warning(NOTIONAL_MODEL_NOTICE)
    nc = await nats.connect(NATS_URL, max_reconnect_attempts=-1, reconnect_time_wait=2)
    log.info(
        "connected to %s; sensor=%s effector=%s region=%s asset=(%.6f, %.6f) seed=%d",
        NATS_URL,
        SENSOR_ID,
        EFFECTOR_ID,
        REGION,
        ASSET.lat,
        ASSET.lon,
        SEED,
    )

    async def order_callback(msg) -> None:
        await _handle_order(nc, msg)

    await nc.subscribe("cuas.sensor.task.>", cb=_handle_task)
    await nc.subscribe(f"cuas.engagement.order.{EFFECTOR_ID}", cb=order_callback)
    await nc.subscribe(f"cuas.engagement.control.{EFFECTOR_ID}", cb=_handle_control)
    await asyncio.gather(_publish_tracks(nc), _publish_material_status(nc), _run_scenario())


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
