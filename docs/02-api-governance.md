# 02 — API Governance (Government-Owned Interfaces)

> Imperative 2: *Create, publish, and manage open-source APIs for integration of
> C-UAS. The department must define and own the APIs connecting all sensors,
> effectors, and C2.*

This is the keystone. Everything else recompetes cheaply only if the government
holds the interfaces. This chapter defines **what the government owns, how it
publishes it, and how it keeps it open over time.**

## 1. What "government-owned API" concretely means

It is not a slogan; it is a set of acquirable artifacts and rights:

1. **Interface Control Documents (ICDs)** expressed as machine-readable specs —
   the `specs/` in this repo (JSON Schema, OpenAPI, AsyncAPI). The government holds
   the authoritative copy and the version history.
2. **Data rights** to those interfaces and the canonical data model — at minimum
   Government Purpose Rights, captured in every contract from now on, so the
   interfaces can be shared with competing vendors.
3. **A public conformance suite** — the executable tests a vendor's adapter must
   pass. Integration becomes "pass the suite," not "win an integration contract."
4. **A reference implementation** (this scaffold) — proof the interfaces are
   implementable and a fixture vendors test against.
5. **A registry** — one authoritative, versioned home for all of the above.

DoD's **API Technical Guidance** (MVCR1) and the **MOSA Implementation Guidebook**
are the policy backing: open, consensus-based, standards-based interfaces, owned by
the government, are the prescribed means of preventing vendor lock-in.

## 2. The interface owner (decide this first)

| Role | Held by | Responsibility |
|---|---|---|
| **Interface Design Authority (IDA)** | A government office under JIATF 401 / Army EA | Owns the canonical schemas/specs; approves changes; runs the registry |
| **Conformance Authority** | Government test org (can leverage a Service lab) | Maintains and runs the conformance suite; issues conformance certificates |
| **Change Control Board (CCB)** | IDA-chaired, Service + vendor reps | Reviews proposed interface changes; balances stability vs. capability |

Without a named IDA, "open APIs" silently revert to whoever writes the most code.
This is the single most important governance decision.

## 3. Versioning and compatibility policy

The bus and the REST API are versioned independently of any vendor product.

- **SemVer for interfaces.** `MAJOR.MINOR.PATCH`.
  - PATCH: clarifications, no wire change.
  - MINOR: backward-compatible additions (new optional fields, new topics). Fielded
    components keep working.
  - MAJOR: breaking change. Requires CCB approval and a migration window.
- **Additive-by-default.** New capability is added as optional fields/topics so old
  consumers ignore what they don't understand (the "tolerant reader" rule).
- **Schema in the envelope.** Every message carries `schemaVersion`
  (`specs/schemas/envelope.schema.json`) so consumers can route by version and the
  registry can enforce compatibility.
- **Deprecation, never silent removal.** Fields/topics are marked deprecated for a
  published window before a MAJOR removes them.

## 4. Standards posture (open, not bespoke)

Per the API Technical Guidance, prefer publicly available standards over
vendor-specific protocols. This design's choices and the alternatives it stays
compatible with are in the [Standards Crosswalk](08-standards-crosswalk.md). In
brief:

- **Description languages:** OpenAPI 3.1 (REST), AsyncAPI 3.0 (pub/sub),
  JSON Schema 2020-12 (data model). All open, tool-rich, machine-checkable.
- **Track semantics** align with **Cursor-on-Target (CoT)** concepts and remain
  bridgeable to **Link 16 / VMF** at gateways; the canonical model is JSON-native
  for cloud/edge ergonomics with a documented CoT mapping.
- **MOSA alignment:** the L1↔L2 interface is a *key interface* in MOSA terms and is
  the one the government most jealously guards.

## 5. Publication and "open source"

"Open-source APIs" in the DoD sense means **openly published and
government-controlled**, not necessarily public-internet. Practical model:

- **Specs + conformance suite + reference impl** published to the appropriate
  level (e.g., a DoD source repository / Iron Bank-adjacent registry, or
  controlled distribution where required).
- **Versioned, citable releases** so a contract can require "conformance to
  C-UAS C2 Interface vX.Y."
- **Issue/proposal process** so Services and vendors can propose changes through
  the CCB rather than forking the interface in private.

## 6. Conformance: turning integration into a test

```
Vendor sensor  ──> [vendor-written adapter] ──> Conformance Suite ──> Certificate
                                                  │
                                                  ├─ schema validation (all messages)
                                                  ├─ topic/QoS behavior (AsyncAPI)
                                                  ├─ REST contract (OpenAPI) where applicable
                                                  ├─ tasking round-trip (cue → higher-rate track)
                                                  └─ engagement round-trip (order → status lifecycle)
```

A passing certificate is the entry ticket to the bus. This is the mechanism that
converts the integration cost curve from **O(sensors × effectors)** to
**O(sensors + effectors)**: each component is integrated once, to the interface,
not pairwise to every counterpart.

## 7. What the government does *not* need to own

To keep this affordable and to attract vendors, the government owns the
*interfaces and data model*, not necessarily:

- vendor signal-processing internals,
- proprietary classifier weights/algorithms,
- effector fire-control internals below the safety interface.

It owns enough to **swap the box** without owning everything inside it. That is the
MOSA bargain: open at the seams, competitive behind them.

## 8. Anti-lock-in checklist (put in every solicitation)

- [ ] Adapter maps to the government canonical schemas (`specs/schemas/`).
- [ ] Vendor delivers a passing conformance certificate.
- [ ] Government Purpose Rights to the adapter's interface mapping.
- [ ] No proprietary, undocumented extensions on key interfaces.
- [ ] OTA update hooks conform to [§06](06-edge-topology-devsecops-ota.md).
- [ ] Component is demonstrably swappable in the reference scaffold.

Continue to [§03 — Pub/Sub & Data Model](03-pubsub-and-data-model.md).
