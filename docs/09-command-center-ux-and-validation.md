# 09 — Command-Center UX and Validation Baseline

> This chapter defines the human-machine-interface baseline for the reference
> COP. It is an illustrative, unvalidated command-center concept. It is not a
> fielded fire-control system and does not replace operational, weapons-safety,
> airspace, cybersecurity, or rules-of-engagement certification.

## 1. Intended environment

The COP is a fixed-site, role-based Counter-UAS command-center application. It
does not target phones. Supported workstation targets are:

- 1280x720 minimum;
- 1920x1080 or 2560x1440 recommended;
- ultrawide and multi-display layouts where available; and
- 100%, 125%, and 150% Windows display scaling.

Unsupported sizes must receive an explicit workstation requirement instead of
a partially functional compact layout. Desktop zoom, keyboard access, focus,
contrast, and alarm behavior remain safety and accessibility requirements.

## 2. Workstation responsibilities

The application separates responsibilities instead of giving every user every
control:

| Workspace | Primary responsibility | State-changing authority |
|---|---|---|
| Shared COP | Common operational picture and critical alarms | Normally view-only |
| Fire Control | Prioritize, propose, authorize, release, hold, and abort | Policy and role gated |
| Sensor Manager | Sensor health, coverage, tasking, and custody | Sensor-task authority |
| Supervisor | WCS/ROE, authority, assignments, handover, and resource posture | Command-authority gated |
| AAR / Analyst | Reconstruct events, decisions, latency, expenditure, and BDA | Review/export only |
| Exercise Control | Threat injection, node/comms faults, autoplay, and reset | Simulation-only, isolated |

Identity, role, sector, WCS, authority source, policy version, controlling node,
communication condition, data age, and simulation/live state are persistent and
cannot be inferred solely from color.

## 3. Operator decision loop

The primary workflow follows:

`DETECT → CORRELATE → IDENTIFY → PRIORITIZE → PROPOSE → AUTHORIZE → ORDER → ACKNOWLEDGE → EFFECT → ASSESS`

Before a release, the decision pane must answer:

1. What is the object, and what evidence supports identity and classification?
2. How current and uncertain is the track?
3. What protected asset is threatened, and what is the predicted trajectory?
4. Which sensors contribute, and are they healthy?
5. Which effectors are feasible, why, and with what limitations?
6. Which policy, WCS, authority, and safety checks permit action?
7. Is another node or shooter already assigned?
8. Was the command delivered and acknowledged?

Denials are first-class results. A denial remains visible with the failed gate,
reason, policy version, and corrective action; it is not a transient toast only.

## 4. Alert and control behavior

Alerts are latched by severity and object, expose the required action, support
acknowledgement, and retain transition history. Audio is generated from actual
state, never from unrelated presentation chatter.

WEAPONS HOLD and ABORT are persistent, unambiguous controls. WEAPONS FREE or a
delegated automatic-release envelope requires a deliberate acknowledgement and
permanently visible armed state. Exercise injects and resets require confirmation
and are audited separately from operational actions.

## 5. Truth and uncertainty

Every operational value declares its source and freshness. LIVE data never falls
back silently to simulation. Connection states are explicit:

`CONNECTING → LIVE → DEGRADED → OFFLINE`

`SIM` is a separate truth domain. Static demonstration inventory or assumptions
are labeled `STATIC DEMO`.

Track quality is derived from observation age, continuity, covariance, source
geometry, and trust—not directly increased by a task request. Identity includes
confidence, evidence, declaring authority, time, and reversal history. Tasking
changes future observations rather than directly changing fused quality.

## 6. Engagement and AAR semantics

Authorization, transport, and effect are separate. The implemented lifecycle is:

`PROPOSED → AUTHORIZED → ACCEPTED → ACTIVE → ASSESSING → COMPLETE`

`NOT_SENT`, `DELIVERY_UNKNOWN`, and `BROKER_ACCEPTED` are orthogonal transport
outcomes, not evidence that an effector acted. Terminal lifecycle alternatives are
`DENIED`, `ABORTED`, and `FAILED`. A disconnected transport cannot report a queued
or delivered order unless durable storage or acknowledgement exists.

AAR metrics are reconstructed from immutable lifecycle snapshots and hash-chained
audit records. “Model Pk,” terminal hits,
confirmed defeats, misses, pending BDA, engagements, denials, aborts, leakers,
cost committed, and cost expended are separate measures. Defeats divided by
engagements is labeled “observed defeats per engagement,” never “hit rate (Pk).”

The scaffold remains a single-process demonstration: request idempotency,
reservations, consumed token IDs, simulator inventory, and active lifecycle state
are held in memory. A process restart or duplicate effector replica is therefore a
release inhibit/reconciliation boundary, not a supported continuity path. A fielded
adapter must persist monotonic command state and inventory at the authoritative
effector, restore reservations before accepting releases, and use durable workload
identity and replay protection shared across replicas.

## 7. Validation gates

A change is not complete until the following automated or operator-in-the-loop
checks pass:

- keyboard-only selection, tasking, proposal, authorization, abort, and AAR;
- focus retention across dynamic updates and modal focus trap/restore;
- 1280x720, 1920x1080, 2560x1440, and common display-scaling checks;
- one backend-published track visible inside the configured AO;
- server enforcement of WCS HOLD regardless of browser state;
- explicit disconnect, expiry, replay, stale-track, and duplicate-order failures;
- deterministic pause, speed, seed, and replay across every simulation subsystem;
- tasking outside sensor coverage does not increase track quality;
- ambiguous identity does not become hostile solely because time elapsed;
- AAR totals reconcile exactly with the engagement event chain; and
- defined frame-time and memory budgets for 150, 350, and 1,200 tracks.

Parameters and scenarios require subject-matter-expert review and provenance
before any claim of operational or comparative validity.
