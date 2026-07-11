"""End-to-end API tests via FastAPI TestClient.

Runs without a broker: the bus connects in degraded mode (publish returns False),
which is itself the DDIL behavior we want to verify the node tolerates. Tracks are
seeded directly into the COP to stand in for the bus track stream.
"""
import asyncio
import json
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import AsyncMock

import pytest
from fastapi.testclient import TestClient

from app.bus import PublishOutcome
from app.main import (
    State,
    _bus_payload,
    _envelope,
    _on_effector_status,
    _on_engagement_status,
    _on_sensor_status,
    app,
    state,
)
from app.models import (
    AuditRecord,
    EngagementState,
    EngagementStatus,
    Identity,
    Kinematics,
    Position,
    Track,
)


def seed_track(track_id="TRK-1001", tq=12, identity=Identity.HOSTILE, lat=34.20, lon=-118.20):
    state.cop.upsert(
        Track(
            trackId=track_id,
            kinematics=Kinematics(position=Position(lat=lat, lon=lon, altMeters=120)),
            trackQuality=tq,
            identity=identity,
            timeObserved=datetime.now(timezone.utc),
            timeToLiveSeconds=300,
        )
    )


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setattr(state.bus, "connect", AsyncMock(return_value=False))
    monkeypatch.setattr(state.bus, "subscribe", AsyncMock(return_value=None))
    monkeypatch.setattr(state.bus, "close", AsyncMock(return_value=None))
    monkeypatch.setattr("app.main.ALLOW_DEMO_REGISTRATION", True)
    with TestClient(app) as c:
        # Fresh state per test.
        state.sensors.clear()
        state.effectors.clear()
        state.engagements.clear()
        state.engagement_history.clear()
        state.engagement_requests.clear()
        state.effector_reservations.clear()
        state.inventory_committed.clear()
        state.task_results.clear()
        state.task_inflight.clear()
        state.sensor_task_leases.clear()
        state.engagement_locks.clear()
        state.abort_locks.clear()
        state.control_requests.clear()
        state.abort_pending.clear()
        state.ack_timeouts.clear()
        state.abort_ack_timeouts.clear()
        state.control_inflight.clear()
        state.delivery_unknown.clear()
        state.materiel_stale.clear()
        state.seen_bus_messages.clear()
        state.audit.clear()
        state.audit_healthy = True
        state.audit_error = None
        state.no_fire_zones = []
        # Most API tests exercise the broker-accepted path without requiring a
        # local NATS process. Dedicated tests below assert truthful failure when
        # publish returns False; transport behavior itself is covered by test_bus.
        monkeypatch.setattr(state.bus, "publish", AsyncMock(return_value=True))
        monkeypatch.setattr(
            state.bus,
            "publish_outcome",
            AsyncMock(return_value=PublishOutcome.BROKER_ACCEPTED),
        )
        registered_at = datetime.now(timezone.utc).isoformat()
        c.post(
            "/sensors",
            headers={"X-Component-Id": "SEN-RAD-01"},
            json={
                "sensorId": "SEN-RAD-01",
                "sensorType": "RADAR",
                "readiness": "READY",
                "taskable": True,
                "timeReported": registered_at,
            },
        )
        c.post(
            "/effectors",
            headers={"X-Component-Id": "EFF-EW-01"},
            json={
                "effectorId": "EFF-EW-01",
                "effectorType": "EW_JAMMER",
                "readiness": "READY",
                "timeReported": registered_at,
                "magazine": {"remaining": 100, "capacity": 100, "unit": "seconds"},
                "engagementEnvelope": {
                    "location": {"lat": 34.20, "lon": -118.20, "altMeters": 0},
                    "maxRangeMeters": 8000,
                    "maxAltMeters": 1500,
                },
            },
        )
        yield c


def test_ui_served_with_backend_flag(client):
    # c2-core serves the web COP at / and injects the LIVE backend flag so the
    # same page that runs an embedded sim standalone drives the real bus here.
    r = client.get("/")
    assert r.status_code == 200
    assert "text/html" in r.headers["content-type"]
    assert "__CUAS_BACKEND__=true" in r.text
    assert 'id="cuas"' in r.text


def test_health_endpoint_serves(client):
    # The node serves REST regardless of bus state (degraded-tolerant); we don't
    # assert a specific busConnected value because a broker may or may not be up in
    # the environment. The deterministic DDIL behavior is covered in test_bus.py.
    body = client.get("/health").json()
    assert body["nodeId"] == "C2-NODE-01"
    assert body["schemaVersion"] == "1.0.0"
    assert body["status"] in ("ok", "degraded")
    assert isinstance(body["busConnected"], bool)


def test_engage_hostile_high_tq_authorized(client):
    seed_track(tq=12, identity=Identity.HOSTILE)
    r = client.post(
        "/engagements",
        headers={"X-Operator-Role": "FIRE_CONTROL_AUTHORITY"},
        json={"trackId": "TRK-1001", "effectorId": "EFF-EW-01", "engagementType": "EW_DEFEAT", "humanConfirmation": True},
    )
    assert r.status_code == 202, r.text
    assert r.json()["state"] == "AUTHORIZED"


def test_engage_low_tq_denied(client):
    seed_track(tq=6, identity=Identity.HOSTILE)
    r = client.post(
        "/engagements",
        headers={"X-Operator-Role": "FIRE_CONTROL_AUTHORITY"},
        json={"trackId": "TRK-1001", "effectorId": "EFF-EW-01", "engagementType": "EW_DEFEAT", "humanConfirmation": True},
    )
    assert r.status_code == 403
    assert r.json()["reasonCode"] == "TRACK_QUALITY_INSUFFICIENT"


def test_engage_friend_denied(client):
    seed_track(track_id="TRK-2001", tq=15, identity=Identity.FRIEND)
    r = client.post(
        "/engagements",
        headers={"X-Operator-Role": "FIRE_CONTROL_AUTHORITY"},
        json={"trackId": "TRK-2001", "effectorId": "EFF-EW-01", "engagementType": "EW_DEFEAT", "humanConfirmation": True},
    )
    assert r.status_code == 403
    assert r.json()["reasonCode"] == "ROE_PROHIBITED"


