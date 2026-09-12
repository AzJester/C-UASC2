"""Training-only CoT and credential boundary tests, including a local mTLS peer."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
import importlib.util
import ipaddress
import json
from pathlib import Path
import socket
import ssl
import sys
import threading
import time
import xml.etree.ElementTree as ET
import zipfile

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "services"))
from exercise_service.tak import TakAdapter, TakConfigurationError, TakValidationError, validate_config
from exercise_service import tak

spec = importlib.util.spec_from_file_location("configure_exercise_tak", ROOT / "scripts/configure_exercise_tak.py")
setup = importlib.util.module_from_spec(spec)
spec.loader.exec_module(setup)

ROOM = {"id": "fictional-room", "name": "Exercise Alpha"}
REPORT = {"id": "report-one", "title": "Training review point", "text": "Fictional note only",
    "source": "Exercise controller", "observedAt": "2026-01-02T12:00:00Z",
    "receivedAt": "2026-01-02T12:00:12Z", "lat": 34.5, "lon": -110.5, "version": 1}


def cert_material(*, server_ip=True, expired=False):
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import rsa
    from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID
    now = datetime.now(timezone.utc)
    ca_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    ca_name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "Exercise test CA")])
    ca = (x509.CertificateBuilder().subject_name(ca_name).issuer_name(ca_name).public_key(ca_key.public_key())
        .serial_number(x509.random_serial_number()).not_valid_before(now - timedelta(days=1)).not_valid_after(now + timedelta(days=2))
        .add_extension(x509.BasicConstraints(ca=True, path_length=0), critical=True)
        .add_extension(x509.KeyUsage(False, False, False, False, False, True, True, False, False), critical=True)
        .add_extension(x509.SubjectKeyIdentifier.from_public_key(ca_key.public_key()), critical=False)
        .sign(ca_key, hashes.SHA256()))
    def leaf(name, server=False):
        key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        builder = (x509.CertificateBuilder().subject_name(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, name)]))
            .issuer_name(ca_name).public_key(key.public_key()).serial_number(x509.random_serial_number())
            .not_valid_before(now - timedelta(days=2)).not_valid_after(now - timedelta(days=1) if expired and not server else now + timedelta(days=1))
            .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
            .add_extension(x509.KeyUsage(True, False, True, False, False, False, False, False, False), critical=True)
            .add_extension(x509.AuthorityKeyIdentifier.from_issuer_public_key(ca_key.public_key()), critical=False)
            .add_extension(x509.ExtendedKeyUsage([ExtendedKeyUsageOID.SERVER_AUTH if server else ExtendedKeyUsageOID.CLIENT_AUTH]), critical=False))
        if server:
            names = [x509.DNSName("localhost")]
            if server_ip:
                names.append(x509.IPAddress(ipaddress.ip_address("127.0.0.1")))
            builder = builder.add_extension(x509.SubjectAlternativeName(names), critical=False)
        return key, builder.sign(ca_key, hashes.SHA256())
    client_key, client = leaf("exercise-client")
    server_key, server = leaf("localhost", server=True)
    pem = serialization.Encoding.PEM
    def key_pem(key):
        return key.private_bytes(pem, serialization.PrivateFormat.PKCS8, serialization.NoEncryption())
    return {"ca": ca.public_bytes(pem), "client": client.public_bytes(pem), "client-key": key_pem(client_key),
        "server": server.public_bytes(pem), "server-key": key_pem(server_key),
        "caObject": ca, "clientObject": client, "clientKeyObject": client_key}


@pytest.fixture(scope="module")
def material():
    return cert_material()


def configured(tmp_path, material, *, port=8089):
    for name in ("ca", "client", "client-key", "server", "server-key"):
        (tmp_path / f"{name}.pem").write_bytes(material[name])
    path = tmp_path / "tak.json"
    path.write_text(json.dumps({"host": "127.0.0.1", "port": port, "caFile": "ca.pem", "certFile": "client.pem", "keyFile": "client-key.pem"}))
    return path


def test_disabled_is_exportable_without_network(monkeypatch):
    monkeypatch.delenv("EXERCISE_TAK_CONFIG", raising=False)
    monkeypatch.setattr(tak, "_connect", lambda *a, **k: pytest.fail("default must never connect"))
    adapter = TakAdapter.from_env()
    assert adapter.status() == {"configured": False, "mode": "manual", "lastAttemptAt": None,
        "lastSentAt": None, "lastError": None, "clientReceipt": "unverified", "exportAvailable": True,
        "exportFormat": "CoT XML review bundle", "transport": "certificate-verified TLS"}
    assert ET.fromstring(adapter.export(ROOM, [REPORT])).tag == "event"
    result = adapter.send(ROOM, [REPORT])
    assert result["transportStatus"] == "not_sent"
    assert result["lastSentAt"] is None and result["lastAttemptAt"]


def test_xml_escaping_training_metadata_and_stable_uid():
    adapter = TakAdapter()
    report = {**REPORT, "title": 'Note <one> & "two"', "text": 'A < B & C; </remarks><command>none</command>'}
    first = ET.fromstring(adapter.export(ROOM, [report]))
    next_version = ET.fromstring(adapter.export(ROOM, [{**report, "version": 2}]))
    assert first.attrib["uid"].startswith("exercise-")
    assert first.attrib["uid"] == next_version.attrib["uid"]
    assert first.attrib["type"] == "b-m-p-s-m" and first.attrib["opex"] == "e"
    assert first.find("detail/contact").attrib["callsign"].startswith("EXERCISE/TRAINING ")
    remarks = first.findtext("detail/remarks")
    assert report["text"] in remarks and "EXERCISE / TRAINING ONLY" in remarks
    assert first.find(".//command") is None
    point = first.find("point").attrib
    assert point["hae"] == point["ce"] == point["le"] == "9999999.0"
    observed = first.find("detail/{urn:insightfuldefense:exercise:1}report").attrib["observedAt"]
    assert observed == "2026-01-02T12:00:00.000Z"
    assert (datetime.fromisoformat(first.attrib["stale"]) - datetime.fromisoformat(first.attrib["time"])).total_seconds() == 900
    assert "does not refresh the report" in remarks


def test_missing_observation_time_stays_unavailable():
    event = ET.fromstring(TakAdapter().export(ROOM, [{**REPORT, "observedAt": None}]))
    assert "Observed: Unavailable" in event.findtext("detail/remarks")
    metadata = event.find("detail/{urn:insightfuldefense:exercise:1}report").attrib
    assert metadata["observationTimeKnown"] == "false" and metadata["observedAt"] == ""


@pytest.mark.parametrize("patch", [
    {"lat": float("nan")}, {"lon": float("inf")}, {"lat": 91}, {"lon": -181},
    {"lat": True}, {"lat": "34.5"}, {"lon": None}, {"text": "bad\x00text"},
    {"title": "x" * 161}, {"text": "x" * 10001}, {"version": 0}, {"version": True},
    {"receivedAt": "2026-01-01T00:00:00"},
])
def test_invalid_report_does_not_produce_markers(patch):
    with pytest.raises(TakValidationError):
        TakAdapter().export(ROOM, [{**REPORT, **patch}])


def test_batch_bounds_and_missing_coordinates():
    adapter = TakAdapter()
    with pytest.raises(TakValidationError):
        adapter.export(ROOM, [REPORT] * 101)
    with pytest.raises(TakValidationError, match="current version"):
        adapter.export(ROOM, [REPORT, REPORT])
    with pytest.raises(TakValidationError, match="size limit"):
        adapter.export(ROOM, [{**REPORT, "id": f"report-{i}", "text": "x" * 10000} for i in range(70)])
    bundle = ET.fromstring(adapter.export(ROOM, [{**REPORT, "lat": None, "lon": None}]))
    assert bundle.tag == "events" and bundle.attrib["markerCount"] == "0"
    assert bundle.attrib["omittedWithoutLocation"] == "1"
    with pytest.raises(TakValidationError, match="No reports"):
        adapter.send(ROOM, [])
    bundle = ET.fromstring(adapter.export(ROOM, [REPORT, {**REPORT, "id": "other"}]))
    assert len(bundle) == 2 and bundle.attrib["clientImportCompatibility"] == "unverified"


def test_readiness_validates_real_certificates_and_keys(tmp_path, material):
    path = configured(tmp_path, material)
    host, port, context = validate_config(path)
    assert host == "127.0.0.1" and port == 8089
    assert context.check_hostname and context.verify_mode == ssl.CERT_REQUIRED
    assert context.minimum_version >= ssl.TLSVersion.TLSv1_2
    adapter = TakAdapter(path)
    assert adapter.status()["configured"] is True
    (tmp_path / "client-key.pem").write_bytes(b"invalid SECRET KEY MATERIAL")
    status = adapter.status()
    assert status["configured"] is False and status["lastError"]
    assert "SECRET" not in status["lastError"] and str(tmp_path) not in status["lastError"]


def test_expired_cert_is_not_ready(tmp_path):
    path = configured(tmp_path, cert_material(expired=True))
    status = TakAdapter(path).status()
    assert status["configured"] is False and "expired" in status["lastError"]


def test_configuration_is_fixed_and_rejects_extra_overrides(tmp_path, material):
    path = configured(tmp_path, material)
    config = json.loads(path.read_text())
    config["verifyTLS"] = False
    path.write_text(json.dumps(config))
    with pytest.raises(TakConfigurationError):
        validate_config(path)


def local_tls_peer(tmp_path, material):
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    listener.listen(1)
    listener.settimeout(5)
    path = configured(tmp_path, material, port=listener.getsockname()[1])
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.minimum_version = ssl.TLSVersion.TLSv1_2
    context.load_cert_chain(tmp_path / "server.pem", tmp_path / "server-key.pem")
    context.load_verify_locations(tmp_path / "ca.pem")
    context.verify_mode = ssl.CERT_REQUIRED
    received, errors = [], []
    def listen():
        try:
            with listener:
                raw, _ = listener.accept()
                with raw:
                    raw.settimeout(5)
                    with context.wrap_socket(raw, server_side=True) as secure:
                        while data := secure.recv(65536):
                            received.append(data)
        except (OSError, ssl.SSLError) as error:
            errors.append(type(error).__name__)
    thread = threading.Thread(target=listen, daemon=True)
    thread.start()
    return path, thread, received, errors


def test_mutual_tls_sends_individual_events_without_claiming_receipt(tmp_path, material):
    path, thread, received, errors = local_tls_peer(tmp_path, material)
    result = TakAdapter(path).send(ROOM, [REPORT, {**REPORT, "id": "other"}])
    thread.join(timeout=6)
    assert not thread.is_alive() and not errors
    messages = ET.fromstring(b"<events>" + b"".join(received) + b"</events>")
    assert len(messages) == 2 and all(message.tag == "event" for message in messages)
    assert result["transportStatus"] == "written_to_tls_socket"
    assert result["lastSentAt"] and result["clientReceipt"] == "unverified"
    assert "acceptance and WinTAK receipt are unverified" in result["message"]


def test_wrong_server_hostname_is_rejected_without_fallback(tmp_path):
    path, thread, received, errors = local_tls_peer(tmp_path, cert_material(server_ip=False))
    result = TakAdapter(path).send(ROOM, [REPORT])
    thread.join(timeout=6)
    assert not received and errors
    assert result["transportStatus"] == "not_sent" and result["lastSentAt"] is None
    assert "verification failed" in result["lastError"]


def test_dns_timeout_is_bounded_and_never_connects_later(monkeypatch):
    finish = threading.Event()
    def delayed_resolver(*args, **kwargs):
        finish.wait(1)
        return []
    monkeypatch.setattr(socket, "getaddrinfo", delayed_resolver)
    monkeypatch.setattr(tak, "SOCKET_TIMEOUT_SECONDS", 0.02)
    started = time.monotonic()
    try:
        with pytest.raises(TimeoutError):
            tak._connect("training.invalid", 8089)
        assert time.monotonic() - started < 0.5
    finally:
        finish.set()


def test_partial_write_stays_unknown_and_error_is_sanitized(monkeypatch):
    class Connection:
        def __enter__(self): return self
        def __exit__(self, *args): pass
        def settimeout(self, value): pass
        def sendall(self, payload): raise TimeoutError("sensitive machine detail")
    class Context:
        def wrap_socket(self, *args, **kwargs): return Connection()
    monkeypatch.setattr(tak, "validate_config", lambda path: ("fixed-server", 8089, Context()))
    monkeypatch.setattr(tak, "_connect", lambda host, port: Connection())
    result = TakAdapter(Path("server-owned.json")).send(ROOM, [REPORT])
    assert result["transportStatus"] == "delivery_unknown" and result["lastSentAt"] is None
    assert result["clientReceipt"] == "unverified" and "sensitive" not in result["lastError"]


@pytest.mark.parametrize("name", ["../client.p12", "/client.p12", "C:/client.p12", "..\\client.p12"])
def test_package_paths_are_never_extracted(tmp_path, name):
    path = tmp_path / "package.zip"
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr(name, b"not a certificate")
    with pytest.raises(setup.SetupError, match="unsafe"):
        setup.read_package(path)


def test_archive_bomb_and_preference_entities_rejected(tmp_path):
    path = tmp_path / "compressed.zip"
    with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("cert.p12", b"0" * 100000)
    with pytest.raises(setup.SetupError, match="compressed"):
        setup.read_package(path)
    with pytest.raises(setup.SetupError, match="declarations"):
        setup.package_preferences({"config.pref": b'<!DOCTYPE a [<!ENTITY x "x">]><a/>'})


def test_preferences_do_not_retain_passwords_and_ambiguity_stops():
    prefs = setup.package_preferences({"config.pref": b'''<preferences><preference>
      <entry key="connectString0">tak.example:8089:SSL</entry>
      <entry key="certificateLocation">certs/client.p12</entry>
      <entry key="caLocation">certs/ca.p12</entry>
      <entry key="clientPassword">sensitive-package-password</entry>
    </preference></preferences>'''})
    assert setup.endpoint(prefs, None, None) == ("tak.example", 8089)
    assert "sensitive-package-password" not in json.dumps(prefs)
    with pytest.raises(setup.SetupError, match="Exactly one"):
        setup.endpoint({**prefs, "connectString1": "other.example:8090:SSL"}, None, None)
    with pytest.raises(setup.SetupError, match="ambiguous"):
        setup.select_member({"one/client.p12": b"a", "two/client.p12": b"b"}, prefs, None, client=True)


def test_protected_package_import_writes_valid_private_configuration(tmp_path, material, monkeypatch):
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.serialization import pkcs12
    password = b"test-only-generated-package-pass"
    client = pkcs12.serialize_key_and_certificates(b"exercise", material["clientKeyObject"], material["clientObject"],
        [material["caObject"]], serialization.BestAvailableEncryption(password))
    ca = pkcs12.serialize_key_and_certificates(None, None, None, [material["caObject"]], serialization.BestAvailableEncryption(password))
    monkeypatch.setattr(setup, "ask_password", lambda label: password)
    converted = setup.certificate_material({"client.p12": client, "ca.p12": ca}, {}, "client.p12", "ca.p12")
    output = setup.write_config(tmp_path / "private-output", "127.0.0.1", 8089, converted)
    assert TakAdapter(output).status()["configured"] is True
    assert not b"test-only" in (output.parent / "client-key.pem").read_bytes()
    with pytest.raises(setup.SetupError, match="already exists"):
        setup.write_config(output.parent, "127.0.0.1", 8089, converted)


def test_noninteractive_password_prompt_never_falls_back_to_echo(monkeypatch):
    monkeypatch.setattr(sys.stdin, "isatty", lambda: False)
    monkeypatch.setattr(setup.getpass, "getpass", lambda *a: pytest.fail("must not prompt with echo fallback"))
    with pytest.raises(setup.SetupError, match="interactive terminal"):
        setup.ask_password("certificate")
