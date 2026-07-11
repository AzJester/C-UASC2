"""Producer-model and canonical wire-schema conformance tests (ADR-0002).

The JSON Schemas in specs/schemas are the source of truth for the government-owned
interface; the Pydantic models mirror them. These tests guard against drift on the
fields that gate engagement. Producer-side models may generate IDs/timestamps;
bus/REST edge tests separately prove canonical required wire fields are not filled
on behalf of an untrusted publisher.
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


def test_bus_addressed_component_ids_are_one_subject_token_in_model_and_schema():
    from jsonschema import ValidationError as SchemaValidationError, validate

    from app.models import EffectorStatus, SensorStatus

    with pytest.raises(ValidationError):
        SensorStatus(sensorId="SEN.DOTTED", sensorType="RADAR")
    with pytest.raises(ValidationError):
        EffectorStatus(effectorId="EFF.DOTTED", effectorType="EW_JAMMER")

    invalid_sensor = {
        "sensorId": "SEN.DOTTED",
        "sensorType": "RADAR",
        "readiness": "READY",
        "timeReported": "2026-01-01T00:00:00Z",
    }
    with pytest.raises(SchemaValidationError):
        validate(invalid_sensor, load_schema("sensor.schema.json"))


def test_sensor_task_search_volume_is_accepted_and_cross_field_rules_match_schema():
    from jsonschema import validate

    from app.models import SensorTask

    task = SensorTask(
        taskId="TASK-SEARCH-1",
        sensorId="SEN-RAD-01",
        taskType="SEARCH",
        searchVolume={
            "centerBearingDeg": 90,
            "widthDeg": 40,
            "minAltMeters": 20,
            "maxAltMeters": 500,
        },
        priority=6,
        requestedBy="SM-1",
    )
    validate(
        json.loads(task.model_dump_json(exclude_none=True)),
        load_schema("sensor-task.schema.json"),
    )
    with pytest.raises(ValidationError):
        SensorTask(
            taskId="TASK-BAD-DWELL",
            sensorId="SEN-RAD-01",
            taskType="DWELL",
            priority=5,
            requestedBy="SM-1",
        )


def test_engagement_order_requires_safety_constraints_in_model_and_schema():
    from jsonschema import ValidationError as SchemaValidationError, validate

    from app.models import EngagementOrder

    order = {
        "engagementId": "ENG-1",
        "requestId": "REQ-1",
        "trackId": "TRK-1",
        "effectorId": "EFF-EW-01",
        "orderedBy": "FCA-1",
        "authorityToken": "v1." + "x" * 32,
        "authorityTokenExpiresAt": "2026-01-01T00:00:20Z",
        "authorityTokenScope": {
            "engagementId": "ENG-1",
            "requestId": "REQ-1",
            "trackId": "TRK-1",
            "effectorId": "EFF-EW-01",
            "engagementType": "EW_DEFEAT",
            "policyVersion": "P-1",
            "weaponsControlStatus": "WEAPONS_TIGHT",
        },
        "orderSequence": 2,
        "engagementType": "EW_DEFEAT",
        "trackSnapshotTimeObserved": "2026-01-01T00:00:00Z",
        "timeOrdered": "2026-01-01T00:00:01Z",
    }
    with pytest.raises(ValidationError):
        EngagementOrder.model_validate(order)
    with pytest.raises(SchemaValidationError):
        validate(order, load_schema("engagement-order.schema.json"))