def test_engage_unknown_track_404(client):
    r = client.post(
        "/engagements",
        headers={"X-Operator-Role": "FIRE_CONTROL_AUTHORITY"},
        json={"trackId": "NOPE", "effectorId": "EFF-EW-01", "engagementType": "EW_DEFEAT", "humanConfirmation": True},
    )
    assert r.status_code == 404


def test_wrong_role_denied(client):
    seed_track(tq=12, identity=Identity.HOSTILE)
    r = client.post(
        "/engagements",
        headers={"X-Operator-Role": "OBSERVER"},
        json={"trackId": "TRK-1001", "effectorId": "EFF-EW-01", "engagementType": "EW_DEFEAT", "humanConfirmation": True},
    )
    assert r.status_code == 403
    assert r.json()["reasonCode"] == "NOT_AUTHORIZED"


def test_incompatible_effector_denied(client):
    seed_track(tq=15, identity=Identity.HOSTILE)
    r = client.post(
        "/engagements",
        headers={"X-Operator-Role": "FIRE_CONTROL_AUTHORITY"},
        json={"trackId": "TRK-1001", "effectorId": "EFF-EW-01", "engagementType": "KINETIC", "humanConfirmation": True},
    )
    assert r.status_code == 403
    assert r.json()["reasonCode"] == "EFFECTOR_UNAVAILABLE"


def test_tasking_authorized_and_audited(client):
    seed_track()
    r = client.post(
        "/sensors/SEN-RAD-01/tasks",
        headers={"X-Operator-Role": "SENSOR_MANAGER"},
        json={"sensorId": "SEN-RAD-01", "taskType": "DWELL", "trackId": "TRK-1001", "priority": 7, "requestedBy": "SM-1"},
    )
    assert r.status_code == 202
    assert r.json()["granted"] is True
    actions = [a["action"] for a in client.get("/audit").json()]
    assert "TASK_DWELL" in actions


def test_tasking_observer_denied(client):
    seed_track()
    r = client.post(
        "/sensors/SEN-RAD-01/tasks",
        headers={"X-Operator-Role": "OBSERVER"},
        json={"sensorId": "SEN-RAD-01", "taskType": "DWELL", "trackId": "TRK-1001", "priority": 7, "requestedBy": "OBS-1"},
    )
    assert r.status_code == 403
    assert r.json()["granted"] is False


def test_audit_records_engagement_decision(client):
    seed_track(tq=12, identity=Identity.HOSTILE)
    client.post(
        "/engagements",
        headers={"X-Operator-Role": "FIRE_CONTROL_AUTHORITY"},
        json={"trackId": "TRK-1001", "effectorId": "EFF-EW-01", "engagementType": "EW_DEFEAT", "humanConfirmation": True},
    )
    decisions = [a["decision"] for a in client.get("/audit").json()]
    assert "PERMIT" in decisions


def test_engage_denied_over_no_fire_zone(client):
    # Collateral geometry: kinetic fires denied over the zone, soft-kill permitted.
    state.no_fire_zones = [{"lat": 34.20, "lon": -118.20, "radiusMeters": 3000, "label": "TEST CITY"}]
    client.post(
        "/effectors",
        headers={"X-Component-Id": "EFF-INT-99"},
        json={
            "effectorId": "EFF-INT-99",
            "effectorType": "KINETIC_INTERCEPTOR",
            "readiness": "READY",
            "timeReported": datetime.now(timezone.utc).isoformat(),
            "magazine": {"remaining": 8, "capacity": 8, "unit": "rounds"},
            "engagementEnvelope": {
                "location": {"lat": 34.20, "lon": -118.20, "altMeters": 0},
                "maxRangeMeters": 8000,
                "maxAltMeters": 3000,
            },
        },
    )
    seed_track(tq=15, identity=Identity.HOSTILE)   # track sits inside the zone
    r = client.post(
        "/engagements",
        headers={"X-Operator-Role": "FIRE_CONTROL_AUTHORITY"},
        json={"trackId": "TRK-1001", "effectorId": "EFF-INT-99", "engagementType": "KINETIC", "humanConfirmation": True},
    )
    assert r.status_code == 403
    assert r.json()["reasonCode"] == "COLLATERAL_HOLD"
    # the soft-kill path through the same zone is still legal
    r2 = client.post(
        "/engagements",
        headers={"X-Operator-Role": "FIRE_CONTROL_AUTHORITY"},
        json={"trackId": "TRK-1001", "effectorId": "EFF-EW-01", "engagementType": "EW_DEFEAT", "humanConfirmation": True},
    )
    assert r2.status_code == 202, r2.text


def test_authoritative_scenario_policy_and_demo_session(client):
    scenario = client.get("/scenario").json()
    assert scenario["authoritativeNodeId"] == "C2-NODE-01"
    assert scenario["areaOfOperations"]["center"]["lat"] == pytest.approx(32.699)
    assert scenario["areaOfOperations"]["center"]["lon"] == pytest.approx(-117.215)

    policy = client.get("/policy").json()
    assert policy["readOnly"] is True
    assert policy["weaponsControlStatus"] == "WEAPONS_TIGHT"
    assert policy["policyVersion"]

    session = client.get(
        "/session",
        headers={
            "X-Operator-Role": "FIRE_CONTROL_AUTHORITY",
            "X-Operator-Id": "DEMO-BC-01",
        },
    ).json()
    assert session["principal"] == "DEMO-BC-01"
    assert session["roleReadOnly"] is True
    assert session["assurance"] == "UNTRUSTED_DEMONSTRATION_ONLY"
    assert "RELEASE_ENGAGEMENT" in session["permissions"]


