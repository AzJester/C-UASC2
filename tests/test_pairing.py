"""Tests for feasibility (gate 2) and distributed weapon pairing (docs/04 §3.2)."""
from app.models import (
    EffectorStatus,
    EffectorType,
    EngagementEnvelope,
    EngagementType,
    Identity,
    Kinematics,
    Magazine,
    Position,
    Readiness,
    Track,
)
from app.pairing import best_effector, check_feasibility


def track_at(lat=34.20, lon=-118.20, alt=120):
    return Track(
        trackId="TRK-1",
        kinematics=Kinematics(position=Position(lat=lat, lon=lon, altMeters=alt)),
        trackQuality=12,
        identity=Identity.HOSTILE,
    )


def ew(effector_id="EFF-EW", remaining=100, max_range=8000, loc=(34.20, -118.20)):
    return EffectorStatus(
        effectorId=effector_id,
        effectorType=EffectorType.EW_JAMMER,
        readiness=Readiness.READY,
        magazine=Magazine(remaining=remaining, capacity=100, unit="seconds"),
        engagementEnvelope=EngagementEnvelope(
            location=Position(lat=loc[0], lon=loc[1], altMeters=0),
            maxRangeMeters=max_range,
            maxAltMeters=1500,
        ),
    )


def test_compatible_in_range_feasible():
    d = check_feasibility(track_at(), ew(), EngagementType.EW_DEFEAT)
    assert d.permit, d.detail


def test_incompatible_effect_denied():
    d = check_feasibility(track_at(), ew(), EngagementType.KINETIC)
    assert not d.permit
    assert d.reasonCode == "EFFECTOR_UNAVAILABLE"


def test_out_of_range_denied():
    # Track ~15 km away, effector max range 8 km.
    d = check_feasibility(track_at(lat=34.33), ew(max_range=8000), EngagementType.EW_DEFEAT)
    assert not d.permit
    assert d.reasonCode == "OUT_OF_ENVELOPE"


def test_empty_magazine_denied():
    d = check_feasibility(track_at(), ew(remaining=0), EngagementType.EW_DEFEAT)
    assert not d.permit
    assert d.reasonCode == "EFFECTOR_UNAVAILABLE"


def test_offline_effector_denied():
    e = ew()
    e.readiness = Readiness.OFFLINE
    d = check_feasibility(track_at(), e, EngagementType.EW_DEFEAT)
    assert not d.permit


def test_best_effector_picks_feasible_with_most_magazine():
    near_low = ew(effector_id="EFF-A", remaining=10)
    near_high = ew(effector_id="EFF-B", remaining=90)
    far = ew(effector_id="EFF-C", remaining=100, loc=(34.50, -118.50))  # out of range
    chosen = best_effector(track_at(), [near_low, near_high, far], EngagementType.EW_DEFEAT)
    assert chosen is not None
    assert chosen.effectorId == "EFF-B"


def test_best_effector_none_when_no_feasible():
    far = ew(effector_id="EFF-C", loc=(34.90, -118.90))
    assert best_effector(track_at(), [far], EngagementType.EW_DEFEAT) is None
