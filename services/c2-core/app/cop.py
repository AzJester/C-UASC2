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

    def upsert(self, track: Track) -> None:
        with self._lock:
            self._tracks[track.trackId] = track

    def get(self, track_id: str) -> Track | None:
        with self._lock:
            track = self._tracks.get(track_id)
        if track is None:
            return None
        if self._is_stale(track):
            return None
        return track

    def list(
        self, min_track_quality: int | None = None, identity: str | None = None
    ) -> list[Track]:
        now = datetime.now(timezone.utc)
        with self._lock:
            tracks = list(self._tracks.values())
        live = [t for t in tracks if not self._is_stale(t, now)]
        if min_track_quality is not None:
            live = [t for t in live if t.trackQuality >= min_track_quality]
        if identity is not None:
            live = [t for t in live if t.identity.value == identity]
        return sorted(live, key=lambda t: t.trackId)

    @staticmethod
    def _is_stale(track: Track, now: datetime | None = None) -> bool:
        if track.timeToLiveSeconds <= 0:
            return False
        now = now or datetime.now(timezone.utc)
        observed = track.timeObserved
        if observed.tzinfo is None:
            observed = observed.replace(tzinfo=timezone.utc)
        return (now - observed).total_seconds() > track.timeToLiveSeconds