def test_rest_materiel_registration_is_explicitly_gated(client, monkeypatch):
    monkeypatch.setattr("app.main.ALLOW_DEMO_REGISTRATION", False)
    disabled = client.post(
        "/sensors",
        headers={"X-Component-Id": "SEN-NEW"},
        json={"sensorId": "SEN-NEW", "sensorType": "RADAR"},
    )
    assert disabled.status_code == 403

    monkeypatch.setattr("app.main.ALLOW_DEMO_REGISTRATION", True)
    impersonated = client.post(
        "/sensors",
        headers={"X-Component-Id": "BROWSER-OPERATOR"},
        json={"sensorId": "SEN-NEW", "sensorType": "RADAR"},
    )
    assert impersonated.status_code == 403


def test_engagement_delivery_failure_is_truthful_and_rolls_back_reservation(client):
    state.bus.publish_outcome = AsyncMock(return_value=PublishOutcome.NOT_SENT)
    seed_track(tq=12, identity=Identity.HOSTILE)
    before = state.effectors["EFF-EW-01"].magazine.remaining
    r = client.post(
        "/engagements",
        headers={"X-Operator-Role": "FIRE_CONTROL_AUTHORITY"},
        json={
            "trackId": "TRK-1001",
            "effectorId": "EFF-EW-01",
            "engagementType": "EW_DEFEAT",
            "humanConfirmation": True,
        },
    )
    assert r.status_code == 503
    assert r.json()["state"] == "FAILED"
    assert "not sent" in r.json()["detail"]
    assert "queued" not in r.json()["detail"]
    assert "EFF-EW-01" not in state.effector_reservations
    assert state.effectors["EFF-EW-01"].magazine.remaining == before


def test_task_delivery_failure_is_not_reported_granted(client):
    state.bus.publish_outcome = AsyncMock(return_value=PublishOutcome.NOT_SENT)
    seed_track()
    r = client.post(
        "/sensors/SEN-RAD-01/tasks",
        headers={"X-Operator-Role": "SENSOR_MANAGER", "X-Request-ID": "REQ-TASK-FAIL"},
        json={
            "sensorId": "SEN-RAD-01",
            "taskType": "DWELL",
            "trackId": "TRK-1001",
            "priority": 7,
            "requestedBy": "SM-1",
        },
    )
    assert r.status_code == 503
    assert r.json()["granted"] is False
    assert r.json()["deliveryState"] == "NOT_SENT"


def test_task_transport_failure_can_retry_same_idempotency_key(client):
    state.bus.publish_outcome = AsyncMock(
        side_effect=[PublishOutcome.NOT_SENT, PublishOutcome.BROKER_ACCEPTED]
    )
    seed_track()
    headers = {
        "X-Operator-Role": "SENSOR_MANAGER",
        "X-Operator-Id": "SM-1",
        "Idempotency-Key": "REQ-TASK-RETRY",
    }
    body = {
        "sensorId": "SEN-RAD-01",
        "taskType": "DWELL",
        "trackId": "TRK-1001",
        "priority": 7,
        "requestedBy": "SM-1",
    }
    first = client.post("/sensors/SEN-RAD-01/tasks", headers=headers, json=body)
    second = client.post("/sensors/SEN-RAD-01/tasks", headers=headers, json=body)
    assert first.status_code == 503
    assert second.status_code == 202
    assert first.json()["taskId"] == second.json()["taskId"]
    assert second.json()["deliveryState"] == "BROKER_ACCEPTED"
    assert state.bus.publish_outcome.await_count == 2


def test_engagement_idempotency_prevents_duplicate_order_and_inventory_charge(client):
    seed_track(tq=12, identity=Identity.HOSTILE)
    headers = {
        "X-Operator-Role": "FIRE_CONTROL_AUTHORITY",
        "X-Operator-Id": "FCA-01",
        "Idempotency-Key": "REQ-ENG-001",
    }
    body = {
        "trackId": "TRK-1001",
        "effectorId": "EFF-EW-01",
        "engagementType": "EW_DEFEAT",
        "humanConfirmation": True,
    }
    first = client.post("/engagements", headers=headers, json=body)
    second = client.post("/engagements", headers=headers, json=body)
    assert first.status_code == second.status_code == 202
    assert first.json()["engagementId"] == second.json()["engagementId"]
    assert second.headers["Idempotent-Replay"] == "true"
    assert state.effectors["EFF-EW-01"].magazine.remaining == 99
    engagement_publishes = [
        call
        for call in state.bus.publish_outcome.await_args_list
        if call.args[0].startswith("cuas.engagement.order.")
    ]
    assert len(engagement_publishes) == 1

    conflict_body = {**body, "humanConfirmation": False}
    conflict = client.post("/engagements", headers=headers, json=conflict_body)
    assert conflict.status_code == 409


def test_signed_authority_token_is_scoped_single_use_and_advances_lifecycle(client):
    seed_track(tq=12, identity=Identity.HOSTILE)
    created = client.post(
        "/engagements",
        headers={
            "X-Operator-Role": "FIRE_CONTROL_AUTHORITY",
            "X-Operator-Id": "FCA-01",
            "X-Request-ID": "REQ-TOKEN-001",
        },
        json={
            "trackId": "TRK-1001",
            "effectorId": "EFF-EW-01",
            "engagementType": "EW_DEFEAT",
            "humanConfirmation": True,
        },
    )
    assert created.status_code == 202
    engagement_id = created.json()["engagementId"]
    order_call = next(
        call
        for call in state.bus.publish_outcome.await_args_list
        if call.args[0].startswith("cuas.engagement.order.")
    )
    order = json.loads(order_call.args[1])["payload"]
    assert order["authorityToken"].startswith("v1.")
    consume_body = {
        "token": order["authorityToken"],
        "engagementId": engagement_id,
        "trackId": "TRK-1001",
        "effectorId": "EFF-EW-01",
        "engagementType": "EW_DEFEAT",
    }
    accepted = client.post(
        "/authority/tokens/consume",
        headers={"X-Effector-Id": "EFF-EW-01"},
        json=consume_body,
    )
    assert accepted.status_code == 200
    assert accepted.json()["valid"] is True
    assert client.get(f"/engagements/{engagement_id}").json()["state"] == "ACCEPTED"
    assert [
        item["state"]
        for item in client.get(f"/engagements/{engagement_id}/history").json()
    ] == ["PROPOSED", "AUTHORIZED", "ACCEPTED"]

    replay = client.post(
        "/authority/tokens/consume",
        headers={"X-Effector-Id": "EFF-EW-01"},
        json=consume_body,
    )
    assert replay.status_code == 403
    assert replay.json()["reasonCode"] == "TOKEN_ALREADY_USED"


