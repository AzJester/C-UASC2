"""HTTP boundary for a shared, fictional desktop exercise workspace."""
from __future__ import annotations

import ipaddress
import os
import secrets
from pathlib import Path
from typing import Literal
from urllib.parse import urlsplit

from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse, Response
from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, field_validator, model_validator

from .store import ExerciseError, Store
from .tak import TakAdapter, TakValidationError

REPO_ROOT = Path(__file__).resolve().parents[2]
API = "/api/exercise"
MAX_BODY = 100_000


class ExerciseBoundary:
    """Reject cross-origin and non-JSON mutations before request parsing."""
    def __init__(self, app, allowed_hosts):
        self.app = app
        self.allowed_hosts = allowed_hosts

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        headers = {key.decode().lower(): value.decode() for key, value in scope.get("headers", [])}
        raw_host = headers.get("host", "")
        try:
            host = urlsplit("//" + raw_host)
            valid_host = bool(host.hostname) and host.hostname.lower().rstrip(".") in self.allowed_hosts
            valid_host = valid_host and not any(char in raw_host for char in "@/\\, \t\r\n") and host.port != 0
        except ValueError:
            valid_host = False
        if not valid_host:
            return await JSONResponse({"detail": "Host is not allowed for this exercise service"}, 400)(scope, receive, send)

        async def safe_send(message):
            if message["type"] == "http.response.start":
                message["headers"] = list(message.get("headers", [])) + [
                    (b"cache-control", b"no-store"), (b"x-content-type-options", b"nosniff"),
                    (b"referrer-policy", b"same-origin"), (b"x-frame-options", b"SAMEORIGIN")]
                if scope["path"] in {"/exercise.html", "/exercise.js", "/exercise.css"}:
                    message["headers"].append((b"content-security-policy", (
                        "default-src 'self'; script-src 'self'; style-src 'self'; "
                        "img-src 'self' data: https://server.arcgisonline.com; "
                        "connect-src 'self' https://server.arcgisonline.com; "
                        "object-src 'none'; base-uri 'self'; form-action 'self'; frame-ancestors 'self'"
                    ).encode()))
            await send(message)

        if scope["method"] == "POST" and scope["path"].startswith(API + "/"):
            own_origin = f"{scope.get('scheme', 'http')}://{headers.get('host', '')}"
            if (headers.get("origin") and headers["origin"] != own_origin) or headers.get("sec-fetch-site") == "cross-site":
                return await JSONResponse({"detail": "Cross-origin exercise changes are not allowed"}, 403)(scope, receive, safe_send)
            if headers.get("content-type", "").split(";", 1)[0].strip().lower() != "application/json":
                return await JSONResponse({"detail": "Exercise changes require application/json"}, 415)(scope, receive, safe_send)
            body = bytearray()
            while True:
                message = await receive()
                if message["type"] == "http.disconnect":
                    return
                body.extend(message.get("body", b""))
                if len(body) > MAX_BODY:
                    return await JSONResponse({"detail": "Exercise request exceeds the 100 KB limit"}, 413)(scope, receive, safe_send)
                if not message.get("more_body"):
                    break
            delivered = False

            async def bounded_receive():
                nonlocal delivered
                if not delivered:
                    delivered = True
                    return {"type": "http.request", "body": bytes(body), "more_body": False}
                return await receive()

            return await self.app(scope, bounded_receive, safe_send)
        return await self.app(scope, receive, safe_send)


