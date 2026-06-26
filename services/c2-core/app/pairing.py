"""Weapon-target pairing and effector feasibility (gate 2).

Distributed weapon pairing (docs/04 §3.2): pairing is computed at decision time
from what is available and in range, not wired at install time. This is the kind of
scoring model competed *behind* the government-owned interface; the version here is
deliberately simple so the interface is demonstrable.
"""
from __future__ import annotations

import math

from .authority import Decision
from .models import EffectorStatus, EffectorType, EngagementType, Readiness, Track

# Which effect types each effector class can deliver.
_COMPATIBLE = {
    EffectorType.EW_JAMMER: {EngagementType.EW_DEFEAT},
    EffectorType.RF_TAKEOVER: {EngagementType.RF_TAKEOVER},
    EffectorType.NET_CAPTURE: {EngagementType.NET_CAPTURE},
    EffectorType.DIRECTED_ENERGY: {EngagementType.DIRECTED_ENERGY},
    EffectorType.KINETIC_GUN: {EngagementType.KINETIC},
    EffectorType.KINETIC_INTERCEPTOR: {EngagementType.KINETIC},
    EffectorType.OTHER: set(EngagementType),
}


def _haversine_m(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Great-circle distance in meters."""
    r = 6371000.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp = math.radians(lat2 - lat1)
    dl = math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * r * math.asin(math.sqrt(a))


def check_feasibility(
    track: Track, effector: EffectorStatus, engagement_type: EngagementType
) -> Decision:
    """Gate 2: is this effector available, compatible, and in envelope for this track?"""
    if effector.readiness not in (Readiness.READY, Readiness.DEGRADED):
        return Decision(False, "EFFECTOR_UNAVAILABLE", f"effector readiness {effector.readiness}")

    if engagement_type not in _COMPATIBLE.get(effector.effectorType, set()):
        return Decision(
            False,
            "EFFECTOR_UNAVAILABLE",
            f"{effector.effectorType.value} cannot deliver {engagement_type.value}",
        )

    if effector.magazine is not None and effector.magazine.remaining <= 0:
        return Decision(False, "EFFECTOR_UNAVAILABLE", "magazine empty")

    env = effector.engagementEnvelope
    if env is not None and env.location is not None and env.maxRangeMeters > 0:
        d = _haversine_m(
            env.location.lat,
            env.location.lon,
            track.kinematics.position.lat,
            track.kinematics.position.lon,
        )
        if d > env.maxRangeMeters:
            return Decision(
                False, "OUT_OF_ENVELOPE", f"range {d:.0f}m > max {env.maxRangeMeters:.0f}m"
            )
        if d < env.minRangeMeters:
            return Decision(
                False, "OUT_OF_ENVELOPE", f"range {d:.0f}m < min {env.minRangeMeters:.0f}m"
            )
        alt = track.kinematics.position.altMeters
        if env.maxAltMeters > 0 and not (env.minAltMeters <= alt <= env.maxAltMeters):
            return Decision(False, "OUT_OF_ENVELOPE", f"altitude {alt:.0f}m outside envelope")

    return Decision(True, "OK", "effector feasible")


def best_effector(
    track: Track, effectors: list[EffectorStatus], engagement_type: EngagementType
) -> EffectorStatus | None:
    """Pick the best feasible effector for a track (distributed weapon pairing).

    Scoring here is intentionally simple (feasible + most magazine remaining). A
    fielded system competes a richer model: Pk vs. classification, collateral,
    husbanding effectors for higher-priority threats (docs/04 §3.2).
    """
    feasible = [
        e for e in effectors if check_feasibility(track, e, engagement_type).permit
    ]
    if not feasible:
        return None
    return max(feasible, key=lambda e: (e.magazine.remaining if e.magazine else 0.0))