def test_c2_wire_order_runs_through_effector_and_back_into_c2_lifecycle(client):
    simulator_dir = Path(__file__).resolve().parents[1] / "services" / "sensor-sim"
    if str(simulator_dir) not in sys.path:
        sys.path.insert(0, str(simulator_dir))
    from simulation import (  # noqa: PLC0415
        AuthorityTokenVerifier,
        EffectorModel,
        EngagementSimulator,
        Scenario,
    )

    # The deterministic test advances mission time without wall-clock sleeping;
    # start slightly behind real time so valid synthetic reports remain within
    # the C2 future-skew safety window.
    scenario = Scenario(seed=404, start_time=datetime.now(timezone.utc))
    scenario.tick(0.5)
    target = next(track for track in scenario.tracks.values() if track.identity == "HOSTILE")
    target.emitter_state = "EMITTING"
    payload = target.payload(scenario.asset, scenario.clock, scenario.ttl_seconds)
    live_track = Track.model_validate(payload)
    assert state.cop.upsert(live_track)
    envelope = state.effectors["EFF-EW-01"].engagementEnvelope
    envelope.location.lat = scenario.asset.lat
    envelope.location.lon = scenario.asset.lon

    created = client.post(
        "/engagements",
        headers={
            "X-Operator-Role": "FIRE_CONTROL_AUTHORITY",
            "X-Operator-Id": "FCA-INTEGRATION",
            "Idempotency-Key": "REQ-INTEGRATION-001",
        },
        json={
            "trackId": target.track_id,
            "effectorId": "EFF-EW-01",
            "engagementType": "EW_DEFEAT",
            "humanConfirmation": True,
        },
    )
    assert created.status_code == 202, created.text
    engagement_id = created.json()["engagementId"]
    order_call = next(
        call
        for call in state.bus.publish_outcome.await_args_list
        if call.args[0] == "cuas.engagement.order.EFF-EW-01"
    )
    order = json.loads(order_call.args[1])["payload"]
    effector = EngagementSimulator(
        EffectorModel("EFF-EW-01"),
        AuthorityTokenVerifier(
            "cuas-local-reference-key-not-for-production",
            issuer="C2-NODE-01",
        ),
        seed=404,
    )

    async def report(status_payload: dict) -> None:
        await _on_engagement_status(
            f"cuas.engagement.status.{engagement_id}",
            _envelope(
                "EngagementStatus",
                status_payload,
                source_id="EFF-EW-01",
                component_type="effector",
            ),
        )

    async def advance_sleep(seconds: float) -> None:
        # Preserve lifecycle ordering without making deterministic mission time
        # appear future-dated relative to the receiving C2 wall clock.
        scenario.clock.advance(0.001)
        await asyncio.sleep(0)

    asyncio.run(effector.execute(order, scenario, report, advance_sleep))
    assert state.engagements[engagement_id].state == EngagementState.COMPLETE
    assert [item.state for item in state.engagement_history[engagement_id]] == [
        EngagementState.PROPOSED,
        EngagementState.AUTHORIZED,
        EngagementState.ACCEPTED,
        EngagementState.ACTIVE,
        EngagementState.ASSESSING,
        EngagementState.COMPLETE,
    ]
    completed_evidence = next(
        record
        for record in reversed(state.audit)
        if record.engagementId == engagement_id
        and record.lifecycleState == EngagementState.COMPLETE.value
    )
    assert completed_evidence.effectorId == "EFF-EW-01"
    assert completed_evidence.sequence == 6
    assert completed_evidence.assessmentOutcome in {
        "CONFIRMED_EFFECT",
        "NO_CONFIRMED_EFFECT",
        "INDETERMINATE",
    }


def test_task_path_and_body_sensor_ids_must_match(client):
    r = client.post(
        "/sensors/SEN-RAD-01/tasks",
        headers={"X-Operator-Role": "SENSOR_MANAGER"},
        json={
            "sensorId": "SEN-OTHER",
            "taskType": "DWELL",
            "trackId": "TRK-1001",
            "requestedBy": "SM-1",
        },
    )
    assert r.status_code == 409


def test_optional_jsonl_audit_survives_reference_node_restart(monkeypatch, tmp_path):
    path = tmp_path / "audit.jsonl"
    monkeypatch.setenv("C2_AUDIT_FILE", str(path))
    first = State()
    first.record(
        AuditRecord(
            principal="FCA-01",
            action="TEST_DECISION",
            decision="PERMIT",
            detail="restart persistence boundary",
        )
    )
    second = State()
    assert len(second.audit) == 1
    assert second.audit[0].action == "TEST_DECISION"


