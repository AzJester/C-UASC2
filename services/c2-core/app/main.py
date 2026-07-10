"""c2-core FastAPI application.

A reference C-UAS C2 node. It builds the COP from the pub/sub track stream, accepts
remote sensor tasking, and runs the four engagement gates before publishing a fire
order. Endpoints honor specs/openapi/cuas-c2.yaml; bus traffic honors
specs/asyncapi/cuas-pubsub.yaml.

Not a fielded weapons system: the effector is simulated and the Zero Trust
identity/PDP is stubbed by an operator-role header (see docs/05 for the production
model).
"""
from __future__ import annotations

import json
import logging
import math
import os
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

from fastapi import FastAPI, Header, HTTPException, Response
from fastapi.responses import HTMLResponse

from . import SCHEMA_VERSION
from .authority import ROE, authorize_engagement, authorize_tasking
from .bus import Bus
from .cop import CommonOperatingPicture
from .models import (
    AuditRecord,
    EffectorStatus,
    EngagementOrder,
    EngagementRequest,
    EngagementState,
    EngagementStatus,
    Envelope,
    Role,
    SensorStatus,
    SensorTask,
    Source,
    Track,
)
from .pairing import check_feasibility

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
log = logging.getLogger("c2.core")

NODE_ID = os.environ.get("C2_NODE_ID", "C2-NODE-01")
NATS_URL = os.environ.get("NATS_URL", "nats://localhost:4222")

# Subject helpers (mirror specs/asyncapi/cuas-pubsub.yaml).
SUBJ_FUSED_TRACKS = "cuas.track.fused.>"
SUBJ_ENGAGEMENT_STATUS = "cuas.engagement.status.>"


def subj_sensor_task(sensor_id: str) -> str:
    return f"cuas.sensor.task.{sensor_id}"


def subj_engagement_order(effector_id: str) -> str:
    return f"cuas.engagement.order.{effector_id}"


def subj_audit(domain: str) -> str:
    return f"cuas.audit.{domain}"


def _load_no_fire_zones() -> list[dict]:
    """No-fire zones (collateral geometry) from CUAS_NO_FIRE_ZONES: a JSON list
    of {lat, lon, radiusMeters, label}. Empty by default; a fielded node gets
    these from the ROE authority, not an environment variable."""
    raw = os.environ.get("CUAS_NO_FIRE_ZONES", "")
    if not raw:
        return []
    try:
        zones = json.loads(raw)
        return [z for z in zones if {"lat", "lon", "radiusMeters"} <= set(z)]
    except (ValueError, TypeError):
        log.warning("CUAS_NO_FIRE_ZONES is not valid JSON; ignoring")
        return []


