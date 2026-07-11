"""Contract and deterministic-behavior tests for the notional edge simulator."""
from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import json
import math
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

from jsonschema import FormatChecker, validate


ROOT = Path(__file__).resolve().parent.parent
SIMULATOR = ROOT / "services" / "sensor-sim"
SCHEMAS = ROOT / "specs" / "schemas"
if str(SIMULATOR) not in sys.path:
    sys.path.insert(0, str(SIMULATOR))

from simulation import (  # noqa: E402
    DEFAULT_ASSET_LAT,
    DEFAULT_ASSET_LON,
    AssetConfig,
    AuthorityTokenVerifier,
    BusEnvelopeVerifier,
    EffectorModel,
    EngagementSimulator,
    Scenario,
    SensorSpec,
    SimulationClock,
    Track,
)


SECRET = "cuas-local-reference-key-not-for-production"
START = datetime(2026, 1, 1, tzinfo=timezone.utc)


def schema(name: str) -> dict:
    return json.loads((SCHEMAS / name).read_text())


def compact_token(claims: dict, secret: str = SECRET) -> str:
    key = secret.encode()
    if len(key) < 32:
        key = hashlib.sha256(key).digest()
    payload = json.dumps(claims, sort_keys=True, separators=(",", ":")).encode()
    encoded = base64.urlsafe_b64encode(payload).rstrip(b"=").decode()
    signature = base64.urlsafe_b64encode(
        hmac.new(key, f"v1.{encoded}".encode(), hashlib.sha256).digest()
    ).rstrip(b"=").decode()
    return f"v1.{encoded}.{signature}"


