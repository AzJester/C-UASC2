"""Behavioral tests for the web COP's simulation mechanics.

Covers the realism systems that plain smoke can't see: threat profiles,
modality-aware notional sensing, no-fire zone feasibility, weather effects, the TEWA
queue, BDA, terrain masking, civilians, presets, and the replay recorder.
Driven through the ``?debug=1`` hook (window.__CUAS__); no network needed
(``basemap=tac`` keeps the page fully offline).

Skips automatically when Playwright or a browser is unavailable, same as
tests/test_cop_smoke.py.
"""
from __future__ import annotations

import os
from pathlib import Path

import pytest

sync_api = pytest.importorskip("playwright.sync_api")

COP = Path(__file__).resolve().parents[1] / "services" / "c2-core" / "app" / "static" / "cop.html"
URL = f"file://{COP}?debug=1&basemap=tac&seed=42&wx=CLEAR&tod=DAY"


@pytest.fixture(scope="module")
def page():
    from playwright.sync_api import sync_playwright

    exe = os.environ.get("COP_SMOKE_CHROMIUM") or None
    with sync_playwright() as pw:
        try:
            browser = pw.chromium.launch(executable_path=exe)
        except Exception as exc:  # noqa: BLE001 - no browser binary in this env
            pytest.skip(f"Chromium not available for Playwright: {exc}")
        context = browser.new_context(viewport={"width": 1680, "height": 1020})
        pg = context.new_page()
        errors: list[str] = []
        pg.on("console", lambda m: errors.append(m.text) if m.type == "error" else None)
        pg.on("pageerror", lambda e: errors.append(f"pageerror {e}"))
        pg.console_errors = errors  # type: ignore[attr-defined]
        pg.goto(URL)
        pg.wait_for_timeout(800)
        yield pg
        browser.close()


def test_threat_profiles(page):
    profs = page.evaluate(
        """() => {
      const C = window.__CUAS__;
      return {
        low: C.spawnThreat(0.45, {r: 5000}),
        cruise: C.spawnThreat(0.65, {r: 5000}),
        isr: C.spawnThreat(0.8, {}),
        decoy: C.spawnThreat(0.95, {r: 5000}),
      };
    }"""
    )
    assert profs["low"]["profile"] == "lowIngress" and profs["low"]["altMeters"] < 60
    assert profs["cruise"]["profile"] == "cruise" and len(profs["cruise"]["wps"]) == 2
    assert profs["cruise"]["speed"] >= 50, "cruise must fly fixed-wing speeds"
    assert profs["isr"]["identity"] == "SUSPECT" and profs["isr"]["orbR"] > 3000
    assert profs["decoy"]["decoy"] is True


def test_silent_tracks_invisible_to_rf(page):
    rf = page.evaluate(
        """() => {
      const C = window.__CUAS__;
      const t = C.spawnHostile({r: 4500, alt: 300, emitting: false});
      return new Promise(res => setTimeout(() => {
        const rf = t.contributingSensors.map(id =>
          [...(window.__CUAS__.S ? [] : []), id]).length &&
          t.contributingSensors.filter(id => id.includes("RF") || id.includes("MADIS")).length;
        res(rf || 0);
      }, 600));
    }"""
    )
    assert rf == 0, "RF sensors must not track a comms-silent contact"


def test_no_fire_zone_forces_soft_kill(page):
    out = page.evaluate(
        """() => {
      const C = window.__CUAS__;
      const t = C.spawnHostile({r: 1});
      t.x = 4870; t.y = 1890; t.tq = 15;   // over SAN DIEGO CITY
      const feas = C.feasibleEffectors(t).map(e => e.effectorType);
      C.S.tracks.delete(t.trackId);
      return {zone: (C.noFireZoneAt(4870, 1890) || {}).label, feas};
    }"""
    )
    assert out["zone"] == "SAN DIEGO CITY"
    assert out["feas"], "soft-kill must remain feasible over the zone"
    assert not [f for f in out["feas"] if f in ("KINETIC_GUN", "KINETIC_INTERCEPTOR", "DIRECTED_ENERGY")]


