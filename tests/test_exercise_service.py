"""Persistent human coordination contracts, independent of the C2 models."""
import json
import sqlite3
from datetime import timedelta

import pytest
from fastapi.testclient import TestClient

from services.exercise_service.app import API, create_app
from services.exercise_service.store import ExerciseError, iso, utcnow


class ClientAddress:
    """Supply a transport client address on the pinned Starlette TestClient."""
    def __init__(self, app, host):
        self.app, self.host = app, host

    async def __call__(self, scope, receive, send):
        if scope["type"] == "http":
            scope = {**scope, "client": (self.host, 12345)}
        return await self.app(scope, receive, send)


@pytest.fixture
def client(tmp_path):
    with TestClient(create_app(tmp_path / "exercise.sqlite3", setup_token="test-setup")) as value:
        yield value


def room(client, name="Test exercise", participant="Controller"):
    response = client.post(API + "/rooms", json={"name": name, "participantName": participant},
                           headers={"X-Setup-Key": "test-setup"})
    assert response.status_code == 201, response.text
    return response.json()


def headers(session):
    return {"Authorization": "Bearer " + session["token"]}


def post(client, session, path, body=None):
    return client.post(API + path, json={} if body is None else body, headers=headers(session))


def state(client, session):
    response = client.get(API + "/state", headers=headers(session))
    assert response.status_code == 200, response.text
    return response.json()


def participant(client, controller, name="Reviewer", role="reviewer"):
    invite = post(client, controller, "/invitations", {"name": name, "role": role})
    assert invite.status_code == 201, invite.text
    joined = client.post(API + "/join", json={"inviteToken": invite.json()["inviteToken"]})
    assert joined.status_code == 200, joined.text
    return joined.json()


def report(client, session, **changes):
    data = {"title": "Fictional observation", "text": "Initial unconfirmed account", "source": "Training source A",
            "observedAt": iso(utcnow() - timedelta(seconds=30)), "lat": 38.89, "lon": -77.035}
    data.update(changes)
    response = post(client, session, "/reports", data)
    assert response.status_code == 201, response.text
    return response.json()["result"]


def test_creation_setup_and_mutation_boundary(client, tmp_path):
    assert client.get("/health").json() == {"service": "exercise", "version": "1", "canCreate": True, "setupRequired": True}
    body = {"name": "Test", "participantName": "Person"}
    assert client.post(API + "/rooms", json=body).status_code == 403
    assert client.post(API + "/rooms", json=body, headers={"X-Setup-Key": "wrong"}).status_code == 403
    assert client.post(API + "/rooms", json=body, headers={"X-Setup-Key": "test-setup", "Origin": "https://other.example"}).status_code == 403
    assert client.post(API + "/rooms", content=json.dumps(body), headers={"Content-Type": "text/plain"}).status_code == 415
    assert client.post(API + "/rooms", content=b"x" * 100_001, headers={"Content-Type": "application/json"}).status_code == 413
    assert client.post(API + "/rooms", json=body, headers={"X-Setup-Key": "test-setup", "Origin": "http://testserver"}).status_code == 201
    with TestClient(ClientAddress(create_app(tmp_path / "local.sqlite3", setup_token=""), "127.0.0.1")) as local:
        assert local.post(API + "/rooms", json=body).status_code == 201
        assert local.post(API + "/rooms", json=body, headers={"Host": "rebind.example"}).status_code == 400
    with TestClient(ClientAddress(create_app(tmp_path / "remote.sqlite3", setup_token=""), "192.0.2.1")) as remote:
        assert remote.get("/health").json()["canCreate"] is False
        assert remote.post(API + "/rooms", json=body).status_code == 403


