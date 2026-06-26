"""Canonical data model (Pydantic mirror of specs/schemas/*.json).

The JSON Schema files under specs/schemas/ are the source of truth for the
government-owned interface. These Pydantic models mirror them so the service can
validate at the edge and so codegen/conformance can compare the two. Keep them in
sync; ADR-0002 explains why there is one canonical model reused by REST and bus.
"""
from __future__ import annotations

from datetime import datetime, timezone
from enum import Enum
from typing import Optional
from uuid import uuid4

from pydantic import BaseModel, Field


def _now() -> datetime:
    return datetime.now(timezone.utc)


# --- enumerations -----------------------------------------------------------


class Identity(str, Enum):
    PENDING = "PENDING"
    UNKNOWN = "UNKNOWN"
    SUSPECT = "SUSPECT"
    FRIEND = "FRIEND"
    ASSUMED_FRIEND = "ASSUMED_FRIEND"
    NEUTRAL = "NEUTRAL"
    HOSTILE = "HOSTILE"


class ClassificationType(str, Enum):
    FIXED_WING = "FIXED_WING"
    ROTARY = "ROTARY"
    MULTIROTOR = "MULTIROTOR"
    UAS_GROUP_1 = "UAS_GROUP_1"
    UAS_GROUP_2 = "UAS_GROUP_2"
    UAS_GROUP_3 = "UAS_GROUP_3"
    UNKNOWN = "UNKNOWN"


class SensorType(str, Enum):
    RADAR = "RADAR"
    RF = "RF"
    EO_IR = "EO_IR"
    ACOUSTIC = "ACOUSTIC"
    FUSION_INPUT = "FUSION_INPUT"
    OTHER = "OTHER"


class EffectorType(str, Enum):
    EW_JAMMER = "EW_JAMMER"
    RF_TAKEOVER = "RF_TAKEOVER"
    KINETIC_GUN = "KINETIC_GUN"
    KINETIC_INTERCEPTOR = "KINETIC_INTERCEPTOR"
    DIRECTED_ENERGY = "DIRECTED_ENERGY"
    NET_CAPTURE = "NET_CAPTURE"
    OTHER = "OTHER"


class Readiness(str, Enum):
    READY = "READY"
    DEGRADED = "DEGRADED"
    OFFLINE = "OFFLINE"
    MAINTENANCE = "MAINTENANCE"
    WEAPONS_HOLD = "WEAPONS_HOLD"


class TaskType(str, Enum):
    SEARCH = "SEARCH"
    CUE = "CUE"
    SLEW = "SLEW"
    DWELL = "DWELL"
    HANDOFF = "HANDOFF"


class EngagementType(str, Enum):
    EW_DEFEAT = "EW_DEFEAT"
    RF_TAKEOVER = "RF_TAKEOVER"
    KINETIC = "KINETIC"
    DIRECTED_ENERGY = "DIRECTED_ENERGY"
    NET_CAPTURE = "NET_CAPTURE"


class EngagementState(str, Enum):
    PROPOSED = "PROPOSED"
    AUTHORIZED = "AUTHORIZED"
    ACCEPTED = "ACCEPTED"
    ACTIVE = "ACTIVE"
    COMPLETE = "COMPLETE"
    ABORTED = "ABORTED"
    FAILED = "FAILED"
    DENIED = "DENIED"


class Role(str, Enum):
    OBSERVER = "OBSERVER"
    SENSOR_MANAGER = "SENSOR_MANAGER"
    ENGAGEMENT_OPERATOR = "ENGAGEMENT_OPERATOR"
    FIRE_CONTROL_AUTHORITY = "FIRE_CONTROL_AUTHORITY"


# --- geometry ---------------------------------------------------------------


class Position(BaseModel):
    lat: float = Field(ge=-90, le=90)
    lon: float = Field(ge=-180, le=180)
    altMeters: float
    frame: str = "WGS84"