class Input(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    @field_validator("*", mode="after")
    @classmethod
    def no_control_characters(cls, value):
        if isinstance(value, str) and any(ord(char) < 32 and char not in "\n\r\t" for char in value):
            raise ValueError("Control characters are not allowed")
        return value


class NewRoom(Input):
    name: str = Field(min_length=1, max_length=120)
    participantName: str = Field(min_length=1, max_length=80)


class Invitation(Input):
    name: str = Field(min_length=1, max_length=80)
    role: Literal["controller", "reviewer", "observer"]


class Join(Input):
    inviteToken: str = Field(min_length=20, max_length=256)


class TextAttachment(Input):
    name: str = Field(min_length=1, max_length=120)
    content: str = Field(min_length=1, max_length=50_000)

    @field_validator("name")
    @classmethod
    def simple_filename(cls, value):
        if any(char in value for char in '/\\:<>|?*') or value in {".", ".."}:
            raise ValueError("Use a plain attachment name without a path")
        return value

    @field_validator("content")
    @classmethod
    def text_size(cls, value):
        if len(value.encode("utf-8")) > 50_000:
            raise ValueError("Text attachments are limited to 50 KB")
        return value


class Report(Input):
    title: str = Field(min_length=1, max_length=160)
    text: str = Field(min_length=1, max_length=10_000)
    source: str = Field(min_length=1, max_length=160)
    observedAt: AwareDatetime | None = None
    lat: float | None = Field(default=None, ge=-90, le=90)
    lon: float | None = Field(default=None, ge=-180, le=180)
    attachment: TextAttachment | None = None

    @model_validator(mode="after")
    def coordinate_pair(self):
        if (self.lat is None) != (self.lon is None):
            raise ValueError("Provide both latitude and longitude, or leave both unavailable")
        return self


class Revision(Input):
    expectedVersion: int = Field(ge=1, le=200)
    text: str = Field(min_length=1, max_length=10_000)
    reason: str = Field(min_length=1, max_length=2_000)
    source: str | None = Field(default=None, min_length=1, max_length=160)
    observedAt: AwareDatetime | None = None


class ReviewRequest(Input):
    reportId: str = Field(min_length=1, max_length=80)
    assigneeId: str = Field(min_length=1, max_length=80)
    summary: str = Field(min_length=1, max_length=2_000)
    dueAt: AwareDatetime | None = None


class Resolution(Input):
    reason: str = Field(min_length=1, max_length=2_000)


class Handover(Input):
    toParticipantId: str = Field(min_length=1, max_length=80)
    note: str = Field(min_length=1, max_length=2_000)


class Clock(Input):
    action: Literal["start", "pause", "resume", "end"]


class Empty(Input):
    pass


class TakSend(Input):
    reportIds: list[str] | None = Field(default=None, max_length=100)


def loopback(request: Request):
    try:
        return bool(request.client and ipaddress.ip_address(request.client.host).is_loopback)
    except ValueError:
        return False


def create_app(db_path: str | Path | None = None, setup_token: str | None = None):
    app = FastAPI(title="C-UAS Shared Exercise", version="1", docs_url=None, redoc_url=None)
    allowed_hosts = {"localhost", "127.0.0.1", "::1", "testserver"}
    allowed_hosts.update(value.strip().lower().strip("[]").rstrip(".") for value in os.environ.get("EXERCISE_ALLOWED_HOSTS", "").split(",") if value.strip())
    app.add_middleware(ExerciseBoundary, allowed_hosts=allowed_hosts)
    store = Store(db_path or os.environ.get("EXERCISE_DB_PATH", str(REPO_ROOT / "work" / "exercise.sqlite3")))
    setup_key = os.environ.get("EXERCISE_SETUP_TOKEN", "") if setup_token is None else setup_token
    app.state.exercise_store = store
    # Transport status belongs to this room; credentials remain server-owned.
    adapters: dict[str, TakAdapter] = {}

    def adapter(room_id):
        if room_id not in adapters:
            adapters[room_id] = TakAdapter.from_env()
        return adapters[room_id]

    def auth(request: Request):
        scheme, _, token = request.headers.get("Authorization", "").partition(" ")
        if scheme.lower() != "bearer":
            raise ExerciseError(401, "An invitation-bound exercise session is required")
        return store.authenticate(token)

    app.state.exercise_auth = auth

    @app.exception_handler(ExerciseError)
    async def exercise_error(request, error):
        return JSONResponse({"detail": error.detail}, error.status)

    @app.get("/health")
    @app.get(API + "/health")
    def health(request: Request):
        return {"service": "exercise", "version": "1", "canCreate": bool(setup_key) or loopback(request),
                "setupRequired": bool(setup_key)}

    @app.get("/")
    def root():
        return RedirectResponse("/exercise.html")

    def static_file(name):
        path = REPO_ROOT / "site" / name
        if not path.is_file():
            return JSONResponse({"detail": "Exercise workspace file is unavailable"}, 404)
        return FileResponse(path)

    @app.get("/exercise.html")
    def exercise_html():
        return static_file("exercise.html")

    @app.get("/exercise.js")
    def exercise_js():
        return static_file("exercise.js")

    @app.get("/exercise.css")
    def exercise_css():
        return static_file("exercise.css")

    @app.get("/cop")
    @app.get("/index.html")
    def cop():
        return static_file("index.html")

    @app.post(API + "/rooms", status_code=201)
    def rooms(body: NewRoom, request: Request):
        if setup_key:
            if not secrets.compare_digest(request.headers.get("X-Setup-Key", ""), setup_key):
                raise ExerciseError(403, "A valid exercise setup key is required to create a room")
        elif not loopback(request):
            raise ExerciseError(403, "Room creation is loopback-only until EXERCISE_SETUP_TOKEN is configured")
        return store.create_room(body.name, body.participantName)

    @app.post(API + "/invitations", status_code=201)
    def invitations(body: Invitation, request: Request):
        return store.invite(auth(request), body.name, body.role)

    @app.post(API + "/join")
    def join(body: Join):
        return store.join(body.inviteToken)

    @app.get(API + "/state")
    def state(request: Request):
        return store.state_for(auth(request))

    @app.post(API + "/reports", status_code=201)
    def reports(body: Report, request: Request):
        return store.add_report(auth(request), body.model_dump(mode="json"))

    @app.post(API + "/reports/{report_id}/revisions")
    def revisions(report_id: str, body: Revision, request: Request):
        return store.revise_report(auth(request), report_id, body.model_dump(mode="json"))

    @app.post(API + "/requests", status_code=201)
    def requests(body: ReviewRequest, request: Request):
        return store.add_request(auth(request), body.model_dump(mode="json"))

    @app.post(API + "/requests/{request_id}/ack")
    def acknowledge(request_id: str, body: Empty, request: Request):
        return store.transition_request(auth(request), request_id, "ack")

    @app.post(API + "/requests/{request_id}/resolve")
    def resolve(request_id: str, body: Resolution, request: Request):
        return store.transition_request(auth(request), request_id, "resolve", body.reason)

    @app.post(API + "/handover", status_code=201)
    def handover(body: Handover, request: Request):
        return store.offer_handover(auth(request), body.model_dump())

    @app.post(API + "/handover/{handover_id}/accept")
    def accept(handover_id: str, body: Empty, request: Request):
        return store.accept_handover(auth(request), handover_id)

    @app.post(API + "/clock")
    def clock(body: Clock, request: Request):
        return store.clock(auth(request), body.action)

    @app.post(API + "/scenario/advance")
    def advance(body: Empty, request: Request):
        return store.advance_scenario(auth(request))

    @app.get(API + "/events")
    def events(request: Request, after: int = 0):
        if after < 0:
            raise ExerciseError(422, "Event sequence cannot be negative")
        result = store.events_for(auth(request), after)
        return {"events": result, "latestSeq": result[-1]["seq"] if result else after}

    @app.get(API + "/replay")
    def replay(request: Request, through: int):
        if through < 1:
            raise ExerciseError(422, "Choose a saved event sequence")
        return store.state_for(auth(request), through)

    @app.get(API + "/export")
    def export(request: Request):
        return JSONResponse(store.export(auth(request)), headers={"Content-Disposition": 'attachment; filename="exercise-record.json"'})

    @app.get(API + "/tak/status")
    def tak_status(request: Request):
        member = auth(request)
        result = adapter(member["roomId"]).status()
        attempts = store.state_for(member).get("takAttempts", [])
        if attempts:
            latest = attempts[-1]
            outcome = latest.get("result", {})
            result["lastAttemptAt"] = outcome.get("lastAttemptAt") or latest["createdAt"]
            result["lastSentAt"] = next((item.get("result", {}).get("lastSentAt")
                for item in reversed(attempts) if item.get("result", {}).get("lastSentAt")), None)
            result["pendingAttemptId"] = latest["id"] if latest["status"] == "pending" else None
            result["lastError"] = result["lastError"] or (
                "A saved transfer has no recorded completion. Delivery is unknown; review its record before another send."
                if latest["status"] == "pending" else outcome.get("lastError"))
            result["transportStatus"] = "delivery_unknown" if latest["status"] == "pending" else outcome.get("transportStatus", "not_sent")
        return result

    @app.get(API + "/tak/export")
    def tak_export(request: Request):
        member = auth(request)
        saved = store.state_for(member)
        try:
            xml = adapter(member["roomId"]).export(saved["room"], saved["reports"])
        except TakValidationError as error:
            raise ExerciseError(422, str(error)) from None
        return Response(xml, media_type="application/xml", headers={
            "Content-Disposition": 'attachment; filename="training-report-review.xml"'})

    @app.post(API + "/tak/send")
    def tak_send(body: TakSend, request: Request):
        member = auth(request)
        attempt = store.reserve_tak(member, body.reportIds)
        try:
            result = adapter(member["roomId"]).send(attempt["room"], attempt["reports"])
        except TakValidationError as error:
            result = {"transportStatus": "not_sent", "clientReceipt": "unverified",
                      "lastError": str(error), "message": str(error)}
        store.finish_tak(member, attempt["attemptId"], result)
        return result

    return app


app = create_app()
