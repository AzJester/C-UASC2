"""Canonical data model (Pydantic mirror of specs/schemas/*.json).

The JSON Schema files under specs/schemas/ are the source of truth for the
government-owned interface. These Pydantic models mirror them so the service can
validate at the edge and so codegen/conformance can compare the two. Keep them in
sync; ADR-0002 explains why there is one canonical model reused by REST and bus.
"""
from __future__ import annotations

from datetime import datetime, timezone
from enum import Enum
from typing import Literal, Optional
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field, model_validator


SUBJECT_TOKEN_ID_PATTERN = r"^[A-Za-z0-9][A-Za-z0-9_:@/-]{0,127}$"


def _now() -> datetime:
    return datetime.now(timezone.utc)


class StrictModel(BaseModel):
    """Reject undeclared wire fields; canonical schemas forbid extras."""

    model_config = ConfigDict(extra="forbid")


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


class EmitterState(str, Enum):
    """RF emitter state from ESM/RF sensing. SILENT threats cannot be detected
    or soft-killed through the RF path (docs/03)."""

    EMITTING = "EMITTING"
    SILENT = "SILENT"
    UNKNOWN = "UNKNOWN"


class EngagementState(str, Enum):
    PROPOSED = "PROPOSED"
    AUTHORIZED = "AUTHORIZED"
    ACCEPTED = "ACCEPTED"
    ACTIVE = "ACTIVE"
    ASSESSING = "ASSESSING"
    COMPLETE = "COMPLETE"
    ABORTED = "ABORTED"
    FAILED = "FAILED"
    DENIED = "DENIED"


class Role(str, Enum):
    OBSERVER = "OBSERVER"
    SENSOR_MANAGER = "SENSOR_MANAGER"
    ENGAGEMENT_OPERATOR = "ENGAGEMENT_OPERATOR"
    FIRE_CONTROL_AUTHORITY = "FIRE_CONTROL_AUTHORITY"


class AltitudeReference(str, Enum):
    WGS84_ELLIPSOID = "WGS84_ELLIPSOID"
    MSL = "MSL"
    AGL = "AGL"
    UNKNOWN = "UNKNOWN"


class FusionState(str, Enum):
    TENTATIVE = "TENTATIVE"
    CONFIRMED = "CONFIRMED"
    COASTING = "COASTING"
    STALE = "STALE"


class Service(str, Enum):
    USA = "USA"
    USN = "USN"
    USMC = "USMC"
    USAF = "USAF"
    USSF = "USSF"
    JOINT = "JOINT"
    COALITION = "COALITION"
    UNKNOWN = "UNKNOWN"


class EffectOutcome(str, Enum):
    PENDING = "PENDING"
    CONFIRMED_EFFECT = "CONFIRMED_EFFECT"
    NO_CONFIRMED_EFFECT = "NO_CONFIRMED_EFFECT"
    INDETERMINATE = "INDETERMINATE"


# --- geometry ---------------------------------------------------------------


class Position(StrictModel):
    lat: float = Field(ge=-90, le=90)
    lon: float = Field(ge=-180, le=180)
    altMeters: float
    frame: Literal["WGS84"] = "WGS84"
    altitudeReference: AltitudeReference = AltitudeReference.UNKNOWN


class Velocity(StrictModel):
    speedMps: float = Field(default=0, ge=0)
    courseDeg: float = Field(default=0, ge=0, lt=360)
    verticalRateMps: float = 0


class Kinematics(StrictModel):
    position: Position
    velocity: Optional[Velocity] = None


class GeoPoint(StrictModel):
    lat: float = Field(ge=-90, le=90)
    lon: float = Field(ge=-180, le=180)


class Covariance(StrictModel):
    horizontalMeters: Optional[float] = Field(default=None, ge=0)
    verticalMeters: Optional[float] = Field(default=None, ge=0)
    referenceFrame: Optional[str] = None
    confidenceLevel: Optional[str] = None
    eastVarianceM2: Optional[float] = Field(default=None, ge=0)
    northVarianceM2: Optional[float] = Field(default=None, ge=0)
    upVarianceM2: Optional[float] = Field(default=None, ge=0)
    eastNorthCovarianceM2: Optional[float] = None


