# 00 — Executive Summary

**For:** JIATF 401 leadership, the Army (DoD Executive Agent for C-sUAS), Service
acquisition executives, and integrating program offices.

**Bottom line:** The department can field a common, joint C-UAS C2 capability fast
*and* avoid locking itself to any one vendor — but only if it **owns the
interfaces** and adopts a **publish/subscribe data backbone** that lets any
authorized node use any sensor and direct any effector. The five imperatives below
are mutually reinforcing; the government-owned API layer (imperative 2) is the
keystone that makes the other four affordable and recompetable.

---

## The problem, stated plainly

Today's C-sUAS fielding is a patchwork. Each new sensor or effector arrives with a
bespoke integration. FAAD C2 was selected as the *interim* joint C2 precisely to
stop every Service from building its own stovepipe, but interim C2 plus
vendor-defined interfaces still produces a hub-and-spoke system: a sensor is wired
to a specific shooter, integration is a contract action, and every addition is
slow and expensive. The Jailbreak experience is the symptom — integration friction
shows up the moment you try to mix vendors.

The fix is not "pick a better box." It is to make the **connective tissue** —
interfaces and the data bus — a government asset, then let industry compete to plug
into it.

## The five imperatives → what they actually require

| # | Imperative | What it requires to be real | Where it lives |
|---|---|---|---|
| 1 | **Common C2 for all services** | Web/cloud-native C2 app, role-based intuitive UI, OTA update of C2, sensors, effectors | [§01](01-reference-architecture.md), [§06](06-edge-topology-devsecops-ota.md) |
| 2 | **Government-owned open APIs** | Government holds the interface control documents (ICDs), data rights, and a public conformance suite; standards-based, not vendor-proprietary | [§02](02-api-governance.md) |
| 3 | **Pub/sub backbone at edge & echelon** | A message bus that fuses tracks and shares them in real time, degrading gracefully when disconnected | [§03](03-pubsub-and-data-model.md) |
| 4 | **Remote sensor tasking** | Sensors/effectors taskable over the network with a common command schema and arbitration | [§04](04-sensor-tasking-and-fire-control.md) |
| 5 | **Remote fire control / any-sensor any-shooter** | Any permitted C2 node can engage any effector with any sufficient-quality track; positive control preserved | [§04](04-sensor-tasking-and-fire-control.md), [§05](05-security-authority-safety.md) |

## Why government-owned APIs are the keystone

DoD's own **API Technical Guidance** and **MOSA Implementation Guidebook** say it
directly: open, standards-based, government-controlled interfaces are how you
prevent vendor lock-in and enable competition. The mechanism is concrete:

- The government publishes the **Track**, **Sensor Tasking**, **Effector Status**,
  and **Engagement** interface contracts (this repo's `specs/`).
- Any vendor that conforms can be plugged in or swapped without a new integration
  contract. Integration becomes a **conformance test**, not a bespoke project.
- The government acquires sufficient **data rights** to the interfaces (not
  necessarily to vendor internals) so it can recompete components for the system's
  life.

This is the difference between buying a system and owning an ecosystem.

## The architectural move: from hub-and-spoke to a shared bus

```
   HUB-AND-SPOKE (today)                  SHARED BUS (target)

   Sensor A --- C2 --- Effector A         Sensor A --\         /-- Effector A
                                          Sensor B ---+= BUS =+--- Effector B
   Sensor B --- C2 --- Effector B         Sensor C --/         \-- Effector C
                                                      |
   each link a bespoke integration         any node: any sensor -> any effector
   single points of failure                fused COP, distributed pairing
```

Every sensor publishes tracks to a common bus; a fusion service produces one
coherent threat picture; any authorized C2 node can pair *any* track of sufficient
quality with *any* available effector. Lose a node or a sensor and the rest of the
network keeps fighting. This is the practical meaning of "distributed weapon
pairing" and "ecosystem of survivability."

## What we are delivering in this repository

1. A **layered reference architecture** ([§01](01-reference-architecture.md)) the
   government can hand to integrators as the target state.
2. **Government-owned interface specs** (`specs/`): JSON-Schema data model,
   OpenAPI for C2 REST, AsyncAPI for the pub/sub topics — the literal artifacts
   imperative 2 calls for.
3. A **runnable scaffold** (`services/`) that stands up a broker, a C2 node, and a
   simulated sensor, and demonstrates the any-sensor/any-shooter flow with
   authority checks. It proves the interfaces are real and testable.

This is deliberately **not** a weapons system. It is the connective tissue,
specified and demonstrated, so the department can own it and compete the parts.

## Decisions leadership must make now

1. **Designate the interface authority.** Name the government office (under JIATF
   401 / the Army EA) that owns, versions, and governs the APIs and the
   conformance suite. Without an owner, "open APIs" drift back to vendor control.
2. **Mandate conformance, not bespoke integration.** New C-sUAS materiel competes
   against the published conformance suite as a contract requirement.
3. **Acquire the right data rights.** Government-purpose rights to the interfaces
   and the data model, captured in every contract from here forward.
4. **Fund the bus and the C2 baseline as products,** with continuous ATO and OTA,
   not as one-time deliveries. (See [§06](06-edge-topology-devsecops-ota.md).)
5. **Set the engagement-authority policy** (ROE encoding, human-in-the-loop
   requirements) that the any-shooter capability must enforce in software. (See
   [§05](05-security-authority-safety.md).)

## Cost / speed logic in one line

Every bespoke integration you remove is schedule and money returned; the
government-owned API layer converts an O(sensors × effectors) integration problem
into an O(sensors + effectors) conformance problem.

---

## References

- Congressional Research Service, *DoD Counter-Unmanned Aircraft Systems: Background and Issues for Congress*, R48477. https://www.congress.gov/crs-product/R48477
- DoD Executive Agent listing, *Counter-Small Unmanned Aircraft Systems (C-sUAS)*. https://dod-executiveagent.osd.mil/Agents/ViewAgent.aspx?agentId=2137
- SecDef memo, *Establishment of Joint Interagency Task Force 401* (28 Aug 2025). https://media.defense.gov/2025/Aug/28/2003790021/-1/-1/0/ESTABLISHMENT-OF-JOINT-INTERAGENCY-TASK-FORCE-401.PDF
- DefenseScoop, *Hegseth orders Army secretary to create new joint interagency counter-drone task force* (28 Aug 2025). https://defensescoop.com/2025/08/28/hegseth-army-new-counter-drone-task-force-jiatf-401/
- U.S. Army, *Joint Counter-sUAS strategy to address need for improved technology*. https://www.army.mil/article/239593/joint_counter_suas_strategy_to_address_need_for_improved_technology
- Office of the CTO (DoD), *Application Programming Interface (API) Technical Guidance*, MVCR1 (Jul 2024). https://www.cto.mil/wp-content/uploads/2024/08/API-Tech-Guidance-MVCR1-July2024-Cleared.pdf
- Office of the CTO (DoD), *Implementing a Modular Open Systems Approach in DoD Programs* (MOSA Implementation Guidebook, 27 Feb 2025). https://www.cto.mil/wp-content/uploads/2025/03/MOSA-Implementation-Guidebook-27Feb2025-Cleared.pdf
- U.S. Army, *Army announces selection of interim C-sUAS systems* (FAAD C2 as interim C2). https://www.army.mil/article/236713/army_announces_selection_of_interim_c_suas_systems

> MOSA is a statutory requirement for major defense acquisition programs
> (10 U.S.C. § 4401). Treat the references above as the policy spine for every
> design choice in this repository.