def _haversine_m(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    r = 6371000.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp, dl = math.radians(lat2 - lat1), math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * r * math.asin(math.sqrt(a))


class State:
    """Process-local state for the reference node (in-memory by design)."""

    def __init__(self) -> None:
        self.bus = Bus(NATS_URL)
        self.cop = CommonOperatingPicture()
        self.sensors: dict[str, SensorStatus] = {}
        self.effectors: dict[str, EffectorStatus] = {}
        self.engagements: dict[str, EngagementStatus] = {}
        self.audit: list[AuditRecord] = []
        self.roe = ROE()  # default: WEAPONS_TIGHT, human-in-the-loop required
        self.no_fire_zones: list[dict] = _load_no_fire_zones()

    def record(self, rec: AuditRecord) -> None:
        self.audit.append(rec)
        log.info("AUDIT %s %s %s %s", rec.action, rec.decision, rec.reasonCode, rec.detail or "")


state = State()


def _envelope(message_type: str, payload: dict) -> bytes:
    # Wire serialization omits absent optionals rather than emitting nulls, so the
    # message conforms to the JSON Schema (which forbids null for typed fields).
    env = Envelope(
        messageType=message_type,
        source=Source(nodeId=NODE_ID, componentType="c2"),
        payload=payload,
    )
    return env.model_dump_json(exclude_none=True).encode()


async def _on_fused_track(subject: str, data: bytes) -> None:
    try:
        env = json.loads(data)
        track = Track.model_validate(env["payload"] if "payload" in env else env)
        state.cop.upsert(track)
    except Exception as exc:  # noqa: BLE001
        log.warning("dropping malformed track on %s: %s", subject, exc)


async def _on_engagement_status(subject: str, data: bytes) -> None:
    try:
        env = json.loads(data)
        status = EngagementStatus.model_validate(env["payload"] if "payload" in env else env)
        state.engagements[status.engagementId] = status
    except Exception as exc:  # noqa: BLE001
        log.warning("dropping malformed engagement status on %s: %s", subject, exc)


@asynccontextmanager
async def lifespan(app: FastAPI):
    await state.bus.connect()
    await state.bus.subscribe(SUBJ_FUSED_TRACKS, _on_fused_track)
    await state.bus.subscribe(SUBJ_ENGAGEMENT_STATUS, _on_engagement_status)
    yield
    await state.bus.close()


app = FastAPI(
    title="C-UAS C2 REST API (reference node)",
    version=SCHEMA_VERSION,
    description="Reference C2 node demonstrating government-owned C-UAS interfaces.",
    lifespan=lifespan,
)


# --- web COP UI -------------------------------------------------------------

_UI_FILE = Path(__file__).parent / "static" / "cop.html"


@app.get("/", response_class=HTMLResponse, include_in_schema=False)
async def cop_ui() -> HTMLResponse:
    """Serve the web COP. The UI auto-detects this backend and runs in LIVE mode,
    polling the REST endpoints below. The same file is published as a standalone
    Artifact, where it runs an embedded simulation instead."""
    try:
        content = _UI_FILE.read_text(encoding="utf-8")
    except FileNotFoundError:
        return HTMLResponse("<h1>COP UI not found</h1>", status_code=404)
    return HTMLResponse(
        '<!doctype html><html lang="en"><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width,initial-scale=1">'
        "<title>JIATF C-UAS C2 — Common Operating Picture</title>"
        "<script>window.__CUAS_BACKEND__=true;</script></head>"
        f"<body>{content}</body></html>"
    )


# --- health & COP -----------------------------------------------------------


@app.get("/health", tags=["health"])
async def health() -> dict:
    return {
        "status": "ok" if state.bus.connected else "degraded",
        "busConnected": state.bus.connected,
        "nodeId": NODE_ID,
        "schemaVersion": SCHEMA_VERSION,
    }


@app.get("/cop", tags=["cop"], response_model=list[Track])
async def get_cop(minTrackQuality: int | None = None, identity: str | None = None):
    return state.cop.list(min_track_quality=minTrackQuality, identity=identity)


# --- materiel registration (no pairing) -------------------------------------


@app.get("/sensors", tags=["materiel"], response_model=list[SensorStatus])
async def list_sensors():
    return list(state.sensors.values())


@app.post("/sensors", tags=["materiel"], status_code=201, response_model=SensorStatus)
async def register_sensor(sensor: SensorStatus):
    state.sensors[sensor.sensorId] = sensor
    state.record(
        AuditRecord(principal=NODE_ID, action="REGISTER_SENSOR", detail=sensor.sensorId)
    )
    return sensor


@app.get("/effectors", tags=["materiel"], response_model=list[EffectorStatus])
async def list_effectors():
    return list(state.effectors.values())


@app.post("/effectors", tags=["materiel"], status_code=201, response_model=EffectorStatus)
async def register_effector(effector: EffectorStatus):
    state.effectors[effector.effectorId] = effector
    state.record(
        AuditRecord(principal=NODE_ID, action="REGISTER_EFFECTOR", detail=effector.effectorId)
    )
    return effector


# --- remote sensor tasking (Imperative 4) -----------------------------------


@app.post("/sensors/{sensor_id}/tasks", tags=["tasking"], status_code=202)
async def task_sensor(
    sensor_id: str,
    task: SensorTask,
    response: Response,
    x_operator_role: str = Header(default=Role.SENSOR_MANAGER.value, alias="X-Operator-Role"),
):
    sensor = state.sensors.get(sensor_id)
    if sensor is None:
        raise HTTPException(status_code=404, detail=f"sensor {sensor_id} not registered")

    role = _parse_role(x_operator_role)
    decision = authorize_tasking(role, sensor.taskable, task.taskType)
    state.record(
        AuditRecord(
            principal=task.requestedBy,
            action=f"TASK_{task.taskType.value}",
            trackId=task.trackId,
            decision="GRANTED" if decision.permit else "DENY",
            reasonCode=decision.reasonCode,
            detail=f"sensor={sensor_id}; {decision.detail}",
        )
    )
    if not decision.permit:
        response.status_code = 403
        return {"taskId": task.taskId, "granted": False, "reason": decision.detail}

    task.sensorId = sensor_id
    await state.bus.publish(subj_sensor_task(sensor_id), _envelope("SensorTask", task.model_dump(mode="json", exclude_none=True)))
    return {"taskId": task.taskId, "granted": True, "reason": decision.detail}


# --- engagement (Imperatives 5; gated by docs/05) ---------------------------


@app.get("/engagements", tags=["engagement"], response_model=list[EngagementStatus])
async def list_engagements():
    return list(state.engagements.values())


@app.post("/engagements", tags=["engagement"], status_code=202, response_model=EngagementStatus)
async def request_engagement(
    req: EngagementRequest,
    response: Response,
    x_operator_role: str = Header(..., alias="X-Operator-Role"),
):
    role = _parse_role(x_operator_role)
    track = state.cop.get(req.trackId)
    effector = state.effectors.get(req.effectorId)
    if track is None:
        raise HTTPException(status_code=404, detail=f"track {req.trackId} not in COP (absent or stale)")
    if effector is None:
        raise HTTPException(status_code=404, detail=f"effector {req.effectorId} not registered")

    engagement_id = f"ENG-{uuid4().hex[:10]}"

    def deny(reason: str, detail: str) -> EngagementStatus:
        status = EngagementStatus(
            engagementId=engagement_id,
            effectorId=req.effectorId,
            trackId=req.trackId,
            state=EngagementState.DENIED,
            reasonCode=reason,
            detail=detail,
        )
        state.engagements[engagement_id] = status
        state.record(
            AuditRecord(
                principal=role.value,
                action="ENGAGEMENT_REQUEST",
                trackId=req.trackId,
                decision="DENY",
                reasonCode=reason,
                detail=f"effector={req.effectorId}; {detail}",
            )
        )
        response.status_code = 403
        return status

    # Gate 2: effector feasibility (availability, compatibility, envelope).
    feas = check_feasibility(track, effector, req.engagementType)
    if not feas.permit:
        return deny(feas.reasonCode, feas.detail)

    # Gates 1 & 3: track quality + authority/ROE (including collateral geometry).
    zone = None
    for z in state.no_fire_zones:
        p = track.kinematics.position
        if _haversine_m(p.lat, p.lon, z["lat"], z["lon"]) <= z["radiusMeters"]:
            zone = str(z.get("label", "NO-FIRE"))
            break
    auth = authorize_engagement(
        role=role,
        track=track,
        effector=effector,
        roe=state.roe,
        human_confirmation=req.humanConfirmation,
        no_fire_zone=zone,
    )
    if not auth.permit:
        return deny(auth.reasonCode, auth.detail)

    # PERMIT: mint a short-lived authority token and publish the fire order.
    authority_token = f"AUTH.{engagement_id}.{uuid4().hex[:8]}"
    order = EngagementOrder(
        engagementId=engagement_id,
        trackId=req.trackId,
        effectorId=req.effectorId,
        orderedBy=role.value,
        authorityToken=authority_token,
        engagementType=req.engagementType,
        trackSnapshotTimeObserved=track.timeObserved,
    )
    published = await state.bus.publish(
        subj_engagement_order(req.effectorId), _envelope("EngagementOrder", order.model_dump(mode="json", exclude_none=True))
    )

    status = EngagementStatus(
        engagementId=engagement_id,
        effectorId=req.effectorId,
        trackId=req.trackId,
        state=EngagementState.AUTHORIZED,
        reasonCode="OK",
        detail="authorized; order published" if published else "authorized; bus degraded, order queued locally",
    )
    state.engagements[engagement_id] = status
    state.record(
        AuditRecord(
            principal=role.value,
            action="ENGAGEMENT_AUTHORIZED",
            trackId=req.trackId,
            decision="PERMIT",
            reasonCode="OK",
            detail=f"effector={req.effectorId}; token={authority_token}; published={published}",
        )
    )
    # Also publish the authority decision to the audit stream.
    await state.bus.publish(
        subj_audit("engagement"),
        _envelope("EngagementStatus", status.model_dump(mode="json", exclude_none=True)),
    )
    return status


# --- audit ------------------------------------------------------------------


@app.get("/audit", tags=["audit"], response_model=list[AuditRecord])
async def get_audit():
    return state.audit


def _parse_role(value: str) -> Role:
    try:
        return Role(value)
    except ValueError:
        raise HTTPException(status_code=400, detail=f"unknown operator role: {value}")