def test_abort_is_scoped_idempotent_and_waits_for_effector_ack(client):
    seed_track(tq=12, identity=Identity.HOSTILE)
    created = client.post(
        "/engagements",
        headers={"X-Operator-Role": "FIRE_CONTROL_AUTHORITY", "X-Request-ID": "REQ-FIRE-1"},
        json={
            "trackId": "TRK-1001",
            "effectorId": "EFF-EW-01",
            "engagementType": "EW_DEFEAT",
            "humanConfirmation": True,
        },
    )
    engagement_id = created.json()["engagementId"]
    headers = {
        "X-Operator-Role": "FIRE_CONTROL_AUTHORITY",
        "X-Operator-Id": "FCA-01",
        "Idempotency-Key": "REQ-ABORT-1",
    }
    body = {"reason": "friendly entered safety volume", "humanConfirmation": True}
    first = client.post(f"/engagements/{engagement_id}/abort", headers=headers, json=body)
    second = client.post(f"/engagements/{engagement_id}/abort", headers=headers, json=body)
    assert first.status_code == second.status_code == 202
    assert first.json()["accepted"] is True
    assert first.json()["deliveryState"] == "BROKER_ACCEPTED_AWAITING_EFFECTOR_ACK"
    assert first.json()["lifecycleState"] == "AUTHORIZED"
    assert second.headers["Idempotent-Replay"] == "true"
    assert state.engagements[engagement_id].state == EngagementState.AUTHORIZED

    controls = [
        call
        for call in state.bus.publish_outcome.await_args_list
        if call.args[0].startswith("cuas.engagement.control.")
    ]
    assert len(controls) == 1
    directive = json.loads(controls[0].args[1])["payload"]
    assert directive["action"] == "ABORT"
    assert directive["trackId"] == "TRK-1001"
    assert directive["directiveSequence"] == 3
    assert directive["authorityToken"].startswith("v1.")

    # Only an acknowledged effector status makes the lifecycle terminal.
    aborted = EngagementStatus(
        engagementId=engagement_id,
        effectorId="EFF-EW-01",
        trackId="TRK-1001",
        state="ABORTED",
        sequence=4,
        terminal=True,
        reasonCode="OPERATOR_ABORT",
        detail="effect delivery stopped",
    )
    assert state.transition(aborted) == "APPLIED"
    assert engagement_id not in state.abort_pending
    assert "EFF-EW-01" not in state.effector_reservations


def test_abort_transport_failure_does_not_change_lifecycle(client):
    seed_track(tq=12, identity=Identity.HOSTILE)
    created = client.post(
        "/engagements",
        headers={"X-Operator-Role": "FIRE_CONTROL_AUTHORITY"},
        json={
            "trackId": "TRK-1001",
            "effectorId": "EFF-EW-01",
            "engagementType": "EW_DEFEAT",
            "humanConfirmation": True,
        },
    )
    engagement_id = created.json()["engagementId"]
    state.bus.publish_outcome = AsyncMock(return_value=PublishOutcome.NOT_SENT)
    aborted = client.post(
        f"/engagements/{engagement_id}/abort",
        headers={"X-Operator-Role": "FIRE_CONTROL_AUTHORITY"},
        json={"reason": "operator safety stop", "humanConfirmation": True},
    )
    assert aborted.status_code == 503
    assert aborted.json()["accepted"] is False
    assert aborted.json()["lifecycleState"] == "AUTHORIZED"
    assert state.engagements[engagement_id].state == EngagementState.AUTHORIZED
    assert engagement_id not in state.abort_pending


def test_lifecycle_rejects_late_regressive_and_time_reversed_updates(client):
    now = datetime.now(timezone.utc)
    engagement_id = "ENG-SEQUENCE-TEST"
    proposed = EngagementStatus(
        engagementId=engagement_id,
        effectorId="EFF-EW-01",
        trackId="TRK-1001",
        state="PROPOSED",
        sequence=1,
        terminal=False,
        timeReported=now,
    )
    authorized = EngagementStatus(
        engagementId=engagement_id,
        effectorId="EFF-EW-01",
        trackId="TRK-1001",
        state="AUTHORIZED",
        sequence=2,
        terminal=False,
        timeReported=now + timedelta(seconds=1),
    )
    accepted = EngagementStatus(
        engagementId=engagement_id,
        effectorId="EFF-EW-01",
        trackId="TRK-1001",
        state="ACCEPTED",
        sequence=3,
        terminal=False,
        timeReported=now + timedelta(seconds=2),
    )
    assert state.transition(proposed) == "APPLIED"
    assert state.transition(authorized) == "APPLIED"
    assert state.transition(accepted) == "APPLIED"
    assert state.transition(accepted) == "DUPLICATE"
    assert state.transition(authorized) == "INVALID"

    reversed_time = EngagementStatus(
        engagementId=engagement_id,
        effectorId="EFF-EW-01",
        trackId="TRK-1001",
        state="ACTIVE",
        sequence=4,
        terminal=False,
        timeReported=now + timedelta(seconds=1),
    )
    assert state.transition(reversed_time) == "INVALID"
    assert state.engagements[engagement_id].state == EngagementState.ACCEPTED


def test_effector_may_terminally_deny_but_cannot_mutate_engagement_scope(client):
    now = datetime.now(timezone.utc)
    engagement_id = "ENG-EFFECTOR-DENY"
    assert state.transition(EngagementStatus(
        engagementId=engagement_id, effectorId="EFF-EW-01", trackId="TRK-1001",
        state="PROPOSED", sequence=1, terminal=False, timeReported=now,
    )) == "APPLIED"
    assert state.transition(EngagementStatus(
        engagementId=engagement_id, effectorId="EFF-EW-01", trackId="TRK-1001",
        state="AUTHORIZED", sequence=2, terminal=False,
        timeReported=now + timedelta(seconds=1),
    )) == "APPLIED"
    state.effector_reservations["EFF-EW-01"] = engagement_id

    spoofed = EngagementStatus(
        engagementId=engagement_id, effectorId="EFF-OTHER", trackId="TRK-1001",
        state="DENIED", sequence=3, terminal=True,
        reasonCode="INTERLOCK_BLOCKED", timeReported=now + timedelta(seconds=2),
    )
    assert state.transition(spoofed) == "INVALID"
    assert state.effector_reservations["EFF-EW-01"] == engagement_id

    denied = spoofed.model_copy(update={"effectorId": "EFF-EW-01"})
    assert state.transition(denied) == "APPLIED"
    assert state.engagements[engagement_id].state == EngagementState.DENIED
    assert "EFF-EW-01" not in state.effector_reservations


