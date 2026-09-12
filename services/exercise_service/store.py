"""Persistent, room-scoped exercise records and invitation authentication.

This service records human training reports. It has no connection to sensors,
effectors, identity declarations, or the C2 authority/release policy.
"""
from __future__ import annotations

import copy
import hashlib
import json
import secrets
import sqlite3
import threading
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def iso(value: datetime | None = None) -> str:
    return (value or utcnow()).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def parse_time(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def token_hash(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def new_id(prefix: str) -> str:
    return f"{prefix}-{secrets.token_hex(8)}"


class ExerciseError(Exception):
    def __init__(self, status: int, detail: str):
        self.status = status
        self.detail = detail
        super().__init__(detail)


class Store:
    def __init__(self, db_path: str | Path, session_hours: int = 24):
        self.db_path = Path(db_path)
        self.session_hours = session_hours
        self._lock = threading.RLock()
        self._ready = False

    def _connect(self):
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        db = sqlite3.connect(str(self.db_path), timeout=15)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA foreign_keys = ON")
        if not self._ready:
            db.executescript("""
                PRAGMA journal_mode=WAL;
                CREATE TABLE IF NOT EXISTS rooms (
                    id TEXT PRIMARY KEY, state TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS sessions (
                    token_hash TEXT PRIMARY KEY,
                    room_id TEXT NOT NULL REFERENCES rooms(id),
                    participant_id TEXT NOT NULL, expires_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS invitations (
                    token_hash TEXT PRIMARY KEY,
                    room_id TEXT NOT NULL REFERENCES rooms(id),
                    name TEXT NOT NULL, role TEXT NOT NULL,
                    expires_at TEXT NOT NULL, used_at TEXT
                );
                CREATE TABLE IF NOT EXISTS events (
                    seq INTEGER PRIMARY KEY AUTOINCREMENT,
                    room_id TEXT NOT NULL REFERENCES rooms(id),
                    event TEXT NOT NULL, state_after TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS events_by_room ON events(room_id, seq);
                CREATE TRIGGER IF NOT EXISTS events_no_update BEFORE UPDATE ON events
                    BEGIN SELECT RAISE(ABORT, 'Exercise events are append-only'); END;
                CREATE TRIGGER IF NOT EXISTS events_no_delete BEFORE DELETE ON events
                    BEGIN SELECT RAISE(ABORT, 'Exercise events are append-only'); END;
            """)
            self._ready = True
        return db

    @contextmanager
    def transaction(self):
        with self._lock:
            db = self._connect()
            try:
                db.execute("BEGIN IMMEDIATE")
                yield db
                db.commit()
            except Exception:
                db.rollback()
                raise
            finally:
                db.close()

    @staticmethod
    def _load(db, room_id):
        row = db.execute("SELECT state FROM rooms WHERE id=?", (room_id,)).fetchone()
        if not row:
            raise ExerciseError(404, "Exercise room not found")
        return json.loads(row["state"])

    @staticmethod
    def _participant(state, participant_id):
        person = next((p for p in state["participants"] if p["id"] == participant_id), None)
        if person is None:
            raise ExerciseError(401, "Participant membership is no longer available")
        return person

    @staticmethod
    def _writable(state):
        if state["room"]["status"] == "ended":
            raise ExerciseError(409, "This exercise has ended and is read-only")

    def _authorize(self, db, auth, roles=None, writable=False):
        state = self._load(db, auth["roomId"])
        person = self._participant(state, auth["participantId"])
        if roles and person["role"] not in roles:
            raise ExerciseError(403, "Your exercise role does not allow this action")
        if writable:
            self._writable(state)
        return state, person

    def authenticate(self, token: str):
        if not token or len(token) > 256:
            raise ExerciseError(401, "An exercise session is required")
        with self.transaction() as db:
            session = db.execute("SELECT * FROM sessions WHERE token_hash=?", (token_hash(token),)).fetchone()
            if not session or parse_time(session["expires_at"]) <= utcnow():
                raise ExerciseError(401, "Exercise session is invalid or expired")
            state = self._load(db, session["room_id"])
            person = self._participant(state, session["participant_id"])
            return {"roomId": session["room_id"], "participantId": person["id"], "participant": person}

    def _session(self, db, room_id, participant):
        token = secrets.token_urlsafe(32)
        expires_at = iso(utcnow() + timedelta(hours=self.session_hours))
        db.execute("INSERT INTO sessions VALUES (?,?,?,?)", (token_hash(token), room_id, participant["id"], expires_at))
        return {"roomId": room_id, "participant": participant, "token": token, "expiresAt": expires_at}

    def _append(self, db, state, actor, kind, summary, payload):
        count = db.execute("SELECT COUNT(*) FROM events WHERE room_id=?", (state["room"]["id"],)).fetchone()[0]
        reserved_completions = sum(attempt["status"] == "pending" for attempt in state.get("takAttempts", []))
        if count + 1 + reserved_completions > 2000:
            raise ExerciseError(409, "This pilot exercise has reached its 2,000-event limit. Export its record and create a new room")
        state_text = json.dumps(state)
        if len(state_text.encode("utf-8")) + reserved_completions * 32_000 > 1_000_000:
            raise ExerciseError(409, "This pilot exercise has reached its 1 MB saved-state limit. Export its record and create a new room")
        event = {"type": kind, "at": iso(), "actor": {"id": actor["id"], "name": actor["name"]},
                 "summary": summary, "payload": payload}
        # State is the saved observation at this event. The public live state can
        # derive a running clock; historical snapshots never advance that clock.
        snapshot = self._project_state(state)
        row = db.execute("INSERT INTO events(room_id,event,state_after) VALUES (?,?,?)",
                         (state["room"]["id"], json.dumps(event), json.dumps(snapshot)))
        seq = row.lastrowid
        event["seq"] = seq
        db.execute("UPDATE rooms SET state=? WHERE id=?", (state_text, state["room"]["id"]))
        return event

    def create_room(self, name, participant_name):
        with self.transaction() as db:
            person = {"id": new_id("person"), "name": participant_name, "role": "controller", "joinedAt": iso()}
            room = {"id": new_id("room"), "name": name, "status": "ready", "createdAt": iso(),
                    "elapsedSeconds": 0, "scenarioStep": -1, "clockStartedAt": None}
            state = {"room": room, "participants": [person], "reports": [], "requests": [], "handovers": []}
            db.execute("INSERT INTO rooms VALUES (?,?)", (room["id"], json.dumps(state)))
            self._append(db, state, person, "room.created", "Fictional exercise room created", {"room": room, "participant": person})
            return self._session(db, room["id"], person)

    def invite(self, auth, name, role):
        with self.transaction() as db:
            state, actor = self._authorize(db, auth, {"controller"}, True)
            invite_count = db.execute("SELECT COUNT(*) FROM invitations WHERE room_id=?", (auth["roomId"],)).fetchone()[0]
            if invite_count >= 200:
                raise ExerciseError(409, "This pilot exercise has reached its 200-invitation limit")
            token = secrets.token_urlsafe(32)
            expires_at = iso(utcnow() + timedelta(hours=1))
            db.execute("INSERT INTO invitations VALUES (?,?,?,?,?,NULL)",
                       (token_hash(token), auth["roomId"], name, role, expires_at))
            self._append(db, state, actor, "invitation.created", f"Invitation created for {name}",
                         {"name": name, "role": role, "expiresAt": expires_at})
            return {"inviteToken": token, "expiresAt": expires_at}

    def join(self, token):
        with self.transaction() as db:
            invitation = db.execute("SELECT * FROM invitations WHERE token_hash=?", (token_hash(token),)).fetchone()
            if not invitation or invitation["used_at"] or parse_time(invitation["expires_at"]) <= utcnow():
                raise ExerciseError(401, "Invitation is invalid, expired, or already used")
            state = self._load(db, invitation["room_id"])
            self._writable(state)
            if len(state["participants"]) >= 100:
                raise ExerciseError(409, "This exercise has reached its participant limit")
            person = {"id": new_id("person"), "name": invitation["name"], "role": invitation["role"], "joinedAt": iso()}
            state["participants"].append(person)
            db.execute("UPDATE invitations SET used_at=? WHERE token_hash=?", (iso(), token_hash(token)))
            self._append(db, state, person, "participant.joined", f"{person['name']} joined as {person['role']}", {"participant": person})
            return self._session(db, invitation["room_id"], person)

    @staticmethod
    def _project_state(state):
        result = copy.deepcopy(state)
        room = result["room"]
        if room.get("clockStartedAt") and room["status"] == "running":
            room["elapsedSeconds"] += max(0, (utcnow() - parse_time(room["clockStartedAt"])).total_seconds())
        room["elapsedSeconds"] = round(room["elapsedSeconds"], 1)
        room.pop("clockStartedAt", None)
        return result

    @staticmethod
    def _events(db, room_id, through=None):
        query = "SELECT seq,event FROM events WHERE room_id=?"
        args = [room_id]
        if through is not None:
            query += " AND seq<=?"
            args.append(through)
        query += " ORDER BY seq"
        return [{**json.loads(row["event"]), "seq": row["seq"]} for row in db.execute(query, args)]

    def state_for(self, auth, through=None):
        with self.transaction() as db:
            current, person = self._authorize(db, auth)
            if through is None:
                result = self._project_state(current)
            else:
                row = db.execute("SELECT seq,event,state_after FROM events WHERE room_id=? AND seq<=? ORDER BY seq DESC LIMIT 1",
                                 (auth["roomId"], through)).fetchone()
                if not row:
                    raise ExerciseError(404, "No saved exercise state exists at this event")
                result = json.loads(row["state_after"])
                result.update(historical=True, throughSeq=row["seq"], snapshotAt=json.loads(row["event"])["at"])
            events = self._events(db, auth["roomId"], through)
            result.update(participant=person, events=events, latestSeq=events[-1]["seq"] if events else 0, serverTime=iso())
            return result

    def events_for(self, auth, after=0):
        with self.transaction() as db:
            self._authorize(db, auth)
            return [event for event in self._events(db, auth["roomId"]) if event["seq"] > after]

    def record(self, auth, kind, summary, payload, mutator: Callable, roles=None):
        """Atomic integration hook. Mutator receives (saved_state, current_actor)."""
        with self.transaction() as db:
            state, actor = self._authorize(db, auth, roles or {"controller", "reviewer"}, True)
            result = mutator(state, actor)
            event = self._append(db, state, actor, kind, summary, payload if payload is not None else result)
            return {"event": event, "result": result}

    @staticmethod
    def _find(items, item_id, label):
        item = next((item for item in items if item["id"] == item_id), None)
        if item is None:
            raise ExerciseError(404, f"{label} not found in this exercise")
        return item

    @staticmethod
    def _new_report(state, actor, data):
        if len(state["reports"]) >= 100:
            raise ExerciseError(409, "This exercise has reached its report limit")
        report = {**data, "id": new_id("report"), "receivedAt": iso(), "version": 1, "createdBy": actor["id"]}
        report["revisions"] = [{key: report.get(key) for key in ("version", "text", "source", "observedAt", "receivedAt")} |
                               {"reason": "Initial report", "actor": {"id": actor["id"], "name": actor["name"]}}]
        state["reports"].append(report)
        return report

    def add_report(self, auth, data):
        return self.record(auth, "report.created", f"Training report received: {data['title']}", None,
                           lambda state, actor: self._new_report(state, actor, data))

    @staticmethod
    def _revise_report(state, actor, report_id, data):
        report = Store._find(state["reports"], report_id, "Report")
        if report["version"] != data["expectedVersion"]:
            raise ExerciseError(409, "Report changed since you opened it. Refresh and review the latest version")
        if len(report["revisions"]) >= 200:
            raise ExerciseError(409, "This report has reached its revision limit")
        report["version"] += 1
        report["text"] = data["text"]
        for key in ("source", "observedAt"):
            if data.get(key) is not None:
                report[key] = data[key]
        report["receivedAt"] = iso()
        report["revisions"].append({key: report.get(key) for key in ("version", "text", "source", "observedAt", "receivedAt")} |
                                   {"reason": data["reason"], "actor": {"id": actor["id"], "name": actor["name"]}})
        return report

    def revise_report(self, auth, report_id, data):
        return self.record(auth, "report.corrected", "Report corrected with retained source history", None,
                           lambda state, actor: self._revise_report(state, actor, report_id, data))

    def add_request(self, auth, data):
        def change(state, actor):
            self._find(state["reports"], data["reportId"], "Report")
            assignee = self._participant(state, data["assigneeId"])
            if assignee["role"] == "observer":
                raise ExerciseError(409, "Assign review work to a controller or reviewer")
            if len(state["requests"]) >= 300:
                raise ExerciseError(409, "This exercise has reached its request limit")
            request = {**data, "id": new_id("request"), "status": "open", "createdAt": iso(), "createdBy": actor["id"]}
            state["requests"].append(request)
            return request
        return self.record(auth, "request.created", "Review request assigned", None, change)

    def transition_request(self, auth, request_id, action, reason=None):
        def change(state, actor):
            request = self._find(state["requests"], request_id, "Request")
            if request["assigneeId"] != actor["id"]:
                raise ExerciseError(403, "Only the assigned participant can acknowledge or resolve this request")
            required = "open" if action == "ack" else "acknowledged"
            if request["status"] != required:
                raise ExerciseError(409, f"Request must be {required} before this action")
            request["status"] = "acknowledged" if action == "ack" else "resolved"
            request["acknowledgedAt" if action == "ack" else "resolvedAt"] = iso()
            if reason is not None:
                request["resolution"] = reason
            return request
        return self.record(auth, f"request.{'acknowledged' if action == 'ack' else 'resolved'}", "Review request updated", None, change)

    def offer_handover(self, auth, data):
        def change(state, actor):
            target = self._participant(state, data["toParticipantId"])
            if target["id"] == actor["id"]:
                raise ExerciseError(409, "Choose a different participant for handover")
            if target["role"] == "observer":
                raise ExerciseError(409, "Handover requires a controller or reviewer")
            if any(h["status"] == "pending" and h["fromParticipantId"] == actor["id"] for h in state["handovers"]):
                raise ExerciseError(409, "Accept the pending handover before starting another")
            handover = {**data, "id": new_id("handover"), "fromParticipantId": actor["id"], "status": "pending", "createdAt": iso()}
            state["handovers"].append(handover)
            return handover
        return self.record(auth, "handover.offered", "Controller handover offered", None, change, {"controller"})

    def accept_handover(self, auth, handover_id):
        def change(state, actor):
            handover = self._find(state["handovers"], handover_id, "Handover")
            if handover["toParticipantId"] != actor["id"]:
                raise ExerciseError(403, "Only the named recipient can accept this handover")
            if handover["status"] != "pending":
                raise ExerciseError(409, "This handover has already been accepted")
            previous = self._participant(state, handover["fromParticipantId"])
            if previous["role"] != "controller":
                raise ExerciseError(409, "The outgoing participant is no longer a controller")
            previous["role"] = "reviewer"
            actor["role"] = "controller"
            handover.update(status="accepted", acceptedAt=iso())
            return handover
        return self.record(auth, "handover.accepted", "Controller responsibility transferred", None, change)

    def clock(self, auth, action):
        def change(state, actor):
            room = state["room"]
            allowed = {"start": {"ready"}, "pause": {"running"}, "resume": {"paused"}, "end": {"ready", "running", "paused"}}
            if room["status"] not in allowed[action]:
                raise ExerciseError(409, f"Cannot {action} an exercise that is {room['status']}")
            room["elapsedSeconds"] = self._project_state(state)["room"]["elapsedSeconds"]
            room["status"] = {"start": "running", "pause": "paused", "resume": "running", "end": "ended"}[action]
            room["clockStartedAt"] = iso() if room["status"] == "running" else None
            if action == "end":
                room["endedAt"] = iso()
            return self._project_state(state)["room"]
        return self.record(auth, f"clock.{action}", f"Exercise clock: {action}", None, change, {"controller"})

    def advance_scenario(self, auth):
        def change(state, actor):
            step = state["room"]["scenarioStep"] + 1
            if step > 5:
                raise ExerciseError(409, "All six fictional checkpoints have been published")
            state["room"]["scenarioStep"] = step
            report = None
            cases = [
                ("Initial visual report", "Training source A", "A small object was reported near the exercise meeting area. Identity is unconfirmed.", 0, 38.8895, -77.0353),
                ("Conflicting visual account", "Training source B", "A second observer reports a bird in the same general area. This conflicts with source A's possible small-drone description. Neither account establishes identity.", 15, 38.8901, -77.0348),
                ("Delayed delivery notice", "Exercise delivery coordinator", "Source A's connection is interrupted. The last observation is older than its receipt time. No new observation was made by this notice.", 90, 38.8895, -77.0353),
                None,
                ("Coordination pending", "Training airport liaison", "A liaison clarification is pending. Assign a review request and record its acknowledgement before treating coordination as complete.", 20, 38.8901, -77.0348),
                ("End-of-case review", "Exercise facilitator", "Review outstanding requests, source disagreements, corrections, and handover ownership. This checkpoint does not establish an operational outcome or end the room automatically.", 0, 38.8895, -77.0353),
            ]
            if step == 3:
                previous = next((r for r in state["reports"] if r.get("scenarioCheckpoint") == 1), None)
                if previous:
                    report = self._revise_report(state, actor, previous["id"], {
                        "expectedVersion": previous["version"], "text": "Source B has corrected its account: likely biological activity. Confidence remains limited; source A's earlier description is not silently overwritten.",
                        "source": "Training source B", "observedAt": iso(utcnow() - timedelta(seconds=30)),
                        "reason": "Source reviewed the original observation and issued a correction."})
            else:
                title, source, text, delay, lat, lon = cases[step]
                report = self._new_report(state, actor, {"title": title, "source": source, "text": text,
                          "observedAt": iso(utcnow() - timedelta(seconds=delay)), "lat": lat, "lon": lon,
                          "scenarioCheckpoint": step})
            return {"checkpoint": step, "scriptElapsedSeconds": step * 60, "report": report,
                    "fictional": True, "note": "Instructor-paced checkpoint; scenario time is separate from the saved room clock."}
        return self.record(auth, "scenario.checkpoint", "Fictional coordination checkpoint published", None, change, {"controller"})

    def export(self, auth):
        result = self.state_for(auth)
        result.update(format="cuas-shared-exercise", formatVersion=1, exportedAt=iso(),
                      trainingOnly=True, assumptions=[
                          "Human-entered and scripted fictional training reports; no sensor or weapon integration.",
                          "The service authenticates invitation holders, not independently verified personal identities.",
                          "Server receipt times and source observation times have different meanings.",
                          "The append-only application record is not a cryptographically tamper-evident audit system.",
                          "Manual checkpoints have scripted times independent of the saved room clock."])
        return result

    def reserve_tak(self, auth, report_ids=None):
        """Persist intent and immutable report versions before external I/O.

        The caller must release this transaction before opening any connection.
        Only one unresolved send may exist per room, avoiding concurrent repeats.
        A process failure leaves an explicitly pending record, never a success.
        """
        with self.transaction() as db:
            state, actor = self._authorize(db, auth, {"controller"}, True)
            attempts = state.setdefault("takAttempts", [])
            if any(attempt["status"] == "pending" for attempt in attempts):
                raise ExerciseError(409, "A TAK send is already pending or has an unknown outcome. Review its record before retrying")
            if len(attempts) >= 100:
                raise ExerciseError(409, "This pilot exercise has reached its 100-transfer limit")
            if report_ids is None:
                selected = [report for report in state["reports"] if report.get("lat") is not None and report.get("lon") is not None]
            else:
                if not report_ids or len(report_ids) > 100 or len(set(report_ids)) != len(report_ids):
                    raise ExerciseError(422, "Select between 1 and 100 distinct report IDs")
                selected = [self._find(state["reports"], report_id, "Report") for report_id in report_ids]
            if not selected:
                raise ExerciseError(409, "No reports with locations are available to send")
            if any(report.get("lat") is None or report.get("lon") is None for report in selected):
                raise ExerciseError(422, "All selected reports must have a reported latitude and longitude")
            reports = [{key: report.get(key) for key in ("id", "title", "text", "source", "observedAt", "receivedAt", "lat", "lon", "version")}
                       for report in selected]
            attempt = {"id": new_id("tak"), "participantId": actor["id"], "status": "pending", "createdAt": iso(),
                       "reportIds": [report["id"] for report in reports],
                       "reportVersions": {report["id"]: report["version"] for report in reports}, "clientReceipt": "unverified"}
            attempts.append(attempt)
            room = self._project_state(state)["room"]
            self._append(db, state, actor, "tak.send_requested", "Manual TAK training-report transfer requested",
                         {"attempt": attempt, "room": room, "reports": reports})
            return {"attemptId": attempt["id"], "room": room, "reports": reports}

    def finish_tak(self, auth, attempt_id, result):
        """Record a previously authorized transport outcome, including after end.

        This cannot start a send or modify exercise evidence. It only completes
        the actor's existing pending record after external I/O has returned.
        """
        with self.transaction() as db:
            state, actor = self._authorize(db, auth)
            attempt = self._find(state.get("takAttempts", []), attempt_id, "TAK transfer attempt")
            if attempt["participantId"] != actor["id"]:
                raise ExerciseError(403, "Only the participant who started this transfer can record its outcome")
            if attempt["status"] != "pending":
                raise ExerciseError(409, "This TAK transfer outcome has already been recorded")
            outcome = {"transportStatus": result.get("transportStatus") if result.get("transportStatus") in
                       {"not_sent", "delivery_unknown", "written_to_tls_socket"} else "delivery_unknown",
                       "clientReceipt": "unverified", "markerCount": len(attempt["reportIds"])}
            for key in ("message", "lastError"):
                value = result.get(key)
                if isinstance(value, str):
                    outcome[key] = "".join(char for char in value if ord(char) >= 32 or char in "\n\t")[:1000]
            for key in ("lastAttemptAt", "lastSentAt"):
                value = result.get(key)
                if isinstance(value, str) and len(value) <= 60:
                    try:
                        parsed = parse_time(value)
                        if parsed.tzinfo is not None:
                            outcome[key] = iso(parsed.astimezone(timezone.utc))
                    except ValueError:
                        pass
            attempt.update(status="completed", finishedAt=iso(), result=outcome)
            return self._append(db, state, actor, "tak.send_finished", "TAK transport outcome recorded; client receipt remains unverified",
                                {"attemptId": attempt_id, "result": outcome})