def test_terrain_mask_hides_low_target_beyond_ridge(page):
    out = page.evaluate(
        """() => {
      const C = window.__CUAS__;
      const s = {x: 200, y: -150, mask: [{from: 2.85, to: 3.45, minAlt: 250, beyond: 2600}]};
      const low = {x: -4300, y: -150, altMeters: 60};    // due west, low, beyond ridge
      const high = {x: -4300, y: -150, altMeters: 1200}; // same spot, high
      const near = {x: -1300, y: -150, altMeters: 60};   // low but inside the ridge
      return [C.sensorMasked(s, low), C.sensorMasked(s, high), C.sensorMasked(s, near)];
    }"""
    )
    assert out == [True, False, False]


def test_wind_slows_small_uas(page):
    mx = page.evaluate(
        """() => {
      const C = window.__CUAS__;
      C.S.wx = 'WIND';
      const v = [];
      for (let i = 0; i < 8; i++) v.push(C.spawnHostile({cls: 'MULTIROTOR', r: 5900}).speed);
      C.S.wx = 'CLEAR';
      return Math.max(...v);
    }"""
    )
    assert mx <= 25 * 0.75 + 0.01


def test_sensor_task_waits_for_observation(page):
    page.evaluate("window.__CUAS__.resetScenario()")
    out = page.evaluate(
        """() => {
      const C = window.__CUAS__;
      const t = C.spawnHostile({r: 1800, tq: 4, alt: 200});
      C.selectTrack(t.trackId);
      const before = t.tq;
      C.taskSensor();
      return {before, after: t.tq, contributors: t.contributingSensors.length,
              pending: t.taskObservationPending === true};
    }"""
    )
    assert out["after"] == out["before"], "task acceptance must not mutate TQ immediately"
    assert out["pending"], "task must remain pending until a sensing tick returns an observation"


def test_identity_timer_only_prompts_review(page):
    page.evaluate("window.__CUAS__.resetScenario()")
    identity = page.evaluate(
        """() => {
      const C = window.__CUAS__;
      const t = C.spawnHostile({identity:'SUSPECT', r:9000, tq:3, speed:1, emitting:false});
      t.declareAt = 1;
      return new Promise(res => setTimeout(() => res(t.identity), 250));
    }"""
    )
    assert identity == "SUSPECT", "elapsed time alone must never declare HOSTILE"


def test_sim_pause_freezes_engagement_lifecycle(page):
    page.evaluate("window.__CUAS__.resetScenario()")
    state = page.evaluate(
        """() => {
      const C = window.__CUAS__;
      const t = C.spawnHostile({r:900, tq:15, alt:250});
      C.engageTrack(t, false);
      C.setPaused(true);
      return new Promise(res => setTimeout(() => res({id:t.trackId, state:t.state}), 2200));
    }"""
    )
    assert state["state"] == "ENGAGING", "outcome/BDA advanced while mission time was paused"
    page.evaluate("window.__CUAS__.setPaused(false)")


def test_delegated_self_defense_defaults_off(page):
    page.evaluate("window.__CUAS__.resetScenario()")
    out = page.evaluate(
        """() => {
      const C = window.__CUAS__;
      C.spawnHostile({r:700, tq:15, alt:200});
      return new Promise(res => setTimeout(() => res({armed:C.S.delegatedDefense.armed,
        engagements:C.S.stats.engagements}), 700));
    }"""
    )
    assert out == {"armed": False, "engagements": 0}, "hidden autonomous fires occurred"


def test_weapons_free_requires_two_step_confirmation(page):
    page.evaluate("window.__CUAS__.resetScenario(); window.__CUAS__.setWorkspace('FIRES')")
    free = page.locator("#wcsSeg button[data-v='WEAPONS_FREE']")
    free.click()
    assert page.evaluate("window.__CUAS__.S.wcs") == "WEAPONS_TIGHT"
    assert free.text_content() == "CONFIRM FREE"
    free.click()
    assert page.evaluate("window.__CUAS__.S.wcs") == "WEAPONS_FREE"
    assert page.evaluate("window.__CUAS__.S.autoReleaseArmed") is True
    page.locator("#wcsSeg button[data-v='WEAPONS_TIGHT']").click()


def test_dense_track_list_is_keyboard_pageable(page):
    page.evaluate(
        """() => {
      const C = window.__CUAS__; C.resetScenario(); C.setWorkspace('COP');
      for (let i=0; i<26; i++) C.spawnHostile({r:3000+i*20, tq:5});
    }"""
    )
    page.wait_for_timeout(850)
    assert page.locator("#trackList button[data-track-select]").count() == 18
    assert page.locator("#trackList button[data-page-delta='1']").is_enabled()
    page.locator("#trackList button[data-page-delta='1']").click()
    assert "page 2/" in page.locator("#trackList .track-pager span").text_content().lower()