def fire_order(scenario: Scenario, track_id: str, engagement_id: str = "ENG-001") -> tuple[dict, dict]:
    track = scenario.tracks[track_id]
    assert track.last_observed is not None
    now_epoch = int(scenario.clock.now.timestamp())
    request_id = f"REQ-{engagement_id}"
    order_sequence = 1
    constraints = {
        "abortIfFriendlyWithinMeters": 150,
        "maxEngagementSeconds": 30,
        "requireHumanConfirmation": True,
        "humanConfirmed": True,
    }
    constraints_hash = hashlib.sha256(
        json.dumps(constraints, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    claims = {
        "jti": f"JTI-{engagement_id}",
        "iss": "C2-NODE-01",
        "sub": "operator-alpha",
        "engagementId": engagement_id,
        "requestId": request_id,
        "trackId": track_id,
        "effectorId": "EFF-EW-01",
        "engagementType": "EW_DEFEAT",
        "policyVersion": "reference-1",
        "weaponsControlStatus": "WEAPONS_TIGHT",
        "trackSnapshotTimeObserved": track.last_observed.isoformat(),
        "orderSequence": str(order_sequence),
        "constraintsHash": constraints_hash,
        "iat": now_epoch,
        "exp": now_epoch + 20,
    }
    order = {
        "engagementId": engagement_id,
        "requestId": request_id,
        "trackId": track_id,
        "effectorId": "EFF-EW-01",
        "orderedBy": "operator-alpha",
        "authorityToken": compact_token(claims),
        "authorityTokenExpiresAt": datetime.fromtimestamp(claims["exp"], timezone.utc).isoformat(),
        "authorityTokenScope": {
            name: claims[name]
            for name in (
                "engagementId",
                "requestId",
                "trackId",
                "effectorId",
                "engagementType",
                "policyVersion",
                "weaponsControlStatus",
            )
        },
        "orderSequence": order_sequence,
        "engagementType": "EW_DEFEAT",
        "constraints": constraints,
        "trackSnapshotTimeObserved": claims["trackSnapshotTimeObserved"],
        "timeOrdered": scenario.clock.now.isoformat(),
    }
    return order, claims


def emitting_hostile(scenario: Scenario) -> Track:
    for track in scenario.tracks.values():
        if track.identity == "HOSTILE" and track.emitter_state == "EMITTING":
            return track
    raise AssertionError("seeded scenario should contain an emitting hostile")


def test_default_and_environment_asset_configuration_align_with_north_island():
    assert (DEFAULT_ASSET_LAT, DEFAULT_ASSET_LON) == (32.699, -117.215)
    configured = AssetConfig.from_env({"SIM_ASSET_LAT": "32.7", "SIM_ASSET_LON": "-117.21"})
    assert (configured.lat, configured.lon) == (32.7, -117.21)


def test_seeded_scenario_and_single_clock_are_reproducible():
    left = Scenario(seed=71, start_time=START)
    right = Scenario(seed=71, start_time=START)
    for dt in (0.5, 0.5, 1.0, 0.25):
        left.tick(dt)
        right.tick(dt)
    assert left.clock.tick_sequence == right.clock.tick_sequence == 4
    assert left.clock.now == right.clock.now
    assert left.track_payloads() == right.track_payloads()


def test_multisensor_uncertainty_coasts_and_rf_silent_tracks_exclude_rf():
    clock = SimulationClock(START)
    sensors = (
        SensorSpec("RAD", "RADAR", 0, 0, 1000, 30, 40, 100),
        SensorSpec("RF", "RF", 0, 0, 1000, 50, 80, 250),
    )
    track = Track(
        track_id="TRK-X",
        identity="HOSTILE",
        x=20,
        y=20,
        speed=0,
        track_quality=2,
        classification="MULTIROTOR",
        heading=0,
        emitter_state="SILENT",
    )
    clock.advance(0.5)
    track.observe(sensors, clock, 0.5, set())
    assert track.contributing_sensors == ["RAD"]
    first_sigma = track.horizontal_sigma_meters
    first_age = track.data_age_seconds(clock.now)
    clock.advance(1.0)
    track.observe((), clock, 1.0, set())
    assert track.fusion_state == "COASTING"
    assert track.horizontal_sigma_meters > first_sigma
    assert track.data_age_seconds(clock.now) > first_age


def test_tasking_changes_next_observation_not_track_quality_directly():
    tasked = Scenario(seed=41, start_time=START)
    baseline = Scenario(seed=41, start_time=START)
    tasked.tick(0.5)
    baseline.tick(0.5)
    target = emitting_hostile(tasked)
    peer = baseline.tracks[target.track_id]
    result = tasked.task(target.track_id, "SEN-RAD-01")
    assert result == (target.track_quality, target.track_quality)
    tasked.tick(0.5)
    baseline.tick(0.5)
    assert target.horizontal_sigma_meters < peer.horizontal_sigma_meters
    assert target.track_quality >= peer.track_quality


def test_simulator_track_payload_conforms_and_has_explicit_datum_and_age():
    from app.models import Track as CanonicalTrack

    scenario = Scenario(seed=11, start_time=START)
    scenario.tick(0.5)
    payload = emitting_hostile(scenario).payload(scenario.asset, scenario.clock, scenario.ttl_seconds)
    validate(payload, schema("track.schema.json"), format_checker=FormatChecker())
    CanonicalTrack.model_validate(payload)
    assert payload["kinematics"]["position"]["altitudeReference"] == "MSL"
    assert payload["covariance"]["confidenceLevel"] == "ONE_SIGMA"
    assert payload["observationSequence"] == 1
    assert payload["dataAgeSeconds"] >= 0
    assert payload["modelProvenance"].startswith("NOTIONAL")


def test_authority_token_enforces_signature_expiry_scope_and_replay():
    scenario = Scenario(seed=13, start_time=START)
    scenario.tick(0.5)
    target = emitting_hostile(scenario)
    order, claims = fire_order(scenario, target.track_id)
    verifier = AuthorityTokenVerifier(SECRET, issuer="C2-NODE-01")

    valid = verifier.validate(order, scenario.clock.now)
    assert valid.valid
    verifier.consume(valid)
    assert verifier.validate(order, scenario.clock.now).reason == "DUPLICATE_ORDER"

    wrong_scope = dict(order, engagementId="ENG-DIFFERENT")
    fresh_verifier = AuthorityTokenVerifier(SECRET, issuer="C2-NODE-01")
    assert fresh_verifier.validate(wrong_scope, scenario.clock.now).reason == "TOKEN_SCOPE_MISMATCH"

    expired_claims = dict(claims, jti="JTI-EXPIRED", exp=int(scenario.clock.now.timestamp()))
    expired = dict(order, authorityToken=compact_token(expired_claims))
    assert fresh_verifier.validate(expired, scenario.clock.now).reason == "TOKEN_EXPIRED"


def test_real_c2_order_json_timestamp_normalization_verifies_at_effector():
    """Exercise the actual issuer -> Pydantic wire JSON -> effector seam."""
    from app.models import AuthorityTokenScope, EngagementOrder
    from app.tokens import AuthorityTokenIssuer

    observed = datetime.now(timezone.utc).replace(microsecond=123456)
    issuer = AuthorityTokenIssuer(SECRET, "C2-NODE-01", ttl_seconds=20)
    constraints = {
        "abortIfFriendlyWithinMeters": 150,
        "maxEngagementSeconds": 60,
        "requireHumanConfirmation": True,
        "humanConfirmed": True,
    }
    constraints_hash = hashlib.sha256(
        json.dumps(constraints, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    token, claims = issuer.mint(
        sub="FCA-01",
        engagementId="ENG-WIRE-001",
        requestId="REQ-WIRE-001",
        trackId="TRK-WIRE-001",
        effectorId="EFF-EW-01",
        engagementType="EW_DEFEAT",
        policyVersion="DEMO-ROE-1",
        weaponsControlStatus="WEAPONS_TIGHT",
        trackSnapshotTimeObserved=observed.isoformat(),
        orderSequence="2",
        constraintsHash=constraints_hash,
    )
    order = EngagementOrder(
        engagementId="ENG-WIRE-001",
        requestId="REQ-WIRE-001",
        trackId="TRK-WIRE-001",
        effectorId="EFF-EW-01",
        orderedBy="FCA-01",
        authorityToken=token,
        authorityTokenExpiresAt=datetime.fromtimestamp(claims["exp"], timezone.utc),
        authorityTokenScope=AuthorityTokenScope(
            engagementId="ENG-WIRE-001",
            requestId="REQ-WIRE-001",
            trackId="TRK-WIRE-001",
            effectorId="EFF-EW-01",
            engagementType="EW_DEFEAT",
            policyVersion="DEMO-ROE-1",
            weaponsControlStatus="WEAPONS_TIGHT",
        ),
        orderSequence=2,
        engagementType="EW_DEFEAT",
        constraints=constraints,
        trackSnapshotTimeObserved=observed,
    )
    wire = json.loads(order.model_dump_json(exclude_none=True))
    assert claims["trackSnapshotTimeObserved"].endswith("+00:00")
    assert wire["trackSnapshotTimeObserved"].endswith("Z")
    result = AuthorityTokenVerifier(SECRET, issuer="C2-NODE-01").validate(
        wire,
        datetime.now(timezone.utc),
    )
    assert result.valid, result.reason


def test_engagement_reports_ordered_lifecycle_inventory_and_explicit_bda():
    from app.models import EffectorStatus, EngagementOrder, EngagementStatus

    scenario = Scenario(seed=17, start_time=START)
    scenario.tick(0.5)
    target = emitting_hostile(scenario)
    target.emitter_state = "EMITTING"
    order, _ = fire_order(scenario, target.track_id)
    validate(order, schema("engagement-order.schema.json"), format_checker=FormatChecker())
    EngagementOrder.model_validate(order)
    effector = EffectorModel("EFF-EW-01", capacity=2, remaining=2)
    simulator = EngagementSimulator(
        effector,
        AuthorityTokenVerifier(SECRET, issuer="C2-NODE-01"),
        seed=17,
    )
    reports: list[dict] = []

    async def report(payload: dict) -> None:
        reports.append(payload)

    async def advance_sleep(seconds: float) -> None:
        scenario.clock.advance(max(seconds, 0.001))

    asyncio.run(simulator.execute(order, scenario, report, advance_sleep))
    assert [item["state"] for item in reports] == ["ACCEPTED", "ACTIVE", "ASSESSING", "COMPLETE"]
    assert [item["sequence"] for item in reports] == sorted(item["sequence"] for item in reports)
    assert reports[-1]["terminal"] is True
    assert reports[-1]["effectAssessment"]["outcome"] in {
        "CONFIRMED_EFFECT",
        "NO_CONFIRMED_EFFECT",
    }
    assert effector.remaining == 1
    for payload in reports:
        validate(payload, schema("engagement-status.schema.json"), format_checker=FormatChecker())
        EngagementStatus.model_validate(payload)
    effector_payload = effector.status_payload(scenario.asset, scenario.clock.now)
    validate(
        effector_payload,
        schema("effector.schema.json"),
        format_checker=FormatChecker(),
    )
    EffectorStatus.model_validate(effector_payload)


def test_seeded_outcome_is_stable_when_c2_engagement_uuid_changes():
    outcomes = []
    for engagement_id in ("ENG-RANDOM-A", "ENG-RANDOM-B"):
        scenario = Scenario(seed=31, start_time=START)
        scenario.tick(0.5)
        target = emitting_hostile(scenario)
        order, _ = fire_order(scenario, target.track_id, engagement_id=engagement_id)
        simulator = EngagementSimulator(
            EffectorModel("EFF-EW-01"),
            AuthorityTokenVerifier(SECRET, issuer="C2-NODE-01"),
            seed=31,
        )
        reports = []

        async def report(payload: dict) -> None:
            reports.append(payload)

        async def advance_sleep(seconds: float) -> None:
            scenario.clock.advance(max(seconds, 0.001))

        asyncio.run(simulator.execute(order, scenario, report, advance_sleep))
        outcomes.append(reports[-1]["effectAssessment"]["outcome"])
    assert outcomes[0] == outcomes[1]


def test_signed_abort_is_terminal_and_suppresses_complete():
    from app.models import EngagementControlDirective, EngagementStatus

    scenario = Scenario(seed=19, start_time=START)
    scenario.tick(0.5)
    target = emitting_hostile(scenario)
    target.emitter_state = "EMITTING"
    order, _ = fire_order(scenario, target.track_id, engagement_id="ENG-ABORT")
    verifier = AuthorityTokenVerifier(SECRET, issuer="C2-NODE-01")
    simulator = EngagementSimulator(EffectorModel("EFF-EW-01"), verifier, seed=19)
    reports: list[dict] = []
    abort_sent = False

    now_epoch = int(scenario.clock.now.timestamp())
    abort_claims = {
        "jti": "JTI-ABORT",
        "iss": "C2-NODE-01",
        "sub": "operator-alpha",
        "engagementId": "ENG-ABORT",
        "requestId": "REQ-ABORT",
        "trackId": target.track_id,
        "effectorId": "EFF-EW-01",
        "action": "ABORT",
        "policyVersion": "reference-1",
        "weaponsControlStatus": "WEAPONS_TIGHT",
        "directiveSequence": "10",
        "reasonHash": hashlib.sha256(b"operator safety abort test").hexdigest(),
        "iat": now_epoch,
        "exp": now_epoch + 20,
    }
    directive = {
        "engagementId": "ENG-ABORT",
        "requestId": "REQ-ABORT",
        "trackId": target.track_id,
        "effectorId": "EFF-EW-01",
        "action": "ABORT",
        "reason": "operator safety abort test",
        "orderedBy": "operator-alpha",
        "authorityToken": compact_token(abort_claims),
        "authorityTokenExpiresAt": datetime.fromtimestamp(abort_claims["exp"], timezone.utc).isoformat(),
        "timeOrdered": scenario.clock.now.isoformat(),
        "directiveSequence": 10,
    }
    validate(directive, schema("engagement-control.schema.json"), format_checker=FormatChecker())
    EngagementControlDirective.model_validate(directive)

    async def report(payload: dict) -> None:
        reports.append(payload)

    async def aborting_sleep(seconds: float) -> None:
        nonlocal abort_sent
        scenario.clock.advance(max(seconds, 0.001))
        if not abort_sent:
            abort_sent = True
            assert simulator.request_abort(directive, scenario.clock.now).valid

    asyncio.run(simulator.execute(order, scenario, report, aborting_sleep))
    assert [item["state"] for item in reports] == ["ACCEPTED", "ABORTED"]
    assert reports[-1]["sequence"] > directive["directiveSequence"]
    assert reports[-1]["terminal"] is True
    assert reports[-1]["reasonCode"] == "OPERATOR_ABORT"
    assert reports[-1]["effectAssessment"]["outcome"] == "INDETERMINATE"
    assert target.track_id in scenario.tracks
    for payload in reports:
        EngagementStatus.model_validate(payload)


def test_invalid_coordinate_configuration_is_rejected():
    try:
        AssetConfig.from_env({"SIM_ASSET_LAT": "91", "SIM_ASSET_LON": "0"})
    except ValueError as exc:
        assert "valid WGS84" in str(exc)
    else:
        raise AssertionError("invalid latitude was accepted")


def test_course_is_normalized_to_half_open_interval():
    scenario = Scenario(seed=23, start_time=START)
    scenario.tick(0.5)
    for payload in scenario.track_payloads():
        course = payload["kinematics"]["velocity"]["courseDeg"]
        assert math.isfinite(course) and 0 <= course < 360


def signed_command_envelope(
    message_type: str,
    payload: dict,
    *,
    message_id: str = "MSG-COMMAND-1",
    source: str = "C2-NODE-01",
    created: datetime = START,
) -> bytes:
    raw = {
        "messageId": message_id,
        "schemaVersion": "1.0.0",
        "messageType": message_type,
        "source": {"nodeId": source, "componentType": "c2"},
        "classification": "UNCLASSIFIED",
        "timeCreated": created.isoformat(),
        "payload": payload,
    }
    canonical = json.dumps(
        raw, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    ).encode()
    raw["signature"] = "hmac-sha256:" + hmac.new(
        SECRET.encode(), canonical, hashlib.sha256
    ).hexdigest()
    return json.dumps(raw, separators=(",", ":")).encode()


def test_simulator_bus_envelope_verifier_binds_source_subject_freshness_and_replay():
    verifier = BusEnvelopeVerifier(SECRET, "C2-NODE-01")
    payload = {
        "taskId": "TASK-1",
        "sensorId": "SEN-RAD-01",
        "taskType": "SEARCH",
        "priority": 5,
        "requestedBy": "SM-1",
    }
    wire = signed_command_envelope("SensorTask", payload)
    assert verifier.verify(
        wire,
        expected_message_type="SensorTask",
        subject="cuas.sensor.task.SEN-RAD-01",
        subject_prefix="cuas.sensor.task",
        target_field="sensorId",
        now=START,
    ) == payload

    try:
        verifier.verify(
            wire,
            expected_message_type="SensorTask",
            subject="cuas.sensor.task.SEN-RAD-01",
            subject_prefix="cuas.sensor.task",
            target_field="sensorId",
            now=START,
        )
    except ValueError as exc:
        assert "replay" in str(exc)
    else:
        raise AssertionError("replayed command was accepted")

    wrong_source = signed_command_envelope(
        "SensorTask", payload, message_id="MSG-COMMAND-2", source="C2-ROGUE"
    )
    for invalid, subject, expected in (
        (wrong_source, "cuas.sensor.task.SEN-RAD-01", "authoritative"),
        (
            signed_command_envelope("SensorTask", payload, message_id="MSG-COMMAND-3"),
            "cuas.sensor.task.SEN-OTHER",
            "target",
        ),
        (
            signed_command_envelope(
                "SensorTask",
                payload,
                message_id="MSG-COMMAND-4",
                created=START - timedelta(seconds=31),
            ),
            "cuas.sensor.task.SEN-RAD-01",
            "timestamp",
        ),
    ):
        try:
            verifier.verify(
                invalid,
                expected_message_type="SensorTask",
                subject=subject,
                subject_prefix="cuas.sensor.task",
                target_field="sensorId",
                now=START,
            )
        except ValueError as exc:
            assert expected in str(exc)
        else:
            raise AssertionError(f"invalid command accepted: {expected}")


def test_search_volume_changes_future_observation_geometry_and_handoff_custody():
    sensor = SensorSpec("SEN-TEST-01", "RADAR", 0, 0, 10_000, 20, 30, 50)
    scenario = Scenario(seed=31, start_time=START, sensors=(sensor,))
    target = scenario.spawn_hostile(
        bearing=0,
        range_meters=2000,
        track_quality=4,
        classification="MULTIROTOR",
        speed=1,
    )
    assert scenario.set_search_volume(
        sensor.sensor_id,
        {"centerBearingDeg": 180, "widthDeg": 20, "minAltMeters": 0, "maxAltMeters": 1000},
    )
    scenario.tick(0.5)
    assert sensor.sensor_id not in target.contributing_sensors

    assert scenario.set_search_volume(
        sensor.sensor_id,
        {"centerBearingDeg": 0, "widthDeg": 30, "minAltMeters": 0, "maxAltMeters": 1000},
    )
    scenario.tick(0.5)
    assert sensor.sensor_id in target.contributing_sensors

    handoff_scenario = Scenario(seed=32, start_time=START)
    handoff_target = handoff_scenario.spawn_hostile(
        bearing=0, range_meters=1000, track_quality=6, speed=1
    )
    assert handoff_scenario.handoff(
        handoff_target.track_id, "SEN-RAD-01", "SEN-EO-03"
    )
    assert handoff_scenario.track_custody[handoff_target.track_id] == "SEN-EO-03"
