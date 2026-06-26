# 08 — Standards Crosswalk

This design favors open, government-ownable standards over vendor protocols, per
the DoD API Technical Guidance and MOSA. This chapter maps the choices here to the
broader standards landscape so integrators know what to conform to and what to
bridge.

## 1. Interface description languages

| Concern | This design | Why | Bridges to |
|---|---|---|---|
| REST contracts | **OpenAPI 3.1** | Ubiquitous, machine-checkable, codegen + mock + conformance tooling | — |
| Pub/sub contracts | **AsyncAPI 3.0** | The OpenAPI-equivalent for event/message APIs; describes topics, schemas, QoS | — |
| Data model | **JSON Schema 2020-12** | Transport-independent validation; one model reused by REST + bus | Protobuf/Avro if a binary wire format is needed |

## 2. Messaging / transport

| Option | Strengths | Best role | Notes |
|---|---|---|---|
| **NATS + JetStream** (scaffold default) | Lightweight, edge-friendly, leaf-node federation edge↔echelon, persistence | General bus, edge tier, COP/status/tasking | See [ADR-0001](decisions/0001-pubsub-backbone.md) |
| **DDS** (OMG) | Hard real-time, rich QoS, no broker, defense pedigree (used in combat systems) | The time-critical effector/fire-control loop | Bridge to NATS at a gateway |
| **MQTT** | Minimal footprint, ubiquitous at constrained edge | Lowest-end sensor/IoT links | Bridge to the main bus |
| **Kafka** | High-throughput durable log | Echelon analytics/audit archive | Not for edge/real-time |

The canonical schemas are transport-independent on purpose, so a program can mix
these and bridge at gateways without changing the data model.

## 3. Track / situational-awareness semantics

| Standard | Relationship to this design |
|---|---|
| **Cursor-on-Target (CoT)** | The `Track`/envelope model is CoT-aligned in meaning (event, point, detail, time, identity); a documented CoT XML ⇄ canonical-JSON mapping lets TAK/ATAK consume the COP at a gateway |
| **TAK (ATAK/WinTAK/TAK Server)** | A natural consumer/producer of the COP via the CoT bridge; not the system of record but a widely fielded client |
| **Link 16 / TADIL-J, VMF** | Joint tactical data links; bridged at a gateway for joint/coalition air picture exchange (Phase 6) |
| **STANAG 4586 / MAVLink** | UAS *control* protocols (for the threats and for friendly UAS); relevant to sensor/effector adapters, not the C2 bus |

## 4. Open-architecture frameworks (MOSA family)

The L1↔L2 interface ([§01](01-reference-architecture.md#2-logical-layers)) is the
"key interface" MOSA tells you to own. Adjacent service frameworks this design
aligns with rather than competes against:

| Framework | Domain | Alignment |
|---|---|---|
| **MOSA** (10 U.S.C. § 4401) | DoD-wide statutory open systems | Foundational; government owns key interfaces |
| **SOSA** (Sensor Open Systems Architecture) | Sensor hardware/firmware modularity | Sensor adapters can sit atop SOSA-aligned sensors |
| **OMS / UCI** (Open Mission Systems / Universal C2 Interface) | Air Force mission-systems interfaces | Canonical model bridgeable to OMS/UCI payloads at echelon |
| **VICTORY** | Ground-vehicle C4ISR/EW integration | Ground-mounted C-UAS nodes ride VICTORY on-platform; bridge to the bus |
| **FACE** | Avionics software portability | Relevant to airborne effector/sensor adapter portability |

## 5. Security / platform standards

| Standard | Role |
|---|---|
| **DoD Zero Trust Reference Architecture** | mTLS identity, per-topic ACLs, signed messages ([§05](05-security-authority-safety.md)) |
| **RMF / cATO** | Continuous authorization basis for OTA ([§06](06-edge-topology-devsecops-ota.md)) |
| **DoD Enterprise DevSecOps / Platform One / Iron Bank** | Hardened images, pipeline, inheritance |
| **SBOM (SPDX/CycloneDX)** | Supply-chain integrity, signed with each artifact |
| **PKI / mTLS, FIPS-validated crypto** | Component and operator identity, message signing |

## 6. Policy spine (the "why we may")

| Reference | What it authorizes / requires |
|---|---|
| DoDD 3800.01E | C-sUAS for UAS Groups 1–3; Army as EA |
| SecDef memo, JIATF 401 (Aug 2025) | Realigns JCO authorities/resources into JIATF 401 |
| DoD API Technical Guidance (MVCR1, Jul 2024) | Open, standards-based, government-controlled APIs |
| MOSA Implementation Guidebook (Feb 2025) | How to implement MOSA / avoid vendor lock-in |
| 10 U.S.C. § 4401 | MOSA as a statutory requirement |

(Full hyperlinks in [§00 References](00-executive-summary.md#references).)

## 7. Conformance, not adoption-by-name

Listing a standard is not interoperability. A component is interoperable here only
when it **passes the conformance suite** against the government-owned specs
([§02](02-api-governance.md#6-conformance-turning-integration-into-a-test)).
Standards in this chapter define *what to bridge to*; the suite defines *what
"done" means*.
