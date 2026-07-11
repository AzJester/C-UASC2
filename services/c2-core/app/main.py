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

import asyncio
import hashlib
import hmac
import json
import logging
import math
import os
import re
import threading
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from uuid import uuid4

from fastapi import FastAPI, Header, HTTPException, Query, Response
from fastapi.responses import HTMLResponse

from . import SCHEMA_VERSION
from .authority import (
    ROE,
    ROLES_MAY_RELEASE,
    ROLES_MAY_TASK,
    authorize_engagement,
    authorize_tasking,
)
from .bus import Bus, PublishOutcome
from .cop import CommonOperatingPicture
from .models import (
    AuditRecord,
    AreaOfOperations,
    AuthorityTokenValidationRequest,
    AuthorityTokenValidationResult,
    EffectorStatus,
    EngagementAbortRequest,
    EngagementControlDirective,
    EngagementControlReceipt,
    EngagementConstraints,
    EngagementOrder,
    EngagementRequest,
    EngagementState,
    EngagementStatus,
    Envelope,
    Identity,
    PolicyState,
    Readiness,
    Role,
    ScenarioConfig,
    SensorStatus,
    SensorTask,
    SessionView,
    Source,
    TaskType,
    Track,
)
from .pairing import check_feasibility
from .tokens import AuthorityTokenIssuer

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
log = logging.getLogger("c2.core")

NODE_ID = os.environ.get("C2_NODE_ID", "C2-NODE-01")
NATS_URL = os.environ.get("NATS_URL", "nats://localhost:4222")
POLICY_VERSION = os.environ.get("C2_POLICY_VERSION", "DEMO-ROE-1")
DEMO_AUTH_HEADERS = os.environ.get("C2_DEMO_AUTH_HEADERS", "true").lower() in {
    "1",
    "true",
    "yes",
}
ALLOW_DEMO_REGISTRATION = os.environ.get("C2_ALLOW_DEMO_REGISTRATION", "false").lower() in {
    "1",
    "true",
    "yes",
}
AUTHORITY_TOKEN_TTL_SECONDS = int(os.environ.get("C2_AUTHORITY_TOKEN_TTL_SECONDS", "20"))
FRIENDLY_SEPARATION_METERS = float(
    os.environ.get("C2_FRIENDLY_SEPARATION_METERS", "150")
)
MAX_ENGAGEMENT_SECONDS = float(os.environ.get("C2_MAX_ENGAGEMENT_SECONDS", "60"))
EFFECTOR_ACK_TIMEOUT_SECONDS = float(
    os.environ.get("C2_EFFECTOR_ACK_TIMEOUT_SECONDS", "15")
)
ENGAGEMENT_STALL_TIMEOUT_SECONDS = float(
    os.environ.get("C2_ENGAGEMENT_STALL_TIMEOUT_SECONDS", "120")
)
ABORT_ACK_TIMEOUT_SECONDS = float(
    os.environ.get("C2_ABORT_ACK_TIMEOUT_SECONDS", "20")
)
MATERIEL_STATUS_TTL_SECONDS = float(
    os.environ.get("C2_MATERIEL_STATUS_TTL_SECONDS", "10")
)
STATUS_MAX_FUTURE_SKEW_SECONDS = float(
    os.environ.get("C2_STATUS_MAX_FUTURE_SKEW_SECONDS", "5")
)
_DEMO_SIGNING_SECRET = "cuas-local-reference-key-not-for-production"
_SIGNING_SECRET = os.environ.get("C2_AUTHORITY_SIGNING_KEY")
_EFFECTIVE_SIGNING_SECRET = _SIGNING_SECRET or _DEMO_SIGNING_SECRET
DEMO_SHARED_SIGNING_KEY = _EFFECTIVE_SIGNING_SECRET == _DEMO_SIGNING_SECRET
_BUS_SIGNING_SECRET = os.environ.get("CUAS_BUS_SIGNING_KEY", _EFFECTIVE_SIGNING_SECRET)
REQUIRE_SIGNED_BUS_MESSAGES = os.environ.get(
    "C2_REQUIRE_SIGNED_BUS_MESSAGES", "true"
).lower() in {"1", "true", "yes"}
ALLOW_RAW_BUS_PAYLOADS = os.environ.get(
    "C2_ALLOW_RAW_BUS_PAYLOADS", "false"
).lower() in {"1", "true", "yes"}
_IDENTIFIER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:@/-]{0,127}$")
_SUBJECT_TOKEN_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_:@/-]{0,127}$")

# Subject helpers (mirror specs/asyncapi/cuas-pubsub.yaml).
SUBJ_FUSED_TRACKS = "cuas.track.fused.>"
SUBJ_ENGAGEMENT_STATUS = "cuas.engagement.status.>"
SUBJ_SENSOR_STATUS = "cuas.sensor.status.>"
SUBJ_EFFECTOR_STATUS = "cuas.effector.status.>"


def subj_sensor_task(sensor_id: str) -> str:
    return f"cuas.sensor.task.{sensor_id}"


def subj_engagement_order(effector_id: str) -> str:
    return f"cuas.engagement.order.{effector_id}"


def subj_engagement_control(effector_id: str) -> str:
    return f"cuas.engagement.control.{effector_id}"


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
        return _validated_no_fire_zones(zones, "CUAS_NO_FIRE_ZONES")
    except (ValueError, TypeError) as exc:
        # Safety configuration must never fail open. An operator must correct the
        # deployment input before the node can start.
        raise RuntimeError("CUAS_NO_FIRE_ZONES is invalid") from exc


def _validated_no_fire_zones(value: object, source: str) -> list[dict]:
    if not isinstance(value, list):
        raise ValueError(f"{source} must be a JSON list")
    normalized: list[dict] = []
    for index, zone in enumerate(value):
        if not isinstance(zone, dict) or not {"lat", "lon", "radiusMeters"} <= set(zone):
            raise ValueError(f"{source}[{index}] is missing lat/lon/radiusMeters")
        lat = float(zone["lat"])
        lon = float(zone["lon"])
        radius = float(zone["radiusMeters"])
        if not -90 <= lat <= 90 or not -180 <= lon <= 180 or radius <= 0:
            raise ValueError(f"{source}[{index}] contains unsafe geometry")
        label = str(zone.get("label", f"NO-FIRE-{index + 1}"))[:128]
        normalized.append({"lat": lat, "lon": lon, "radiusMeters": radius, "label": label})
    return normalized


def _load_scenario(no_fire_zones: list[dict]) -> ScenarioConfig:
    """Load the single server-authoritative AO used by UI and integrations.

    ``CUAS_SCENARIO_CONFIG`` may contain a complete ScenarioConfig JSON object.
    The default deliberately matches the standalone San Diego demonstration AO;
    simulators should discover this endpoint instead of carrying another origin.
    """
    raw = os.environ.get("CUAS_SCENARIO_CONFIG")
    if raw:
        try:
            return ScenarioConfig.model_validate_json(raw)
        except Exception as exc:  # noqa: BLE001
            log.error("invalid CUAS_SCENARIO_CONFIG; refusing ambiguous AO: %s", exc)
            raise RuntimeError("CUAS_SCENARIO_CONFIG is invalid") from exc
    return ScenarioConfig(
        scenarioId="SAN-DIEGO-CUAS-DEMO-01",
        name="San Diego C-UAS Command Center Demonstration",
        operationalMode="DEMONSTRATION",
        authoritativeNodeId=NODE_ID,
        revision="1",
        areaOfOperations=AreaOfOperations(
            center={"lat": 32.699, "lon": -117.215, "altMeters": 0},
            radiusMeters=20000,
            label="NAS North Island demonstration AO",
        ),
        noFireZones=no_fire_zones,
    )