def test_invitation_is_named_single_use_and_session_is_hashed(client):
    controller = room(client)
    invite = post(client, controller, "/invitations", {"name": "Invited reviewer", "role": "reviewer"}).json()
    joined = client.post(API + "/join", json={"inviteToken": invite["inviteToken"]}).json()
    assert joined["participant"]["name"] == "Invited reviewer"
    assert joined["participant"]["role"] == "reviewer"
    assert client.post(API + "/join", json={"inviteToken": invite["inviteToken"]}).status_code == 401
    assert client.post(API + "/join", json={"inviteToken": invite["inviteToken"], "role": "controller"}).status_code == 422
    db_text = client.app.state.exercise_store.db_path.read_bytes()
    assert controller["token"].encode() not in db_text
    assert joined["token"].encode() not in db_text
    assert invite["inviteToken"].encode() not in db_text
    db = sqlite3.connect(client.app.state.exercise_store.db_path)
    db.execute("UPDATE sessions SET expires_at=?", (iso(utcnow() - timedelta(seconds=1)),))
    db.commit()
    db.close()
    assert client.get(API + "/state", headers=headers(joined)).status_code == 401


def test_room_isolation_and_role_enforcement(client):
    alpha = room(client, "Alpha")
    beta = room(client, "Beta")
    observer = participant(client, alpha, "Observer", "observer")
    item = report(client, alpha)
    assert client.get(API + "/state").status_code == 401
    assert state(client, beta)["reports"] == []
    assert post(client, beta, f"/reports/{item['id']}/revisions", {
        "expectedVersion": 1, "text": "Other room", "reason": "Wrong room"}).status_code == 404
    assert post(client, observer, "/reports", {"title": "Forbidden", "source": "Person", "text": "Cannot write"}).status_code == 403
    assert post(client, observer, "/clock", {"action": "start"}).status_code == 403
    assert post(client, observer, "/invitations", {"name": "Extra", "role": "controller"}).status_code == 403
    assert state(client, observer)["reports"][0]["id"] == item["id"]


def test_durable_restart_and_correction_conflict(client):
    controller = room(client)
    item = report(client, controller, attachment={"name": "source-note.txt", "content": "Original source wording"})
    payload = {"expectedVersion": 1, "text": "Corrected likely biological account", "reason": "Source clarification", "source": "Training source B"}
    corrected = post(client, controller, f"/reports/{item['id']}/revisions", payload)
    assert corrected.status_code == 200, corrected.text
    assert post(client, controller, f"/reports/{item['id']}/revisions", payload).status_code == 409
    with TestClient(create_app(client.app.state.exercise_store.db_path, setup_token="test-setup")) as restarted:
        saved = state(restarted, controller)["reports"][0]
        assert saved["version"] == 2
        assert saved["attachment"]["content"] == "Original source wording"
        assert [r["source"] for r in saved["revisions"]] == ["Training source A", "Training source B"]
        assert saved["revisions"][0]["text"] == "Initial unconfirmed account"
        assert saved["revisions"][1]["reason"] == "Source clarification"


def test_request_has_assignee_enforced_transitions(client):
    controller = room(client)
    reviewer = participant(client, controller)
    other = participant(client, controller, "Other reviewer")
    item = report(client, controller)
    response = post(client, controller, "/requests", {"reportId": item["id"], "assigneeId": reviewer["participant"]["id"], "summary": "Clarify the source account"})
    assert response.status_code == 201, response.text
    request_id = response.json()["result"]["id"]
    assert post(client, controller, f"/requests/{request_id}/ack").status_code == 403
    assert post(client, other, f"/requests/{request_id}/resolve", {"reason": "Not assigned"}).status_code == 403
    assert post(client, reviewer, f"/requests/{request_id}/resolve", {"reason": "Too soon"}).status_code == 409
    assert post(client, reviewer, f"/requests/{request_id}/ack").status_code == 200
    assert post(client, reviewer, f"/requests/{request_id}/ack").status_code == 409
    assert post(client, reviewer, f"/requests/{request_id}/resolve", {"reason": "  "}).status_code == 422
    assert post(client, reviewer, f"/requests/{request_id}/resolve", {"reason": "Source contacted; disagreement remains documented"}).status_code == 200
    saved = state(client, controller)["requests"][0]
    assert saved["status"] == "resolved"
    assert saved["acknowledgedAt"] <= saved["resolvedAt"]


