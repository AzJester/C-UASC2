"""Tests that the Pydantic model and the JSON Schema agree (ADR-0002).

The JSON Schemas in specs/schemas are the source of truth for the government-owned
interface; the Pydantic models mirror them. These tests guard against drift on the
fields that gate engagement.
"""
import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from app.models import Identity, Kinematics, Position, Track

SCHEMAS = Path(__file__).resolve().parent.parent / "specs" / "schemas"


def load_schema(name):
    return json.loads((SCHEMAS / name).read_text())


def test_track_schema_is_valid_draft2020():
    from jsonschema.validators import Draft202012Validator

    Draft202012Validator.check_schema(load_schema("track.schema.json"))


def test_pydantic_track_instance_validates_against_json_schema():
    from jsonschema import validate

    track = Track(
        trackId="TRK-1",
        kinematics=Kinematics(position=Position(lat=34.2, lon=-118.2, altMeters=120)),
        trackQuality=12,
        identity=Identity.HOSTILE,
    )
    # Wire serialization omits absent optionals (the schema forbids null for typed
    # fields); see ADR-0002 and _envelope() in c2-core.
    instance = json.loads(track.model_dump_json(exclude_none=True))
    validate(instance=instance, schema=load_schema("track.schema.json"))


def test_track_quality_out_of_range_rejected_by_pydantic():
    with pytest.raises(ValidationError):
        Track(
            trackId="TRK-1",
            kinematics=Kinematics(position=Position(lat=0, lon=0, altMeters=0)),
            trackQuality=99,  # schema caps TQ at 15
            identity=Identity.HOSTILE,
        )


def test_bad_latitude_rejected():
    with pytest.raises(ValidationError):
        Position(lat=200, lon=0, altMeters=0)


def test_all_schemas_are_valid_draft2020():
    from jsonschema.validators import Draft202012Validator

    files = list(SCHEMAS.glob("*.schema.json"))
    assert files, "no schemas found"
    for f in files:
        Draft202012Validator.check_schema(json.loads(f.read_text()))


def test_track_emitter_state_round_trips_and_validates():
    from jsonschema import validate

    from app.models import EmitterState

    track = Track(
        trackId="TRK-9",
        kinematics=Kinematics(position=Position(lat=32.7, lon=-117.2, altMeters=40)),
        trackQuality=7,
        identity=Identity.HOSTILE,
        emitterState="SILENT",
    )
    assert track.emitterState == EmitterState.SILENT
    instance = json.loads(track.model_dump_json(exclude_none=True))
    validate(instance=instance, schema=load_schema("track.schema.json"))


def test_track_rejects_invalid_emitter_state():
    with pytest.raises(ValidationError):
        Track(
            trackId="TRK-9",
            kinematics=Kinematics(position=Position(lat=32.7, lon=-117.2, altMeters=40)),
            trackQuality=7,
            identity=Identity.HOSTILE,
            emitterState="LOUD",
        )
