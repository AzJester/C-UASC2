# C-UAS C2 Reference Architecture (JIATF 401)

A government-owned, layered reference architecture and runnable scaffold for a
**common Counter-Unmanned Aircraft Systems (C-UAS) Command and Control (C2)**
capability, scoped to the Department of Defense's counter-small-UAS (C-sUAS)
enterprise under **Joint Interagency Task Force 401 (JIATF 401)**.

It exists to turn five strategic imperatives into something buildable:

1. **One common C-UAS C2 for all services** — web-based, cloud-enabled, intuitive across MOSs, over-the-air updatable.
2. **Government-owned, open APIs** — the department defines and owns the interfaces connecting every sensor, effector, and C2 node.
3. **A pub/sub backbone at the edge and at echelon** — real-time data sharing and track fusion across heterogeneous sensors/effectors.
4. **Remote sensor tasking** — sensors and effectors controlled across the network, not just locally.
5. **Remote fire control and engagement** — any authorized C2 node may engage any effector using any track of sufficient quality (any-sensor / any-shooter), replacing hub-and-spoke pairings.

> This repository is a **reference design and interface contract**, not a fielded
> weapons system. The scaffold demonstrates the interfaces and data flows so the
> government can own and competition-test them. See
> [`docs/05-security-authority-safety.md`](docs/05-security-authority-safety.md)
> for the positive-control and safety boundaries any production build must enforce.

## How to read this

The documentation is **layered** — start at the top for the "what and why,"
descend for the engineering reference.

| Layer | Document | Audience |
|---|---|---|
| Decision brief | [`docs/00-executive-summary.md`](docs/00-executive-summary.md) | Leadership, acquisition, JIATF 401 staff |
| Reference architecture | [`docs/01-reference-architecture.md`](docs/01-reference-architecture.md) | Architects, integrators |
| API governance | [`docs/02-api-governance.md`](docs/02-api-governance.md) | Program / interface-control owners |
| Pub/sub & data model | [`docs/03-pubsub-and-data-model.md`](docs/03-pubsub-and-data-model.md) | Data / messaging engineers |
| Sensor tasking & fire control | [`docs/04-sensor-tasking-and-fire-control.md`](docs/04-sensor-tasking-and-fire-control.md) | Fires / fire-control engineers |
| Security, authority & safety | [`docs/05-security-authority-safety.md`](docs/05-security-authority-safety.md) | Security, safety, ATO authorities |
| Edge topology, DevSecOps & OTA | [`docs/06-edge-topology-devsecops-ota.md`](docs/06-edge-topology-devsecops-ota.md) | Platform / DevSecOps engineers |
| Roadmap | [`docs/07-roadmap.md`](docs/07-roadmap.md) | Program managers |
| Standards crosswalk | [`docs/08-standards-crosswalk.md`](docs/08-standards-crosswalk.md) | Standards / interoperability leads |
| Command-center UX & validation | [`docs/09-command-center-ux-and-validation.md`](docs/09-command-center-ux-and-validation.md) | Operators, human-factors engineers, testers |
| Decisions (ADRs) | [`docs/decisions/`](docs/decisions/) | All |

## Public COP demo

The **Web COP** — the leadership-demo Common Operating Picture — is published
to GitHub Pages so anyone can view it with no install:

**<https://azjester.github.io/C-UASC2/>**

