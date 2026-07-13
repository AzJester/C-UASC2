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
| Change history | [`CHANGELOG.md`](CHANGELOG.md) | Everyone |
| Decisions (ADRs) | [`docs/decisions/`](docs/decisions/) | All |

## Public COP demo

The **Web COP** — the leadership-demo Common Operating Picture — is published
to GitHub Pages so anyone can view it with no install:

**<https://cuas.insightfuldefense.com/>**

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
Confirmed WEAPONS FREE is a joint simulation state: eligible ground effectors,
armed Blue aircraft, and designated naval close-in defenses all prosecute
HOSTILE tracks within their modeled quality, range, identity, and no-fire gates.
AutoBrief is hands-free: choose Joint Defense, Airport Defense, or Network
Resilience, then choose a real-time 2-, 5-, 10-, or 15-minute run. The brief
detects, fuses, tasks, shows the red release warning, confirms simulated
WEAPONS FREE, fires eligible joint effectors, and adds bounded waves until the
selected time limit before presenting outcomes.
Preset links: `?scn=guam&arch=NETWORKED&wx=WIND&tod=NIGHT&seed=N`.

The embedded simulation includes separate San Diego / North Island and MCAS
Miramar maps plus El Paso, Norfolk / Hampton Roads, Washington / National
Capital Region, and Guam area-of-operations presets. North Island includes
notional civilian arrivals and departures at KSAN with Mode-S / ADS-B identity
fields, a soft-kill counter-UAS layer around the airport, and an off-scale San
Clemente Island microwave-backhaul inset. Miramar has its own local satellite
map, dedicated sensors, six effector types, armed USMC air patrols, multi-axis
threats, and an offshore Navy screen. El Paso adds KELP traffic and airport defense plus three
distributed Border Patrol sensor/effector sites, all physically sited north of
the international border. Norfolk adds KORF traffic and airport defense, an
explicitly boxed inland terrestrial 5G fiber handoff, and a denser naval/
commercial surface picture. Washington opens as a satellite-preferred
National Capital Region view with layered illustrative protection around the
federal core, Pentagon, Joint Base Myer-Henderson Hall, Fort McNair, Joint Base
Andrews, Fort Belvoir, the Mark Center, and Reagan National Airport. It includes
360-degree multi-site approaches, KDCA civil arrivals/departures, three no-fire
areas, and an on-land hardened metro-fiber backhaul. Guam opens at whole-island scale with satellite
imagery preferred, a simplified public Census island boundary, FAA-referenced
PGUM/PGUA anchors, four simultaneous civil arrival/departure tracks, and a
four-sensor/four-effector airport-defense ring around PGUM plus four additional
notional protected sites. All fixed Guam nodes and the sampled 5G
fiber route are constrained to the island; only maritime tracks occupy the sea.
Guam's Regional view uses an INDOPACOM roster anchored at Camp H.M. Smith with
Pearl Harbor-Hickam, Kadena, and MCAS Iwakuni instead of continental peers.
Geography, inventory, coverage, and performance remain
illustrative and are not suitable for operational decisions.

5G endpoint descriptions are contextual: the map shows compact gNB and POP
nodes during normal operation, and clicking a 5G node on any scenario opens the
boxed site description. Clicking the node again or clicking elsewhere dismisses
it. Guam's coverage symbol is clipped to the island and its backbone follows an
explicitly on-land path to an on-island POP.

Moving track identifiers use persistent local label slots. Vessel tags stay
beside their hulls with a short leader line and shift only when an actual label
collision requires it, eliminating frame-to-frame jumping in busy sea lanes.

The workstation uses a compact split-wing layout: priority tracks and posture
remain in a narrow bounded left wing, decision support remains in a narrow
bounded right wing, and the complete tactical AO receives most of the screen.
Routine notices
replace one compact status line instead of stacking over the map. Complete
decision evidence and audit history open in dedicated dialogs, and the entire
interface—including the AAR and regional rollup—uses one command-screen
monospace typeface. The latest-events strip is tall enough to show wrapped
incoming messages without clipping its two-event summary.

The COP is designed for a fixed command-center workstation, not a phone. The
supported effective-display target is 1280x720 or larger (1920x1080, 4K, 5K,
ultrawide, and wall displays are supported). A desktop scale-to-fit path accounts
for Windows display scaling and browser chrome without misclassifying a high-DPI
display as undersized. Operator, sensor-management,
supervisor, shared-COP, AAR, and exercise-control responsibilities are separated
as described in [`docs/09-command-center-ux-and-validation.md`](docs/09-command-center-ux-and-validation.md).

`site/index.html` is the single source; `make build-cop` regenerates the
served and demo copies and stamps `COP_BUILD` (drift is CI-checked).
Deployment is automated by
[`.github/workflows/deploy-pages.yml`](.github/workflows/deploy-pages.yml):
every push to `main` that touches `site/` (or on-demand dispatch) snapshots
the `site/` tree onto the `gh-pages` branch, which GitHub Pages serves. The
`site/CNAME` file preserves the custom domain across those force-pushed
deployments. The `gh-pages` branch is a deployment artifact, not a development
branch.

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