def test_handover_is_atomic_and_updates_existing_session_authority(client):
    outgoing = room(client)
    incoming = participant(client, outgoing)
    wrong = participant(client, outgoing, "Wrong recipient")
    response = post(client, outgoing, "/handover", {"toParticipantId": incoming["participant"]["id"], "note": "Review source B and the remaining open request"})
    assert response.status_code == 201, response.text
    handover_id = response.json()["result"]["id"]
    assert post(client, wrong, f"/handover/{handover_id}/accept").status_code == 403
    assert post(client, incoming, f"/handover/{handover_id}/accept").status_code == 200
    assert state(client, outgoing)["participant"]["role"] == "reviewer"
    assert state(client, incoming)["participant"]["role"] == "controller"
    assert post(client, outgoing, "/clock", {"action": "start"}).status_code == 403
    assert post(client, incoming, "/clock", {"action": "start"}).status_code == 200
    assert post(client, incoming, f"/handover/{handover_id}/accept").status_code == 409


def test_replay_retains_prior_evidence_and_export_omits_credentials(client):
    controller = room(client)
    item = report(client, controller)
    before = state(client, controller)["latestSeq"]
    assert post(client, controller, f"/reports/{item['id']}/revisions", {"expectedVersion": 1, "text": "Later correction", "reason": "Reviewed original account"}).status_code == 200
    historical = client.get(API + f"/replay?through={before}", headers=headers(controller)).json()
    assert historical["historical"] is True
    assert historical["throughSeq"] == before
    assert historical["reports"][0]["text"] == "Initial unconfirmed account"
    assert historical["reports"][0]["version"] == 1
    assert state(client, controller)["reports"][0]["text"] == "Later correction"
    record = client.get(API + "/export", headers=headers(controller))
    assert record.status_code == 200
    assert record.json()["trainingOnly"] is True
    assert record.json()["formatVersion"] == 1
    assert controller["token"] not in record.text
    assert "token_hash" not in record.text
    incremental = client.get(API + f"/events?after={before}", headers=headers(controller)).json()
    assert len(incremental["events"]) == 1
    assert incremental["events"][0]["type"] == "report.corrected"
    with sqlite3.connect(client.app.state.exercise_store.db_path) as db:
        with pytest.raises(sqlite3.IntegrityError, match="append-only"):
            db.execute("UPDATE events SET event='{}'")


def test_historical_clock_and_evidence_age_are_anchored_to_saved_event(client, monkeypatch):
    instant = utcnow()
    monkeypatch.setattr("services.exercise_service.store.utcnow", lambda: instant)
    controller = room(client)
    assert post(client, controller, "/clock", {"action": "start"}).status_code == 200
    instant += timedelta(seconds=30)
    report(client, controller)
    sequence = state(client, controller)["latestSeq"]
    saved_at = iso(instant)
    instant += timedelta(hours=1)
    historical = client.get(API + f"/replay?through={sequence}", headers=headers(controller)).json()
    assert historical["snapshotAt"] == saved_at
    assert historical["serverTime"] == iso(instant)
    assert historical["room"]["elapsedSeconds"] == 30
    assert state(client, controller)["room"]["elapsedSeconds"] == 3630


def test_request_and_pending_handover_history_survive_later_completion(client):
    outgoing = room(client)
    incoming = participant(client, outgoing)
    item = report(client, outgoing)
    request_id = post(client, outgoing, "/requests", {"reportId": item["id"],
        "assigneeId": incoming["participant"]["id"], "summary": "Clarify the conflicting account"}).json()["result"]["id"]
    assert post(client, incoming, f"/requests/{request_id}/ack").status_code == 200
    handover_id = post(client, outgoing, "/handover", {"toParticipantId": incoming["participant"]["id"],
        "note": "Clarification is acknowledged but still unresolved"}).json()["result"]["id"]
    saved = state(client, outgoing)
    assert post(client, outgoing, "/handover", {"toParticipantId": incoming["participant"]["id"], "note": "Duplicate pending"}).status_code == 409
    assert state(client, outgoing)["latestSeq"] == saved["latestSeq"]
    assert post(client, incoming, f"/handover/{handover_id}/accept").status_code == 200
    assert post(client, incoming, f"/requests/{request_id}/resolve", {"reason": "Source contacted; uncertainty retained"}).status_code == 200
    with TestClient(create_app(client.app.state.exercise_store.db_path, setup_token="test-setup")) as restarted:
        historical = restarted.get(API + f"/replay?through={saved['latestSeq']}", headers=headers(outgoing)).json()
        assert historical["requests"][0]["status"] == "acknowledged"
        assert "resolution" not in historical["requests"][0]
        assert historical["handovers"][0]["status"] == "pending"
        roles = {person["id"]: person["role"] for person in historical["participants"]}
        assert roles[outgoing["participant"]["id"]] == "controller"
        assert roles[incoming["participant"]["id"]] == "reviewer"
        # The authenticated actor remains current for authorization, while the
        # participant list accurately represents responsibilities at the event.
        assert historical["participant"]["role"] == "reviewer"
        current = state(restarted, incoming)
        assert current["requests"][0]["status"] == "resolved"
        assert current["handovers"][0]["status"] == "accepted"


