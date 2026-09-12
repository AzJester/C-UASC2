# Shared evidence and coordination exercises

The Exercise room is a desktop workspace for a fictional incident shared by named
participants. It preserves reports, corrections, review requests, acknowledgments,
handover, and the evidence available at each event. It does not mount c2-core's
sensor-tasking, engagement, or command endpoints.

The public GitHub Pages route `/exercise.html` is a clearly labeled read-only
preview. Shared rooms require the exercise service. Static hosting alone cannot
provide shared persistence, participant authentication, or a TAK connection.

## Start a local pilot

From the repository root, use Python 3.11 or later:

```powershell
python -m venv .venv
.venv\Scripts\python -m pip install -r services/exercise_service/requirements.txt
.venv\Scripts\python scripts/run_exercise.py
```

Open `http://127.0.0.1:8787/exercise.html`. Create a room with an exercise name and
your participant name. Invite a reviewer or observer using the Invite control.
Invitation links are single-use and have an expiry. A participant receives a
room-scoped bearer session; the service stores token hashes, not readable tokens.
Participant sessions live in browser session storage so two tabs can hold
different exercise identities. This is invitation-based exercise authentication,
not proof of a person's organizational identity or an accredited identity system.

The default database is `work/exercise.sqlite3`. Room records survive browser
refresh and service restart. Back up that database when the service is stopped;
do not publish it in the repository. Session expiry is independent of saved rooms.

For Linux or macOS, the corresponding executable is `.venv/bin/python`.
The service uses the existing local hardware and has no mandatory hosted-service
subscription. Setup, maintenance, backups, and any optional cloud hosting remain
the deployment owner's responsibility.

## Desktop exercise walkthrough

1. Create a room and invite a reviewer in a second tab or browser.
2. Start the clock. Advance the fictional case checkpoint or add a training report.
3. Inspect the report's source, observed time, receipt time, original text, and
   optional text attachment. Report marker coordinates provide map context only.
4. Request a review from the named reviewer. The reviewer acknowledges it and
   records a reason when resolving it.
5. Correct a report with a reason. The original revision remains available;
   concurrent edits with an old version are rejected instead of overwriting work.
6. Pause or disconnect a participant. Reconnect and reconcile to the saved room.
7. Offer a controller handover. The recipient accepts before control transfers.
8. Review a historical event snapshot, return to the current incident, and export
   the final record. Ended rooms preserve unresolved requests and become read-only.

The exercise clock measures elapsed room time. The scripted checkpoint's narrative
time is separate, so advancing a five-minute case does not invent five minutes of
actual participant activity. The event history records human actions and script
injections distinctly. Historical mode disables mutation controls.

## TAK connection

WinTAK is a separate Windows client. The exercise service has an optional adapter
for generic training report markers. It uses a configured TAK Server connection
with server certificate validation and a client certificate. Destinations and
private keys are set on the service, not accepted from browser requests.

Set `EXERCISE_TAK_CONFIG` to a private JSON file or pass `--tak-config` to the
launcher. That file contains `host`, `port`, `caFile`, `certFile`, and `keyFile`.
Keep downloaded packages and generated credentials outside the checkout, or in
the ignored `work/` directory. Never commit a connection package, database, or
participant invitation.

For a supplied TAK connection package, inspect its configuration metadata first:

```powershell
.venv\Scripts\python scripts/configure_exercise_tak.py --package C:\private\connection.zip --inspect
.venv\Scripts\python scripts/configure_exercise_tak.py --package C:\private\connection.zip --output C:\private\exercise-tak
.venv\Scripts\python scripts/run_exercise.py --tak-config C:\private\exercise-tak\config.json
```

The setup tool requests protected certificate passwords without echoing them.
It creates a new private output directory and validates the certificate material
locally before any network connection. A software installer alone is not a server
connection package. Run `--help` for separate PKCS12 or PEM files.

The XML review export contains a standard CoT event for one located report or a
clearly labeled review bundle for multiple reports. Direct WinTAK import of the
multi-report wrapper is unverified. The transport sends individual CoT events.

Live testing with TAK Server 5.8-RELEASE-79 confirmed forwarding to an independent
certificate-authenticated receiver. To support that server, provenance uses an
unqualified `exerciseReport` detail element with a `schema` attribute. The server
rewrote the previous namespaced extension into malformed forwarded XML. The
adapter also allows a bounded interval for
the server's initial subscription setup and TLS shutdown. That protocol traffic
is not a report acknowledgment, and this check does not establish WinTAK display.

TAK transmission is manual. Each marker carries explicit EXERCISE/TRAINING labels
and an exercise-specific ID. The adapter does not transmit simulation target
tracks, sensor feeds, or command/release messages. The interface distinguishes a
successful TLS write from a verified WinTAK receipt. A complete interoperability
test must inspect the actual authorized client and confirm the intended marker,
source information, timestamp, and stale behavior.

Source references:

- [TAK product descriptions](https://tak.gov/products.com)
- [TAK Server source](https://github.com/TAK-Product-Center/Server)
- [TAK distribution and access policy](https://tak.gov/pages/our-process)

## Hosting beyond the local computer

The launcher binds to loopback by default. To allow other computers, configure a
long random `EXERCISE_SETUP_TOKEN` (or `--setup-key-file`) and a controlled network
deployment. Use HTTPS and a reverse proxy that preserves the correct origin,
and set `EXERCISE_ALLOWED_HOSTS` to the deployment's comma-separated hostnames.
Protect the SQLite data and certificate files, and restrict who receives invites.
Use a suitable database and identity provider before expanding beyond a small
exercise pilot. Do not directly expose the reference c2-core service as the
shared-room backend.

The service checks room membership, role, request transitions, origin, and revision
numbers server-side. This is a tested pilot implementation, not an operational
certification, security accreditation, or validation of sensing/weapon models.