The published page is the standalone build ([`site/index.html`](site/index.html)):
it runs an embedded, seeded-random simulation entirely in the browser (no
backend). Satellite imagery is the default basemap and fetches public Esri World
Imagery tiles; when they are unreachable the COP falls back to its synthetic
tactical map, so the page still works fully offline. All figures are simulated;
every view is labeled for demonstration only.
The same page runs in LIVE mode against the real c2-core REST API and bus
when c2-core serves it (`make up`, then <http://localhost:8000/>).

The COP uses **modality-inspired, illustrative sensing behavior** (radar clutter
floor, RF-blind comms-silent threats, EO/IR cueing and night/rain degradation)
and **notional engagement timing and outcomes** (flight times,
directed-energy dwell, EW soft-kill outcomes, magazine rearm). These values are
unvalidated and must not be used for operational decisions or comparative
effectiveness claims. The demonstration includes a TEWA engagement queue with
human-in/on-the-loop consent, battle damage assessment, no-fire collateral zones,
threat profiles (low ingress, OWA cruise, ISR orbiters, decoys), civilian
air and boat traffic, seeded weather and time of day, a degraded-comms
inject, a notional cost-exchange ledger, and an AAR replay scrubber.
Preset links: `?scn=elpaso&arch=HUB&wx=RAIN&tod=NIGHT&seed=N`.

The COP is designed for a fixed command-center workstation, not a phone. The
supported target is 1280x720 or larger (1920x1080 or 2560x1440 recommended),
including common Windows display-scaling settings. Operator, sensor-management,
supervisor, shared-COP, AAR, and exercise-control responsibilities are separated
as described in [`docs/09-command-center-ux-and-validation.md`](docs/09-command-center-ux-and-validation.md).

`site/index.html` is the single source; `make build-cop` regenerates the
served and demo copies and stamps `COP_BUILD` (drift is CI-checked).
Deployment is automated by
[`.github/workflows/deploy-pages.yml`](.github/workflows/deploy-pages.yml):
every push to `main` that touches `site/` (or on-demand dispatch) snapshots
the `site/` tree onto the `gh-pages` branch, which GitHub Pages serves. The
`gh-pages` branch is a deployment artifact, not a development branch.

## The runnable scaffold

The interfaces are not just described, they are **specified and demonstrated** so
they can be competed and conformance-tested:

```
specs/
  schemas/      canonical JSON Schema data model (track, sensor, effector, tasks, engagements)
  openapi/      OpenAPI 3.1 contract for the C2 REST API
  asyncapi/     AsyncAPI 3.0 contract for the pub/sub topic taxonomy
services/
  c2-core/      FastAPI C2 node: builds the COP from the track stream, tasks sensors,
                orders engagements (with authority checks); serves the web COP at /
    app/static/cop.html   the web COP UI (LIVE on the stack, embedded SIM standalone)
  sensor-sim/   scenario engine: flies a moving UAS swarm, acts as taskable sensor + effector
```

### Web COP (the leadership demo)

A single self-contained page (`services/c2-core/app/static/cop.html`) is the visual
front end: a live tactical map with track fusion, remote sensor tasking, distributed
any-sensor/any-shooter engagement, and positive-control denials, plus an audit log.

- **Served by c2-core** (`make up`, then http://localhost:8000/) it runs in **LIVE**
  mode against the real bus and engagement gates.
- **Opened standalone** it runs an **embedded simulation** with no backend — the
  zero-install build you can hand to a briefer. See [`docs/QUICKSTART.md`](docs/QUICKSTART.md#the-web-cop-leadership-demo).

Stand the whole thing up locally (broker + C2 node + a simulated sensor) with one command:

```bash
make up        # docker compose: ACL-scoped NATS + c2-core + sensor-sim
make demo      # walk the any-sensor/any-shooter flow end to end
make test      # run the data-model and engagement-authority tests
make down
```

JetStream is enabled in the local broker for extension work, but the checked-in
command adapter intentionally uses signed Core NATS and distinguishes NOT_SENT,
DELIVERY_UNKNOWN, and broker acceptance. It does not claim durable delivery or
effector action without an application-level lifecycle acknowledgement.

See [`docs/QUICKSTART.md`](docs/QUICKSTART.md) for the guided walkthrough.

## Provenance of the institutional context

- Army designated DoD Executive Agent for C-sUAS (2019); Joint C-sUAS Office (JCO) established under DoDD 3800.01E.
- **JIATF 401** established Aug 2025 (SecDef memo), realigning JCO authorities/resources.
- **FAAD C2** (Northrop Grumman) is the interim C-sUAS C2 of record; this design describes the open, government-owned target state it should evolve toward.
- Interface direction follows the DoD **API Technical Guidance** (MVCR1, Jul 2024) and **MOSA Implementation Guidebook** (Feb 2025), both from the Office of the CTO/DoD.

Full citations in [`docs/00-executive-summary.md`](docs/00-executive-summary.md#references).
