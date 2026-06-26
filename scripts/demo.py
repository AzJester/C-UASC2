#!/usr/bin/env python3
"""Scripted any-sensor/any-shooter walkthrough against a running stack.

Run `make up` first, then `make demo` (or `python scripts/demo.py`). Exercises the
sequence in docs/01 §4 end to end: register materiel, watch tracks flow, raise track
quality with remote tasking, engage a *non-paired* effector under authority, and see
a friendly/under-quality engagement denied.

Requires httpx (in requirements-dev.txt). Talks only to the c2-core REST API.
"""
from __future__ import annotations

import sys
import time

try:
    import httpx
except ImportError:
    print("httpx not installed. Run: make venv  (then) .venv/bin/python scripts/demo.py")
    sys.exit(1)

BASE = "http://localhost:8000"
SENSOR_ID = "SEN-RAD-01"
EFFECTOR_ID = "EFF-EW-01"
HOSTILE = "TRK-1001"
FRIENDLY = "TRK-2001"


def banner(text: str) -> None:
    print("\n" + "=" * 72)
    print(f"  {text}")
    print("=" * 72)


def wait_for_track(client: httpx.Client, track_id: str, min_tq: int = 0, timeout: float = 20) -> dict:
    deadline = time.time() + timeout
    while time.time() < deadline:
        for t in client.get(f"{BASE}/cop").json():
            if t["trackId"] == track_id and t["trackQuality"] >= min_tq:
                return t
        time.sleep(0.5)
    raise TimeoutError(f"track {track_id} (TQ>={min_tq}) not seen within {timeout}s")


def main() -> int:
    with httpx.Client(timeout=10) as client:
        banner("0. Health")
        print(client.get(f"{BASE}/health").json())

        banner("1. Register materiel (NO sensor<->effector pairing)")
        client.post(
            f"{BASE}/sensors",
            json={"sensorId": SENSOR_ID, "sensorType": "RADAR", "vendor": "VendorA", "taskable": True},
        )
        client.post(
            f"{BASE}/effectors",
            json={
                "effectorId": EFFECTOR_ID,
                "effectorType": "EW_JAMMER",
                "vendor": "VendorB",
                "readiness": "READY",
                "magazine": {"remaining": 100, "capacity": 100, "unit": "seconds"},
                "engagementEnvelope": {
                    "location": {"lat": 34.20, "lon": -118.20, "altMeters": 0},
                    "maxRangeMeters": 8000,
                    "maxAltMeters": 1500,
                },
            },
        )
        print("registered sensor:", [s["sensorId"] for s in client.get(f"{BASE}/sensors").json()])
        print("registered effector:", [e["effectorId"] for e in client.get(f"{BASE}/effectors").json()])

        banner("2. Tracks flow over the bus -> COP")
        t = wait_for_track(client, HOSTILE)
        print(f"  {HOSTILE}: identity={t['identity']} TQ={t['trackQuality']} (below engagement threshold)")

        banner("3. Engagement attempt at low track quality -> DENIED")
        r = client.post(
            f"{BASE}/engagements",
            headers={"X-Operator-Role": "FIRE_CONTROL_AUTHORITY"},
            json={"trackId": HOSTILE, "effectorId": EFFECTOR_ID, "engagementType": "EW_DEFEAT", "humanConfirmation": True},
        )
        print(f"  HTTP {r.status_code}: {r.json()['state']} / {r.json()['reasonCode']} - {r.json()['detail']}")

        banner("4. Remote sensor tasking (DWELL) to raise track quality")
        r = client.post(
            f"{BASE}/sensors/{SENSOR_ID}/tasks",
            headers={"X-Operator-Role": "SENSOR_MANAGER"},
            json={"sensorId": SENSOR_ID, "taskType": "DWELL", "trackId": HOSTILE, "priority": 7, "requestedBy": "SM-1"},
        )
        print(f"  tasking: {r.json()}")
        t = wait_for_track(client, HOSTILE, min_tq=8)
        print(f"  {HOSTILE}: TQ now {t['trackQuality']} (engageable)")

        banner("5. Any-shooter engagement under authority -> AUTHORIZED -> COMPLETE")
        r = client.post(
            f"{BASE}/engagements",
            headers={"X-Operator-Role": "FIRE_CONTROL_AUTHORITY"},
            json={"trackId": HOSTILE, "effectorId": EFFECTOR_ID, "engagementType": "EW_DEFEAT", "humanConfirmation": True},
        )
        print(f"  HTTP {r.status_code}: {r.json()['state']} / {r.json()['reasonCode']} - {r.json()['detail']}")
        eng_id = r.json()["engagementId"]
        for _ in range(20):
            time.sleep(0.4)
            states = {e["engagementId"]: e["state"] for e in client.get(f"{BASE}/engagements").json()}
            if states.get(eng_id) == "COMPLETE":
                break
        print(f"  effector reported: {states.get(eng_id)}")

        banner("6. Positive control: engaging a FRIEND -> DENIED")
        wait_for_track(client, FRIENDLY)
        r = client.post(
            f"{BASE}/engagements",
            headers={"X-Operator-Role": "FIRE_CONTROL_AUTHORITY"},
            json={"trackId": FRIENDLY, "effectorId": EFFECTOR_ID, "engagementType": "EW_DEFEAT", "humanConfirmation": True},
        )
        print(f"  HTTP {r.status_code}: {r.json()['state']} / {r.json()['reasonCode']} - {r.json()['detail']}")

        banner("7. Positive control: wrong role -> DENIED")
        r = client.post(
            f"{BASE}/engagements",
            headers={"X-Operator-Role": "OBSERVER"},
            json={"trackId": HOSTILE, "effectorId": EFFECTOR_ID, "engagementType": "EW_DEFEAT", "humanConfirmation": True},
        )
        print(f"  HTTP {r.status_code}: {r.json()['state']} / {r.json()['reasonCode']} - {r.json()['detail']}")

        banner("8. Audit trail (non-repudiation)")
        for rec in client.get(f"{BASE}/audit").json():
            print(f"  {rec['action']:<22} {rec['decision']:<8} {rec['reasonCode']:<28} {rec.get('detail','')}")

    print("\nDemo complete.\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
