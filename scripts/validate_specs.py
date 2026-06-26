#!/usr/bin/env python3
"""Validate the government-owned interface specs.

Checks that every JSON Schema is a valid Draft 2020-12 schema, and that the OpenAPI
and AsyncAPI documents parse and carry their required top-level fields with the
schema $refs resolving to files that exist. This is the seed of the conformance
suite described in docs/02 §6.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SCHEMAS = ROOT / "specs" / "schemas"
OPENAPI = ROOT / "specs" / "openapi" / "cuas-c2.yaml"
ASYNCAPI = ROOT / "specs" / "asyncapi" / "cuas-pubsub.yaml"


def fail(msg: str) -> None:
    print(f"  FAIL: {msg}")


def validate_json_schemas() -> list[str]:
    errors: list[str] = []
    try:
        from jsonschema.validators import Draft202012Validator
    except ImportError:
        return ["jsonschema not installed (pip install -r requirements-dev.txt)"]

    files = sorted(SCHEMAS.glob("*.schema.json"))
    if not files:
        return [f"no schema files found in {SCHEMAS}"]
    for f in files:
        try:
            schema = json.loads(f.read_text())
            Draft202012Validator.check_schema(schema)
            print(f"  ok   {f.relative_to(ROOT)}")
        except Exception as exc:  # noqa: BLE001
            errors.append(f"{f.name}: {exc}")
            fail(f"{f.name}: {exc}")
    return errors


def _check_refs(node, base: Path, errors: list[str]) -> None:
    if isinstance(node, dict):
        for k, v in node.items():
            if k == "$ref" and isinstance(v, str) and v.startswith("../"):
                target = (base.parent / v).resolve()
                if not target.exists():
                    errors.append(f"dangling $ref {v} -> {target}")
                    fail(f"dangling $ref {v}")
            else:
                _check_refs(v, base, errors)
    elif isinstance(node, list):
        for item in node:
            _check_refs(item, base, errors)


def validate_doc(path: Path, required: list[str], kind: str) -> list[str]:
    errors: list[str] = []
    try:
        import yaml
    except ImportError:
        return ["pyyaml not installed (pip install -r requirements-dev.txt)"]
    try:
        doc = yaml.safe_load(path.read_text())
    except Exception as exc:  # noqa: BLE001
        fail(f"{path.name}: parse error {exc}")
        return [f"{path.name}: {exc}"]
    for key in required:
        if key not in doc:
            errors.append(f"{path.name}: missing top-level '{key}'")
            fail(f"{path.name}: missing '{key}'")
    _check_refs(doc, path, errors)
    if not errors:
        print(f"  ok   {path.relative_to(ROOT)} ({kind})")
    return errors


def main() -> int:
    print("Validating JSON Schemas...")
    errors = validate_json_schemas()
    print("Validating OpenAPI...")
    errors += validate_doc(OPENAPI, ["openapi", "info", "paths", "components"], "OpenAPI 3.1")
    print("Validating AsyncAPI...")
    errors += validate_doc(ASYNCAPI, ["asyncapi", "info", "channels", "operations"], "AsyncAPI 3.0")

    print()
    if errors:
        print(f"{len(errors)} problem(s) found.")
        return 1
    print("All specs valid.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
