"""Monotonicity and expiry invariants for the authoritative COP store."""
from datetime import datetime, timedelta, timezone

from app.cop import CommonOperatingPicture
from app.models import Identity, Kinematics, Position, Track


def make_track(
    sequence: int,
    observed: datetime,
    *,
    updated: datetime | None = None,
    ttl: float = 30,
    quality: int = 10,
    fusion_state: str | None = None,
) -> Track:
    return Track(
        trackId="TRK-MONOTONIC",
        kinematics=Kinematics(position=Position(lat=32.7, lon=-117.2, altMeters=100)),
        trackQuality=quality,
        identity=Identity.HOSTILE,
        observationSequence=sequence,
        timeObserved=observed,
        timeUpdated=updated,
        fusionState=fusion_state,
        timeToLiveSeconds=ttl,
    )


def test_upsert_rejects_equal_or_regressive_sequence_and_observation_time():
    cop = CommonOperatingPicture()
    now = datetime.now(timezone.utc)
    assert cop.upsert(make_track(2, now)) is True
    assert cop.upsert(make_track(2, now + timedelta(seconds=1))) is False
    assert cop.upsert(make_track(3, now)) is False
    assert cop.upsert(make_track(1, now + timedelta(seconds=2))) is False
    assert cop.upsert(make_track(3, now + timedelta(seconds=2))) is True
    assert cop.get("TRK-MONOTONIC").observationSequence == 3


def test_expired_updates_are_rejected_and_stored_tracks_are_evicted():
    cop = CommonOperatingPicture()
    now = datetime.now(timezone.utc)
    assert cop.upsert(make_track(1, now - timedelta(seconds=10), ttl=1)) is False
    assert cop.upsert(make_track(1, now, ttl=0.05)) is True

    # Drive expiry deterministically by replacing the stored track with an already
    # expired observation under the lock; public reads must evict, not merely hide.
    with cop._lock:
        cop._tracks["TRK-MONOTONIC"] = make_track(
            1, now - timedelta(seconds=1), ttl=0.05
        )
    assert cop.get("TRK-MONOTONIC") is None
    assert cop.evict_expired() == 0


def test_equal_observation_sequence_accepts_newer_coasting_fusion_update():
    cop = CommonOperatingPicture()
    now = datetime.now(timezone.utc)
    assert cop.upsert(
        make_track(7, now, updated=now, quality=12, fusion_state="CONFIRMED")
    )
    assert cop.upsert(
        make_track(
            7,
            now,
            updated=now + timedelta(seconds=1),
            quality=9,
            fusion_state="COASTING",
        )
    )
    stored = cop.get("TRK-MONOTONIC")
    assert stored.trackQuality == 9
    assert stored.fusionState.value == "COASTING"