def test_missing_effector_ack_inhibits_and_retains_reservation(client):
    now = datetime.now(timezone.utc)
    engagement_id = "ENG-ACK-TIMEOUT"
    assert state.transition(EngagementStatus(
        engagementId=engagement_id, effectorId="EFF-EW-01", trackId="TRK-1001",
        state="PROPOSED", sequence=1, terminal=False,
        timeReported=now - timedelta(seconds=30),
    )) == "APPLIED"
    assert state.transition(EngagementStatus(
        engagementId=engagement_id, effectorId="EFF-EW-01", trackId="TRK-1001",
        state="AUTHORIZED", sequence=2, terminal=False,
        timeReported=now - timedelta(seconds=29),
    )) == "APPLIED"
    state.effector_reservations["EFF-EW-01"] = engagement_id
    state.expire_stale_commands(now)
    assert state.engagements[engagement_id].state == EngagementState.AUTHORIZED
    assert "state unknown" in state.engagements[engagement_id].detail
    assert state.effector_reservations["EFF-EW-01"] == engagement_id
    assert engagement_id in state.ack_timeouts


def test_bus_materiel_status_is_authoritative_and_timestamp_ordered(client):
    base = datetime.now(timezone.utc)
    sensor_payload = {
        "sensorId": "SEN-BUS-01",
        "sensorType": "RADAR",
        "readiness": "READY",
        "coverage": {
            "center": {"lat": 32.7, "lon": -117.2},
            "rangeMeters": 5000,
            "minAltMeters": 0,
            "maxAltMeters": 2000,
        },
        "timeReported": base.isoformat(),
    }
    asyncio.run(
        _on_sensor_status(
            "cuas.sensor.status.SEN-BUS-01",
            _envelope(
                "SensorStatus",
                sensor_payload,
                source_id="SEN-BUS-01",
                component_type="sensor",
            ),
        )
    )
    assert state.sensors["SEN-BUS-01"].coverage.center.lat == pytest.approx(32.7)

    older_sensor = {**sensor_payload, "readiness": "OFFLINE", "timeReported": (base - timedelta(seconds=1)).isoformat()}
    asyncio.run(
        _on_sensor_status(
            "cuas.sensor.status.SEN-BUS-01",
            _envelope(
                "SensorStatus",
                older_sensor,
                source_id="SEN-BUS-01",
                component_type="sensor",
            ),
        )
    )
    assert state.sensors["SEN-BUS-01"].readiness.value == "READY"

    effector_payload = {
        "effectorId": "EFF-BUS-01",
        "effectorType": "EW_JAMMER",
        "readiness": "READY",
        "magazine": {"remaining": 12, "capacity": 12, "unit": "seconds"},
        "modelProvenance": "NOTIONAL_REFERENCE_MODEL",
        "timeReported": base.isoformat(),
    }
    asyncio.run(
        _on_effector_status(
            "cuas.effector.status.EFF-BUS-01",
            _envelope(
                "EffectorStatus",
                effector_payload,
                source_id="EFF-BUS-01",
                component_type="effector",
            ),
        )
    )
    assert state.effectors["EFF-BUS-01"].modelProvenance == "NOTIONAL_REFERENCE_MODEL"


def test_engagement_delivery_unknown_retains_inhibit_and_replays_truthfully(client):
    state.bus.publish_outcome = AsyncMock(return_value=PublishOutcome.DELIVERY_UNKNOWN)
    seed_track(tq=12, identity=Identity.HOSTILE)
    headers = {
        "X-Operator-Role": "FIRE_CONTROL_AUTHORITY",
        "X-Operator-Id": "FCA-UNKNOWN",
        "Idempotency-Key": "REQ-ENG-UNKNOWN",
    }
    body = {
        "trackId": "TRK-1001",
        "effectorId": "EFF-EW-01",
        "engagementType": "EW_DEFEAT",
        "humanConfirmation": True,
    }
    before = state.effectors["EFF-EW-01"].magazine.remaining
    first = client.post("/engagements", headers=headers, json=body)
    second = client.post("/engagements", headers=headers, json=body)

    assert first.status_code == second.status_code == 503
    assert first.json()["state"] == "AUTHORIZED"
    assert second.headers["Idempotent-Replay"] == "true"
    engagement_id = first.json()["engagementId"]
    assert state.effector_reservations["EFF-EW-01"] == engagement_id
    assert engagement_id in state.delivery_unknown
    assert state.effectors["EFF-EW-01"].magazine.remaining == before
    assert state.bus.publish_outcome.await_count == 1


def test_immediate_effector_ack_during_publish_is_not_overwritten(client):
    seed_track(tq=12, identity=Identity.HOSTILE)

    async def publish_and_ack(subject: str, data: bytes):
        order = json.loads(data)["payload"]
        status = {
            "engagementId": order["engagementId"],
            "effectorId": order["effectorId"],
            "trackId": order["trackId"],
            "state": "ACCEPTED",
            "sequence": 3,
            "terminal": False,
            "reasonCode": "OK",
            "timeReported": datetime.now(timezone.utc).isoformat(),
        }
        await _on_engagement_status(
            f"cuas.engagement.status.{order['engagementId']}",
            _envelope(
                "EngagementStatus",
                status,
                source_id=order["effectorId"],
                component_type="effector",
            ),
        )
        return PublishOutcome.BROKER_ACCEPTED

    state.bus.publish_outcome = AsyncMock(side_effect=publish_and_ack)
    before = state.effectors["EFF-EW-01"].magazine.remaining
    response = client.post(
        "/engagements",
        headers={"X-Operator-Role": "FIRE_CONTROL_AUTHORITY"},
        json={
            "trackId": "TRK-1001",
            "effectorId": "EFF-EW-01",
            "engagementType": "EW_DEFEAT",
            "humanConfirmation": True,
        },
    )
    assert response.status_code == 202
    assert response.json()["state"] == "ACCEPTED"
    engagement_id = response.json()["engagementId"]
    assert state.engagements[engagement_id].state == EngagementState.ACCEPTED
    assert state.effectors["EFF-EW-01"].magazine.remaining == before - 1


