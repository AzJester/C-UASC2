"""Optional, manual CoT map annotations for the fictional review exercise.

This module has no dependencies on c2-core, tracks, sensors or commands. It sends
only explicit EXERCISE/TRAINING annotations. A TLS write is not a client receipt.

CoT envelope/point references: TAK-Product-Center/Server, cotevent.proto and
ProtoAndCotMessageConversionTests.java. TLS reference: that project's README
Certificates section. Unknown point values follow the XML fixtures (9999999).
"""
from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import datetime, timedelta, timezone
import hashlib
import json
import math
import os
from pathlib import Path
import queue
import re
import socket
import ssl
import threading
import time
import xml.etree.ElementTree as ET

MAX_REPORTS = 100
MAX_XML_BYTES = 512 * 1024
MAX_CERT_BYTES = 1024 * 1024
UNKNOWN_POINT_VALUE = "9999999.0"
MARKER_LIFETIME_SECONDS = 15 * 60
SOCKET_TIMEOUT_SECONDS = 5.0
SERVER_CONTROL_TIMEOUT_SECONDS = 1.0
SERVER_DRAIN_TIMEOUT_SECONDS = 0.25


def _read_server_control(secure: ssl.SSLSocket, *, before_write: bool) -> None:
    """Allow TAK's asynchronous subscription setup before closing the stream.

    The initial CoT protocol announcement follows the authenticated subscription
    setup in TAK Server. It is not an acknowledgement of any training report.
    Older compatible peers may send nothing, so this wait is strictly bounded.
    Incoming control bytes are discarded and never treated as exercise content.
    """
    duration = SERVER_CONTROL_TIMEOUT_SECONDS if before_write else SERVER_DRAIN_TIMEOUT_SECONDS
    deadline = time.monotonic() + duration
    data = bytearray()
    try:
        while len(data) < 65536:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            secure.settimeout(remaining)
            chunk = secure.recv(min(8192, 65536 - len(data)))
            if not chunk:
                break
            data.extend(chunk)
            if before_write and b"</event>" in data:
                break
    except (TimeoutError, ssl.SSLWantReadError):
        pass
    finally:
        secure.settimeout(SOCKET_TIMEOUT_SECONDS)


class TakValidationError(ValueError):
    """The requested training annotations are not valid bounded report data."""


class TakConfigurationError(ValueError):
    """The server-owned TAK configuration cannot be used securely."""


def _utc() -> datetime:
    return datetime.now(timezone.utc)


