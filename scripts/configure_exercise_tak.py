#!/usr/bin/env python3
"""Inspect/import a locally supplied TAK connection package without connecting.

Examples:
  python scripts/configure_exercise_tak.py --package supplied.zip --inspect
  python scripts/configure_exercise_tak.py --package supplied.zip --output private-tak
  python scripts/configure_exercise_tak.py --client-p12 client.p12 --ca ca.p12 \
      --host tak.example --port 8089 --output private-tak
  python scripts/configure_exercise_tak.py --host tak.example --port 8089 \
      --ca ca.pem --cert client.pem --key client-key.pem --output private-tak

No certificate passwords are accepted as command arguments. This tool never
searches Downloads, extracts archive paths, enrolls certificates, or sends data.
"""
from __future__ import annotations

import argparse
import csv
import getpass
import io
import json
import os
from pathlib import Path, PurePosixPath
import re
import stat
import subprocess
import sys
import xml.etree.ElementTree as ET
import zipfile

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "services"))
from exercise_service.tak import TakConfigurationError, validate_config  # noqa: E402

MAX_ARCHIVE_BYTES = 32 * 1024 * 1024
MAX_EXPANDED_BYTES = 16 * 1024 * 1024
MAX_MEMBER_BYTES = 4 * 1024 * 1024
MAX_ENTRIES = 128


class SetupError(ValueError):
    pass


def read_bounded(path: Path, limit: int = MAX_MEMBER_BYTES) -> bytes:
    if not path.is_file() or not 0 < path.stat().st_size <= limit:
        raise SetupError("The selected input file is missing, empty or too large.")
    data = path.read_bytes()
    if len(data) > limit:
        raise SetupError("The selected input file exceeds the size limit.")
    return data


def read_package(path: Path) -> dict[str, bytes]:
    """Read small selected configuration/certificate members, never extract."""
    raw = read_bounded(path, MAX_ARCHIVE_BYTES)
    try:
        with zipfile.ZipFile(io.BytesIO(raw)) as archive:
            members = archive.infolist()
            if len(members) > MAX_ENTRIES or sum(m.file_size for m in members) > MAX_EXPANDED_BYTES:
                raise SetupError("Connection package exceeds the archive limits.")
            result, seen = {}, set()
            for member in members:
                name = member.filename
                parts = PurePosixPath(name).parts
                mode = member.external_attr >> 16
                if (not name or "\\" in name or ":" in name or name.startswith("/")
                        or any(p in {"..", "."} for p in parts) or stat.S_ISLNK(mode)
                        or any(ord(c) < 32 for c in name)):
                    raise SetupError("Connection package contains an unsafe member path.")
                if name.casefold() in seen:
                    raise SetupError("Connection package contains ambiguous duplicate names.")
                seen.add(name.casefold())
                if member.flag_bits & 1:
                    raise SetupError("Encrypted ZIP files are unsupported. Supply an unencrypted package containing protected certificates.")
                if (member.file_size > MAX_MEMBER_BYTES or
                        member.file_size > max(1, member.compress_size) * 200):
                    raise SetupError("Connection package contains an oversized or highly compressed member.")
                if member.is_dir():
                    continue
                if PurePosixPath(name).suffix.lower() not in {".pref", ".p12", ".pfx", ".pem", ".crt", ".cer", ".key"}:
                    continue
                # ZipFile.read verifies CRC, and the declared bounds above keep
                # the small certificate/config inputs within the memory budget.
                data = archive.read(member)
                if len(data) != member.file_size:
                    raise SetupError("Connection package member size did not match its manifest.")
                result[name] = data
            return result
    except (zipfile.BadZipFile, RuntimeError, OSError):
        raise SetupError("The connection package could not be read as a valid ZIP.") from None


def package_preferences(members: dict[str, bytes]) -> dict[str, str]:
    pref_names = [name for name in members if PurePosixPath(name).suffix.lower() == ".pref"]
    if len(pref_names) != 1:
        if not pref_names:
            return {}
        raise SetupError("Multiple preference files found. Inspect the package and configure explicit PEM paths instead.")
    data = members[pref_names[0]]
    if len(data) > 256 * 1024 or b"<!DOCTYPE" in data.upper() or b"<!ENTITY" in data.upper():
        raise SetupError("Preference XML contains unsupported declarations or is too large.")
    try:
        root = ET.fromstring(data)
    except ET.ParseError:
        raise SetupError("The selected preference XML could not be parsed.") from None
    result = {}
    for item in root.iter():
        if item.tag.rsplit("}", 1)[-1] != "entry":
            continue
        key, value = item.get("key", ""), (item.text or "").strip()
        # Read only endpoint and certificate-location metadata. Password entries
        # are never retained, printed or used as a guessed password.
        if re.fullmatch(r"connectString\d*|(?:caLocation|certificateLocation|clientCertificateLocation)\d*", key):
            if key in result and result[key] != value:
                raise SetupError("Preference XML has conflicting configuration entries.")
            result[key] = value
    return result


