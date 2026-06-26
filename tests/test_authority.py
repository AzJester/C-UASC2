"""Tests for the authority/ROE engine — the positive-control boundary (docs/05).

These lock the behavior the architecture promises: any authorized node may engage
any sufficient-quality hostile track, and nothing else.
"""
from app.authority import (
    ROE,
    WeaponsControlStatus,
    authorize_engagement,
    authorize_tasking,
)
from app.models import (
    EffectorStatus,
    EffectorType,
    Identity,
    Kinematics,
    Position,
    Readiness,
    Role,
    TaskType,
    Track,
)


def make_track(tq=12, identity=Identity.HOSTILE):
    return Track(
        trackId="TRK-1",
        kinematics=Kinematics(position=Position(lat=34.2, lon=-118.2, altMeters=120)),
        trackQuality=tq,
        identity=identity,
    )


def make_effector(etype=EffectorType.EW_JAMMER, readiness=Readiness.READY):
    return EffectorStatus(effectorId="EFF-1", effectorType=etype, readiness=readiness)


def base_roe():
    return ROE(weaponsControlStatus=WeaponsControlStatus.WEAPONS_TIGHT, requireHumanInTheLoop=True)


def test_authorized_hostile_engagement_permitted():
    d = authorize_engagement(
        role=Role.FIRE_CONTROL_AUTHORITY,
        track=make_track(),
        effector=make_effector(),
        roe=base_roe(),
        human_confirmation=True,
    )
    assert d.permit, d.detail
    assert d.reasonCode == "OK"


def test_any_node_with_fca_role_can_engage_any_effector():
    # The "any-shooter" property: no sensor<->effector pairing is consulted.
    for etype in (EffectorType.EW_JAMMER, EffectorType.KINETIC_GUN, EffectorType.DIRECTED_ENERGY):
        track = make_track(tq=15)
        d = authorize_engagement(
            role=Role.FIRE_CONTROL_AUTHORITY,
            track=track,
            effector=make_effector(etype=etype),
            roe=base_roe(),
            human_confirmation=True,
        )
        assert d.permit, f"{etype}: {d.detail}"


def test_low_track_quality_denied():
    d = authorize_engagement(
        role=Role.FIRE_CONTROL_AUTHORITY,
        track=make_track(tq=6),  # below EW_JAMMER min of 8
        effector=make_effector(),
        roe=base_roe(),
        human_confirmation=True,
    )
    assert not d.permit
    assert d.reasonCode == "TRACK_QUALITY_INSUFFICIENT"


def test_friendly_never_engageable():
    d = authorize_engagement(
        role=Role.FIRE_CONTROL_AUTHORITY,
        track=make_track(tq=15, identity=Identity.FRIEND),
        effector=make_effector(),
        roe=base_roe(),
        human_confirmation=True,
    )
    assert not d.permit
    assert d.reasonCode == "ROE_PROHIBITED"


def test_weapons_hold_overrides_everything():
    roe = ROE(weaponsControlStatus=WeaponsControlStatus.WEAPONS_HOLD)
    d = authorize_engagement(
        role=Role.FIRE_CONTROL_AUTHORITY,
        track=make_track(tq=15),
        effector=make_effector(),
        roe=roe,
        human_confirmation=True,
    )
    assert not d.permit
    assert d.reasonCode == "WEAPONS_HOLD"


def test_observer_role_cannot_release_fires():
    d = authorize_engagement(
        role=Role.OBSERVER,
        track=make_track(tq=15),
        effector=make_effector(),
        roe=base_roe(),
        human_confirmation=True,
    )
    assert not d.permit
    assert d.reasonCode == "NOT_AUTHORIZED"


def test_engagement_operator_may_not_release():
    # Operator may propose but only FCA releases (docs/05 roles table).
    d = authorize_engagement(
        role=Role.ENGAGEMENT_OPERATOR,
        track=make_track(tq=15),
        effector=make_effector(),
        roe=base_roe(),
        human_confirmation=True,
    )
    assert not d.permit
    assert d.reasonCode == "NOT_AUTHORIZED"


def test_human_in_the_loop_required():
    d = authorize_engagement(
        role=Role.FIRE_CONTROL_AUTHORITY,
        track=make_track(tq=15),
        effector=make_effector(),
        roe=base_roe(),
        human_confirmation=False,  # ROE requires it
    )
    assert not d.permit
    assert d.reasonCode == "NOT_AUTHORIZED"


def test_weapons_tight_requires_hostile_not_suspect():
    d = authorize_engagement(
        role=Role.FIRE_CONTROL_AUTHORITY,
        track=make_track(tq=15, identity=Identity.SUSPECT),
        effector=make_effector(),
        roe=base_roe(),
        human_confirmation=True,
    )
    assert not d.permit
    assert d.reasonCode == "ROE_PROHIBITED"


def test_tasking_authority():
    assert authorize_tasking(Role.SENSOR_MANAGER, True, TaskType.DWELL).permit
    assert not authorize_tasking(Role.OBSERVER, True, TaskType.DWELL).permit
    assert not authorize_tasking(Role.SENSOR_MANAGER, False, TaskType.CUE).permit
