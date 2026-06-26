"""Authority / ROE engine — the Policy Decision Point (PDP).

Pure decision logic for the engagement gates from docs/05 and docs/04 §3.1. Kept
free of network/IO so it is unit-testable in isolation and so the *policy* (what
this file encodes) stays government-owned while implementations are competed.

The four engagement gates, evaluated in order:
  1. track quality   >= ROE minimum for the effector class
  2. effector feasibility (availability, envelope) -- see pairing.py
  3. authority / ROE (role, identity, weapons control status, human-in-the-loop)
  4. hardware interlock -- enforced in the effector, below this software (not here)

This module covers gates 1 and 3 (and sensor-tasking authority). Gate 2 lives in
pairing.py; gate 4 is intentionally outside any C2 software.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from .models import (
    EffectorStatus,
    EffectorType,
    Identity,
    Role,
    Track,
    TaskType,
)


class WeaponsControlStatus(str):
    """Air-defense weapons control status (set by the ROE authority)."""

    WEAPONS_FREE = "WEAPONS_FREE"  # engage anything not positively friendly
    WEAPONS_TIGHT = "WEAPONS_TIGHT"  # engage only positively identified hostile
    WEAPONS_HOLD = "WEAPONS_HOLD"  # do not engage except in self-defense


# Identities that may ever be engaged. Friendly/neutral are never engageable.
ENGAGEABLE_IDENTITIES = {Identity.HOSTILE, Identity.SUSPECT}

# Minimum fused track quality (0..15) required to engage, per effector class.
# Government-owned policy (docs/04 §4.1): set here, never hard-coded by a vendor.
MIN_TRACK_QUALITY = {
    EffectorType.EW_JAMMER: 8,
    EffectorType.RF_TAKEOVER: 8,
    EffectorType.NET_CAPTURE: 9,
    EffectorType.DIRECTED_ENERGY: 10,
    EffectorType.KINETIC_GUN: 11,
    EffectorType.KINETIC_INTERCEPTOR: 12,
    EffectorType.OTHER: 12,
}

# Roles permitted to release fires vs. only propose.
ROLES_MAY_RELEASE = {Role.FIRE_CONTROL_AUTHORITY}
ROLES_MAY_PROPOSE = {Role.ENGAGEMENT_OPERATOR, Role.FIRE_CONTROL_AUTHORITY}
ROLES_MAY_TASK = {
    Role.SENSOR_MANAGER,
    Role.ENGAGEMENT_OPERATOR,
    Role.FIRE_CONTROL_AUTHORITY,
}


@dataclass
class ROE:
    """Rules of engagement, authored/changed only by the ROE authority (docs/05 §3.3)."""

    weaponsControlStatus: str = WeaponsControlStatus.WEAPONS_TIGHT
    requireHumanInTheLoop: bool = True
    minTrackQuality: dict = field(default_factory=lambda: dict(MIN_TRACK_QUALITY))


@dataclass
class Decision:
    permit: bool
    reasonCode: str
    detail: str = ""


def authorize_tasking(role: Role, sensor_taskable: bool, task_type: TaskType) -> Decision:
    """Authority gate for remote sensor tasking (Imperative 4)."""
    if role not in ROLES_MAY_TASK:
        return Decision(False, "NOT_AUTHORIZED", f"role {role.value} may not task sensors")
    if not sensor_taskable:
        return Decision(False, "EFFECTOR_UNAVAILABLE", "sensor does not accept remote tasking")
    return Decision(True, "OK", f"{task_type.value} tasking authorized")


def authorize_engagement(
    *,
    role: Role,
    track: Track,
    effector: EffectorStatus,
    roe: ROE,
    human_confirmation: bool,
) -> Decision:
    """Gates 1 and 3 for an engagement request (Imperative 5, bounded by docs/05).

    Gate 2 (feasibility) is checked separately in pairing.py before this call.
    Returns a PERMIT/DENY decision with a reason code; never reaches an effector on
    DENY.
    """
    # Gate 3a: weapons control status overrides everything.
    if roe.weaponsControlStatus == WeaponsControlStatus.WEAPONS_HOLD:
        return Decision(False, "WEAPONS_HOLD", "weapons control status is WEAPONS_HOLD")
    if effector.readiness.value == "WEAPONS_HOLD":
        return Decision(False, "WEAPONS_HOLD", "effector is in WEAPONS_HOLD")

    # Gate 3b: role must be permitted to release fires.
    if role not in ROLES_MAY_RELEASE:
        may_propose = role in ROLES_MAY_PROPOSE
        return Decision(
            False,
            "NOT_AUTHORIZED",
            f"role {role.value} may not release fires"
            + (" (may propose only)" if may_propose else ""),
        )

    # Gate 3c: ROE identity rules.
    if track.identity not in ENGAGEABLE_IDENTITIES:
        return Decision(
            False, "ROE_PROHIBITED", f"identity {track.identity.value} is not engageable"
        )
    if (
        roe.weaponsControlStatus == WeaponsControlStatus.WEAPONS_TIGHT
        and track.identity != Identity.HOSTILE
    ):
        return Decision(
            False,
            "ROE_PROHIBITED",
            "WEAPONS_TIGHT requires positively identified HOSTILE",
        )

    # Gate 1: fused track quality threshold for this effector class.
    min_tq = roe.minTrackQuality.get(effector.effectorType, 12)
    if track.trackQuality < min_tq:
        return Decision(
            False,
            "TRACK_QUALITY_INSUFFICIENT",
            f"TQ {track.trackQuality} < required {min_tq} for {effector.effectorType.value}",
        )

    # Gate 3d: human-in-the-loop when ROE requires it.
    if roe.requireHumanInTheLoop and not human_confirmation:
        return Decision(
            False,
            "NOT_AUTHORIZED",
            "human-in-the-loop confirmation required by ROE",
        )

    return Decision(True, "OK", "engagement authorized")
