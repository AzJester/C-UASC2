"""Authenticated, durable training-transfer boundaries. Never uses an external peer."""
import xml.etree.ElementTree as ET

import pytest
from fastapi.testclient import TestClient

from services.exercise_service.app import API, create_app
from services.exercise_service import tak
from services.exercise_service.store import iso, utcnow


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.delenv("EXERCISE_TAK_CONFIG", raising=False)
    monkeypatch.setattr(tak, "_connect", lambda *args, **kwargs: pytest.fail("API tests must not open TAK connections"))
    with TestClient(create_app(tmp_path / "exercise.sqlite3", setup_token="tak-api-test")) as value:
        yield value


def headers(session):
    return {"Authorization": "Bearer " + session["token"]}


def room(client, name="TAK training room"):
    response = client.post(API + "/rooms", json={"name": name, "participantName": "Controller"},
        headers={"X-Setup-Key": "tak-api-test"})
    assert response.status_code == 201, response.text
    return response.json()


def post(client, session, path, body=None):
    return client.post(API + path, json={} if body is None else body, headers=headers(session))


def saved(client, session):
    response = client.get(API + "/state", headers=headers(session))
    assert response.status_code == 200, response.text
    return response.json()


def report(client, session, **changes):
    payload = {"title": "Fictional training note", "text": "Report wording", "source": "Exercise Source A",
        "observedAt": None, "lat": 35.0, "lon": -110.0}
    payload.update(changes)
    response = post(client, session, "/reports", payload)
    assert response.status_code == 201, response.text
    return response.json()["result"]


def join(client, controller, role):
    invitation = post(client, controller, "/invitations", {"name": role.title(), "role": role})
    response = client.post(API + "/join", json={"inviteToken": invitation.json()["inviteToken"]})
    assert response.status_code == 200, response.text
    return response.json()


def transport_result(state="written_to_tls_socket"):
    now = iso(utcnow())
    return {"transportStatus": state, "clientReceipt": "unverified", "lastAttemptAt": now,
        "lastSentAt": now if state == "written_to_tls_socket" else None,
        "lastError": None if state == "written_to_tls_socket" else "Test transport did not confirm delivery.",
        "message": "Test TLS transport only; WinTAK receipt is unverified."}


def test_tak_routes_require_room_authentication(client):
    assert client.get(API + "/tak/status").status_code == 401
    assert client.get(API + "/tak/export").status_code == 401
    assert client.post(API + "/tak/send", json={}).status_code == 401
    bad = {"Authorization": "Bearer invalid"}
    assert client.get(API + "/tak/status", headers=bad).status_code == 401


@pytest.mark.parametrize("role", ["reviewer", "observer"])
def test_readers_can_inspect_export_but_cannot_send(client, monkeypatch, role):
    controller = room(client)
    report(client, controller)
    participant = join(client, controller, role)
    monkeypatch.setattr(tak.TakAdapter, "send", lambda *args: pytest.fail("role denial must precede transport"))
    assert client.get(API + "/tak/status", headers=headers(participant)).status_code == 200
    assert client.get(API + "/tak/export", headers=headers(participant)).status_code == 200
    assert post(client, participant, "/tak/send").status_code == 403
    assert not saved(client, controller).get("takAttempts")


def test_export_contains_only_this_rooms_current_report_annotations(client):
    alpha, beta = room(client, "Alpha"), room(client, "Beta")
    item = report(client, alpha, attachment={"name": "evidence.txt", "content": "ATTACHMENT_NOT_A_MAP_ANNOTATION"})
    report(client, alpha, title="No reported location", lat=None, lon=None)
    report(client, beta, text="OTHER_ROOM_PRIVATE_WORDING")
    revised = post(client, alpha, f"/reports/{item['id']}/revisions", {
        "expectedVersion": 1, "text": "Latest <review> & correction", "reason": "REASON_RETAINED_IN_CASE_ONLY"})
    assert revised.status_code == 200, revised.text
    response = client.get(API + "/tak/export", headers=headers(alpha))
    assert response.status_code == 200 and response.headers["content-type"].startswith("application/xml")
    event = ET.fromstring(response.content)
    assert event.tag == "event" and event.attrib["type"] == "b-m-p-s-m"
    assert "EXERCISE/TRAINING" in event.find("detail/contact").attrib["callsign"]
    assert "Latest <review> & correction" in event.findtext("detail/remarks")
    assert "Observed: Unavailable" in event.findtext("detail/remarks")
    assert event.find("detail/{urn:insightfuldefense:exercise:1}report").attrib["version"] == "2"
    for excluded in ("OTHER_ROOM_PRIVATE_WORDING", "ATTACHMENT_NOT_A_MAP_ANNOTATION", "REASON_RETAINED_IN_CASE_ONLY", alpha["token"], beta["token"]):
        assert excluded not in response.text