def _iso(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _text(value: object, field: str, limit: int, *, required: bool = True) -> str:
    if value is None and not required:
        return ""
    if not isinstance(value, str) or len(value) > limit or (required and not value.strip()):
        raise TakValidationError(f"{field} must be text of at most {limit} characters.")
    # XML 1.0 forbids control characters, unpaired surrogates and noncharacters.
    if any(not (c in "\t\n\r" or 0x20 <= ord(c) <= 0xD7FF or 0xE000 <= ord(c) <= 0xFFFD or 0x10000 <= ord(c) <= 0x10FFFF) for c in value):
        raise TakValidationError(f"{field} contains an unsupported control character.")
    return value


def _timestamp(value: object, field: str) -> str:
    raw = _text(value, field, 64)
    try:
        result = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        if result.tzinfo is None or result.utcoffset() is None:
            raise ValueError
        return _iso(result)
    except (ValueError, OverflowError):
        raise TakValidationError(f"{field} must be an ISO timestamp with a timezone.") from None


def _coordinate(value: object, name: str, limit: float) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise TakValidationError(f"{name} must be a finite coordinate.")
    value = float(value)
    if not math.isfinite(value) or not -limit <= value <= limit:
        raise TakValidationError(f"{name} is outside the valid coordinate range.")
    return value


def _events(room: Mapping, reports: Sequence, now: datetime) -> tuple[list[ET.Element], int]:
    if not isinstance(room, Mapping):
        raise TakValidationError("Exercise room must be an object.")
    room_id = _text(room.get("id"), "room id", 128)
    room_name = _text(room.get("name", room.get("title", room_id)), "room name", 160)
    if not isinstance(reports, (list, tuple)) or len(reports) > MAX_REPORTS:
        raise TakValidationError(f"At most {MAX_REPORTS} reports may be exported together.")
    events, seen, omitted = [], set(), 0
    for report in reports:
        if not isinstance(report, Mapping):
            raise TakValidationError("Each report must be an object.")
        lat, lon = report.get("lat"), report.get("lon")
        if lat is None and lon is None:
            omitted += 1
            continue
        lat, lon = _coordinate(lat, "Latitude", 90), _coordinate(lon, "Longitude", 180)
        report_id = _text(report.get("id"), "report id", 128)
        if report_id in seen:
            raise TakValidationError("Export only the current version of each report.")
        seen.add(report_id)
        title = _text(report.get("title"), "report title", 160)
        body = _text(report.get("text", ""), "report text", 10000, required=False)
        source = _text(report.get("source", "Unspecified exercise source"), "report source", 160)
        observed = _timestamp(report["observedAt"], "Observation time") if report.get("observedAt") is not None else "Unavailable"
        received = _timestamp(report.get("receivedAt"), "Receipt time")
        version = report.get("version", 1)
        if isinstance(version, bool) or not isinstance(version, int) or not 1 <= version <= 1000000:
            raise TakValidationError("Report version must be a positive bounded integer.")
        # Fixed-width hashes prevent special characters and delimiter ambiguity;
        # updating a report keeps the same UID instead of creating another point.
        uid = "exercise-" + hashlib.sha256(json.dumps([room_id, report_id]).encode()).hexdigest()[:32]
        event = ET.Element("event", {
            "version": "2.0", "uid": uid, "type": "b-m-p-s-m", "how": "h-e",
            "access": "Undefined", "opex": "e", "time": _iso(now),
            "start": _iso(now), "stale": _iso(now + timedelta(seconds=MARKER_LIFETIME_SECONDS)),
        })
        ET.SubElement(event, "point", {"lat": f"{lat:.7f}", "lon": f"{lon:.7f}",
            "hae": UNKNOWN_POINT_VALUE, "ce": UNKNOWN_POINT_VALUE, "le": UNKNOWN_POINT_VALUE})
        detail = ET.SubElement(event, "detail")
        ET.SubElement(detail, "contact", {"callsign": f"EXERCISE/TRAINING {title}"[:190]})
        ET.SubElement(detail, "precisionlocation", {"geopointsrc": "USER", "altsrc": "???"})
        # Visible remarks carry essential meaning even when an external client
        # ignores the custom evidence metadata.
        ET.SubElement(detail, "remarks").text = (
            f"EXERCISE / TRAINING ONLY. Fictional review annotation, not a live observation.\n"
            f"Exercise: {room_name}\nReport: {report_id}, version {version}\n"
            f"Source: {source}\nObserved: {observed}\nReceived: {received}\n"
            f"Annotation exported: {_iso(now)}. Marker expires in 15 minutes; this does not refresh the report.\n"
            f"Altitude and positional accuracy: unknown.\n{body}"
        )
        # TAK 5.8's CoT stream parser drops events containing XML namespace
        # prefixes, including prefixes declared only on a custom detail child.
        # Keep the custom tag unqualified and identify its schema explicitly.
        ET.SubElement(detail, "exerciseReport", {
            "schema": "urn:insightfuldefense:exercise:1",
            "roomId": room_id, "reportId": report_id, "version": str(version),
            "observedAt": observed if observed != "Unavailable" else "", "receivedAt": received,
            "observationTimeKnown": str(observed != "Unavailable").lower(), "training": "true",
        })
        events.append(event)
    size = sum(len(ET.tostring(event, encoding="utf-8")) for event in events)
    if size > MAX_XML_BYTES:
        raise TakValidationError("Training marker export exceeds the size limit.")
    return events, omitted


def _bounded_file(path: Path, limit: int) -> bytes:
    if not path.is_file() or path.stat().st_size > limit:
        raise TakConfigurationError("A configured credential file is missing or too large.")
    data = path.read_bytes()
    if not data or len(data) > limit:
        raise TakConfigurationError("A configured credential file is empty or too large.")
    return data


def validate_config(config_path: Path) -> tuple[str, int, ssl.SSLContext]:
    """Validate file material locally. This performs no network connection."""
    try:
        config_path = config_path.resolve(strict=True)
        config = json.loads(_bounded_file(config_path, 32 * 1024))
        if not isinstance(config, dict) or set(config) != {"host", "port", "caFile", "certFile", "keyFile"}:
            raise TakConfigurationError("TAK config needs only host, port, caFile, certFile and keyFile.")
        host, port = config["host"], config["port"]
        if not isinstance(host, str) or len(host) > 253 or not re.fullmatch(r"[A-Za-z0-9_.:-]+", host) or host.startswith("-"):
            raise TakConfigurationError("TAK host must be a hostname or IP address, without a URL or credentials.")
        if isinstance(port, bool) or not isinstance(port, int) or not 1 <= port <= 65535:
            raise TakConfigurationError("TAK port must be an integer from 1 to 65535.")
        files = {}
        for key in ("caFile", "certFile", "keyFile"):
            value = config[key]
            if not isinstance(value, str) or not value or len(value) > 4096:
                raise TakConfigurationError("TAK credential paths must be nonempty strings.")
            path = (config_path.parent / value).resolve(strict=True)
            _bounded_file(path, MAX_CERT_BYTES)
            files[key] = path
        from cryptography import x509
        from cryptography.x509.oid import ExtendedKeyUsageOID
        certs = x509.load_pem_x509_certificates(files["certFile"].read_bytes())
        if not certs:
            raise TakConfigurationError("TAK client certificate is invalid.")
        now = _utc()
        for cert in certs:
            if not cert.not_valid_before_utc <= now < cert.not_valid_after_utc:
                raise TakConfigurationError("TAK client certificate is expired or not yet valid.")
        try:
            usage = certs[0].extensions.get_extension_for_class(x509.ExtendedKeyUsage).value
            if ExtendedKeyUsageOID.CLIENT_AUTH not in usage:
                raise TakConfigurationError("TAK certificate is not valid for client authentication.")
        except x509.ExtensionNotFound:
            pass
        ca_certs = x509.load_pem_x509_certificates(files["caFile"].read_bytes())
        if not ca_certs:
            raise TakConfigurationError("TAK CA certificate bundle is invalid.")
        for ca in ca_certs:
            if not ca.not_valid_before_utc <= now < ca.not_valid_after_utc:
                raise TakConfigurationError("TAK CA certificate is expired or not yet valid.")
            if not ca.extensions.get_extension_for_class(x509.BasicConstraints).value.ca:
                raise TakConfigurationError("TAK trust bundle must contain CA certificates.")
        context = ssl.create_default_context(ssl.Purpose.SERVER_AUTH, cafile=str(files["caFile"]))
        context.minimum_version = ssl.TLSVersion.TLSv1_2
        context.check_hostname = True
        context.verify_mode = ssl.CERT_REQUIRED
        # A callback avoids an interactive OpenSSL prompt in the web process.
        # Protected keys must be converted by the local setup CLI, never a browser.
        context.load_cert_chain(str(files["certFile"]), str(files["keyFile"]), password=lambda: "")
        return host, port, context
    except TakConfigurationError:
        raise
    except ImportError:
        raise TakConfigurationError("Install the exercise service requirements to configure TAK.") from None
    except Exception:
        raise TakConfigurationError("TAK configuration or certificate material could not be validated.") from None


def _connect(host: str, port: int) -> socket.socket:
    """Bound hostname resolution plus all address attempts to one deadline."""
    result = queue.Queue(maxsize=1)
    deadline = time.monotonic() + SOCKET_TIMEOUT_SECONDS
    def resolve():
        try:
            result.put(socket.getaddrinfo(host, port, type=socket.SOCK_STREAM))
        except OSError as error:
            result.put(error)
    # The OS resolver has no per-call timeout. A late result is discarded and
    # never opens a socket; only this caller proceeds to a connection attempt.
    threading.Thread(target=resolve, daemon=True).start()
    try:
        addresses = result.get(timeout=SOCKET_TIMEOUT_SECONDS)
    except queue.Empty:
        raise TimeoutError("TAK hostname resolution timed out") from None
    if isinstance(addresses, Exception):
        raise addresses
    for family, kind, protocol, _, address in addresses[:8]:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            break
        connection = socket.socket(family, kind, protocol)
        try:
            connection.settimeout(remaining)
            connection.connect(address)
            return connection
        except OSError:
            connection.close()
    raise OSError("TAK connection failed within the configured deadline")


class TakAdapter:
    def __init__(self, config_path: Path | None = None):
        self._config_path = config_path
        self._lock = threading.RLock()
        self._last_attempt = None
        self._last_sent = None
        self._last_error = None

    @classmethod
    def from_env(cls) -> "TakAdapter":
        value = os.environ.get("EXERCISE_TAK_CONFIG", "").strip()
        return cls(Path(value) if value else None)

    def status(self) -> dict:
        with self._lock:
            configured, config_error = False, None
            if self._config_path is not None:
                try:
                    validate_config(self._config_path)
                    configured = True
                except TakConfigurationError as error:
                    config_error = str(error)
            return {"configured": configured, "mode": "manual", "lastAttemptAt": self._last_attempt,
                "lastSentAt": self._last_sent, "lastError": config_error or self._last_error,
                "clientReceipt": "unverified", "exportAvailable": True,
                "exportFormat": "CoT XML review bundle", "transport": "certificate-verified TLS"}

    def export(self, room: Mapping, reports: Sequence) -> bytes:
        """A single marker is .cot XML; multiple markers form a review bundle.

        The <events> review wrapper is not advertised as a WinTAK import format.
        The manual transport below always sends individual <event> elements.
        """
        events, omitted = _events(room, reports, _utc())
        if len(events) == 1:
            root = events[0]
        else:
            root = ET.Element("events", {"purpose": "EXERCISE/TRAINING review bundle",
                "markerCount": str(len(events)), "omittedWithoutLocation": str(omitted),
                "clientImportCompatibility": "unverified"})
            root.extend(events)
        return ET.tostring(root, encoding="utf-8", xml_declaration=True)

    def send(self, room: Mapping, reports: Sequence) -> dict:
        """Manually write a bounded batch. Never infer server/client acceptance."""
        events, omitted = _events(room, reports, _utc())
        if not events:
            raise TakValidationError("No reports with coordinates are available to send.")
        payload = b"\n".join(ET.tostring(event, encoding="utf-8") for event in events) + b"\n"
        with self._lock:
            self._last_attempt = _iso(_utc())
            self._last_error = None
            transport_status = "not_sent"
            try:
                if self._config_path is None:
                    raise TakConfigurationError("TAK is disabled. Configure the service locally before manual sending.")
                host, port, context = validate_config(self._config_path)
                with _connect(host, port) as raw:
                    raw.settimeout(SOCKET_TIMEOUT_SECONDS)
                    with context.wrap_socket(raw, server_hostname=host) as secure:
                        _read_server_control(secure, before_write=True)
                        # Once writing begins, a timeout can mean partial delivery.
                        transport_status = "delivery_unknown"
                        secure.sendall(payload)
                        self._last_sent = _iso(_utc())
                        transport_status = "written_to_tls_socket"
                        _read_server_control(secure, before_write=False)
                        # Send TLS close_notify so closing with unread TLS 1.3
                        # session tickets does not reset the peer's connection.
                        # A shutdown response is transport housekeeping, not a
                        # CoT acceptance or a WinTAK application receipt.
                        try:
                            secure.unwrap()
                        except (OSError, ssl.SSLError):
                            pass
            except TakConfigurationError as error:
                self._last_error = str(error)
            except ssl.SSLCertVerificationError:
                self._last_error = "TAK server certificate or hostname verification failed. No insecure fallback was attempted."
            except (OSError, ssl.SSLError):
                self._last_error = "TAK TLS connection or write failed. Delivery and client receipt are unverified."
            result = self.status()
            result.update({"transportStatus": transport_status, "markerCount": len(events),
                "omittedWithoutLocation": omitted,
                "message": "TLS write completed; TAK Server acceptance and WinTAK receipt are unverified."
                if transport_status == "written_to_tls_socket" else "Markers were not confirmed delivered."})
            return result
