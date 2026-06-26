# ADR-0001 — Pub/Sub Backbone Selection

- **Status:** Accepted (reference design)
- **Context date:** 2026
- **Deciders:** Reference architecture (illustrative; a fielded program's IDA/CCB owns the final call)

## Context

Imperative 3 requires a pub/sub engine at the edge and at echelon to fuse tracks
and share them in real time across heterogeneous sensors and effectors. The choice
must satisfy: edge↔echelon federation, DDIL tolerance, QoS tiering (fires must
preempt status), a small enough footprint for rugged edge compute, persistence for
audit/replay, and transport-independence from the canonical data model.

## Options

1. **DDS (OMG Data Distribution Service)** — brokerless peer-to-peer, very rich QoS,
   hard-real-time, strong defense/combat-systems pedigree. Heavier to operate at
   scale across WANs; discovery across many sites/enclaves is non-trivial.
2. **NATS + JetStream** — lightweight broker with leaf-node federation (clean
   edge↔echelon model), subjects with wildcards, JetStream persistence, simple ops.
   Not hard-real-time in the DDS sense.
3. **MQTT** — minimal, ubiquitous at the constrained edge/IoT. Limited native
   federation and QoS richness; typically paired with a heavier backbone.
4. **Kafka** — durable high-throughput log; excellent for echelon analytics/audit;
   wrong fit for low-latency edge fan-out.

## Decision

Use **NATS + JetStream as the reference backbone** for the general bus (tracks,
tasking, status, COP, audit) at both tiers, because its leaf-node federation maps
directly to the edge↔echelon topology and its footprint suits the edge.

Keep the canonical schemas **transport-independent** so a fielded program can run
**DDS for the hard-real-time effector/fire-control loop** and bridge it to NATS at
a gateway, and bridge **MQTT** for the lowest-end sensors. Kafka is reserved for the
echelon audit/analytics archive.

## Consequences

- The scaffold is runnable with one lightweight dependency; the demo stands up fast.
- A program is not locked to NATS: because the data model is transport-independent,
  swapping or supplementing the transport (DDS for fires) is a gateway change, not a
  data-model change — consistent with the MOSA "own the interface, compete the
  implementation" posture.
- QoS tiering ([§03](../03-pubsub-and-data-model.md#3-quality-of-service-tiers)) must
  be configured explicitly; NATS does not enforce fires-over-status priority by
  default.
- Hard-real-time guarantees for time-critical fire control should be validated on
  DDS in Phase 4 rather than assumed on NATS.