def _haversine_m(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    r = 6371000.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp, dl = math.radians(lat2 - lat1), math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * r * math.asin(math.sqrt(a))


_TERMINAL_STATES = {
    EngagementState.COMPLETE,
    EngagementState.ABORTED,
    EngagementState.FAILED,
    EngagementState.DENIED,
}
_ALLOWED_TRANSITIONS = {
    EngagementState.PROPOSED: {
        EngagementState.AUTHORIZED,
        EngagementState.DENIED,
        EngagementState.FAILED,
        EngagementState.ABORTED,
    },
    EngagementState.AUTHORIZED: {
        EngagementState.ACCEPTED,
        # The effector is the final policy/safety enforcement point. It may
        # reject an order after C2 authorization (scope, freshness, interlock,
        # applicability); that rejection is terminal and releases reservation.
        EngagementState.DENIED,
        EngagementState.FAILED,
        EngagementState.ABORTED,
    },
    EngagementState.ACCEPTED: {
        EngagementState.ACTIVE,
        EngagementState.FAILED,
        EngagementState.ABORTED,
    },
    EngagementState.ACTIVE: {
        EngagementState.ASSESSING,
        EngagementState.COMPLETE,
        EngagementState.FAILED,
        EngagementState.ABORTED,
    },
    EngagementState.ASSESSING: {
        EngagementState.COMPLETE,
        EngagementState.FAILED,
        EngagementState.ABORTED,
    },
}


class State:
    """Process-local operational state with optional append-only audit storage.

    The reference remains a single-node demonstration.  Setting ``C2_AUDIT_FILE``
    provides restart-surviving JSONL records; a fielded system replaces this with
    replicated, tamper-evident event storage.
    """

    def __init__(self) -> None:
        self.bus = Bus(NATS_URL)
        self.cop = CommonOperatingPicture()
        self.sensors: dict[str, SensorStatus] = {}
        self.effectors: dict[str, EffectorStatus] = {}
        self.engagements: dict[str, EngagementStatus] = {}
        self.engagement_history: dict[str, list[EngagementStatus]] = {}
        self.engagement_requests: dict[str, tuple[str, str]] = {}
        self.effector_reservations: dict[str, str] = {}
        self.inventory_committed: dict[str, float] = {}
        self.task_results: dict[str, tuple[str, dict]] = {}
        self.task_inflight: set[str] = set()
        self.sensor_task_leases: dict[str, dict] = {}
        self.control_requests: dict[str, tuple[str, EngagementControlReceipt, int]] = {}
        self.control_inflight: set[str] = set()
        self.abort_pending: dict[str, str] = {}
        self.ack_timeouts: set[str] = set()
        self.abort_ack_timeouts: set[str] = set()
        self.delivery_unknown: set[str] = set()
        self.materiel_stale: set[str] = set()
        self.seen_bus_messages: dict[str, datetime] = {}
        self.audit: list[AuditRecord] = []
        self.audit_healthy = True
        self.audit_error: str | None = None
        self.roe = ROE()  # default: WEAPONS_TIGHT, human-in-the-loop required
        configured_zones = _load_no_fire_zones()
        self.scenario = _load_scenario(configured_zones)
        # A dedicated environment override wins when present; otherwise the
        # canonical scenario document owns the safety geometry.
        if not os.environ.get("CUAS_NO_FIRE_ZONES", "").strip():
            configured_zones = _validated_no_fire_zones(
                self.scenario.noFireZones,
                "CUAS_SCENARIO_CONFIG.noFireZones",
            )
        self.no_fire_zones = configured_zones
        self.scenario = self.scenario.model_copy(
            update={"noFireZones": list(configured_zones)}
        )
        self.engagement_locks: dict[str, asyncio.Lock] = {}
        # Safety-control traffic has a per-engagement lane and never queues
        # behind an unrelated release or abort awaiting transport flush.
        self.abort_locks: dict[str, asyncio.Lock] = {}
        self._audit_lock = threading.Lock()
        self._task_idempotency_lock = threading.Lock()
        self._sensor_arbiter_lock = threading.Lock()
        self._engagement_idempotency_lock = threading.Lock()
        self._control_idempotency_lock = threading.Lock()
        audit_path = os.environ.get("C2_AUDIT_FILE")
        self.audit_path = Path(audit_path).resolve() if audit_path else None
        if self.audit_path is not None and self.audit_path.exists():
            for line_number, line in enumerate(
                self.audit_path.read_text(encoding="utf-8").splitlines(), start=1
            ):
                if not line.strip():
                    continue
                try:
                    record = AuditRecord.model_validate_json(line)
                    previous_hash = self.audit[-1].recordHash if self.audit else None
                    if record.previousRecordHash != previous_hash:
                        raise ValueError("audit previousRecordHash does not match chain")
                    if record.recordHash != self._audit_hash(record):
                        raise ValueError("audit recordHash verification failed")
                    self.audit.append(record)
                except Exception as exc:  # noqa: BLE001
                    raise RuntimeError(
                        f"audit integrity verification failed at "
                        f"{self.audit_path}:{line_number}"
                    ) from exc
        self.tokens = AuthorityTokenIssuer(
            _EFFECTIVE_SIGNING_SECRET,
            NODE_ID,
            AUTHORITY_TOKEN_TTL_SECONDS,
        )

    def policy(self) -> PolicyState:
        return PolicyState(
            policyVersion=POLICY_VERSION,
            weaponsControlStatus=self.roe.weaponsControlStatus,
            requireHumanInTheLoop=self.roe.requireHumanInTheLoop,
            minTrackQuality={
                key.value if hasattr(key, "value") else str(key): int(value)
                for key, value in self.roe.minTrackQuality.items()
            },
            authoritySource=NODE_ID,
        )

    def engagement_lock_for(self, effector_id: str) -> asyncio.Lock:
        return self.engagement_locks.setdefault(effector_id, asyncio.Lock())

    def abort_lock_for(self, engagement_id: str) -> asyncio.Lock:
        return self.abort_locks.setdefault(engagement_id, asyncio.Lock())

    @staticmethod
    def _audit_hash(rec: AuditRecord) -> str:
        canonical = json.dumps(
            rec.model_dump(
                mode="json",
                exclude_none=True,
                exclude={"recordHash"},
            ),
            sort_keys=True,
            separators=(",", ":"),
        )
        return hashlib.sha256(canonical.encode()).hexdigest()

    def record(self, rec: AuditRecord) -> None:
        with self._audit_lock:
            rec.previousRecordHash = self.audit[-1].recordHash if self.audit else None
            rec.recordHash = self._audit_hash(rec)
            try:
                if self.audit_path is not None:
                    self.audit_path.parent.mkdir(parents=True, exist_ok=True)
                    # Replace a complete candidate file atomically. This is slower
                    # than append but prevents a partial fsync failure from making
                    # the in-memory hash chain diverge from restart evidence.
                    candidate = [*self.audit, rec]
                    temp_path = self.audit_path.with_name(
                        f".{self.audit_path.name}.pending"
                    )
                    with temp_path.open("w", encoding="utf-8", newline="\n") as handle:
                        for item in candidate:
                            handle.write(item.model_dump_json(exclude_none=True) + "\n")
                        handle.flush()
                        os.fsync(handle.fileno())
                    os.replace(temp_path, self.audit_path)
                self.audit.append(rec)
                self.audit_healthy = True
                self.audit_error = None
            except OSError as exc:
                self.audit_healthy = False
                self.audit_error = str(exc)
                raise RuntimeError("audit evidence persistence failed; command path inhibited") from exc
        log.info("AUDIT %s %s %s %s", rec.action, rec.decision, rec.reasonCode, rec.detail or "")

    def probe_audit_sink(self) -> bool:
        """Re-arm a transiently failed local evidence sink without hiding loss."""
        if self.audit_healthy:
            return True
        if self.audit_path is None:
            self.audit_healthy = True
            self.audit_error = None
            return True
        probe = self.audit_path.with_name(f".{self.audit_path.name}.healthcheck")
        try:
            self.audit_path.parent.mkdir(parents=True, exist_ok=True)
            with probe.open("w", encoding="utf-8") as handle:
                handle.write("audit-sink-healthcheck\n")
                handle.flush()
                os.fsync(handle.fileno())
            probe.unlink()
            self.audit_healthy = True
            self.audit_error = None
            return True
        except OSError as exc:
            self.audit_error = str(exc)
            try:
                probe.unlink(missing_ok=True)
            except OSError:
                pass
            return False

    def transition(self, status: EngagementStatus) -> str:
        """Apply one legal, monotonic lifecycle transition.

        Returns ``APPLIED``, ``DUPLICATE``, or ``INVALID``. Sequence is the primary
        ordering key; timestamps reject regressions after the first cross-node ACK.
        """
        previous = self.engagements.get(status.engagementId)
        if previous is not None:
            if (
                status.effectorId != previous.effectorId
                or status.trackId != previous.trackId
            ):
                return "INVALID"
            if status.sequence < previous.sequence:
                return "INVALID"
            if status.sequence == previous.sequence:
                return "DUPLICATE" if status.state == previous.state else "INVALID"
            if status.state == previous.state:
                return "DUPLICATE"
            if previous.state in _TERMINAL_STATES:
                return "INVALID"
            if status.state not in _ALLOWED_TRANSITIONS.get(previous.state, set()):
                return "INVALID"
            previous_time = previous.timeReported
            status_time = status.timeReported
            if previous_time.tzinfo is None:
                previous_time = previous_time.replace(tzinfo=timezone.utc)
            if status_time.tzinfo is None:
                return "INVALID"
            # The initial ACK crosses node clocks; tolerate bounded negative skew.
            if previous.state == EngagementState.AUTHORIZED:
                if (previous_time - status_time).total_seconds() > 5:
                    return "INVALID"
            elif status_time < previous_time:
                return "INVALID"
        elif status.state != EngagementState.PROPOSED or status.sequence != 1:
            return "INVALID"

        effector = self.effectors.get(status.effectorId)
        if status.inventoryRemaining is not None and effector is not None and effector.magazine:
            if status.inventoryRemaining > effector.magazine.capacity:
                return "INVALID"

        self.engagements[status.engagementId] = status
        # Lifecycle history is an immutable event log. Live delivery/timeout
        # annotations must never rewrite an already-recorded event.
        self.engagement_history.setdefault(status.engagementId, []).append(
            status.model_copy(deep=True)
        )
        if status.sequence > 2 or status.state in _TERMINAL_STATES:
            self.delivery_unknown.discard(status.engagementId)
        self.ack_timeouts.discard(status.engagementId)
        if status.inventoryRemaining is not None and effector is not None and effector.magazine:
            effector.magazine.remaining = status.inventoryRemaining
        if status.state in _TERMINAL_STATES:
            pending_abort_request = self.abort_pending.get(status.engagementId)
            if pending_abort_request is not None:
                stored_control = self.control_requests.get(pending_abort_request)
                if stored_control is not None:
                    fingerprint, receipt, _ = stored_control
                    aborted = status.state == EngagementState.ABORTED
                    receipt.accepted = aborted
                    receipt.deliveryState = (
                        "EFFECTOR_ACKNOWLEDGED"
                        if aborted
                        else "TERMINAL_WITHOUT_ABORT_ACK"
                    )
                    receipt.lifecycleState = status.state
                    receipt.detail = (
                        "effector acknowledged ABORTED"
                        if aborted
                        else f"engagement became {status.state.value} before an ABORTED acknowledgement"
                    )
                    self.control_requests[pending_abort_request] = (
                        fingerprint,
                        receipt,
                        202 if aborted else 409,
                    )
            if self.effector_reservations.get(status.effectorId) == status.engagementId:
                self.effector_reservations.pop(status.effectorId, None)
            self.abort_pending.pop(status.engagementId, None)
            self.ack_timeouts.discard(status.engagementId)
            self.abort_ack_timeouts.discard(status.engagementId)
        return "APPLIED"

    def expire_stale_commands(self, now: datetime | None = None) -> None:
        """Fail-safe reconciliation for commands that never receive an ACK.

        An AUTHORIZED command retains its effector reservation while delivery is
        pending/unknown. A timeout marks the state uncertain and keeps the
        reservation/inhibit in place until positive reconciliation, preventing an
        unsafe duplicate effect.
        """
        now = now or datetime.now(timezone.utc)
        for engagement_id, current in list(self.engagements.items()):
            timeout = None
            if current.state == EngagementState.AUTHORIZED:
                timeout = EFFECTOR_ACK_TIMEOUT_SECONDS
            elif current.state in {
                EngagementState.ACCEPTED,
                EngagementState.ACTIVE,
                EngagementState.ASSESSING,
            }:
                timeout = ENGAGEMENT_STALL_TIMEOUT_SECONDS
            if timeout is None:
                continue
            reported = current.timeReported
            if reported.tzinfo is None:
                reported = reported.replace(tzinfo=timezone.utc)
            if (now - reported).total_seconds() <= timeout:
                continue
            if engagement_id not in self.ack_timeouts:
                timeout_detail = (
                    f"no lifecycle acknowledgement within {timeout:g}s; "
                    "state unknown and effector remains inhibited/reserved pending positive reconciliation"
                )
                self.record(
                    AuditRecord(
                        principal=NODE_ID,
                        action="ENGAGEMENT_ACK_TIMEOUT",
                        trackId=current.trackId,
                        engagementId=engagement_id,
                        effectorId=current.effectorId,
                        lifecycleState=current.state.value,
                        sequence=current.sequence,
                        deliveryState="ACK_TIMEOUT",
                        decision="DENY",
                        reasonCode="EFFECTOR_ACK_TIMEOUT",
                        detail=f"engagement={engagement_id}; prior={current.state.value}; reservation=retained",
                    )
                )
                current.detail = timeout_detail
                self.ack_timeouts.add(engagement_id)

        for engagement_id, request_id in list(self.abort_pending.items()):
            stored = self.control_requests.get(request_id)
            if stored is None:
                self.abort_pending.pop(engagement_id, None)
                continue
            fingerprint, receipt, http_status = stored
            reported = receipt.timeReported
            if reported.tzinfo is None:
                reported = reported.replace(tzinfo=timezone.utc)
            if (now - reported).total_seconds() <= ABORT_ACK_TIMEOUT_SECONDS:
                continue
            if engagement_id in self.abort_ack_timeouts:
                continue
            self.record(
                AuditRecord(
                    principal=NODE_ID,
                    action="ENGAGEMENT_ABORT_ACK_TIMEOUT",
                    trackId=self.engagements.get(engagement_id).trackId
                    if engagement_id in self.engagements
                    else None,
                    engagementId=engagement_id,
                    requestId=request_id,
                    effectorId=self.engagements.get(engagement_id).effectorId
                    if engagement_id in self.engagements
                    else None,
                    lifecycleState=receipt.lifecycleState.value,
                    deliveryState="ACK_TIMEOUT",
                    decision="DENY",
                    reasonCode="EFFECTOR_ACK_TIMEOUT",
                    detail=f"engagement={engagement_id}; request={request_id}",
                )
            )
            receipt.accepted = False
            receipt.deliveryState = "ACK_TIMEOUT"
            receipt.detail = "no ABORTED acknowledgement received; engagement remains inhibited pending reconciliation"
            self.control_requests[request_id] = (fingerprint, receipt, 503)
            self.abort_ack_timeouts.add(engagement_id)

    def expire_materiel(self, now: datetime | None = None) -> None:
        """Mark devices OFFLINE when their authoritative heartbeat expires."""
        now = now or datetime.now(timezone.utc)
        for component_id, status in [
            *self.sensors.items(),
            *self.effectors.items(),
        ]:
            reported = status.timeReported
            if reported.tzinfo is None:
                reported = reported.replace(tzinfo=timezone.utc)
            if (now - reported).total_seconds() <= MATERIEL_STATUS_TTL_SECONDS:
                continue
            status.readiness = Readiness.OFFLINE
            if component_id not in self.materiel_stale:
                self.record(
                    AuditRecord(
                        principal=NODE_ID,
                        action="MATERIEL_STATUS_EXPIRED",
                        decision="DENY",
                        reasonCode="STATUS_STALE",
                        detail=f"component={component_id}; ttl={MATERIEL_STATUS_TTL_SECONDS:g}s",
                    )
                )
                self.materiel_stale.add(component_id)

    def expire_sensor_task_leases(self, now: datetime | None = None) -> None:
        now = now or datetime.now(timezone.utc)
        with self._sensor_arbiter_lock:
            self.sensor_task_leases = {
                sensor_id: lease
                for sensor_id, lease in self.sensor_task_leases.items()
                if lease["expiresAt"] > now
            }


state = State()


def _envelope(
    message_type: str,
    payload: dict,
    *,
    source_id: str = NODE_ID,
    component_type: str = "c2",
) -> bytes:
    # Wire serialization omits absent optionals rather than emitting nulls, so the
    # message conforms to the JSON Schema (which forbids null for typed fields).
    env = Envelope(
        messageType=message_type,
        source=Source(nodeId=source_id, componentType=component_type),
        payload=payload,
    )
    raw = env.model_dump(mode="json", exclude_none=True)
    raw["signature"] = _wire_signature(raw)
    return json.dumps(raw, separators=(",", ":")).encode()


def _wire_signature(raw: dict) -> str:
    unsigned = {key: value for key, value in raw.items() if key != "signature"}
    canonical = json.dumps(
        unsigned,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    ).encode()
    digest = hmac.new(_BUS_SIGNING_SECRET.encode(), canonical, hashlib.sha256).hexdigest()
    return f"hmac-sha256:{digest}"


def _bus_payload(
    data: bytes,
    expected_message_type: str,
    *,
    expected_source_id: str | None = None,
    expected_component_type: str | None = None,
    source_matches_payload_field: str | None = None,
    required_payload_fields: tuple[str, ...] = (),
) -> dict:
    raw = json.loads(data)
    if not isinstance(raw, dict):
        raise ValueError("bus message must be a JSON object")
    if "payload" not in raw:
        if not ALLOW_RAW_BUS_PAYLOADS:
            raise ValueError("un-enveloped bus payloads are disabled")
        return raw
    envelope_required = {
        "messageId",
        "schemaVersion",
        "messageType",
        "source",
        "classification",
        "timeCreated",
        "payload",
    }
    if REQUIRE_SIGNED_BUS_MESSAGES:
        envelope_required.add("signature")
    missing_envelope = sorted(envelope_required - raw.keys())
    if missing_envelope:
        raise ValueError(
            f"envelope is missing required source fields: {', '.join(missing_envelope)}"
        )
    envelope = Envelope.model_validate(raw)
    if envelope.schemaVersion != SCHEMA_VERSION:
        raise ValueError(
            f"unsupported schemaVersion {envelope.schemaVersion}; expected {SCHEMA_VERSION}"
        )
    if envelope.messageType != expected_message_type:
        raise ValueError(
            f"expected {expected_message_type}, received {envelope.messageType}"
        )
    if expected_source_id is not None and envelope.source.nodeId != expected_source_id:
        raise ValueError(
            f"source {envelope.source.nodeId} does not match expected identity {expected_source_id}"
        )
    if (
        expected_component_type is not None
        and envelope.source.componentType != expected_component_type
    ):
        raise ValueError(
            f"source type {envelope.source.componentType} does not match "
            f"expected {expected_component_type}"
        )
    if source_matches_payload_field is not None:
        payload_identity = envelope.payload.get(source_matches_payload_field)
        if payload_identity != envelope.source.nodeId:
            raise ValueError(
                f"source {envelope.source.nodeId} does not match payload "
                f"{source_matches_payload_field}={payload_identity}"
            )
    missing = [field for field in required_payload_fields if field not in envelope.payload]
    if missing:
        raise ValueError(f"payload is missing required source fields: {', '.join(missing)}")
    if REQUIRE_SIGNED_BUS_MESSAGES:
        supplied = envelope.signature or ""
        expected = _wire_signature(raw)
        if not hmac.compare_digest(supplied, expected):
            raise ValueError("bus envelope signature is absent or invalid")
    if envelope.messageId in state.seen_bus_messages:
        raise ValueError(f"replayed messageId {envelope.messageId}")
    state.seen_bus_messages[envelope.messageId] = envelope.timeCreated
    if len(state.seen_bus_messages) > 4096:
        oldest = sorted(
            state.seen_bus_messages,
            key=state.seen_bus_messages.__getitem__,
        )[:1024]
        for message_id in oldest:
            state.seen_bus_messages.pop(message_id, None)
    return envelope.payload


def _subject_identity(subject: str, prefix: str) -> str:
    expected_prefix = f"{prefix}."
    if not subject.startswith(expected_prefix):
        raise ValueError(f"unexpected subject {subject}")
    identity = subject[len(expected_prefix) :]
    if "." in identity or not _IDENTIFIER.fullmatch(identity):
        raise ValueError(f"subject identity is invalid: {identity}")
    return identity


def _validate_status_timestamp(reported: datetime) -> None:
    if reported.tzinfo is None:
        raise ValueError("timeReported must include a timezone")
    now = datetime.now(timezone.utc)
    if (reported - now).total_seconds() > STATUS_MAX_FUTURE_SKEW_SECONDS:
        raise ValueError("timeReported exceeds the allowed future clock skew")


async def _on_fused_track(subject: str, data: bytes) -> None:
    try:
        track = Track.model_validate(
            _bus_payload(
                data,
                "Track",
                expected_component_type="fusion",
                required_payload_fields=("timeObserved",),
            )
        )
        _validate_status_timestamp(track.timeObserved)
        if not state.cop.upsert(track):
            log.info(
                "ignoring late, duplicate, or expired track update on %s: %s sequence=%s",
                subject,
                track.trackId,
                track.observationSequence,
            )
    except Exception as exc:  # noqa: BLE001
        log.warning("dropping malformed track on %s: %s", subject, exc)


async def _on_sensor_status(subject: str, data: bytes) -> None:
    try:
        subject_id = _subject_identity(subject, "cuas.sensor.status")
        sensor = SensorStatus.model_validate(
            _bus_payload(
                data,
                "SensorStatus",
                expected_source_id=subject_id,
                expected_component_type="sensor",
                required_payload_fields=(
                    "sensorId",
                    "sensorType",
                    "readiness",
                    "timeReported",
                ),
            )
        )
        if not _IDENTIFIER.fullmatch(sensor.sensorId) or sensor.sensorId != subject_id:
            raise ValueError("sensor identity does not match subject/source")
        _validate_status_timestamp(sensor.timeReported)
        previous = state.sensors.get(sensor.sensorId)
        if previous is not None and sensor.timeReported <= previous.timeReported:
            log.info("ignoring late/duplicate sensor status on %s: %s", subject, sensor.sensorId)
            return
        state.sensors[sensor.sensorId] = sensor
        state.materiel_stale.discard(sensor.sensorId)
    except Exception as exc:  # noqa: BLE001
        log.warning("dropping malformed sensor status on %s: %s", subject, exc)


async def _on_effector_status(subject: str, data: bytes) -> None:
    try:
        subject_id = _subject_identity(subject, "cuas.effector.status")
        effector = EffectorStatus.model_validate(
            _bus_payload(
                data,
                "EffectorStatus",
                expected_source_id=subject_id,
                expected_component_type="effector",
                required_payload_fields=(
                    "effectorId",
                    "effectorType",
                    "readiness",
                    "timeReported",
                ),
            )
        )
        if not _IDENTIFIER.fullmatch(effector.effectorId) or effector.effectorId != subject_id:
            raise ValueError("effector identity does not match subject/source")
        _validate_status_timestamp(effector.timeReported)
        previous = state.effectors.get(effector.effectorId)
        if previous is not None and effector.timeReported <= previous.timeReported:
            log.info(
                "ignoring late/duplicate effector status on %s: %s",
                subject,
                effector.effectorId,
            )
            return
        state.effectors[effector.effectorId] = effector
        state.materiel_stale.discard(effector.effectorId)
    except Exception as exc:  # noqa: BLE001
        log.warning("dropping malformed effector status on %s: %s", subject, exc)


async def _on_engagement_status(subject: str, data: bytes) -> None:
    try:
        engagement_id = _subject_identity(subject, "cuas.engagement.status")
        status = EngagementStatus.model_validate(
            _bus_payload(
                data,
                "EngagementStatus",
                expected_component_type="effector",
                source_matches_payload_field="effectorId",
                required_payload_fields=("timeReported",),
            )
        )
        if status.engagementId != engagement_id:
            raise ValueError("engagement identity does not match subject")
        _validate_status_timestamp(status.timeReported)
        if status.engagementId not in state.engagements:
            raise ValueError("engagement status does not match a C2-originated engagement")
        request_id = next(
            (
                candidate_request_id
                for candidate_request_id, (_, candidate_engagement_id) in state.engagement_requests.items()
                if candidate_engagement_id == status.engagementId
            ),
            None,
        )
        transition_result = state.transition(status)
        if transition_result == "DUPLICATE":
            log.info(
                "ignoring idempotent engagement status duplicate on %s: %s sequence=%s",
                subject,
                status.engagementId,
                status.sequence,
            )
            return
        if transition_result != "APPLIED":
            log.warning(
                "dropping invalid engagement transition on %s: %s -> %s",
                subject,
                state.engagements.get(status.engagementId).state
                if status.engagementId in state.engagements
                else "UNKNOWN",
                status.state,
            )
            state.record(
                AuditRecord(
                    principal=NODE_ID,
                    action="ENGAGEMENT_STATUS_REJECTED",
                    trackId=status.trackId,
                    engagementId=status.engagementId,
                    requestId=request_id,
                    effectorId=status.effectorId,
                    lifecycleState=status.state.value,
                    sequence=status.sequence,
                    decision="DENY",
                    reasonCode="INVALID_TRANSITION",
                    detail=f"engagement={status.engagementId}; attempted={status.state.value}",
                )
            )
            return
        state.record(
            AuditRecord(
                principal=status.effectorId,
                action=f"ENGAGEMENT_{status.state.value}",
                trackId=status.trackId,
                engagementId=status.engagementId,
                requestId=request_id,
                effectorId=status.effectorId,
                lifecycleState=status.state.value,
                sequence=status.sequence,
                assessmentOutcome=status.effectAssessment.outcome.value
                if status.effectAssessment is not None
                else None,
                decision="INFO",
                reasonCode=status.reasonCode,
                detail=f"engagement={status.engagementId}; {status.detail or ''}".rstrip(),
            )
        )
    except Exception as exc:  # noqa: BLE001
        log.warning("dropping malformed engagement status on %s: %s", subject, exc)


@asynccontextmanager
async def lifespan(app: FastAPI):
    async def subscribe_operational_feeds() -> None:
        await state.bus.subscribe(SUBJ_FUSED_TRACKS, _on_fused_track)
        await state.bus.subscribe(SUBJ_SENSOR_STATUS, _on_sensor_status)
        await state.bus.subscribe(SUBJ_EFFECTOR_STATUS, _on_effector_status)
        await state.bus.subscribe(SUBJ_ENGAGEMENT_STATUS, _on_engagement_status)

    connected = await state.bus.connect()
    if connected:
        await subscribe_operational_feeds()

    async def reconnect_supervisor() -> None:
        """Recover when the broker was absent at startup.

        Once nats-py has established a connection, its own reconnect machinery
        preserves subscriptions. This loop only creates a fresh connection when
        no client is connected/reconnecting, then installs the operational feeds.
        """
        while True:
            await asyncio.sleep(2)
            try:
                state.expire_stale_commands()
                state.expire_materiel()
                state.expire_sensor_task_leases()
            except Exception as exc:  # noqa: BLE001
                # Safety state remains inhibited/offline; keep supervision alive
                # so a recovered evidence sink and broker can be reconciled.
                log.error("supervisor evidence/freshness cycle failed: %s", exc)
            if state.bus.connected:
                continue
            if await state.bus.connect():
                await subscribe_operational_feeds()

    supervisor = asyncio.create_task(reconnect_supervisor())
    try:
        yield
    finally:
        supervisor.cancel()
        try:
            await supervisor
        except asyncio.CancelledError:
            pass
        await state.bus.close()


app = FastAPI(
    title="C-UAS C2 REST API (reference node)",
    version=SCHEMA_VERSION,
    description="Reference C2 node demonstrating government-owned C-UAS interfaces.",
    lifespan=lifespan,
)


def _request_fingerprint(kind: str, value: dict) -> str:
    canonical = json.dumps({"kind": kind, **value}, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode()).hexdigest()


def _canonical_safety_hash(value: object) -> str:
    """Hash schema-normalized safety data independent of `150` vs `150.0`."""
    def normalize(item: object) -> object:
        if isinstance(item, dict):
            return {key: normalize(item[key]) for key in sorted(item)}
        if isinstance(item, list):
            return [normalize(entry) for entry in item]
        if isinstance(item, float) and item.is_integer():
            return int(item)
        return item

    canonical = json.dumps(
        normalize(value),
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(canonical.encode()).hexdigest()


def _validated_identifier(value: str, label: str) -> str:
    if not _IDENTIFIER.fullmatch(value):
        raise HTTPException(status_code=400, detail=f"invalid {label}")
    return value


def _validated_subject_token(value: str, label: str) -> str:
    if not _SUBJECT_TOKEN_ID.fullmatch(value):
        raise HTTPException(
            status_code=400,
            detail=f"invalid {label}; bus-addressed component IDs must be one NATS subject token",
        )
    return value


def _session(role_value: str, principal_value: str | None) -> tuple[Role, str]:
    """Resolve the demo session without mistaking a browser header for identity."""
    if not DEMO_AUTH_HEADERS:
        raise HTTPException(
            status_code=503,
            detail="production identity provider is not configured; demo headers disabled",
        )
    role = _parse_role(role_value)
    principal = _validated_identifier(principal_value or f"DEMO-{role.value}", "operator identity")
    return role, principal


def _status_http_code(status: EngagementStatus) -> int:
    if status.engagementId in state.delivery_unknown:
        return 503
    if status.state == EngagementState.DENIED:
        return 403
    if status.state == EngagementState.FAILED:
        return 503
    return 202


def _ensure_audit_healthy() -> None:
    if not state.probe_audit_sink():
        raise HTTPException(
            status_code=503,
            detail="audit evidence store is unhealthy; operational writes are inhibited",
        )


def _store_control_result(
    request_id: str,
    value: tuple[str, EngagementControlReceipt, int],
) -> None:
    with state._control_idempotency_lock:
        state.control_requests[request_id] = value
        state.control_inflight.discard(request_id)


def _authorize_demo_registration(component_identity: str | None, expected_id: str) -> str:
    if not ALLOW_DEMO_REGISTRATION:
        raise HTTPException(
            status_code=403,
            detail="REST materiel registration is disabled; publish authenticated status on the bus",
        )
    if component_identity is None:
        raise HTTPException(status_code=401, detail="X-Component-Id is required for demo registration")
    identity = _validated_identifier(component_identity, "component identity")
    if identity != expected_id:
        raise HTTPException(
            status_code=403,
            detail="component identity may register only its own status",
        )
    return identity


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
        "status": "ok" if state.bus.connected and state.audit_healthy else "degraded",
        "busConnected": state.bus.connected,
        "nodeId": NODE_ID,
        "schemaVersion": SCHEMA_VERSION,
        "operationalMode": state.scenario.operationalMode,
        "scenarioId": state.scenario.scenarioId,
        "policyVersion": POLICY_VERSION,
        "authenticationMode": "DEMO_UNTRUSTED_HEADERS"
        if DEMO_AUTH_HEADERS
        else "PRODUCTION_IDENTITY_NOT_CONFIGURED",
        "authoritySigningKey": "DEMO_SHARED_INSECURE"
        if DEMO_SHARED_SIGNING_KEY
        else "CONFIGURED",
        "restMaterielRegistration": "DEMO_ENABLED"
        if ALLOW_DEMO_REGISTRATION
        else "DISABLED_BUS_AUTHORITATIVE",
        "busMessageAuthentication": "HMAC_SHARED_DEMO"
        if REQUIRE_SIGNED_BUS_MESSAGES
        else "UNSIGNED_DEMO_OVERRIDE",
        "staleMaterielCount": len(state.materiel_stale),
        "auditHealthy": state.audit_healthy,
        "operationalStatePersistence": "IN_MEMORY_DEMO_ONLY",
    }


@app.get("/scenario", tags=["control"], response_model=ScenarioConfig)
async def get_scenario() -> ScenarioConfig:
    """Return the node-owned scenario/AO contract for UI and simulator clients."""
    # Reflect zone changes made by a test/exercise controller without creating a
    # second source of truth in the response object.
    return state.scenario.model_copy(
        update={"noFireZones": list(state.no_fire_zones), "timeReported": datetime.now(timezone.utc)}
    )


@app.get("/policy", tags=["control"], response_model=PolicyState)
async def get_policy() -> PolicyState:
    """Return server-authoritative WCS/ROE state; there is no browser write API."""
    return state.policy()


@app.get("/session", tags=["control"], response_model=SessionView)
async def get_session(
    x_operator_role: str = Header(default=Role.OBSERVER.value, alias="X-Operator-Role"),
    x_operator_id: str | None = Header(default=None, alias="X-Operator-Id"),
) -> SessionView:
    role, principal = _session(x_operator_role, x_operator_id)
    permissions: list[str] = []
    if role in ROLES_MAY_TASK:
        permissions.append("TASK_SENSOR")
    if role in ROLES_MAY_RELEASE:
        permissions.append("RELEASE_ENGAGEMENT")
    return SessionView(
        principal=principal,
        role=role,
        authenticationMode="DEMO_UNTRUSTED_HEADERS",
        assurance="UNTRUSTED_DEMONSTRATION_ONLY",
        policyVersion=POLICY_VERSION,
        weaponsControlStatus=state.roe.weaponsControlStatus,
        permissions=permissions,
    )


@app.get("/cop", tags=["cop"], response_model=list[Track])
async def get_cop(
    minTrackQuality: int | None = Query(default=None, ge=0, le=15),
    identity: Identity | None = None,
):
    return state.cop.list(
        min_track_quality=minTrackQuality,
        identity=identity.value if identity is not None else None,
    )


# --- materiel registration (no pairing) -------------------------------------


@app.get("/sensors", tags=["materiel"], response_model=list[SensorStatus])
async def list_sensors():
    state.expire_materiel()
    return list(state.sensors.values())


@app.post("/sensors", tags=["materiel"], status_code=201, response_model=SensorStatus)
async def register_sensor(
    sensor: SensorStatus,
    x_component_id: str | None = Header(default=None, alias="X-Component-Id"),
):
    _ensure_audit_healthy()
    _validated_subject_token(sensor.sensorId, "sensor ID")
    principal = _authorize_demo_registration(x_component_id, sensor.sensorId)
    missing = {"readiness", "timeReported"} - sensor.model_fields_set
    if missing:
        raise HTTPException(
            status_code=422,
            detail=f"registration is missing source fields: {', '.join(sorted(missing))}",
        )
    _validate_status_timestamp(sensor.timeReported)
    state.sensors[sensor.sensorId] = sensor
    state.materiel_stale.discard(sensor.sensorId)
    state.record(
        AuditRecord(principal=principal, action="REGISTER_SENSOR", detail=sensor.sensorId)
    )
    return sensor


@app.get("/effectors", tags=["materiel"], response_model=list[EffectorStatus])
async def list_effectors():
    state.expire_materiel()
    return list(state.effectors.values())


@app.post("/effectors", tags=["materiel"], status_code=201, response_model=EffectorStatus)
async def register_effector(
    effector: EffectorStatus,
    x_component_id: str | None = Header(default=None, alias="X-Component-Id"),
):
    _ensure_audit_healthy()
    _validated_subject_token(effector.effectorId, "effector ID")
    principal = _authorize_demo_registration(x_component_id, effector.effectorId)
    missing = {"readiness", "timeReported"} - effector.model_fields_set
    if missing:
        raise HTTPException(
            status_code=422,
            detail=f"registration is missing source fields: {', '.join(sorted(missing))}",
        )
    _validate_status_timestamp(effector.timeReported)
    if effector.effectorId in state.effector_reservations:
        raise HTTPException(status_code=409, detail="effector has an active engagement reservation")
    state.effectors[effector.effectorId] = effector
    state.materiel_stale.discard(effector.effectorId)
    state.record(
        AuditRecord(principal=principal, action="REGISTER_EFFECTOR", detail=effector.effectorId)
    )
    return effector


# --- remote sensor tasking (Imperative 4) -----------------------------------


@app.post("/sensors/{sensor_id}/tasks", tags=["tasking"], status_code=202)
async def task_sensor(
    sensor_id: str,
    task: SensorTask,
    response: Response,
    x_operator_role: str = Header(default=Role.OBSERVER.value, alias="X-Operator-Role"),
    x_operator_id: str | None = Header(default=None, alias="X-Operator-Id"),
    x_request_id: str | None = Header(default=None, alias="X-Request-ID"),
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
):
    _ensure_audit_healthy()
    _validated_subject_token(sensor_id, "sensor ID")
    state.expire_materiel()
    sensor = state.sensors.get(sensor_id)
    if sensor is None:
        raise HTTPException(status_code=404, detail=f"sensor {sensor_id} not registered")

    if task.sensorId != sensor_id:
        raise HTTPException(status_code=409, detail="body sensorId does not match path sensor_id")
    if sensor.readiness not in (Readiness.READY, Readiness.DEGRADED):
        raise HTTPException(
            status_code=409,
            detail=f"sensor readiness {sensor.readiness.value} does not permit tasking",
        )
    if task.expiresAt is not None:
        if task.expiresAt.utcoffset() is None:
            raise HTTPException(status_code=422, detail="expiresAt must include a timezone")
        if task.expiresAt <= datetime.now(timezone.utc):
            raise HTTPException(status_code=410, detail="sensor task has already expired")
    referenced_track = None
    if task.trackId is not None:
        _validated_identifier(task.trackId, "track ID")
        referenced_track = state.cop.get(task.trackId)
        if referenced_track is None:
            raise HTTPException(
                status_code=404,
                detail=f"track {task.trackId} not in COP (absent or stale)",
            )
    elif task.taskType in {
        TaskType.CUE,
        TaskType.SLEW,
        TaskType.DWELL,
        TaskType.HANDOFF,
    }:
        raise HTTPException(status_code=422, detail=f"{task.taskType.value} requires a trackId")
    if task.taskType == TaskType.HANDOFF:
        if task.handoffToSensorId is None:
            raise HTTPException(status_code=422, detail="HANDOFF requires handoffToSensorId")
        if task.handoffToSensorId not in state.sensors:
            raise HTTPException(status_code=404, detail="handoff destination sensor not registered")
    coverage = sensor.coverage
    if (
        referenced_track is not None
        and coverage is not None
        and coverage.center is not None
        and coverage.rangeMeters > 0
    ):
        p = referenced_track.kinematics.position
        distance = _haversine_m(coverage.center.lat, coverage.center.lon, p.lat, p.lon)
        if distance > coverage.rangeMeters:
            raise HTTPException(
                status_code=422,
                detail=f"track is outside sensor coverage ({distance:.0f}m > {coverage.rangeMeters:.0f}m)",
            )

    role, principal = _session(x_operator_role, x_operator_id)
    # The authenticated session is authoritative. Never forward a caller-supplied
    # attribution that could impersonate a different operator on the bus.
    task.requestedBy = principal
    request_id = _validated_identifier(
        x_request_id or idempotency_key or task.taskId,
        "request ID",
    )
    if "taskId" not in task.model_fields_set:
        # Keep the wire task identifier stable when a caller retries an
        # Idempotency-Key after a transient transport failure.
        task.taskId = f"TASK-{hashlib.sha256(request_id.encode()).hexdigest()[:12]}"
    response.headers["X-Request-ID"] = request_id
    fingerprint = _request_fingerprint(
        "sensor-task",
        {
            "sensorId": sensor_id,
            "task": task.model_dump(
                mode="json",
                exclude_none=True,
                exclude={"taskId"},
            ),
        },
    )
    with state._task_idempotency_lock:
        previous = state.task_results.get(request_id)
        if previous is not None:
            old_fingerprint, old_result = previous
            if old_fingerprint != fingerprint:
                raise HTTPException(
                    status_code=409,
                    detail="request ID was already used for different tasking",
                )
            response.status_code = int(old_result["httpStatus"])
            return {key: value for key, value in old_result.items() if key != "httpStatus"}
        if request_id in state.task_inflight:
            raise HTTPException(
                status_code=409,
                detail="request ID is already in progress; reconcile with the same ID",
            )
        state.task_inflight.add(request_id)

    decision = authorize_tasking(role, sensor.taskable, task.taskType)
    effective_permit = decision.permit
    effective_reason = decision.reasonCode
    effective_detail = decision.detail
    denial_status = 403
    lease_claimed = False
    if effective_permit:
        now = datetime.now(timezone.utc)
        if task.expiresAt is None:
            task.expiresAt = now + timedelta(seconds=10)
        with state._sensor_arbiter_lock:
            current_lease = state.sensor_task_leases.get(sensor_id)
            if current_lease is not None and current_lease["expiresAt"] <= now:
                state.sensor_task_leases.pop(sensor_id, None)
                current_lease = None
            if (
                current_lease is not None
                and current_lease["taskId"] != task.taskId
                and task.priority <= current_lease["priority"]
            ):
                effective_permit = False
                effective_reason = "SENSOR_PRIORITY_HELD"
                effective_detail = (
                    f"sensor reserved by task {current_lease['taskId']} at priority "
                    f"{current_lease['priority']} until "
                    f"{current_lease['expiresAt'].isoformat()}"
                )
                denial_status = 409
            else:
                preempted = current_lease["taskId"] if current_lease is not None else None
                state.sensor_task_leases[sensor_id] = {
                    "taskId": task.taskId,
                    "requestId": request_id,
                    "priority": task.priority,
                    "expiresAt": task.expiresAt,
                    "principal": principal,
                }
                lease_claimed = True
                if preempted:
                    effective_detail = (
                        f"{effective_detail}; higher-priority task preempted {preempted}"
                    )

    def release_task_lease() -> None:
        if not lease_claimed:
            return
        with state._sensor_arbiter_lock:
            lease = state.sensor_task_leases.get(sensor_id)
            if lease is not None and lease["taskId"] == task.taskId:
                state.sensor_task_leases.pop(sensor_id, None)

    try:
        state.record(
            AuditRecord(
                principal=principal,
                action=f"TASK_{task.taskType.value}",
                trackId=task.trackId,
                requestId=request_id,
                deliveryState="NOT_SENT",
                decision="GRANTED" if effective_permit else "DENY",
                reasonCode=effective_reason,
                detail=f"sensor={sensor_id}; {effective_detail}",
            )
        )
    except Exception:
        release_task_lease()
        with state._task_idempotency_lock:
            state.task_inflight.discard(request_id)
        raise
    if not effective_permit:
        response.status_code = denial_status
        result = {
            "taskId": task.taskId,
            "requestId": request_id,
            "granted": False,
            "deliveryState": "NOT_SENT",
            "reason": effective_detail,
            "httpStatus": denial_status,
        }
        state.task_results[request_id] = (fingerprint, result)
        with state._task_idempotency_lock:
            state.task_inflight.discard(request_id)
        return {key: value for key, value in result.items() if key != "httpStatus"}

    try:
        delivery = await state.bus.publish_outcome(
            subj_sensor_task(sensor_id),
            _envelope("SensorTask", task.model_dump(mode="json", exclude_none=True)),
        )
    except Exception:
        release_task_lease()
        with state._task_idempotency_lock:
            state.task_inflight.discard(request_id)
        raise
    if delivery is not PublishOutcome.BROKER_ACCEPTED:
        response.status_code = 503
        result = {
            "taskId": task.taskId,
            "requestId": request_id,
            "granted": False,
            "deliveryState": delivery.value,
            "reason": (
                "task was not sent; retry with the same request ID"
                if delivery is PublishOutcome.NOT_SENT
                else "task delivery is unknown; do not issue a new request ID until reconciled"
            ),
            "httpStatus": 503,
        }
        try:
            state.record(
                AuditRecord(
                    principal=principal,
                    action="TASK_DELIVERY_FAILED",
                    trackId=task.trackId,
                    requestId=request_id,
                    deliveryState=delivery.value,
                    decision="DENY",
                    reasonCode=delivery.value,
                    detail=f"sensor={sensor_id}",
                )
            )
        except Exception:
            with state._task_idempotency_lock:
                state.task_inflight.discard(request_id)
            raise
        if delivery is PublishOutcome.NOT_SENT:
            release_task_lease()
    else:
        result = {
            "taskId": task.taskId,
            "requestId": request_id,
            "granted": True,
            "deliveryState": "BROKER_ACCEPTED",
            "reason": "authorized; broker accepted task (reference adapter logs execution; task ACK contract is not yet implemented)",
            "httpStatus": 202,
        }
        try:
            state.record(
                AuditRecord(
                    principal=principal,
                    action="TASK_TRANSPORT_ACCEPTED",
                    trackId=task.trackId,
                    requestId=request_id,
                    deliveryState=delivery.value,
                    decision="INFO",
                    reasonCode="OK",
                    detail=f"sensor={sensor_id}",
                )
            )
        except Exception:
            with state._task_idempotency_lock:
                state.task_inflight.discard(request_id)
            raise
    # Transport failure is deliberately retriable with the same request ID;
    # caching it forever would turn a transient outage into a permanent denial.
    if result["httpStatus"] != 503 or delivery is PublishOutcome.DELIVERY_UNKNOWN:
        state.task_results[request_id] = (fingerprint, result)
    with state._task_idempotency_lock:
        state.task_inflight.discard(request_id)
    return {key: value for key, value in result.items() if key != "httpStatus"}


# --- engagement (Imperatives 5; gated by docs/05) ---------------------------


@app.get("/engagements", tags=["engagement"], response_model=list[EngagementStatus])
async def list_engagements():
    return list(state.engagements.values())


@app.post("/engagements", tags=["engagement"], status_code=202, response_model=EngagementStatus)
async def request_engagement(
    req: EngagementRequest,
    response: Response,
    x_operator_role: str = Header(..., alias="X-Operator-Role"),
    x_operator_id: str | None = Header(default=None, alias="X-Operator-Id"),
    x_request_id: str | None = Header(default=None, alias="X-Request-ID"),
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
):
    _ensure_audit_healthy()
    role, principal = _session(x_operator_role, x_operator_id)
    state.expire_materiel()
    _validated_identifier(req.trackId, "track ID")
    _validated_subject_token(req.effectorId, "effector ID")
    supplied_request_id = x_request_id or idempotency_key
    request_id = _validated_identifier(supplied_request_id or f"REQ-{uuid4().hex}", "request ID")
    response.headers["X-Request-ID"] = request_id
    fingerprint = _request_fingerprint(
        "engagement",
        {
            "principal": principal,
            "role": role.value,
            "request": req.model_dump(mode="json"),
        },
    )

    track = state.cop.get(req.trackId)
    effector = state.effectors.get(req.effectorId)
    if track is None:
        raise HTTPException(
            status_code=404,
            detail=f"track {req.trackId} not in COP (absent or stale)",
        )
    if effector is None:
        raise HTTPException(status_code=404, detail=f"effector {req.effectorId} not registered")

    with state._engagement_idempotency_lock:
        previous = state.engagement_requests.get(request_id)
        if previous is not None:
            old_fingerprint, old_engagement_id = previous
            if old_fingerprint != fingerprint:
                raise HTTPException(
                    status_code=409,
                    detail="request ID was already used for a different engagement",
                )
            if not old_engagement_id:
                raise HTTPException(
                    status_code=409,
                    detail="request ID is already in progress; reconcile with the same ID",
                )
            status = state.engagements[old_engagement_id]
            response.status_code = _status_http_code(status)
            response.headers["Idempotent-Replay"] = "true"
            return status
        state.engagement_requests[request_id] = (fingerprint, "")

    async with state.engagement_lock_for(req.effectorId):
        engagement_id = f"ENG-{uuid4().hex[:10]}"
        proposed = EngagementStatus(
            engagementId=engagement_id,
            effectorId=req.effectorId,
            trackId=req.trackId,
            state=EngagementState.PROPOSED,
            sequence=1,
            terminal=False,
            reasonCode="OK",
            detail=f"request {request_id} received; gates pending",
        )
        state.transition(proposed)
        with state._engagement_idempotency_lock:
            state.engagement_requests[request_id] = (fingerprint, engagement_id)
        try:
            state.record(
                AuditRecord(
                    principal=principal,
                    action="ENGAGEMENT_PROPOSED",
                    trackId=req.trackId,
                    engagementId=engagement_id,
                    requestId=request_id,
                    effectorId=req.effectorId,
                    lifecycleState=proposed.state.value,
                    sequence=proposed.sequence,
                    decision="INFO",
                    reasonCode="OK",
                    detail=f"engagement={engagement_id}; effector={req.effectorId}; request={request_id}",
                )
            )
        except Exception:
            state.transition(
                EngagementStatus(
                    engagementId=engagement_id,
                    effectorId=req.effectorId,
                    trackId=req.trackId,
                    state=EngagementState.FAILED,
                    sequence=2,
                    terminal=True,
                    reasonCode="INTERLOCK_BLOCKED",
                    detail="audit evidence unavailable; release inhibited before authorization",
                )
            )
            raise

        def deny(reason: str, detail: str) -> EngagementStatus:
            status = EngagementStatus(
                engagementId=engagement_id,
                effectorId=req.effectorId,
                trackId=req.trackId,
                state=EngagementState.DENIED,
                sequence=2,
                terminal=True,
                reasonCode=reason,
                detail=detail,
            )
            state.transition(status)
            state.record(
                AuditRecord(
                    principal=principal,
                    action="ENGAGEMENT_REQUEST",
                    trackId=req.trackId,
                    engagementId=engagement_id,
                    requestId=request_id,
                    effectorId=req.effectorId,
                    lifecycleState=status.state.value,
                    sequence=status.sequence,
                    deliveryState="NOT_SENT",
                    decision="DENY",
                    reasonCode=reason,
                    detail=f"engagement={engagement_id}; effector={req.effectorId}; {detail}",
                )
            )
            response.status_code = 403
            return status

        reserved_by = state.effector_reservations.get(req.effectorId)
        if reserved_by is not None:
            return deny("EFFECTOR_UNAVAILABLE", f"effector reserved by engagement {reserved_by}")

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

        order_sequence = 2
        constraints = EngagementConstraints(
            abortIfFriendlyWithinMeters=FRIENDLY_SEPARATION_METERS,
            maxEngagementSeconds=MAX_ENGAGEMENT_SECONDS,
            requireHumanConfirmation=state.roe.requireHumanInTheLoop,
            humanConfirmed=req.humanConfirmation,
        )
        constraints_payload = constraints.model_dump(mode="json", exclude_none=True)
        constraints_hash = _canonical_safety_hash(constraints_payload)
        token, claims = state.tokens.mint(
            sub=principal,
            engagementId=engagement_id,
            requestId=request_id,
            trackId=req.trackId,
            effectorId=req.effectorId,
            engagementType=req.engagementType.value,
            policyVersion=POLICY_VERSION,
            weaponsControlStatus=state.roe.weaponsControlStatus,
            trackSnapshotTimeObserved=track.timeObserved.isoformat(),
            orderSequence=str(order_sequence),
            constraintsHash=constraints_hash,
        )
        order_fields = {
            "engagementId": engagement_id,
            "requestId": request_id,
            "trackId": req.trackId,
            "effectorId": req.effectorId,
            "orderedBy": principal,
            "authorityToken": token,
            "engagementType": req.engagementType,
            "trackSnapshotTimeObserved": track.timeObserved,
        }
        order_fields["authorityTokenExpiresAt"] = datetime.fromtimestamp(
            claims["exp"], timezone.utc
        )
        order_fields["authorityTokenScope"] = {
            "engagementId": engagement_id,
            "requestId": request_id,
            "trackId": req.trackId,
            "effectorId": req.effectorId,
            "engagementType": req.engagementType.value,
            "policyVersion": POLICY_VERSION,
            "weaponsControlStatus": state.roe.weaponsControlStatus,
        }
        order_fields["orderSequence"] = order_sequence
        order_fields["constraints"] = constraints
        order = EngagementOrder(**order_fields)

        authorized = EngagementStatus(
            engagementId=engagement_id,
            effectorId=req.effectorId,
            trackId=req.trackId,
            state=EngagementState.AUTHORIZED,
            sequence=2,
            terminal=False,
            reasonCode="OK",
            detail="policy authorized; attempting transport delivery",
        )
        state.transition(authorized)
        try:
            state.record(
                AuditRecord(
                    principal=principal,
                    action="ENGAGEMENT_AUTHORIZED",
                    trackId=req.trackId,
                    engagementId=engagement_id,
                    requestId=request_id,
                    effectorId=req.effectorId,
                    lifecycleState=authorized.state.value,
                    sequence=authorized.sequence,
                    deliveryState="PENDING",
                    decision="PERMIT",
                    reasonCode="OK",
                    detail=(
                        f"engagement={engagement_id}; effector={req.effectorId}; "
                        f"request={request_id}; tokenJti={claims['jti']}; delivery=pending"
                    ),
                )
            )
        except Exception:
            state.transition(
                EngagementStatus(
                    engagementId=engagement_id,
                    effectorId=req.effectorId,
                    trackId=req.trackId,
                    state=EngagementState.FAILED,
                    sequence=3,
                    terminal=True,
                    reasonCode="INTERLOCK_BLOCKED",
                    detail="audit evidence unavailable; order was not published",
                )
            )
            raise
        state.effector_reservations[req.effectorId] = engagement_id
        delivery = await state.bus.publish_outcome(
            subj_engagement_order(req.effectorId),
            _envelope("EngagementOrder", order.model_dump(mode="json", exclude_none=True)),
        )

        # An effector on the local broker can acknowledge while publish/flush is
        # still awaiting completion. Never overwrite a newer lifecycle state.
        current = state.engagements[engagement_id]
        if current.sequence > authorized.sequence:
            if effector.magazine is not None:
                committed = min(1.0, effector.magazine.remaining)
                state.inventory_committed[engagement_id] = committed
                if current.inventoryRemaining is None:
                    effector.magazine.remaining -= committed
            ack_record = AuditRecord(
                principal=principal,
                action="ENGAGEMENT_EFFECTOR_ACKNOWLEDGED",
                trackId=req.trackId,
                engagementId=engagement_id,
                requestId=request_id,
                effectorId=req.effectorId,
                lifecycleState=current.state.value,
                sequence=current.sequence,
                deliveryState=delivery.value,
                assessmentOutcome=current.effectAssessment.outcome.value
                if current.effectAssessment is not None
                else None,
                decision="INFO",
                reasonCode="OK",
                detail=(
                    f"engagement={engagement_id}; request={request_id}; "
                    f"state={current.state.value}; transport={delivery.value}"
                ),
            )
            state.record(ack_record)
            response.status_code = _status_http_code(current)
            return current

        if delivery is PublishOutcome.NOT_SENT:
            failed = EngagementStatus(
                engagementId=engagement_id,
                effectorId=req.effectorId,
                trackId=req.trackId,
                state=EngagementState.FAILED,
                sequence=3,
                terminal=True,
                reasonCode="EFFECTOR_UNAVAILABLE",
                detail="authorized but not sent: transport was disconnected before publish",
            )
            state.transition(failed)
            state.record(
                AuditRecord(
                    principal=principal,
                    action="ENGAGEMENT_DELIVERY_FAILED",
                    trackId=req.trackId,
                    engagementId=engagement_id,
                    requestId=request_id,
                    effectorId=req.effectorId,
                    lifecycleState=failed.state.value,
                    sequence=failed.sequence,
                    deliveryState=delivery.value,
                    decision="DENY",
                    reasonCode=delivery.value,
                    detail=f"engagement={engagement_id}; effector={req.effectorId}; request={request_id}; delivery={delivery.value}",
                )
            )
            response.status_code = 503
            return failed

        if delivery is PublishOutcome.DELIVERY_UNKNOWN:
            # The bytes may have reached the effector before flush failed. Keep
            # the reservation and AUTHORIZED lifecycle state until ACK or timeout;
            # issuing another engagement could duplicate an effect.
            authorized.detail = (
                "transport delivery unknown; reservation retained pending "
                "effector acknowledgement or reconciliation timeout"
            )
            state.delivery_unknown.add(engagement_id)
            state.record(
                AuditRecord(
                    principal=principal,
                    action="ENGAGEMENT_DELIVERY_UNKNOWN",
                    trackId=req.trackId,
                    engagementId=engagement_id,
                    requestId=request_id,
                    effectorId=req.effectorId,
                    lifecycleState=authorized.state.value,
                    sequence=authorized.sequence,
                    deliveryState=delivery.value,
                    decision="INFO",
                    reasonCode=delivery.value,
                    detail=f"engagement={engagement_id}; effector={req.effectorId}; request={request_id}",
                )
            )
            response.status_code = 503
            return authorized

        # Broker acceptance commits one notional inventory unit.  The reservation
        # remains until a terminal effector status is received.
        if effector.magazine is not None:
            committed = min(1.0, effector.magazine.remaining)
            effector.magazine.remaining -= committed
            state.inventory_committed[engagement_id] = committed

        authorized.detail = "broker accepted order; awaiting effector acknowledgement"
        transport_record = AuditRecord(
            principal=principal,
            action="ENGAGEMENT_TRANSPORT_ACCEPTED",
            trackId=req.trackId,
            engagementId=engagement_id,
            requestId=request_id,
            effectorId=req.effectorId,
            lifecycleState=authorized.state.value,
            sequence=authorized.sequence,
            deliveryState=delivery.value,
            decision="INFO",
            reasonCode="OK",
            detail=(
                f"engagement={engagement_id}; effector={req.effectorId}; "
                f"request={request_id}; tokenJti={claims['jti']}; brokerAccepted=true"
            ),
        )
        state.record(transport_record)
        # Audit stream publication is best-effort and does not change the truthful
        # delivery state of the already published fire order.
        await state.bus.publish(
            subj_audit("engagement"),
            _envelope("AuditRecord", transport_record.model_dump(mode="json", exclude_none=True)),
        )
        return authorized


@app.post(
    "/engagements/{engagement_id}/abort",
    tags=["engagement"],
    status_code=202,
    response_model=EngagementControlReceipt,
)
async def abort_engagement(
    engagement_id: str,
    request: EngagementAbortRequest,
    response: Response,
    x_operator_role: str = Header(..., alias="X-Operator-Role"),
    x_operator_id: str | None = Header(default=None, alias="X-Operator-Id"),
    x_request_id: str | None = Header(default=None, alias="X-Request-ID"),
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
) -> EngagementControlReceipt:
    """Issue a T0 abort directive without falsely declaring the effect aborted."""
    _ensure_audit_healthy()
    _validated_identifier(engagement_id, "engagement ID")
    role, principal = _session(x_operator_role, x_operator_id)
    request_id = _validated_identifier(
        x_request_id or idempotency_key or f"REQ-ABORT-{uuid4().hex}",
        "request ID",
    )
    response.headers["X-Request-ID"] = request_id
    fingerprint = _request_fingerprint(
        "engagement-abort",
        {
            "engagementId": engagement_id,
            "principal": principal,
            "role": role.value,
            "request": request.model_dump(mode="json"),
        },
    )

    async with state.abort_lock_for(engagement_id):
        with state._control_idempotency_lock:
            prior = state.control_requests.get(request_id)
            if prior is not None:
                old_fingerprint, receipt, http_status = prior
                if old_fingerprint != fingerprint:
                    raise HTTPException(
                        status_code=409,
                        detail="request ID was already used for a different control directive",
                    )
                response.status_code = http_status
                response.headers["Idempotent-Replay"] = "true"
                return receipt

        current = state.engagements.get(engagement_id)
        if current is None:
            raise HTTPException(status_code=404, detail=f"engagement {engagement_id} not found")
        if current.state in _TERMINAL_STATES:
            raise HTTPException(
                status_code=409,
                detail=f"engagement is already terminal ({current.state.value})",
            )
        if current.trackId is None:
            raise HTTPException(status_code=409, detail="engagement has no immutable track scope")
        if role not in ROLES_MAY_RELEASE:
            raise HTTPException(status_code=403, detail="role may not issue an engagement abort")
        if state.roe.requireHumanInTheLoop and not request.humanConfirmation:
            raise HTTPException(
                status_code=403,
                detail="human-in-the-loop confirmation required for abort directive",
            )
        pending_request = state.abort_pending.get(engagement_id)
        if pending_request is not None:
            raise HTTPException(
                status_code=409,
                detail=f"abort directive {pending_request} is already awaiting acknowledgement",
            )

        directive_sequence = current.sequence + 1
        reason_hash = hashlib.sha256(request.reason.encode()).hexdigest()
        token, claims = state.tokens.mint(
            sub=principal,
            engagementId=engagement_id,
            requestId=request_id,
            trackId=current.trackId,
            effectorId=current.effectorId,
            action="ABORT",
            policyVersion=POLICY_VERSION,
            weaponsControlStatus=state.roe.weaponsControlStatus,
            directiveSequence=str(directive_sequence),
            reasonHash=reason_hash,
        )
        directive = EngagementControlDirective(
            engagementId=engagement_id,
            requestId=request_id,
            trackId=current.trackId,
            effectorId=current.effectorId,
            reason=request.reason,
            orderedBy=principal,
            authorityToken=token,
            authorityTokenExpiresAt=datetime.fromtimestamp(claims["exp"], timezone.utc),
            directiveSequence=directive_sequence,
        )
        with state._control_idempotency_lock:
            if request_id in state.control_inflight:
                raise HTTPException(
                    status_code=409,
                    detail="control request ID is already in progress; reconcile with the same ID",
                )
            # A different engagement may have completed the same key while this
            # request was validating under its own per-engagement lock.
            prior = state.control_requests.get(request_id)
            if prior is not None:
                old_fingerprint, receipt, http_status = prior
                if old_fingerprint != fingerprint:
                    raise HTTPException(
                        status_code=409,
                        detail="request ID was already used for a different control directive",
                    )
                response.status_code = http_status
                response.headers["Idempotent-Replay"] = "true"
                return receipt
            state.control_inflight.add(request_id)
        # Mark pending before the await so an immediate ABORTED callback can
        # clear it rather than being followed by a stale pending marker.
        state.abort_pending[engagement_id] = request_id
        state.abort_ack_timeouts.discard(engagement_id)
        try:
            delivery = await state.bus.publish_outcome(
                subj_engagement_control(current.effectorId),
                _envelope(
                    "EngagementControlDirective",
                    directive.model_dump(mode="json", exclude_none=True),
                ),
            )
        except Exception:
            state.abort_pending.pop(engagement_id, None)
            with state._control_idempotency_lock:
                state.control_inflight.discard(request_id)
            raise
        latest = state.engagements[engagement_id]
        if latest.state in _TERMINAL_STATES:
            aborted = latest.state == EngagementState.ABORTED
            receipt = EngagementControlReceipt(
                engagementId=engagement_id,
                requestId=request_id,
                accepted=aborted,
                deliveryState=(
                    "EFFECTOR_ACKNOWLEDGED"
                    if aborted
                    else "TERMINAL_WITHOUT_ABORT_ACK"
                ),
                lifecycleState=latest.state,
                detail=(
                    "effector acknowledged ABORTED during command delivery"
                    if aborted
                    else f"engagement became {latest.state.value} before an ABORTED acknowledgement"
                ),
            )
            http_status = 202 if aborted else 409
            _store_control_result(request_id, (fingerprint, receipt, http_status))
            response.status_code = http_status
            return receipt

        if delivery is PublishOutcome.NOT_SENT:
            state.abort_pending.pop(engagement_id, None)
            receipt = EngagementControlReceipt(
                engagementId=engagement_id,
                requestId=request_id,
                accepted=False,
                deliveryState=delivery.value,
                lifecycleState=current.state,
                detail="abort was not sent; lifecycle unchanged",
            )
            _store_control_result(request_id, (fingerprint, receipt, 503))
            state.record(
                AuditRecord(
                    principal=principal,
                    action="ENGAGEMENT_ABORT_DELIVERY_FAILED",
                    trackId=current.trackId,
                    engagementId=engagement_id,
                    requestId=request_id,
                    effectorId=current.effectorId,
                    lifecycleState=current.state.value,
                    sequence=current.sequence,
                    deliveryState=delivery.value,
                    decision="DENY",
                    reasonCode=delivery.value,
                    detail=f"engagement={engagement_id}; request={request_id}; delivery={delivery.value}",
                )
            )
            response.status_code = 503
            return receipt

        if delivery is PublishOutcome.DELIVERY_UNKNOWN:
            receipt = EngagementControlReceipt(
                engagementId=engagement_id,
                requestId=request_id,
                accepted=False,
                deliveryState=delivery.value,
                lifecycleState=current.state,
                detail="abort delivery unknown; pending marker retained for acknowledgement/reconciliation",
            )
            _store_control_result(request_id, (fingerprint, receipt, 503))
            state.record(
                AuditRecord(
                    principal=principal,
                    action="ENGAGEMENT_ABORT_DELIVERY_UNKNOWN",
                    trackId=current.trackId,
                    engagementId=engagement_id,
                    requestId=request_id,
                    effectorId=current.effectorId,
                    lifecycleState=current.state.value,
                    sequence=current.sequence,
                    deliveryState=delivery.value,
                    decision="INFO",
                    reasonCode=delivery.value,
                    detail=f"engagement={engagement_id}; request={request_id}",
                )
            )
            response.status_code = 503
            return receipt

        receipt = EngagementControlReceipt(
            engagementId=engagement_id,
            requestId=request_id,
            accepted=True,
            deliveryState="BROKER_ACCEPTED_AWAITING_EFFECTOR_ACK",
            lifecycleState=current.state,
            detail="abort directive accepted by transport; awaiting ABORTED acknowledgement",
        )
        _store_control_result(request_id, (fingerprint, receipt, 202))
        state.record(
            AuditRecord(
                principal=principal,
                action="ENGAGEMENT_ABORT_DIRECTIVE",
                trackId=current.trackId,
                engagementId=engagement_id,
                requestId=request_id,
                effectorId=current.effectorId,
                lifecycleState=current.state.value,
                sequence=current.sequence,
                deliveryState="BROKER_ACCEPTED",
                decision="PERMIT",
                reasonCode="OK",
                detail=(
                    f"engagement={engagement_id}; request={request_id}; "
                    f"tokenJti={claims['jti']}; brokerAccepted=true"
                ),
            )
        )
        return receipt


@app.get(
    "/engagements/{engagement_id}",
    tags=["engagement"],
    response_model=EngagementStatus,
)
async def get_engagement(engagement_id: str) -> EngagementStatus:
    _validated_identifier(engagement_id, "engagement ID")
    status = state.engagements.get(engagement_id)
    if status is None:
        raise HTTPException(status_code=404, detail=f"engagement {engagement_id} not found")
    return status


@app.get(
    "/engagements/{engagement_id}/history",
    tags=["engagement"],
    response_model=list[EngagementStatus],
)
async def get_engagement_history(engagement_id: str) -> list[EngagementStatus]:
    _validated_identifier(engagement_id, "engagement ID")
    history = state.engagement_history.get(engagement_id)
    if history is None:
        raise HTTPException(status_code=404, detail=f"engagement {engagement_id} not found")
    return history


@app.post(
    "/authority/tokens/consume",
    tags=["authority"],
    response_model=AuthorityTokenValidationResult,
)
async def consume_authority_token(
    request: AuthorityTokenValidationRequest,
    response: Response,
    x_effector_id: str = Header(..., alias="X-Effector-Id"),
) -> AuthorityTokenValidationResult:
    """Demonstration verifier for an effector's one-use scoped token check.

    The header is deliberately not represented as production identity.  A fielded
    deployment authenticates the effector workload with mTLS/workload identity and
    performs this verification locally with trusted key material.
    """
    _ensure_audit_healthy()
    effector_id = _validated_subject_token(x_effector_id, "effector identity")
    if effector_id != request.effectorId:
        response.status_code = 403
        return AuthorityTokenValidationResult(valid=False, reasonCode="EFFECTOR_IDENTITY_MISMATCH")
    result = state.tokens.consume(
        request.token,
        {
            "engagementId": request.engagementId,
            "trackId": request.trackId,
            "effectorId": request.effectorId,
            "engagementType": request.engagementType.value,
            "policyVersion": POLICY_VERSION,
            "weaponsControlStatus": state.roe.weaponsControlStatus,
        },
    )
    if not result.valid or result.claims is None:
        response.status_code = 403
        state.record(
            AuditRecord(
                principal=effector_id,
                action="AUTHORITY_TOKEN_REJECTED",
                trackId=request.trackId,
                engagementId=request.engagementId,
                effectorId=request.effectorId,
                decision="DENY",
                reasonCode=result.reason,
                detail=f"engagement={request.engagementId}",
            )
        )
        return AuthorityTokenValidationResult(valid=False, reasonCode=result.reason)

    current = state.engagements.get(request.engagementId)
    if current is None or current.state != EngagementState.AUTHORIZED:
        response.status_code = 409
        return AuthorityTokenValidationResult(valid=False, reasonCode="INVALID_ENGAGEMENT_STATE")
    accepted = EngagementStatus(
        engagementId=current.engagementId,
        effectorId=current.effectorId,
        trackId=current.trackId,
        state=EngagementState.ACCEPTED,
        sequence=current.sequence + 1,
        terminal=False,
        reasonCode="OK",
        detail="authority token verified and consumed by effector",
    )
    state.transition(accepted)
    state.record(
        AuditRecord(
            principal=effector_id,
            action="AUTHORITY_TOKEN_CONSUMED",
            trackId=request.trackId,
            engagementId=request.engagementId,
            effectorId=request.effectorId,
            lifecycleState=accepted.state.value,
            sequence=accepted.sequence,
            decision="PERMIT",
            reasonCode="OK",
            detail=f"engagement={request.engagementId}; tokenJti={result.claims['jti']}",
        )
    )
    return AuthorityTokenValidationResult(
        valid=True,
        reasonCode="OK",
        engagementId=request.engagementId,
        expiresAt=datetime.fromtimestamp(result.claims["exp"], timezone.utc),
    )


# --- audit ------------------------------------------------------------------


@app.get("/audit", tags=["audit"], response_model=list[AuditRecord])
async def get_audit():
    return state.audit


def _parse_role(value: str) -> Role:
    try:
        return Role(value)
    except ValueError:
        raise HTTPException(status_code=400, detail=f"unknown operator role: {value}")