def test_tak_attempt_is_durable_and_completion_allowed_after_handover_and_end(client):
    outgoing = room(client)
    incoming = participant(client, outgoing)
    item = report(client, outgoing, attachment={"name": "private-training-note.txt", "content": "This attachment is not sent to TAK"})
    store = client.app.state.exercise_store
    original_auth = store.authenticate(outgoing["token"])
    attempt = store.reserve_tak(original_auth, [item["id"]])
    assert attempt["reports"][0]["version"] == 1
    assert "attachment" not in attempt["reports"][0]
    assert state(client, outgoing)["events"][-1]["type"] == "tak.send_requested"
    with pytest.raises(ExerciseError, match="already pending"):
        store.reserve_tak(original_auth, [item["id"]])
    assert post(client, outgoing, f"/reports/{item['id']}/revisions", {"expectedVersion": 1,
        "text": "A later report version, after the send started", "reason": "A later source correction"}).status_code == 200
    handover_id = post(client, outgoing, "/handover", {"toParticipantId": incoming["participant"]["id"], "note": "A manual TAK transfer is in progress"}).json()["result"]["id"]
    assert post(client, incoming, f"/handover/{handover_id}/accept").status_code == 200
    assert post(client, incoming, "/clock", {"action": "end"}).status_code == 200
    with TestClient(create_app(store.db_path, setup_token="test-setup")) as restarted:
        recovered_store = restarted.app.state.exercise_store
        with pytest.raises(ExerciseError, match="Only the participant"):
            recovered_store.finish_tak(recovered_store.authenticate(incoming["token"]), attempt["attemptId"], {})
        completed = recovered_store.finish_tak(recovered_store.authenticate(outgoing["token"]), attempt["attemptId"], {
            "transportStatus": "written_to_tls_socket", "markerCount": 999,
            "clientReceipt": "confirmed", "token": "must-not-be-exported", "privateKey": "must-not-be-exported",
            "message": "Transport write completed", "lastSentAt": iso()})
        assert completed["type"] == "tak.send_finished"
        with pytest.raises(ExerciseError, match="already been recorded"):
            recovered_store.finish_tak(original_auth, attempt["attemptId"], {})
        current = state(restarted, outgoing)
        assert current["room"]["status"] == "ended"
        assert current["takAttempts"][0]["result"]["clientReceipt"] == "unverified"
        assert current["takAttempts"][0]["result"]["markerCount"] == 1
        started = next(event for event in current["events"] if event["type"] == "tak.send_requested")
        assert started["payload"]["reports"][0]["version"] == 1
        assert started["payload"]["reports"][0]["text"] == "Initial unconfirmed account"
        assert current["reports"][0]["version"] == 2
        exported = restarted.get(API + "/export", headers=headers(outgoing))
        assert "must-not-be-exported" not in exported.text
        with pytest.raises(ExerciseError, match="read-only"):
            recovered_store.reserve_tak(recovered_store.authenticate(incoming["token"]), [item["id"]])