def test_abort_reports_terminal_race_instead_of_stale_awaiting_ack(client):
    seed_track(tq=12, identity=Identity.HOSTILE)
    created = client.post(
        "/engagements",
        headers={"X-Operator-Role": "FIRE_CONTROL_AUTHORITY"},
        json={
            "trackId": "TRK-1001",
            "effectorId": "EFF-EW-01",
            "engagementType": "EW_DEFEAT",
            "humanConfirmation": True,
        },
    )
    engagement_id = created.json()["engagementId"]

    async def publish_and_fail(subject: str, data: bytes):
        assert state.transition(
            EngagementStatus(
                engagementId=engagement_id,
                effectorId="EFF-EW-01",
                trackId="TRK-1001",
                state="FAILED",
                sequence=3,
                terminal=True,
                reasonCode="EFFECTOR_FAULT",
                timeReported=datetime.now(timezone.utc),
            )
        ) == "APPLIED"
        return PublishOutcome.BROKER_ACCEPTED

    state.bus.publish_outcome = AsyncMock(side_effect=publish_and_fail)
    response = client.post(
        f"/engagements/{engagement_id}/abort",
        headers={
            "X-Operator-Role": "FIRE_CONTROL_AUTHORITY",
            "Idempotency-Key": "REQ-ABORT-RACE",
        },
        json={"reason": "safety stop", "humanConfirmation": True},
    )
    assert response.status_code == 409
    assert response.json()["accepted"] is False
    assert response.json()["deliveryState"] == "TERMINAL_WITHOUT_ABORT_ACK"
    assert response.json()["lifecycleState"] == "FAILED"


def test_task_delivery_unknown_is_cached_and_authenticated_principal_is_forwarded(client):
    state.bus.publish_outcome = AsyncMock(return_value=PublishOutcome.DELIVERY_UNKNOWN)
    seed_track()
    headers = {
        "X-Operator-Role": "SENSOR_MANAGER",
        "X-Operator-Id": "SM-AUTHENTICATED",
        "Idempotency-Key": "REQ-TASK-UNKNOWN",
    }
    body = {
        "sensorId": "SEN-RAD-01",
        "taskType": "DWELL",
        "trackId": "TRK-1001",
        "priority": 7,
        "requestedBy": "SPOOFED-OPERATOR",
    }
    first = client.post("/sensors/SEN-RAD-01/tasks", headers=headers, json=body)
    second = client.post("/sensors/SEN-RAD-01/tasks", headers=headers, json=body)

    assert first.status_code == second.status_code == 503
    assert first.json()["deliveryState"] == "DELIVERY_UNKNOWN"
    assert state.bus.publish_outcome.await_count == 1
    wire = json.loads(state.bus.publish_outcome.await_args.args[1])
    assert wire["payload"]["requestedBy"] == "SM-AUTHENTICATED"
    assert wire["payload"]["expiresAt"]


def test_sensor_priority_lease_blocks_lower_and_allows_higher_preemption(client):
    seed_track()

    def submit(key: str, priority: int):
        return client.post(
            "/sensors/SEN-RAD-01/tasks",
            headers={
                "X-Operator-Role": "SENSOR_MANAGER",
                "X-Operator-Id": "SM-ARBITER",
                "Idempotency-Key": key,
            },
            json={
                "sensorId": "SEN-RAD-01",
                "taskType": "DWELL",
                "trackId": "TRK-1001",
                "priority": priority,
                "requestedBy": "IGNORED",
            },
        )

    first = submit("REQ-PRIORITY-7", 7)
    lower = submit("REQ-PRIORITY-6", 6)
    higher = submit("REQ-PRIORITY-9", 9)

    assert first.status_code == higher.status_code == 202
    assert lower.status_code == 409
    assert lower.json()["deliveryState"] == "NOT_SENT"
    assert "priority 7" in lower.json()["reason"]
    assert state.sensor_task_leases["SEN-RAD-01"]["priority"] == 9
    assert state.bus.publish_outcome.await_count == 2


def test_abort_delivery_unknown_and_late_ack_reconcile_cached_receipt(client):
    seed_track(tq=12, identity=Identity.HOSTILE)
    created = client.post(
        "/engagements",
        headers={"X-Operator-Role": "FIRE_CONTROL_AUTHORITY"},
        json={
            "trackId": "TRK-1001",
            "effectorId": "EFF-EW-01",
            "engagementType": "EW_DEFEAT",
            "humanConfirmation": True,
        },
    )
    engagement_id = created.json()["engagementId"]
    state.bus.publish_outcome = AsyncMock(return_value=PublishOutcome.DELIVERY_UNKNOWN)
    headers = {
        "X-Operator-Role": "FIRE_CONTROL_AUTHORITY",
        "X-Operator-Id": "FCA-ABORT",
        "Idempotency-Key": "REQ-ABORT-UNKNOWN",
    }
    body = {"reason": "safety stop", "humanConfirmation": True}
    first = client.post(f"/engagements/{engagement_id}/abort", headers=headers, json=body)
    replay = client.post(f"/engagements/{engagement_id}/abort", headers=headers, json=body)
    assert first.status_code == replay.status_code == 503
    assert first.json()["deliveryState"] == "DELIVERY_UNKNOWN"
    assert engagement_id in state.abort_pending
    assert state.bus.publish_outcome.await_count == 1

    receipt = state.control_requests["REQ-ABORT-UNKNOWN"][1]
    state.expire_stale_commands(
        receipt.timeReported + timedelta(seconds=30)
    )
    assert state.control_requests["REQ-ABORT-UNKNOWN"][1].deliveryState == "ACK_TIMEOUT"
    assert state.transition(
        EngagementStatus(
            engagementId=engagement_id,
            effectorId="EFF-EW-01",
            trackId="TRK-1001",
            state="ABORTED",
            sequence=3,
            terminal=True,
            reasonCode="OPERATOR_ABORT",
            timeReported=datetime.now(timezone.utc),
        )
    ) == "APPLIED"
    reconciled = client.post(
        f"/engagements/{engagement_id}/abort", headers=headers, json=body
    )
    assert reconciled.status_code == 202
    assert reconciled.json()["accepted"] is True
    assert reconciled.json()["deliveryState"] == "EFFECTOR_ACKNOWLEDGED"