def test_backend_maximum_report_text_exports_without_truncation(client):
    controller = room(client)
    text = "A" * 9990 + "FINAL-TEXT"
    assert len(text) == 10000
    report(client, controller, text=text)
    response = client.get(API + "/tak/export", headers=headers(controller))
    assert response.status_code == 200, response.text
    assert ET.fromstring(response.content).findtext("detail/remarks").endswith(text)


def test_room_isolation_and_unknown_report_selection_precede_transport(client, monkeypatch):
    alpha, beta = room(client, "Alpha"), room(client, "Beta")
    item = report(client, alpha)
    report(client, beta)
    monkeypatch.setattr(tak.TakAdapter, "send", lambda *args: pytest.fail("invalid selection must not reach transport"))
    assert post(client, beta, "/tak/send", {"reportIds": [item["id"]]}).status_code == 404
    assert post(client, alpha, "/tak/send", {"reportIds": [item["id"], item["id"]]}).status_code == 422
    assert post(client, alpha, "/tak/send", {"reportIds": []}).status_code == 422
    assert post(client, alpha, "/tak/send", {"host": "browser-supplied.example", "keyFile": "never-read.pem"}).status_code == 422
    assert not saved(client, alpha).get("takAttempts") and not saved(client, beta).get("takAttempts")


def test_ended_room_remains_readable_but_send_is_inhibited(client, monkeypatch):
    controller = room(client)
    report(client, controller)
    assert post(client, controller, "/clock", {"action": "end"}).status_code == 200
    monkeypatch.setattr(tak.TakAdapter, "send", lambda *args: pytest.fail("ended room must not reach transport"))
    assert post(client, controller, "/tak/send").status_code == 409
    assert client.get(API + "/tak/status", headers=headers(controller)).status_code == 200
    assert client.get(API + "/tak/export", headers=headers(controller)).status_code == 200


@pytest.mark.parametrize("transport", ["not_sent", "delivery_unknown", "written_to_tls_socket"])
def test_send_intent_and_exact_report_versions_commit_before_transport(client, monkeypatch, transport):
    controller = room(client)
    first, second = report(client, controller), report(client, controller, title="Unselected report")
    received = []
    store = client.app.state.exercise_store
    auth = store.authenticate(controller["token"])
    outcome = transport_result(transport)
    def send(adapter, room_data, reports):
        committed = store.state_for(auth)
        attempt = committed["takAttempts"][-1]
        assert attempt["status"] == "pending" and attempt["reportVersions"] == {first["id"]: 1}
        assert [item["id"] for item in reports] == [first["id"]]
        assert set(reports[0]) == {"id", "title", "text", "source", "observedAt", "receivedAt", "lat", "lon", "version"}
        requested = store.events_for(auth, 0)[-1]
        assert requested["type"] == "tak.send_requested"
        assert requested["payload"]["reports"][0]["version"] == 1
        received.append(room_data["id"])
        return outcome
    monkeypatch.setattr(tak.TakAdapter, "send", send)
    response = post(client, controller, "/tak/send", {"reportIds": [first["id"]]})
    assert response.status_code == 200, response.text
    assert response.json()["clientReceipt"] == "unverified"
    assert response.json()["transportStatus"] == transport and len(received) == 1
    attempt = saved(client, controller)["takAttempts"][-1]
    assert attempt["status"] == "completed" and attempt["result"]["transportStatus"] == transport
    assert attempt["result"]["clientReceipt"] == "unverified" and attempt["result"]["markerCount"] == 1
    events = store.events_for(auth, 0)
    assert [event["type"] for event in events[-2:]] == ["tak.send_requested", "tak.send_finished"]
    assert events[-1]["payload"]["attemptId"] == attempt["id"]


def test_disabled_send_has_durable_not_sent_record(client):
    controller = room(client)
    report(client, controller)
    response = post(client, controller, "/tak/send")
    assert response.status_code == 200, response.text
    assert response.json()["transportStatus"] == "not_sent" and response.json()["clientReceipt"] == "unverified"
    attempt = saved(client, controller)["takAttempts"][-1]
    assert attempt["status"] == "completed" and attempt["result"]["lastAttemptAt"]