def test_tak_reservation_checks_role_room_membership_and_locations(client):
    controller = room(client)
    other_room = room(client, "Other room")
    reviewer = participant(client, controller)
    item = report(client, controller)
    unlocated = report(client, controller, lat=None, lon=None, observedAt=None)
    store = client.app.state.exercise_store
    with pytest.raises(ExerciseError) as denied:
        store.reserve_tak(store.authenticate(reviewer["token"]), [item["id"]])
    assert denied.value.status == 403
    with pytest.raises(ExerciseError) as isolated:
        store.reserve_tak(store.authenticate(other_room["token"]), [item["id"]])
    assert isolated.value.status == 404
    for ids in ([], [item["id"], item["id"]], [unlocated["id"]]):
        with pytest.raises(ExerciseError) as invalid:
            store.reserve_tak(store.authenticate(controller["token"]), ids)
        assert invalid.value.status == 422
    attempt = store.reserve_tak(store.authenticate(controller["token"]))
    assert [record["id"] for record in attempt["reports"]] == [item["id"]]
    finished = store.finish_tak(store.authenticate(controller["token"]), attempt["attemptId"], {
        "transportStatus": "delivery_unknown", "lastError": "Write timed out after it began"})
    assert finished["payload"]["result"]["transportStatus"] == "delivery_unknown"


def test_clock_and_ended_room_are_durable_and_read_only(client):
    controller = room(client)
    reviewer = participant(client, controller)
    item = report(client, controller)
    unused = post(client, controller, "/invitations", {"name": "Later", "role": "observer"}).json()
    assert post(client, controller, "/clock", {"action": "resume"}).status_code == 409
    assert post(client, controller, "/clock", {"action": "start"}).status_code == 200
    assert post(client, controller, "/clock", {"action": "pause"}).status_code == 200
    saved = state(client, controller)
    assert saved["room"]["status"] == "paused"
    assert post(client, controller, "/clock", {"action": "end"}).status_code == 200
    ended = state(client, controller)
    assert ended["room"]["status"] == "ended"
    for path, body in [
        ("/clock", {"action": "resume"}), ("/scenario/advance", {}),
        ("/reports", {"title": "After end", "source": "Person", "text": "Should not save"}),
        (f"/reports/{item['id']}/revisions", {"expectedVersion": 1, "text": "After end", "reason": "Should not save"}),
        ("/requests", {"reportId": item["id"], "assigneeId": reviewer["participant"]["id"], "summary": "After end"}),
        ("/invitations", {"name": "After end", "role": "reviewer"}),
        ("/handover", {"toParticipantId": reviewer["participant"]["id"], "note": "After end"}),
    ]:
        assert post(client, controller, path, body).status_code == 409, path
    assert client.post(API + "/join", json={"inviteToken": unused["inviteToken"]}).status_code == 409
    assert client.get(API + "/export", headers=headers(controller)).status_code == 200
    assert state(client, controller)["latestSeq"] == ended["latestSeq"]


def test_six_checkpoints_preserve_conflict_and_correction(client):
    controller = room(client)
    for number in range(6):
        response = post(client, controller, "/scenario/advance")
        assert response.status_code == 200, response.text
        assert response.json()["result"]["checkpoint"] == number
    saved = state(client, controller)
    assert saved["room"]["scenarioStep"] == 5
    assert saved["room"]["status"] == "ready"
    assert len(saved["reports"]) == 5
    corrected = next(r for r in saved["reports"] if r["scenarioCheckpoint"] == 1)
    assert corrected["version"] == 2
    assert "conflicts" in corrected["revisions"][0]["text"]
    assert "biological" in corrected["text"]
    assert all(event["type"] not in {"engagement", "release", "sensor.task"} for event in saved["events"])
    assert post(client, controller, "/scenario/advance").status_code == 409


@pytest.mark.parametrize("change", [
    {"lat": 91}, {"lon": None}, {"observedAt": "2026-09-11T10:00:00"},
    {"weaponId": "unrelated-control"}, {"title": "\u0000"},
    {"attachment": {"name": "../private.txt", "content": "bad path"}},
    {"attachment": {"name": "long.txt", "content": "😀" * 13_000}},
])
def test_untrusted_report_fields_are_bounded(client, change):
    controller = room(client)
    data = {"title": "Test", "source": "Test source", "text": "Fictional report", "lat": 38, "lon": -77}
    data.update(change)
    assert post(client, controller, "/reports", data).status_code == 422