def test_materiel_expiry_recovery_future_skew_and_identity_binding(client):
    base = datetime.now(timezone.utc)
    payload = {
        "sensorId": "SEN-FRESH-01",
        "sensorType": "RADAR",
        "readiness": "READY",
        "timeReported": base.isoformat(),
    }

    def publish_status(value: dict, *, source: str = "SEN-FRESH-01", subject: str = "SEN-FRESH-01"):
        asyncio.run(
            _on_sensor_status(
                f"cuas.sensor.status.{subject}",
                _envelope(
                    "SensorStatus",
                    value,
                    source_id=source,
                    component_type="sensor",
                ),
            )
        )

    publish_status(payload)
    state.expire_materiel(base + timedelta(seconds=11))
    assert state.sensors["SEN-FRESH-01"].readiness.value == "OFFLINE"
    assert "SEN-FRESH-01" in state.materiel_stale

    recovered = {**payload, "timeReported": (base + timedelta(milliseconds=100)).isoformat()}
    publish_status(recovered)
    assert state.sensors["SEN-FRESH-01"].readiness.value == "READY"
    assert "SEN-FRESH-01" not in state.materiel_stale
    state.expire_materiel(base + timedelta(seconds=12))
    expiries = [a for a in state.audit if a.action == "MATERIEL_STATUS_EXPIRED" and "SEN-FRESH-01" in (a.detail or "")]
    assert len(expiries) == 2

    future = {**payload, "sensorId": "SEN-FUTURE-01", "timeReported": (base + timedelta(seconds=30)).isoformat()}
    publish_status(future, source="SEN-FUTURE-01", subject="SEN-FUTURE-01")
    assert "SEN-FUTURE-01" not in state.sensors
    mismatched = {**payload, "sensorId": "SEN-SPOOF-01"}
    publish_status(mismatched, source="SEN-OTHER-01", subject="SEN-SPOOF-01")
    assert "SEN-SPOOF-01" not in state.sensors


def test_bus_envelope_required_fields_signature_and_replay_are_enforced(client):
    payload = {
        "sensorId": "SEN-EDGE-01",
        "sensorType": "RADAR",
        "readiness": "READY",
        "timeReported": datetime.now(timezone.utc).isoformat(),
    }
    wire = _envelope(
        "SensorStatus",
        payload,
        source_id="SEN-EDGE-01",
        component_type="sensor",
    )
    assert _bus_payload(
        wire,
        "SensorStatus",
        expected_source_id="SEN-EDGE-01",
        expected_component_type="sensor",
    )["sensorId"] == "SEN-EDGE-01"
    with pytest.raises(ValueError, match="replayed"):
        _bus_payload(wire, "SensorStatus")

    missing = json.loads(wire)
    missing.pop("messageId")
    with pytest.raises(ValueError, match="missing required"):
        _bus_payload(json.dumps(missing).encode(), "SensorStatus")

    tampered = json.loads(_envelope(
        "SensorStatus", payload, source_id="SEN-EDGE-01", component_type="sensor"
    ))
    tampered["payload"]["readiness"] = "OFFLINE"
    with pytest.raises(ValueError, match="signature"):
        _bus_payload(json.dumps(tampered).encode(), "SensorStatus")
    with pytest.raises(ValueError, match="un-enveloped"):
        _bus_payload(json.dumps(payload).encode(), "SensorStatus")


def test_history_snapshot_and_hash_chain_are_immutable(client):
    now = datetime.now(timezone.utc)
    engagement_id = "ENG-HISTORY-IMMUTABLE"
    for status in (
        EngagementStatus(
            engagementId=engagement_id,
            effectorId="EFF-EW-01",
            trackId="TRK-1001",
            state="PROPOSED",
            sequence=1,
            terminal=False,
            timeReported=now - timedelta(seconds=30),
            detail="proposal snapshot",
        ),
        EngagementStatus(
            engagementId=engagement_id,
            effectorId="EFF-EW-01",
            trackId="TRK-1001",
            state="AUTHORIZED",
            sequence=2,
            terminal=False,
            timeReported=now - timedelta(seconds=29),
            detail="authorization snapshot",
        ),
    ):
        assert state.transition(status) == "APPLIED"
    state.effector_reservations["EFF-EW-01"] = engagement_id
    before = state.engagement_history[engagement_id][-1].model_copy(deep=True)
    state.expire_stale_commands(now)
    after = state.engagement_history[engagement_id][-1]
    assert after.detail == before.detail == "authorization snapshot"
    assert state.engagements[engagement_id].detail != after.detail
    assert all(record.recordHash for record in state.audit)
    for previous, current in zip(state.audit, state.audit[1:]):
        assert current.previousRecordHash == previous.recordHash


def test_atomic_audit_failure_does_not_advance_chain_and_can_recover(monkeypatch, tmp_path):
    path = tmp_path / "audit.jsonl"
    monkeypatch.setenv("C2_AUDIT_FILE", str(path))
    isolated = State()
    isolated.record(AuditRecord(principal="TEST", action="FIRST"))
    first_hash = isolated.audit[-1].recordHash
    real_replace = os.replace

    def fail_replace(source, destination):
        raise OSError("simulated storage fault")

    monkeypatch.setattr(os, "replace", fail_replace)
    with pytest.raises(RuntimeError, match="audit evidence persistence failed"):
        isolated.record(AuditRecord(principal="TEST", action="DROPPED"))
    assert len(isolated.audit) == 1
    assert isolated.audit[-1].recordHash == first_hash
    assert isolated.audit_healthy is False

    monkeypatch.setattr(os, "replace", real_replace)
    assert isolated.probe_audit_sink() is True
    isolated.record(AuditRecord(principal="TEST", action="RECOVERED"))
    reloaded = State()
    assert [record.action for record in reloaded.audit] == ["FIRST", "RECOVERED"]
    assert reloaded.audit[1].previousRecordHash == reloaded.audit[0].recordHash
