# Quickstart — Run the scaffold

This stands up the connective tissue locally: a **NATS** bus, the **c2-core** C2
node, and a **sensor-sim** that publishes tracks. You can then drive the
any-sensor/any-shooter flow through the C2 REST API.

> This is a reference scaffold to demonstrate the **government-owned interfaces**,
> not a fielded weapons system. The "effector" is simulated.

## Prerequisites

- Docker + Docker Compose, **or** Python 3.11+ for the no-Docker path.

## Option A — Docker Compose (recommended)

```bash
make up        # starts NATS (JetStream), c2-core (:8000), sensor-sim
make demo      # scripted walkthrough of the full flow
make logs      # tail all services
make down      # stop and clean up
```

Then open the API docs at **http://localhost:8000/docs** (FastAPI/OpenAPI UI).

## Option B — No Docker (local Python)

```bash
make venv                      # create venv + install deps
make run-nats                  # starts a local nats-server if installed, else see note
# in another shell:
make run-c2                    # uvicorn c2-core on :8000
make run-sim                   # publish simulated tracks
```

If `nats-server` is not installed locally, use Option A (Docker) — the broker is
the only component that benefits from the container.

## The demo flow (what `make demo` walks through)

It exercises the sequence from
[§01 §4](01-reference-architecture.md#4-the-any-sensor--any-shooter-flow):

1. **Register materiel.** A sensor and an effector register with c2-core
   (`POST /sensors`, `POST /effectors`). No pairing between them.
2. **Tracks flow over the bus.** sensor-sim publishes detections/tracks to
   `cuas.track.fused.*`; c2-core builds the COP. View it: `GET /cop`.
3. **Remote sensor tasking raises track quality.** `POST /sensors/{id}/tasks`
   with a `CUE`/`DWELL` task; the simulated sensor responds with a higher-quality
   track. Watch TQ rise in `GET /cop`.
4. **Any-shooter engagement under authority.** `POST /engagements` referencing the
   track and a *non-paired* effector. c2-core runs the four gates
   ([§04 §3.1](04-sensor-tasking-and-fire-control.md#31-the-engagement-pipeline-and-its-gates)):
   track quality, effector feasibility, authority/ROE, then publishes an
   `EngagementOrder`. The (simulated) effector reports
   `ACCEPTED→ACTIVE→COMPLETE` on `cuas.engagement.status.*`.
5. **Authority is enforced.** Try an engagement on a `FRIEND`/low-TQ track or
   without the right role — it is **denied** with a reason code. This is the
   positive-control boundary from [§05](05-security-authority-safety.md).
6. **Everything is audited.** `GET /audit` shows the immutable record linking
   track → authority decision → order → outcome.

## Useful endpoints

| Method | Path | Purpose |
|---|---|---|
| GET | `/health` | liveness + bus connectivity |
| GET | `/cop` | current fused common operating picture |
| POST | `/sensors` / `/effectors` | register materiel (no pairing) |
| POST | `/sensors/{id}/tasks` | remote sensor tasking |
| POST | `/engagements` | request an engagement (runs the four gates) |
| GET | `/engagements` | engagement lifecycle states |
| GET | `/audit` | non-repudiation record |

## What to look at next

- The contracts these endpoints honor: `specs/openapi/cuas-c2.yaml`,
  `specs/asyncapi/cuas-pubsub.yaml`, `specs/schemas/`.
- The authority logic: `services/c2-core/app/authority.py`.
- The tests that lock the behavior: `tests/`.