def endpoint(preferences: dict[str, str], host: str | None, port: int | None) -> tuple[str, int]:
    if host is not None or port is not None:
        if not host or port is None:
            raise SetupError("Supply both --host and --port; no port is guessed.")
        return host, port
    candidates = set()
    for key, value in preferences.items():
        if not key.startswith("connectString"):
            continue
        pieces = value.rsplit(":", 2)
        if len(pieces) != 3 or pieces[2].upper() not in {"SSL", "TLS"}:
            continue
        try:
            candidates.add((pieces[0].strip("[]"), int(pieces[1])))
        except ValueError:
            continue
    if len(candidates) != 1:
        raise SetupError("Exactly one TLS endpoint is required. Supply the intended --host and --port explicitly.")
    return next(iter(candidates))


def select_member(members: dict[str, bytes], preferences: dict[str, str], explicit: str | None, *, client: bool) -> tuple[str, bytes]:
    if explicit:
        if explicit not in members:
            raise SetupError("The named certificate member is not present. Use --inspect to see available names.")
        return explicit, members[explicit]
    prefix = ("certificateLocation", "clientCertificateLocation") if client else ("caLocation",)
    locations = {value for key, value in preferences.items() if key.startswith(prefix) and value}
    matches = set()
    for location in locations:
        # Some packages use /sdcard/... in prefs. Use its basename only when
        # exactly one actual archive member matches. Never read that local path.
        wanted = PurePosixPath(location.replace("\\", "/")).name
        found = [name for name in members if name == location or PurePosixPath(name).name == wanted]
        matches.update(found)
    if len(matches) != 1:
        kind = "client" if client else "CA"
        option = "--client-member" if client else "--ca-member"
        raise SetupError(f"The {kind} certificate selection is ambiguous. Use --inspect, then supply {option} with an exact member name.")
    name = next(iter(matches))
    return name, members[name]


def ask_password(label: str) -> bytes | None:
    if not sys.stdin.isatty():
        raise SetupError("A protected certificate needs an interactive terminal. Run the setup command there; passwords are never command arguments.")
    password = getpass.getpass(f"Password for {label} (not stored; blank only if unprotected): ")
    return password.encode("utf-8") if password else None


def load_p12(data: bytes, label: str):
    from cryptography.hazmat.primitives.serialization import pkcs12
    try:
        return pkcs12.load_key_and_certificates(data, ask_password(label))
    except ValueError:
        raise SetupError("The PKCS12 certificate could not be opened. Check its password and format; no default passwords were tried.") from None


def certificate_material(members: dict[str, bytes], preferences: dict[str, str], client_name: str | None, ca_name: str | None) -> tuple[bytes, bytes, bytes]:
    from cryptography import x509
    from cryptography.hazmat.primitives import serialization
    client_path, client_data = select_member(members, preferences, client_name, client=True)
    ca_path, ca_data = select_member(members, preferences, ca_name, client=False)
    if PurePosixPath(client_path).suffix.lower() not in {".p12", ".pfx"}:
        raise SetupError("Package client material must be PKCS12. For PEM files, use --ca, --cert and --key instead.")
    key, cert, chain = load_p12(client_data, "the selected client certificate")
    if key is None or cert is None:
        raise SetupError("The selected client PKCS12 needs one private key and its certificate.")
    if PurePosixPath(ca_path).suffix.lower() in {".p12", ".pfx"}:
        ca_key, ca_cert, ca_chain = load_p12(ca_data, "the selected CA trust store")
        if ca_key is not None:
            raise SetupError("The selected CA trust store contains a private key. Supply the CA certificate-only trust store.")
        ca_certs = ([ca_cert] if ca_cert is not None else []) + list(ca_chain or [])
    else:
        try:
            try:
                ca_certs = x509.load_pem_x509_certificates(ca_data)
            except ValueError:
                ca_certs = [x509.load_der_x509_certificate(ca_data)]
        except ValueError:
            raise SetupError("The selected CA certificate is neither a usable PEM nor DER certificate.") from None
    if not ca_certs:
        raise SetupError("The selected CA trust store contains no certificates.")
    pem = serialization.Encoding.PEM
    ca_pem = b"".join(c.public_bytes(pem) for c in ca_certs)
    client_pem = b"".join(c.public_bytes(pem) for c in [cert, *(chain or [])])
    key_pem = key.private_bytes(pem, serialization.PrivateFormat.PKCS8, serialization.NoEncryption())
    return ca_pem, client_pem, key_pem


def pem_material(ca_path: Path, cert_path: Path, key_path: Path) -> tuple[bytes, bytes, bytes]:
    from cryptography.hazmat.primitives import serialization
    ca, cert, key_data = read_bounded(ca_path), read_bounded(cert_path), read_bounded(key_path)
    try:
        try:
            key = serialization.load_pem_private_key(key_data, password=None)
        except TypeError:
            key = serialization.load_pem_private_key(key_data, password=ask_password("the selected PEM private key"))
        key_pem = key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption())
    except (TypeError, ValueError):
        raise SetupError("The PEM private key could not be opened. Verify its format and password.") from None
    return ca, cert, key_pem


