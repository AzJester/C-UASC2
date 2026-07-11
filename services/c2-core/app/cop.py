"""Common Operating Picture (COP) store.

Holds the live fused tracks built from the bus track stream. Tracks age out after
their TTL so the COP reflects current truth (docs/03 §5). In-memory by design: this
is a reference node; a fielded node persists/replicates per deployment tier.
"""
from __future__ import annotations

import threading
from datetime import datetime, timezone

from .models import Track


class CommonOperatingPicture:
    def __init__(self) -> None:
        self._tracks: dict[str, Track] = {}
        self._lock = threading.Lock()

    def upsert(self, track: Track) -> bool:
        """Insert a strictly newer live track update; never regress the COP."""
        now = datetime.now(timezone.utc)
        if self._is_stale(track, now):
            return False
        with self._lock:
            self._evict_expired_locked(now)
            previous = self._tracks.get(track.trackId)
            if previous is not None and not self._is_newer(track, previous):
                return False
            self._tracks[track.trackId] = track
        return True

    def get(self, track_id: str) -> Track | None:
        with self._lock:
            self._evict_expired_locked(datetime.now(timezone.utc))
            track = self._tracks.get(track_id)
        return track

    def list(
        self, min_track_quality: int | None = None, identity: str | None = None
    ) -> list[Track]:
        now = datetime.now(timezone.utc)
        with self._lock:
            self._evict_expired_locked(now)
            tracks = list(self._tracks.values())
        live = tracks
        if min_track_quality is not None:
            live = [t for t in live if t.trackQuality >= min_track_quality]
        if identity is not None:
            live = [t for t in live if t.identity.value == identity]
        return sorted(live, key=lambda t: t.trackId)

    def evict_expired(self) -> int:
        """Remove TTL-expired tracks and return the number evicted."""
        with self._lock:
            return self._evict_expired_locked(datetime.now(timezone.utc))

    def _evict_expired_locked(self, now: datetime) -> int:
        expired = [
            track_id
            for track_id, track in self._tracks.items()
            if self._is_stale(track, now)
        ]
        for track_id in expired:
            self._tracks.pop(track_id, None)
        return len(expired)

    @staticmethod
    def _is_newer(candidate: Track, previous: Track) -> bool:
        candidate_time = CommonOperatingPicture._aware(candidate.timeObserved)
        previous_time = CommonOperatingPicture._aware(previous.timeObserved)
        if candidate_time < previous_time:
            return False
        candidate_updated = CommonOperatingPicture._aware(
            candidate.timeUpdated or candidate.timeObserved
        )
        previous_updated = CommonOperatingPicture._aware(
            previous.timeUpdated or previous.timeObserved
        )
        if candidate_updated <= previous_updated:
            return False
        if (
            previous.observationSequence is not None
            and candidate.observationSequence is None
        ):
            return False
        if (
            candidate.observationSequence is not None
            and previous.observationSequence is not None
        ):
            if candidate.observationSequence < previous.observationSequence:
                return False
            if candidate.observationSequence == previous.observationSequence:
                if candidate_time != previous_time:
                    return False
            elif candidate_time <= previous_time:
                return False
        # Equal observation sequence/time is a valid fusion/coast update when the
        # processing time advances: uncertainty, TQ, data age, and fusionState may
        # change without a new sensor observation.
        return True

    @staticmethod
    def _aware(value: datetime) -> datetime:
        return value if value.tzinfo is not None else value.replace(tzinfo=timezone.utc)

    @staticmethod
    def _is_stale(track: Track, now: datetime | None = None) -> bool:
        if track.timeToLiveSeconds <= 0:
            return False
        now = now or datetime.now(timezone.utc)
        observed = CommonOperatingPicture._aware(track.timeObserved)
        return (now - observed).total_seconds() > track.timeToLiveSeconds
