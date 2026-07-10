"""Behavioral tests for the web COP's simulation mechanics.

Covers the realism systems that plain smoke can't see: threat profiles,
modality-true sensing, no-fire zone feasibility, weather effects, the TEWA
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
    assert max(xs) <= -4200, f"boat lane crosses land: {max(xs):.0f}"
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
