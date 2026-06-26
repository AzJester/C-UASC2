# ADR-0002 — Canonical Data Model & Interface Description Languages

- **Status:** Accepted (reference design)
- **Context date:** 2026
- **Deciders:** Reference architecture (illustrative)

## Context

Imperative 2 makes the government own the interfaces. To own them, they must be
**machine-readable, validatable, tool-rich, and standards-based** — not prose ICDs.
The model must serve both request/response (C2 REST) and event/message (the bus)
without divergence, and be ownable as government data with sufficient rights.

## Decision

1. **One canonical data model in JSON Schema 2020-12** (`specs/schemas/`), reused by
   both the REST and the pub/sub contracts so there is a single source of truth for
   `Track`, `Detection`, `SensorTask`, status, and engagement types.
2. **OpenAPI 3.1 for REST** (`specs/openapi/`), **AsyncAPI 3.0 for pub/sub**
   (`specs/asyncapi/`). Both are open, widely tooled, and support codegen, mocking,
   and conformance testing.
3. **Track semantics aligned to Cursor-on-Target** so the model bridges to the
   fielded TAK ecosystem at a gateway, while staying JSON-native for cloud/edge
   ergonomics.
4. **Engagement eligibility keyed on two fused fields** — `trackQuality` and
   `identity` — so any node applies the same gate regardless of which sensors
   contributed the track.

## Alternatives considered

- **Cursor-on-Target XML as the native model.** Rejected as the *native* format
  (verbose, XML tooling, awkward for cloud-native services) but retained as a
  first-class **bridge** because of its huge fielded footprint.
- **Protobuf/Avro as the native model.** Excellent on the wire, but less
  approachable for an openly-published, human-readable government ICD. Kept as an
  option for a binary wire encoding behind the same JSON Schema-defined model.
- **Per-interface bespoke models.** Rejected: guarantees drift and reintroduces the
  pairwise-integration problem the whole design exists to remove.

## Consequences

- A single schema change propagates to both REST and bus contracts and to the
  conformance suite — drift is structurally discouraged.
- The model is openly publishable and citable ("conform to Interface vX.Y"),
  satisfying the API governance model in [§02](../02-api-governance.md).
- A CoT bridge must be maintained as the TAK ecosystem evolves.
- JSON on the wire is heavier than binary; for the highest-rate detection feeds a
  binary encoding of the *same* model can be introduced without changing semantics.