class Velocity(BaseModel):
    speedMps: float = Field(default=0, ge=0)
    courseDeg: float = Field(default=0, ge=0, le=360)
    verticalRateMps: float = 0


class Kinematics(BaseModel):
    position: Position
    velocity: Optional[Velocity] = None


# --- canonical types --------------------------------------------------------


class Track(BaseModel):
    trackId: str
    kinematics: Kinematics
    trackQuality: int = Field(ge=0, le=15)
    identity: Identity
    classificationType: ClassificationType = ClassificationType.UNKNOWN
    contributingSensors: list[str] = Field(default_factory=list)
    timeObserved: datetime = Field(default_factory=_now)
    timeToLiveSeconds: float = 30.0


class Coverage(BaseModel):
    center: Optional[Position] = None
    rangeMeters: float = 0


class SensorStatus(BaseModel):
    sensorId: str
    sensorType: SensorType
    vendor: Optional[str] = None
    readiness: Readiness = Readiness.READY
    mode: Optional[str] = None
    taskable: bool = True
    coverage: Optional[Coverage] = None
    softwareVersion: Optional[str] = None
    timeReported: datetime = Field(default_factory=_now)


class Magazine(BaseModel):
    remaining: float = 0
    capacity: float = 0
    unit: str = "shots"


class EngagementEnvelope(BaseModel):
    location: Optional[Position] = None
    minRangeMeters: float = 0
    maxRangeMeters: float = 0
    minAltMeters: float = 0
    maxAltMeters: float = 0


class EffectorStatus(BaseModel):
    effectorId: str
    effectorType: EffectorType
    vendor: Optional[str] = None
    readiness: Readiness = Readiness.READY
    magazine: Optional[Magazine] = None
    engagementEnvelope: Optional[EngagementEnvelope] = None
    humanControl: str = "IN_THE_LOOP"
    softwareVersion: Optional[str] = None
    timeReported: datetime = Field(default_factory=_now)


class SensorTask(BaseModel):
    taskId: str = Field(default_factory=lambda: f"TASK-{uuid4().hex[:8]}")
    sensorId: str
    taskType: TaskType
    trackId: Optional[str] = None
    bearingDeg: Optional[float] = Field(default=None, ge=0, le=360)
    handoffToSensorId: Optional[str] = None
    priority: int = Field(default=3, ge=0, le=9)
    requestedBy: str
    expiresAt: Optional[datetime] = None


class EngagementRequest(BaseModel):
    trackId: str
    effectorId: str
    engagementType: EngagementType
    humanConfirmation: bool = False


class EngagementOrder(BaseModel):
    engagementId: str
    trackId: str
    effectorId: str
    orderedBy: str
    authorityToken: str
    engagementType: EngagementType
    trackSnapshotTimeObserved: Optional[datetime] = None
    timeOrdered: datetime = Field(default_factory=_now)


class EngagementStatus(BaseModel):
    engagementId: str
    effectorId: str
    trackId: Optional[str] = None
    state: EngagementState
    reasonCode: str = "OK"
    detail: Optional[str] = None
    timeReported: datetime = Field(default_factory=_now)


# --- envelope ---------------------------------------------------------------


class Source(BaseModel):
    nodeId: str
    componentType: str


class Envelope(BaseModel):
    messageId: str = Field(default_factory=lambda: str(uuid4()))
    schemaVersion: str = "1.0.0"
    messageType: str
    source: Source
    classification: str = "UNCLASSIFIED"
    timeCreated: datetime = Field(default_factory=_now)
    signature: Optional[str] = None
    payload: dict


class AuditRecord(BaseModel):
    recordId: str = Field(default_factory=lambda: f"AUD-{uuid4().hex[:10]}")
    timeRecorded: datetime = Field(default_factory=_now)
    principal: str
    action: str
    trackId: Optional[str] = None
    decision: str = "INFO"
    reasonCode: str = "OK"
    detail: Optional[str] = None