def test_tewa_queue_ranks_and_approves(page):
    page.evaluate("window.__CUAS__.resetScenario()")
    out = page.evaluate(
        """() => {
      const C = window.__CUAS__;
      const t = C.spawnHostile({r: 2000, tq: 15, alt: 300});
      const q = C.threatQueue();
      const row = q.find(r => r.t.trackId === t.trackId);
      const ok = row && row.auth.ok;
      if (ok) C.engageTrack(t, false);
      return {queued: !!row, ok, state: t.state};
    }"""
    )
    assert out["queued"], "new hostile must appear in the TEWA queue"
    assert out["ok"] and out["state"] == "ENGAGING"


def test_bda_assesses_before_confirming(page):
    page.evaluate("window.__CUAS__.resetScenario()")
    page.evaluate(
        """() => {
      const C = window.__CUAS__;
      const t = C.spawnHostile({r: 1500, tq: 15, alt: 300});
      C.engageTrack(t, false);
    }"""
    )
    page.wait_for_timeout(9000)
    log = page.evaluate("document.getElementById('log').textContent")
    assert "BDA" in log, "engagements must pass through battle damage assessment"


def test_civilians_present_and_protected(page):
    out = page.evaluate(
        """() => {
      const C = window.__CUAS__;
      const air = C.spawnCivAir();
      const boat = C.spawnCivBoat();
      const a = C.authorize(air);
      return {air: air.identity, boat: boat.platform, civ: air.civil && boat.civil,
              denied: !a.ok && a.code === "ROE_PROHIBITED", surface: boat.surface === true};
    }"""
    )
    assert out["air"] == "NEUTRAL" and out["civ"] and out["surface"]
    # boats must spawn in the open Pacific, clear of the Point Loma peninsula
    xs = page.evaluate(
        "Array.from({length: 12}, () => window.__CUAS__.spawnCivBoat().x)"
    )
    assert max(xs) <= -4650, f"boat lane too close to shore: {max(xs):.0f}"
    # and the lane-keeping clamp must pull a drifted boat back over water
    drifted = page.evaluate(
        """() => {
      const C = window.__CUAS__;
      const b = C.spawnCivBoat();
      b.x = b.laneX + 5000;   // force it ashore
      return new Promise(res => setTimeout(() => {
        res({x: b.x, laneX: b.laneX});
      }, 400));
    }"""
    )
    assert drifted["x"] <= drifted["laneX"] + 301, f"lane keeping failed: {drifted}"
    assert out["denied"], "civilian traffic must never be engageable"


def test_clutter_false_track_classified_out_by_eo(page):
    out = page.evaluate(
        """() => {
      const C = window.__CUAS__;
      const t = C.spawnClutter();
      t.x = 1600; t.y = 700;   // near land EO/IR coverage
      const id = t.trackId;
      return new Promise(res => setTimeout(() => res({gone: !C.S.tracks.has(id)}), 1500));
    }"""
    )
    assert out["gone"], "an EO/IR look must classify clutter out of the picture"


def test_replay_recorder_and_scrubber(page):
    page.wait_for_timeout(1500)
    n = page.evaluate("(window.__CUAS__.S.replay || []).length")
    assert n >= 2, "replay recorder must be capturing snapshots"
    page.evaluate("window.__CUAS__.showAAR()")
    assert page.evaluate("!document.getElementById('aarBack').hidden")
    assert page.evaluate("+document.getElementById('repSlider').max >= 1")
    aar_text = page.locator("#aarBack").text_content()
    assert "Observed defeats / engagement" in aar_text
    assert "Pending effect / BDA" in aar_text
    assert "Hit rate (Pk)" not in aar_text
    page.evaluate("document.getElementById('aarClose').click()")


def test_cost_exchange_accrues(page):
    page.evaluate("window.__CUAS__.resetScenario()")
    page.evaluate(
        """() => {
      const C = window.__CUAS__;
      const t = C.spawnHostile({r: 1500, tq: 15, alt: 300});
      C.engageTrack(t, false);
    }"""
    )
    page.wait_for_timeout(500)
    spent = page.evaluate("window.__CUAS__.S.stats.costSpent")
    assert spent > 0
    sb = page.evaluate("document.getElementById('scoreboard').textContent")
    assert "$" in sb


