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
make up        # starts NATS (JetStream), c2-core (:8000), sensor-sim scenario
make demo      # scripted (CLI) walkthrough of the full flow
make logs      # tail all services
make down      # stop and clean up
```

Then open:
- **http://localhost:8000/** — the **web COP** (the leadership demo: live tactical
  map, click-to-task, click-to-engage, with authority gates and the audit log).
- **http://localhost:8000/docs** — the API docs (FastAPI/OpenAPI UI).

## The web COP (leadership demo)

The browser UI is the front end to everything below. Served by c2-core, it runs in
**LIVE** mode (driven by the real bus and engagement gates). The *same* page,
opened standalone, runs an embedded **SIM** (the shareable, zero-install build).

**Fastest path — Auto-play brief.** Click **▶ Auto-play brief** for a ~90-second
hands-free run that narrates itself: swarm inbound → fuse → track-quality gate →
remote tasking → any-sensor/any-shooter engagement → C2 node loss → fight continues
→ outcomes. Any click takes over manual control. This is the one to show leadership.

The **outcomes scoreboard** (top-right) keeps the running score: threats defeated,
leakers, average time-to-defeat, engagements, and the headline metric —
**% of engagements that paired a sensor and shooter from different vendors** (the
any-shooter / anti-lock-in argument, quantified).

The **Red/Blue picture**: the status bar shows live **Red** (threat) and **Blue**
(friendly force) counts, and the **COP View** control (ALL / RED / BLUE) filters the
plot to each. Blue tracks (friendly CAP, ISR, UAS) patrol and are never engageable.

The **echelon federation HUD** (top-left) shows this Site C2 node and its links to
**BN-7 (Battalion)** and **RGT-3 (Regiment)**. **Integrate up-echelon** federates in
~1.2s — joining a leaf node to the common bus, so the Red+Blue COP, tracks, and
engagements are shared upward with no bespoke integration. If the site node is then
lost, Battalion still holds the shared picture.

**Scenarios** (bottom-left **AO** selector): **San Diego Coast** (Pacific to the
west, a Navy ship offshore, threats from the sea) and **El Paso Border** (US-Mexico
border, a TARS aerostat, threats from the south). Each switches the geography, the
named defended asset, the MGRS grid, and the joint laydown.

**Joint force**: the Blue picture is identifiable aircraft by **platform, service,
and altitude** (e.g., F/A-18E USN 6000 m, MV-22B USMC 900 m, MQ-9 USAF 7600 m), and
the effectors span **USA / USN / USMC** (Navy SeaRAM offshore, USMC LMADIS). Click a
track for its platform/service/altitude; engagement logs flag **cross-service** and
**cross-vendor** pairings.

**Data transport** (rail control): overlay the **MANET** mesh among tactical nodes
and the **5G** gNB with backhaul to echelon — the pub/sub bus riding real transports.

The auto-play now defeats a **mass raid** and reports a **kill ratio** (defeated vs.
leakers) in the scoreboard.

The **Architecture toggle** (NETWORKED / HUB & SPOKE) is the thesis in one click.
Flip to **HUB & SPOKE** and the legacy model appears: sensors hard-wired to single
shooters (dedicated links), the long-range interceptors/EW sitting **idle** with no
paired sensor, and a red **coverage-gap** overlay. The scoreboard quantifies it:
engageable area and effectors-usable drop sharply (e.g. ~81%→~30% area, 7→3
effectors). Flip back to **NETWORKED** and any sensor cues any shooter — the gaps
close and every effector is back in play. The plot itself is now a tactical map:
terrain and coastline, an MGRS grid, the named **FOB EAGLE** defended asset, a radar
sweep, RF lines-of-bearing, EO/IR slew cone, and track trails.

To drive it manually, in order:
1. **Fused picture.** Hostiles (red) and a friendly (blue) are tracked from several
   sensors at once — one coherent picture, not per-vendor screens.
2. **Track quality gates fires.** Click an inbound hostile; try **Engage** — it's
   denied while track quality is low.
3. **Remote tasking raises quality.** Click **Task sensor · DWELL**; watch TQ climb
   past the threshold. (Imperative 4.)
4. **Any-sensor / any-shooter.** Click **Engage** — the C2 node *pairs the best
   in-range effector at decision time* (no fixed sensor-shooter wiring) and the
   engagement runs to COMPLETE. (Imperative 5.)
5. **Positive control.** Try to engage the **friendly**, switch the operator role to
   **OBS**, or set **WEAPONS HOLD** — each is denied with a reason. (docs/05.)
6. **No vendor lock-in.** Hit **Swap radar vendor** — the radar's adapter changes
   vendor mid-scenario with zero integration and no track loss (conformance to the
   government-owned interface is the only requirement). (docs/02.)
7. **Resilience.** Hit **Simulate node loss** — a second C2 node continues the fight
   off the same shared COP. **Launch swarm** shows scale.

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
