"""End-to-end API tests via FastAPI TestClient.

Runs without a broker: the bus connects in degraded mode (publish returns False),
which is itself the DDIL behavior we want to verify the node tolerates. Tracks are
seeded directly into the COP to stand in for the bus track stream.
"""
from datetime import datetime, timezone

import pytest
from fastapi.testclient import TestClient

from app.main import app, state
from app.models import Identity, Kinematics, Position, Track


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
def client():
    with TestClient(app) as c:
        # Fresh state per test.
        state.sensors.clear()
        state.effectors.clear()
        state.engagements.clear()
        state.audit.clear()
        c.post("/sensors", json={"sensorId": "SEN-RAD-01", "sensorType": "RADAR", "taskable": True})
        c.post(
            "/effectors",
            json={
                "effectorId": "EFF-EW-01",
                "effectorType": "EW_JAMMER",
                "readiness": "READY",
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