def test_live_engagement_reconcile_reuses_request_key(page):
    out = page.evaluate(
        """async () => {
      const C = window.__CUAS__;
      C.S.live = false; C.resetScenario();
      const t = C.spawnHostile({r: 900, tq: 15, alt: 250});
      C.S.live = true;
      C.S.effectors.forEach(e => { e.readiness = "READY"; e.geometryUnknown = false; });
      const originalFetch = window.fetch;
      const keys = [];
      let call = 0;
      window.fetch = async (_url, options) => {
        keys.push(options.headers["Idempotency-Key"]);
        call += 1;
        if (call === 1) throw new Error("response lost after send");
        return {
          ok: false,
          status: 503,
          json: async () => ({
            engagementId: "ENG-DELIVERY-UNKNOWN",
            trackId: t.trackId,
            effectorId: t.engagementRequest.effectorId,
            state: "AUTHORIZED",
            terminal: false,
            detail: "transport delivery unknown",
          }),
        };
      };
      try {
        await C.liveEngage(t);
        const afterLoss = {state: t.state, key: t.engagementRequest.requestKey};
        await C.liveEngage(t);
        return {
          keys,
          afterLoss,
          state: t.state,
          lifecycle: t.engagementState,
          pending: C.S.pendingEng.has("ENG-DELIVERY-UNKNOWN"),
        };
      } finally {
        window.fetch = originalFetch;
        C.S.live = false;
        C.resetScenario();
      }
    }"""
    )
    assert out["afterLoss"]["state"] == "AUTHORIZING"
    assert out["keys"][0] == out["keys"][1] == out["afterLoss"]["key"]
    assert out["pending"] is True and out["state"] == "ENGAGING"
    assert "DELIVERY UNKNOWN" in out["lifecycle"]


def test_live_abort_delivery_unknown_reuses_request_key(page):
    out = page.evaluate(
        """async () => {
      const C = window.__CUAS__;
      C.S.live = false; C.resetScenario();
      const t = C.spawnHostile({r: 900, tq: 15, alt: 250});
      t.state = "ENGAGING";
      C.S.live = true;
      C.S.pendingEng.set("ENG-ABORT-UNKNOWN", {
        trackId: t.trackId,
        effectorId: "EFF-TEST",
        firstSeen: t.firstSeen,
        logicalKey: "logical-engage-key",
      });
      const originalFetch = window.fetch;
      const keys = [];
      window.fetch = async (_url, options) => {
        keys.push(options.headers["Idempotency-Key"]);
        return {
          ok: false,
          status: 503,
          json: async () => ({
            engagementId: "ENG-ABORT-UNKNOWN",
            accepted: false,
            deliveryState: "DELIVERY_UNKNOWN",
            lifecycleState: "AUTHORIZED",
            detail: "abort delivery unknown",
          }),
        };
      };
      try {
        await C.liveAbortEngagement(t);
        await C.liveAbortEngagement(t);
        return {
          keys,
          pending: t.abortRequestPending,
          lifecycle: t.engagementState,
          retained: C.S.requestKeys.abort.get("ENG-ABORT-UNKNOWN"),
          log: C.S.events.map(event => String(event.msg)).join(" | "),
        };
      } finally {
        window.fetch = originalFetch;
        C.S.live = false;
        C.resetScenario();
      }
    }"""
    )
    assert out["keys"][0] == out["keys"][1] == out["retained"]
    assert out["pending"] is True
    assert "DELIVERY UNKNOWN" in out["lifecycle"]
    assert "not delivered" not in out["log"].lower()