def test_validation_failure_completes_reserved_attempt_without_sending(client, monkeypatch):
    controller = room(client)
    report(client, controller)
    def send(*args):
        raise tak.TakValidationError("Selected annotation batch exceeds the XML limit.")
    monkeypatch.setattr(tak.TakAdapter, "send", send)
    response = post(client, controller, "/tak/send")
    assert response.status_code == 200, response.text
    assert response.json()["transportStatus"] == "not_sent"
    assert saved(client, controller)["takAttempts"][-1]["status"] == "completed"


def test_unfinished_send_survives_restart_and_blocks_duplicate_transfer(client, monkeypatch):
    controller = room(client)
    report(client, controller)
    store = client.app.state.exercise_store
    attempt = store.reserve_tak(store.authenticate(controller["token"]))
    monkeypatch.setattr(tak.TakAdapter, "send", lambda *args: pytest.fail("pending attempt must prevent automatic duplicate transfer"))
    with TestClient(create_app(store.db_path, setup_token="tak-api-test")) as restarted:
        existing = saved(restarted, controller)["takAttempts"][-1]
        assert existing["id"] == attempt["attemptId"] and existing["status"] == "pending"
        assert existing["clientReceipt"] == "unverified"
        status = restarted.get(API + "/tak/status", headers=headers(controller)).json()
        assert status["pendingAttemptId"] == attempt["attemptId"]
        assert status["transportStatus"] == "delivery_unknown" and status["lastSentAt"] is None
        assert status["lastAttemptAt"] == existing["createdAt"] and status["clientReceipt"] == "unverified"
        assert post(restarted, controller, "/tak/send").status_code == 409


def test_status_after_restart_uses_only_this_rooms_saved_transport_history(client, monkeypatch):
    alpha, beta = room(client, "Alpha"), room(client, "Beta")
    report(client, alpha)
    outcome = transport_result()
    monkeypatch.setattr(tak.TakAdapter, "send", lambda *args: outcome)
    assert post(client, alpha, "/tak/send").status_code == 200
    with TestClient(create_app(client.app.state.exercise_store.db_path, setup_token="tak-api-test")) as restarted:
        status = restarted.get(API + "/tak/status", headers=headers(alpha)).json()
        assert status["lastAttemptAt"] == outcome["lastAttemptAt"]
        assert status["lastSentAt"] == outcome["lastSentAt"]
        assert status["clientReceipt"] == "unverified"
        other = restarted.get(API + "/tak/status", headers=headers(beta)).json()
        assert other["lastAttemptAt"] is None and other["lastSentAt"] is None


def test_unexpected_transport_exception_leaves_durable_unknown_attempt(client, monkeypatch):
    controller = room(client)
    report(client, controller)
    def interrupted(*args):
        raise RuntimeError("internal interruption details must not be returned")
    monkeypatch.setattr(tak.TakAdapter, "send", interrupted)
    with TestClient(client.app, raise_server_exceptions=False) as interrupted_client:
        response = post(interrupted_client, controller, "/tak/send")
    assert response.status_code == 500 and "interruption details" not in response.text
    with TestClient(create_app(client.app.state.exercise_store.db_path, setup_token="tak-api-test")) as restarted:
        attempt = saved(restarted, controller)["takAttempts"][-1]
        assert attempt["status"] == "pending" and attempt["clientReceipt"] == "unverified"
        assert post(restarted, controller, "/tak/send").status_code == 409


def test_existing_transfer_outcome_can_be_saved_if_room_ends_during_io(client, monkeypatch):
    controller = room(client)
    report(client, controller)
    store = client.app.state.exercise_store
    auth = store.authenticate(controller["token"])
    def send(*args):
        store.clock(auth, "end")
        return transport_result("delivery_unknown")
    monkeypatch.setattr(tak.TakAdapter, "send", send)
    response = post(client, controller, "/tak/send")
    assert response.status_code == 200, response.text
    state = saved(client, controller)
    assert state["room"]["status"] == "ended"
    assert state["takAttempts"][-1]["status"] == "completed"
    assert state["takAttempts"][-1]["result"]["transportStatus"] == "delivery_unknown"
    assert post(client, controller, "/tak/send").status_code == 409