# --- canonical types --------------------------------------------------------


class Track(StrictModel):
    trackId: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
    kinematics: Kinematics
    covariance: Optional[Covariance] = None
    trackQuality: int = Field(ge=0, le=15)
    identity: Identity
    classificationType: ClassificationType = ClassificationType.UNKNOWN
    emitterState: Optional[EmitterState] = None
    platform: Optional[str] = None
    service: Optional[Service] = None
    contributingSensors: list[str] = Field(default_factory=list)
    timeObserved: datetime = Field(default_factory=_now)
    timeUpdated: Optional[datetime] = None
    dataAgeSeconds: Optional[float] = Field(default=None, ge=0)
    measurementLatencyMilliseconds: Optional[int] = Field(default=None, ge=0)
    observationSequence: Optional[int] = Field(default=None, ge=0)
    fusionState: Optional[FusionState] = None
    timeToLiveSeconds: float = Field(default=30.0, gt=0)
    modelProvenance: Optional[str] = Field(default=None, max_length=256)


class Coverage(StrictModel):
    center: Optional[GeoPoint] = None
    rangeMeters: float = Field(default=0, ge=0)
    minAltMeters: Optional[float] = None
    maxAltMeters: Optional[float] = None


class SensorStatus(StrictModel):
    sensorId: str = Field(pattern=SUBJECT_TOKEN_ID_PATTERN)
    sensorType: SensorType
    vendor: Optional[str] = None
    readiness: Readiness = Readiness.READY
    mode: Optional[str] = None
    taskable: bool = True
    coverage: Optional[Coverage] = None
    softwareVersion: Optional[str] = None
    timeReported: datetime = Field(default_factory=_now)


class Magazine(StrictModel):
    remaining: float = 0
    capacity: float = 0
    unit: str = "shots"

    @model_validator(mode="after")
    def remaining_not_above_capacity(self) -> "Magazine":
        if self.remaining < 0 or self.capacity < 0 or self.remaining > self.capacity:
            raise ValueError("magazine remaining must be between zero and capacity")
        return self


class EngagementEnvelope(StrictModel):
    location: Optional[Position] = None
    minRangeMeters: float = Field(default=0, ge=0)
    maxRangeMeters: float = Field(default=0, ge=0)
    minAltMeters: float = 0
    maxAltMeters: float = 0


class EffectorStatus(StrictModel):
    effectorId: str = Field(pattern=SUBJECT_TOKEN_ID_PATTERN)
    effectorType: EffectorType
    vendor: Optional[str] = None
    readiness: Readiness = Readiness.READY
    magazine: Optional[Magazine] = None
    engagementEnvelope: Optional[EngagementEnvelope] = None
    humanControl: str = "IN_THE_LOOP"
    softwareVersion: Optional[str] = None
    timeReported: datetime = Field(default_factory=_now)

    @model_validator(mode="after")
    def sensor_readiness_is_not_a_weapons_state(self) -> "SensorStatus":
        if self.readiness == Readiness.WEAPONS_HOLD:
            raise ValueError("WEAPONS_HOLD is an effector readiness state")
        return self
    modelProvenance: Optional[str] = Field(default=None, max_length=256)


class SearchVolume(StrictModel):
    centerBearingDeg: Optional[float] = Field(default=None, ge=0, le=360)
    widthDeg: Optional[float] = Field(default=None, ge=0, le=360)
    minAltMeters: Optional[float] = None
    maxAltMeters: Optional[float] = None

    @model_validator(mode="after")
    def altitude_bounds_are_ordered(self) -> "SearchVolume":
        if (
            self.minAltMeters is not None
            and self.maxAltMeters is not None
            and self.minAltMeters > self.maxAltMeters
        ):
            raise ValueError("searchVolume minAltMeters must not exceed maxAltMeters")
        return self