def test_live_terminal_states_clear_pending_and_pending_bda(page):
    out = page.evaluate(
        """() => {
      const C = window.__CUAS__;
      C.S.live = false; C.resetScenario();
      C.S.live = true;

      const failed = C.spawnHostile({r: 1000, tq: 15});
      failed.state = "ENGAGING";
      C.S.requestKeys.engage.set("failed-logical", "failed-request");
      C.S.pendingEng.set("ENG-FAILED", {
        trackId: failed.trackId, effectorId: "EFF-A", firstSeen: failed.firstSeen,
        logicalKey: "failed-logical", requestKey: "failed-request",
      });
      C.processLiveEngagementStatus({
        engagementId: "ENG-FAILED", state: "FAILED", terminal: true,
        detail: "definitive non-delivery",
      });

      const bda = C.spawnHostile({r: 1100, tq: 15});
      bda.state = "ASSESSING";
      C.S.requestKeys.engage.set("bda-logical", "bda-request");
      C.S.pendingEng.set("ENG-BDA", {
        trackId: bda.trackId, effectorId: "EFF-B", firstSeen: bda.firstSeen,
        logicalKey: "bda-logical", requestKey: "bda-request",
      });
      const defeatedBefore = C.S.stats.defeated;
      const unconfirmedBefore = C.S.stats.unconfirmedEffects || 0;
      C.processLiveEngagementStatus({
        engagementId: "ENG-BDA", state: "COMPLETE", terminal: true,
        effectAssessment: {outcome: "PENDING"},
      });
      const result = {
        failedState: failed.state,
        failedPending: C.S.pendingEng.has("ENG-FAILED"),
        failedKey: C.S.requestKeys.engage.has("failed-logical"),
        bdaState: bda.state,
        bdaLifecycle: bda.engagementState,
        bdaPending: C.S.pendingEng.has("ENG-BDA"),
        bdaKey: C.S.requestKeys.engage.has("bda-logical"),
        defeatsAdded: C.S.stats.defeated - defeatedBefore,
        unconfirmedAdded: (C.S.stats.unconfirmedEffects || 0) - unconfirmedBefore,
      };
      C.S.live = false; C.resetScenario();
      return result;
    }"""
    )
    assert out["failedState"] == "ACTIVE"
    assert out["failedPending"] is False and out["failedKey"] is False
    assert out["bdaState"] == "ACTIVE" and out["bdaLifecycle"] == "INDETERMINATE"
    assert out["bdaPending"] is False and out["bdaKey"] is False
    assert out["defeatsAdded"] == 0 and out["unconfirmedAdded"] == 1


def test_node_failover_inhibits_release_until_reconciliation_completes(page):
    initial = page.evaluate(
        """() => {
      const C = window.__CUAS__;
      C.S.live = false; C.resetScenario();
      const t = C.spawnHostile({r: 1000, tq: 15, identity: "HOSTILE"});
      t.identity = "HOSTILE"; t.tq = 15;
      C.toggleNode(true);
      const auth = C.authorize(t);
      return {
        trackId: t.trackId,
        node: C.S.node,
        handover: C.S.handoverState,
        inhibited: C.S.releasesInhibited,
        code: auth.code,
      };
    }"""
    )
    assert initial == {
        "trackId": initial["trackId"],
        "node": "NO AUTHORITY",
        "handover": "LOSS_DETECTED",
        "inhibited": True,
        "code": "C2_HANDOVER_IN_PROGRESS",
    }

    page.wait_for_timeout(2300)
    final = page.evaluate(
        """trackId => {
      const C = window.__CUAS__, t = C.S.tracks.get(trackId);
      const auth = C.authorize(t);
      const out = {
        node: C.S.node,
        handover: C.S.handoverState,
        inhibited: C.S.releasesInhibited,
        authorized: auth.ok,
      };
      C.toggleNode(false);
      return out;
    }""",
        initial["trackId"],
    )
    assert final["node"] == "C2-NODE-02"
    assert final["handover"] == "AUTHORITY_TRANSFERRED"
    assert final["inhibited"] is False
    assert final["authorized"] is True


def test_presets_and_no_errors(page):
    pg2 = page.context.new_page()
    errors: list[str] = []
    pg2.on("console", lambda m: errors.append(m.text) if m.type == "error" else None)
    pg2.on("pageerror", lambda e: errors.append(f"pageerror {e}"))
    pg2.goto(f"file://{COP}?debug=1&basemap=tac&seed=5&scn=elpaso&arch=HUB&wx=RAIN&tod=NIGHT")
    pg2.wait_for_timeout(1200)
    st = pg2.evaluate("({scn: window.__CUAS__.S.scenario, arch: window.__CUAS__.S.arch, wx: window.__CUAS__.S.wx, tod: window.__CUAS__.S.tod})")
    assert st == {"scn": "elpaso", "arch": "HUB", "wx": "RAIN", "tod": "NIGHT"}
    chip = pg2.evaluate("document.getElementById('envChip').textContent")
    assert "NIGHT" in chip and "RAIN" in chip
    assert not errors, f"console errors: {errors}"
    pg2.close()
    base_errors = getattr(page, "console_errors", [])
    assert not base_errors, f"console errors: {base_errors}"
