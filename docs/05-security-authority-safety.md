# 05 — Security, Authority & Safety

Distributing fire control widens *who can* engage. This chapter is the
counterweight: it pins down *whether they're allowed to*, and guarantees that
distribution never weakens positive control. Read it alongside
[§04](04-sensor-tasking-and-fire-control.md).

> This is a reference design. Nothing here substitutes for Service weapons-safety
> certification, airworthiness/range safety authorities, or the applicable ROE.
> It provides the *digital framework* those authorities attach to.

## 1. Three separate questions

Most failures come from conflating these. Keep them distinct.

| Question | Mechanism | Section |
|---|---|---|
| **Who are you?** (authentication) | Zero Trust identity, mTLS, signed messages | §2 |
| **May you do this, now?** (authorization) | Policy Decision Point: role + ROE + context | §3 |
| **Will the weapon physically permit it?** (safety) | Hardware interlocks, human-in-the-loop | §4 |

## 2. Zero Trust foundation

Per the DoD Zero Trust Reference Architecture, assume the network is contested.

- **Every component has a cryptographic identity** (PKI/mTLS). Sensors, effectors,
  C2 nodes, and operators all authenticate; no "trusted because on the LAN."
- **Every message is signed and integrity-protected.** An `EngagementOrder` carries
  the issuing node's signature and an `authorityToken`; an effector verifies both
  before acting.
- **Least privilege, per topic.** A sensor adapter may publish `cuas.track.*` and
  subscribe `cuas.sensor.task.{its-id}` — nothing else. The broker enforces topic
  ACLs tied to identity.
- **Classification end to end.** The envelope's `classification` is honored by
  brokers, UI banners, and cross-domain guards; data does not leak across levels.

## 3. Authorization: the Policy Decision Point (PDP)

Authorization is centralized in policy, distributed in enforcement. A **PDP**
decides; **PEPs** (policy enforcement points) in each C2 node and effector adapter
enforce.

### 3.1 Attribute-based access control (ABAC) for engagement

A request to engage is evaluated against attributes, not a static pairing table:

```jsonc
PERMIT iff
  subject.role           ∈ { FIRE_CONTROL_AUTHORITY, ... }        // who
  AND subject.sector     covers track.position                    // where they own
  AND track.identity     ∈ ROE.engageableIdentities               // what ROE allows
  AND track.trackQuality ≥ ROE.minTQ[effector.class]              // good enough
  AND airspace.status    ∈ ROE.permittedAirspace                  // deconfliction
  AND ( ROE.weaponsControlStatus == WEAPONS_FREE
        OR explicit.humanAuthorization == true )                  // control status
```

The PDP returns `PERMIT`/`DENY`, a reason code, and on permit a short-lived,
signed **`authorityToken`** scoped to that track+effector+window. The effector
trusts the token, not the requesting node's say-so.

### 3.2 Roles (illustrative)

| Role | May task sensors | May propose engagement | May authorize/release fires |
|---|---|---|---|
| Observer | no | no | no |
| Sensor Manager | yes | no | no |
| Engagement Operator | yes | yes (propose) | no |
| Fire Control Authority | yes | yes | yes |
| Air Defense / ROE Authority | — | — | sets ROE / weapons control status |

This makes "any C2 node *with permissions*" precise: permission is an attribute the
PDP checks, not a property of being on the network.

### 3.3 ROE as code (with a human owner)

ROE is encoded as policy the PDP evaluates (weapons control status, engageable
identities, airspace, minimum TQ per effector class). It is **authored and changed
only by the designated ROE authority**, versioned, and audited. Encoding ROE does
not automate the decision to fight; it makes the boundaries machine-enforceable and
consistent across every node.

### 3.4 Degraded authority (fail-controlled)

If the PDP is unreachable, a node uses a **pre-delegated authority envelope** for
its sector, cached and signed in advance (e.g., "Engagement Operator may engage
HOSTILE Group 1–2 in sector 7 under WEAPONS_TIGHT"). Anything outside the cached
envelope is **denied** until reachback returns. The system fails *controlled*,
never *open*.

## 4. Safety: positive control that software cannot bypass

Authorization says "allowed." Safety says "physically prevented otherwise." These
are independent on purpose.

- **Hardware interlock in the effector.** The effector adapter performs a final
  safety check (arming state, geofence, master-arm) that a compromised or buggy C2
  node cannot override via software. The last gate is below the network.
- **Human-in-the-loop / on-the-loop policy.** The architecture supports both; which
  applies is set by ROE and weapons control status. For lethal effects against
  manned-airspace-adjacent threats, human-in-the-loop is the expected default.
  Autonomy is bounded by policy, not by vendor preference.
- **Master arm / weapons hold.** A `WEAPONS_HOLD` directive on
  `cuas.c2.directive.*` is honored as an overriding stop by every effector,
  independent of any pending order.
- **Deterministic abort.** Every engagement is abortable through its lifecycle
  (`AUTHORIZED→ACCEPTED→ACTIVE`); abort is a T0 message with the same priority as
  an order.
- **Fratricide/deconfliction.** Identity (`FRIEND`/`ASSUMED_FRIEND`/`NEUTRAL`) and
  airspace deconfliction are PDP inputs; the COP shows friendly tracks so operators
  and auto-pairing avoid them.

## 5. Auditability and production non-repudiation target

The production target writes every task, authority decision, order, and status to
a durable, identity-backed `cuas.audit.*` stream, with external signing/anchoring
for non-repudiation. Each record links the **track**, **request**, **engagement**,
**effector**, **authority decision and reason**, issuing identity, lifecycle state,
transport outcome, and assessment.

The checked-in reference node exposes those structured fields through `GET /audit`
and maintains a SHA-256 hash chain. With `C2_AUDIT_FILE`, it atomically persists and
fsyncs the local chain; selected transport evidence is also published best-effort.
This detects local tampering but is not externally anchored, durable-bus evidence or
cryptographic non-repudiation. A non-demo deployment must supply those controls.

## 6. Supply-chain & update integrity

- OTA updates are **signed**; components verify provenance before applying (see
  [§06](06-edge-topology-devsecops-ota.md)).
- Adapters run with least privilege and are scanned/hardened in the pipeline
  (Iron Bank-style hardened images).
- The conformance suite includes negative tests (malformed/forged messages must be
  rejected) so security properties are tested, not assumed.

## 7. Threats this design explicitly counters

| Threat | Countermeasure |
|---|---|
| Spoofed track injects a false target | Signed messages + sensor identity + fusion provenance (`contributingSensors`) |
| Forged engagement order | `authorityToken` from PDP + effector verifies signature + interlock |
| Compromised C2 node fires at will | PDP authority scoping + hardware interlock + WEAPONS_HOLD override |
| Replayed old order | `messageId`, timestamps, track staleness check at effector |
| Bus eavesdrop/tamper | mTLS + per-topic ACLs + classification enforcement |
| Lock-in via proprietary "security" extension | Open, government-owned interfaces + conformance negative tests |

Continue to [§06 — Edge Topology, DevSecOps & OTA](06-edge-topology-devsecops-ota.md).