class SensorTask(StrictModel):
    taskId: str = Field(default_factory=lambda: f"TASK-{uuid4().hex[:8]}")
    sensorId: str = Field(pattern=SUBJECT_TOKEN_ID_PATTERN)
    taskType: TaskType
    trackId: Optional[str] = None
    bearingDeg: Optional[float] = Field(default=None, ge=0, le=360)
    searchVolume: Optional[SearchVolume] = None
    handoffToSensorId: Optional[str] = Field(
        default=None, pattern=SUBJECT_TOKEN_ID_PATTERN
    )
    priority: int = Field(default=3, ge=0, le=9)
    requestedBy: str
    expiresAt: Optional[datetime] = None

    @model_validator(mode="after")
    def task_scope_is_complete(self) -> "SensorTask":
        if self.taskType in {TaskType.CUE, TaskType.SLEW, TaskType.DWELL} and not self.trackId:
            raise ValueError(f"{self.taskType.value} requires trackId")
        if self.taskType == TaskType.HANDOFF and (
            not self.trackId or not self.handoffToSensorId
        ):
            raise ValueError("HANDOFF requires trackId and handoffToSensorId")
        return self


class EngagementRequest(StrictModel):
    trackId: str
    effectorId: str = Field(pattern=SUBJECT_TOKEN_ID_PATTERN)
    engagementType: EngagementType
    humanConfirmation: bool = False


class EngagementConstraints(StrictModel):
    abortIfFriendlyWithinMeters: Optional[float] = Field(default=None, ge=0)
    maxEngagementSeconds: Optional[float] = Field(default=None, gt=0, le=600)
    requireHumanConfirmation: bool = True
    humanConfirmed: bool = False


class AuthorityTokenScope(StrictModel):
    engagementId: str
    requestId: str
    trackId: str
    effectorId: str = Field(pattern=SUBJECT_TOKEN_ID_PATTERN)
    engagementType: EngagementType
    policyVersion: str
    weaponsControlStatus: str


class EngagementOrder(StrictModel):
    engagementId: str
    requestId: str
    trackId: str
    effectorId: str = Field(pattern=SUBJECT_TOKEN_ID_PATTERN)
    orderedBy: str
    authorityToken: str
    authorityTokenExpiresAt: datetime
    authorityTokenScope: AuthorityTokenScope
    orderSequence: int = Field(ge=1)
    engagementType: EngagementType
    constraints: EngagementConstraints
    trackSnapshotTimeObserved: datetime
    timeOrdered: datetime = Field(default_factory=_now)


class EffectAssessment(StrictModel):
    outcome: EffectOutcome
    confidence: float = Field(ge=0, le=1)
    method: str = Field(min_length=1, max_length=128)
    timeAssessed: datetime


class EngagementStatus(StrictModel):
    engagementId: str
    effectorId: str = Field(pattern=SUBJECT_TOKEN_ID_PATTERN)
    trackId: Optional[str] = None
    state: EngagementState
    sequence: int = Field(ge=1)
    terminal: bool
    reasonCode: str = "OK"
    detail: Optional[str] = None
    inventoryRemaining: Optional[float] = Field(default=None, ge=0)
    effectAssessment: Optional[EffectAssessment] = None
    timeReported: datetime = Field(default_factory=_now)

    @model_validator(mode="after")
    def lifecycle_consistency(self) -> "EngagementStatus":
        terminal_states = {
            EngagementState.COMPLETE,
            EngagementState.ABORTED,
            EngagementState.FAILED,
            EngagementState.DENIED,
        }
        if self.terminal != (self.state in terminal_states):
            raise ValueError("terminal flag does not match engagement state")
        if self.state == EngagementState.ASSESSING and self.effectAssessment is None:
            raise ValueError("ASSESSING requires effectAssessment")
        return self


# --- envelope ---------------------------------------------------------------


class Source(StrictModel):
    nodeId: str
    componentType: Literal["sensor", "effector", "c2", "fusion", "gateway"]


