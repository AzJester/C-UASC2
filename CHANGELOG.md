# Change log

This file is the plain-language history of the C-UAS C2 application. Newest
changes are listed first. For the exact code-level history, see the
[GitHub commits](https://github.com/AzJester/C-UASC2/commits/main/).

## 2026-07-13

### West Coast maps and map navigation

- Split the former combined West Coast view into two independent areas:
  **San Diego / North Island** and **MCAS Miramar**.
- Centered each area on its actual location and gave each one a useful local
  scale with real Esri World Imagery satellite tiles.
- Added click-and-drag panning, mouse-wheel zooming around the cursor, a visible
  zoom percentage, and a **Re-center** button to every map.
- Gave Miramar its own sensors, layered effectors, 5G and terrestrial-fiber
  transport, cooperative aircraft traffic, threat axes, and USMC aircraft.
- Kept the map grid, range rings, labels, transport-node hit testing, and
  satellite imagery aligned while the operator pans or zooms.
- Merged in [PR #18](https://github.com/AzJester/C-UASC2/pull/18).

### Regional scenarios and operator workflow

- Added a complex **Washington, DC / National Capital Region** satellite
  scenario spanning the federal core and Northern Virginia. Eight protected
  locations include the White House complex, U.S. Capitol, Pentagon, Joint Base
  Myer-Henderson Hall, Fort McNair, Joint Base Andrews, Fort Belvoir, and the
  Mark Center.
- Added 22 illustrative sensors and 30 illustrative effectors to the NCR view,
  with local protection at every marked site and six-layer clusters at Joint
  Base Andrews and Fort Belvoir. Eight perimeter sectors can target nine NCR
  sites, while three no-fire areas constrain engagements around dense civil
  airspace and the federal core.
- Added Reagan National Airport (KDCA) with four simultaneous synthetic
  arrival/departure tracks, trackable Mode-S / ADS-B fields, local airport
  sensors and soft-kill effectors, plus an on-land hardened metro-fiber
  backhaul.
- Added Guam as a whole-island contested defense scenario with satellite
  imagery, land-constrained sensors and effectors, airport and port protection,
  island-to-island communications, and an Indo-Pacific regional rollup.
- Explicitly marked **Andersen Air Force Base**, **Naval Base Guam**, and
  **Marine Corps Base Camp Blaz** with service-colored base boxes. Expanded the
  island to 23 notional sensors and 31 notional effectors, including six-layer
  local protection around each military installation and a southern island
  screen.
- Expanded Guam International Airport to four simultaneous synthetic arrivals
  and departures with Mode-S / ADS-B identity fields, four local sensors, and
  four local soft-kill/capture effectors inside the airport no-fire area.
- Expanded Guam's airborne defense to six armed fighter patrols and four armed
  MQ-9A orbits. Under confirmed WEAPONS FREE, those aircraft join eligible
  ground and naval systems against hostile swarm tracks.
- Reworked Guam aggressors into eight 360-degree perimeter sectors. Each contact
  selects among Andersen AFB, Naval Base Guam, Camp Blaz, Guam International
  Airport, and the central power/communications site instead of converging on a
  single point.
- Expanded El Paso with airport traffic and defenses plus U.S.-side Border
  Patrol stations, sensors, and effectors that do not cross the international
  border.
- Expanded Norfolk with corrected on-land 5G backhaul, labeled terrestrial
  network endpoints, naval ships, commercial vessels, airport protection, and
  Hampton Roads geography.
- Added airport defenses and cooperative aircraft traffic wherever a scenario
  contains an airport.
- Reworked the command-center layout into narrower left and right operator wings
  so the map remains visible and the essential decision information fits without
  long scrolling.
- Increased the Latest Events area and stabilized nearby vessel identifier tags.
- Made 5G details contextual: the information box opens only after a node is
  clicked.
- Expanded Miramar ingress to all eight compass sectors while weighting most
  raids toward the Pacific approaches. Added an off-coast naval-screen inset,
  two Navy surface tracks, an armed MH-60R, and shipboard radar, SeaRAM, HELIOS,
  and close-in gun coverage that can be inspected by panning west.
- Merged through [PR #13](https://github.com/AzJester/C-UASC2/pull/13),
  [PR #14](https://github.com/AzJester/C-UASC2/pull/14), and the subsequent
  regional-scenario builds.

### Fire control, presentation, and AutoBrief

- Standardized the full application on the same monospaced computer-console
  typeface.
- Made release actions red with white text and made the final
  **CONFIRM FREE** warning flash red.
- Confirmed that WEAPONS FREE tasks eligible ground, aircraft, and naval
  effectors under the modeled range, identity, quality, and safety rules.
- Expanded AutoBrief into a hands-free detect, decide, release, and engagement
  demonstration with Joint Defense, Airport Defense, and Network Resilience
  choices.
- Added selectable AutoBrief durations of 2, 5, 10, and 15 minutes.
- Merged in [PR #17](https://github.com/AzJester/C-UASC2/pull/17).

## 2026-07-12

- Added high-DPI wall-display support and automatic workstation scaling.
- Published the application at the custom domain
  [cuas.insightfuldefense.com](https://cuas.insightfuldefense.com/).
- Merged in [PR #11](https://github.com/AzJester/C-UASC2/pull/11) and
  [PR #12](https://github.com/AzJester/C-UASC2/pull/12).

## 2026-07-11

- Rebuilt the demo as a fixed-site C-UAS command center with persistent command
  state, operator workspaces, authority gates, engagement queues, and regional
  and after-action views.
- Made satellite imagery the default basemap while retaining an offline tactical
  fallback.
- Added Link-16-style J-series event formatting, spoken radio callouts, alert
  tones, routine radio traffic, and bounded event notifications.
- Improved raid-scale rendering, track pagination, identity frames, and seeded
  browser regression tests.
- Merged in [PR #4](https://github.com/AzJester/C-UASC2/pull/4) through
  [PR #10](https://github.com/AzJester/C-UASC2/pull/10).

## 2026-07-10

- Published the standalone Web COP through GitHub Pages.
- Added seeded scenarios, real satellite geography, aircraft patrol variation,
  civilian air and maritime traffic, weather, day/night conditions, degraded
  communications, and no-fire zones.
- Added TEWA prioritization, imperfect sensor fusion, battle-damage assessment,
  terrain masks, engagement physics, cost exchange, replay, guided tours, and
  shareable scenario presets.
- Consolidated the browser demo into a single canonical source with generated
  standalone and service-hosted copies.

## 2026-06-26 to 2026-06-29

- Created the government-owned C-UAS C2 reference architecture, API contracts,
  pub/sub model, security boundaries, runnable service scaffold, and initial Web
  COP.
- Added Red/Blue tracking, layered effectors, joint aircraft and naval assets,
  hub-and-spoke versus networked comparisons, remote sensor tasking, fire-control
  demonstrations, cross-echelon integration, AutoPlay briefing, and the first
  After Action Review.
- Added responsive fit-to-screen behavior, label deconfliction, pause and speed
  controls, help, asset integrity, time-to-impact, and initial CI smoke tests.