def private_directory(path: Path) -> Path:
    path = path.absolute()
    if path.exists():
        raise SetupError("The output directory already exists. Choose a new private directory; existing files are never overwritten.")
    if not path.parent.is_dir():
        raise SetupError("The output parent directory must already exist.")
    path.mkdir(mode=0o700)
    if os.name != "nt":
        path.chmod(0o700)
        return path
    try:
        result = subprocess.run(["whoami", "/user", "/fo", "csv", "/nh"], capture_output=True, text=True, check=True, timeout=10)
        rows = list(csv.reader(io.StringIO(result.stdout)))
        sid = next(cell for row in rows for cell in row if re.fullmatch(r"S-1-5-(?:\d+-)*\d+", cell))
        subprocess.run(["icacls", str(path), "/inheritance:r", "/grant:r", f"*{sid}:(OI)(CI)F", "*S-1-5-18:(OI)(CI)F"],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=True, timeout=10)
    except (OSError, subprocess.SubprocessError, StopIteration):
        raise SetupError("Could not restrict the output directory to the current Windows user and SYSTEM. No credentials were written.") from None
    return path


def write_config(output: Path, host: str, port: int, material: tuple[bytes, bytes, bytes]) -> Path:
    output = private_directory(output)
    names = ("ca.pem", "client.pem", "client-key.pem")
    for name, data in zip(names, material):
        fd = os.open(output / name, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
    config = {"host": host, "port": port, "caFile": names[0], "certFile": names[1], "keyFile": names[2]}
    config_path = output / "config.json"
    fd = os.open(config_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        json.dump(config, handle, indent=2)
        handle.write("\n")
    validate_config(config_path)
    return config_path


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--package", type=Path, help="Explicit downloaded connection-package ZIP path")
    parser.add_argument("--client-p12", type=Path, help="Explicit standalone client PKCS12; also supply --ca, --host and --port")
    parser.add_argument("--inspect", action="store_true", help="List certificate names and TLS endpoints only; write nothing")
    parser.add_argument("--host")
    parser.add_argument("--port", type=int)
    parser.add_argument("--client-member", help="Exact client PKCS12 archive member")
    parser.add_argument("--ca-member", help="Exact CA trust-store archive member")
    parser.add_argument("--ca", type=Path)
    parser.add_argument("--cert", type=Path)
    parser.add_argument("--key", type=Path)
    parser.add_argument("--output", type=Path, help="Explicit new private output directory")
    args = parser.parse_args(argv)
    try:
        if not args.inspect and args.output is None:
            raise SetupError("--output must name a new private directory before credentials can be written.")
        if args.package:
            if any((args.ca, args.cert, args.key, args.client_p12)):
                raise SetupError("Choose a package or explicit PEM files, not both.")
            members = read_package(args.package)
            preferences = package_preferences(members)
            if args.inspect:
                names = sorted(name for name in members if not name.lower().endswith(".pref"))
                endpoints = sorted({v for k, v in preferences.items() if k.startswith("connectString")})
                print(json.dumps({"certificateMembers": names, "connectionPreferences": endpoints,
                    "note": "Inspection only. No password entries are displayed and no connection was attempted."}, indent=2))
                return 0
            host, port = endpoint(preferences, args.host, args.port)
            material = certificate_material(members, preferences, args.client_member, args.ca_member)
        elif args.client_p12:
            if args.inspect or not args.ca or args.cert or args.key:
                raise SetupError("Standalone PKCS12 needs --ca, --host and --port. --inspect supports ZIP packages only.")
            host, port = endpoint({}, args.host, args.port)
            client_name = "selected-client.p12"
            ca_name = "selected-ca" + args.ca.suffix.lower()
            material = certificate_material({client_name: read_bounded(args.client_p12), ca_name: read_bounded(args.ca)},
                {}, client_name, ca_name)
        else:
            if args.inspect or not all((args.ca, args.cert, args.key)):
                raise SetupError("Supply --package, or all of --ca, --cert and --key with --host and --port.")
            host, port = endpoint({}, args.host, args.port)
            material = pem_material(args.ca, args.cert, args.key)
        config_path = write_config(args.output, host, port, material)
        print(f"Validated local TAK configuration: {config_path}")
        print("Set EXERCISE_TAK_CONFIG to that file when starting the exercise service. Sending remains manual.")
        print("Private key access is restricted to the current user and SYSTEM on Windows, or owner-only on other systems.")
        print("No server connection, certificate enrollment or WinTAK receipt test was performed.")
        return 0
    except (SetupError, TakConfigurationError) as error:
        print(f"Setup stopped: {error}", file=sys.stderr)
        return 2
    except ImportError:
        print("Setup stopped: install services/exercise_service/requirements.txt first.", file=sys.stderr)
        return 2
    except OSError:
        print("Setup stopped: an input or output file could not be accessed. Existing credentials were not overwritten.", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