class Envelope(StrictModel):
    messageId: str = Field(default_factory=lambda: str(uuid4()))
    schemaVersion: Literal["1.0.0"] = "1.0.0"
    messageType: Literal[
        "Track",
        "Detection",
        "SensorTask",
        "SensorStatus",
        "EffectorStatus",
        "EngagementOrder",
        "EngagementStatus",
        "EngagementControlDirective",
        "AuditRecord",
        "C2Directive",
    ]
    source: Source
    classification: Literal[
        "UNCLASSIFIED", "CUI", "CONFIDENTIAL", "SECRET", "TOP_SECRET"
    ] = "UNCLASSIFIED"
    timeCreated: datetime = Field(default_factory=_now)
    signature: Optional[str] = None
    payload: dict


class AuditRecord(StrictModel):
    recordId: str = Field(default_factory=lambda: f"AUD-{uuid4().hex[:10]}")
    timeRecorded: datetime = Field(default_factory=_now)
    principal: str
    action: str
    trackId: Optional[str] = None
    engagementId: Optional[str] = None
    requestId: Optional[str] = None
    effectorId: Optional[str] = None
    lifecycleState: Optional[str] = None
    sequence: Optional[int] = Field(default=None, ge=1)
    deliveryState: Optional[str] = None
    assessmentOutcome: Optional[str] = None
    decision: Literal["PERMIT", "DENY", "GRANTED", "INFO"] = "INFO"
    reasonCode: str = "OK"
    detail: Optional[str] = None
    previousRecordHash: Optional[str] = Field(default=None, pattern=r"^[a-f0-9]{64}$")
    recordHash: Optional[str] = Field(default=None, pattern=r"^[a-f0-9]{64}$")


# --- reference-node control plane ------------------------------------------
# These REST view models describe authoritative node/scenario state.  They are
# deliberately separate from the canonical pub/sub schemas above.


class AreaOfOperations(StrictModel):
    center: Position
    radiusMeters: float = Field(gt=0)
    label: str


class ScenarioConfig(StrictModel):
    scenarioId: str
    name: str
    operationalMode: str = "DEMONSTRATION"
    authoritativeNodeId: str
    revision: str = "1"
    areaOfOperations: AreaOfOperations
    noFireZones: list[dict] = Field(default_factory=list)
    timeReported: datetime = Field(default_factory=_now)


class PolicyState(StrictModel):
    policyVersion: str
    weaponsControlStatus: str
    requireHumanInTheLoop: bool
    minTrackQuality: dict[str, int]
    authoritySource: str
    readOnly: bool = True
    timeReported: datetime = Field(default_factory=_now)


class SessionView(StrictModel):
    principal: str
    role: Role
    authenticationMode: str
    assurance: str
    demoOnly: bool = True
    roleReadOnly: bool = True
    policyVersion: str
    weaponsControlStatus: str
    permissions: list[str] = Field(default_factory=list)


class AuthorityTokenValidationRequest(StrictModel):
    token: str = Field(min_length=20, max_length=4096)
    engagementId: str
    trackId: str
    effectorId: str = Field(pattern=SUBJECT_TOKEN_ID_PATTERN)
    engagementType: EngagementType


class AuthorityTokenValidationResult(StrictModel):
    valid: bool
    reasonCode: str
    engagementId: Optional[str] = None
    expiresAt: Optional[datetime] = None


class EngagementAbortRequest(StrictModel):
    reason: str = Field(default="operator requested abort", min_length=1, max_length=256)
    humanConfirmation: bool = False


class EngagementControlDirective(StrictModel):
    engagementId: str
    requestId: str
    trackId: str
    effectorId: str = Field(pattern=SUBJECT_TOKEN_ID_PATTERN)
    action: Literal["ABORT"] = "ABORT"
    reason: str
    orderedBy: str
    authorityToken: str
    authorityTokenExpiresAt: datetime
    directiveSequence: int = Field(ge=1)
    timeOrdered: datetime = Field(default_factory=_now)


class EngagementControlReceipt(StrictModel):
    engagementId: str
    requestId: str
    action: str = "ABORT"
    accepted: bool
    deliveryState: str
    lifecycleState: EngagementState
    detail: str
    timeReported: datetime = Field(default_factory=_now)
