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
    warning = free.evaluate(
        "el => ({color:getComputedStyle(el).color, background:getComputedStyle(el).backgroundColor, animation:getComputedStyle(el).animationName})"
    )
    assert warning["background"] == "rgb(58, 17, 22)"
    assert warning["animation"] == "free-confirm-pulse"
    red, green, blue = (int(value) for value in warning["color"].removeprefix("rgb(").removesuffix(")").split(", "))
    assert red > green and red > blue
    free.click()
    assert page.evaluate("window.__CUAS__.S.wcs") == "WEAPONS_FREE"
    assert page.evaluate("window.__CUAS__.S.autoReleaseArmed") is True
    page.locator("#wcsSeg button[data-v='WEAPONS_TIGHT']").click()


def test_confirmed_weapons_free_activates_air_and_naval_defenses(page):
    out = page.evaluate(
        """() => {
          const C=window.__CUAS__; C.applyScenario('sandiego'); C.setPaused(true);
          C.S.role='FIRE_CONTROL_AUTHORITY'; C.S.wcs='WEAPONS_FREE'; C.S.autoReleaseArmed=true;
          C.S.delegatedDefense.armed=false;
          const aircraft=[...C.S.tracks.values()].find(a => a.armed && a.classificationType !== 'SURFACE'
            && !C.noFireZoneAt(a.x+80,a.y));
          const airTarget=C.spawnHostile({r:3000,tq:15,identity:'HOSTILE',cls:'UAS_GROUP_1'});
          airTarget.x=aircraft.x+80; airTarget.y=aircraft.y; airTarget.heading=Math.atan2(-airTarget.y,-airTarget.x);
          C.airDefense(0.1);

          const shipEffector=C.S.effectors.find(e => e.auto && !C.noFireZoneAt(e.x+80,e.y));
          const navalTarget=C.spawnHostile({r:3000,tq:15,identity:'HOSTILE',cls:'UAS_GROUP_1'});
          navalTarget.x=shipEffector.x+80; navalTarget.y=shipEffector.y; navalTarget.heading=Math.atan2(-navalTarget.y,-navalTarget.x);
          C.navalDefense(0.1);
          const messages=C.S.events.map(e => e.msg);
          const result={active:C.weaponsFreeActive(),delegated:C.S.delegatedDefense.armed,
            air:airTarget.engAir === true,naval:navalTarget.engNaval === true,
            airLog:messages.some(m => m.includes('WEAPONS FREE') && m.includes('AIR INTERCEPT')),
            navalLog:messages.some(m => m.includes('WEAPONS FREE') && (m.includes('CIWS') || m.includes('DE LASE')))};
          C.setPaused(false); return result;
        }"""
    )
    assert out == {
        "active": True,
        "delegated": False,
        "air": True,
        "naval": True,
        "airLog": True,
        "navalLog": True,
    }


def test_autobrief_offers_requested_missions_durations_and_fires_automatically(page):
    choices = page.evaluate(
        """() => ({
          missions:[...document.querySelectorAll('#autoMissionSel option')].map(o => o.value),
          durations:[...document.querySelectorAll('#autoDurationSel option')].map(o => Number(o.value))
        })"""
    )
    assert choices == {
        "missions": ["joint", "airport", "resilience"],
        "durations": [2, 5, 10, 15],
    }

    try:
        page.evaluate(
            """() => {
              const C=window.__CUAS__;
              document.getElementById('autoMissionSel').value='joint';
              document.getElementById('autoDurationSel').value='2';
              C.S.autoplay.delayScale=0.03; C.S.timeScale=35;
              void C.autoplay();
            }"""
        )
        page.wait_for_function(
            """window.__CUAS__.S.stats.engagements > 0 &&
               window.__CUAS__.S.events.some(e => e.msg.includes('AUTO-BRIEF') &&
                 e.msg.includes('WEAPONS FREE CONFIRMED'))""",
            timeout=8000,
        )
        out = page.evaluate(
            """() => {
              const C=window.__CUAS__, selected=C.autoplaySelection();
              return {running:C.S.autoplay.running, minutes:C.S.autoplay.durationMinutes,
                selected, engagements:C.S.stats.engagements,
                warning:C.S.events.some(e => e.msg.includes('AUTO-BRIEF') && e.msg.includes('WEAPONS FREE CONFIRMED')),
                automatic:C.S.events.some(e => e.msg.includes('automatic detect / task / fire'))};
            }"""
        )
        assert out["running"] is True and out["minutes"] == 2
        assert out["selected"]["durationMs"] == 120000
        assert out["engagements"] > 0
        assert out["warning"] and out["automatic"]
    finally:
        page.evaluate(
            """() => {
              const C=window.__CUAS__; C.stopAutoplay();
              C.S.autoplay.delayScale=1; C.S.timeScale=1;
            }"""
        )
    assert page.evaluate("window.__CUAS__.S.wcs") == "WEAPONS_TIGHT"


def test_dense_track_list_is_keyboard_pageable(page):
    page.evaluate(
        """() => {
      const C = window.__CUAS__; C.resetScenario(); C.setWorkspace('COP');
      for (let i=0; i<26; i++) C.spawnHostile({r:3000+i*20, tq:5});
    }"""
    )
    page.wait_for_timeout(850)
    assert page.locator("#trackList button[data-track-select]").count() == 6
    assert page.locator("#trackList button[data-page-delta='1']").is_enabled()
    page.locator("#trackList button[data-page-delta='1']").click()
    assert "page 2/" in page.locator("#trackList .track-pager span").text_content().lower()


def test_norfolk_scenario_is_selectable_and_complete(page):
    out = page.evaluate(
        """() => {
      const C = window.__CUAS__;
      C.applyScenario('norfolk');
      const scn = C.currentScenario();
      const tracks = [...C.S.tracks.values()];
      return {id:C.S.scenario, asset:scn.asset.name, sensors:scn.sensors.length,
        effectors:scn.effectors.length, water:scn.waterLabel,
        backhaul:scn.networkBackhaul, naval:tracks.filter(t => t.identity === 'FRIEND' && t.classificationType === 'SURFACE').length,
        commercial:tracks.filter(t => t.identity === 'NEUTRAL' && t.classificationType === 'SURFACE').length};
    }"""
    )
    assert out["id"] == "norfolk"
    assert "NORFOLK" in out["asset"]
    assert out["sensors"] >= 6 and out["effectors"] >= 8
    assert "CHESAPEAKE" in out["water"]
    assert "FIBER" in out["backhaul"]["medium"]
    assert out["backhaul"]["x"] > -2200, "Norfolk 5G endpoint must remain landward of the configured coast"
    assert out["backhaul"]["y"] < 0, "Norfolk 5G endpoint must be positioned inland, south of Sewells Point"
    assert "LAND SITE" in out["backhaul"]["description"]
    assert "NO OFFSHORE TOWER" in out["backhaul"]["description"]
    assert out["naval"] >= 4 and out["commercial"] >= 4


def test_washington_ncr_has_layered_multi_site_protection(page):
    out = page.evaluate(
        """() => {
          const C = window.__CUAS__; C.applyScenario('washington');
          const scn = C.currentScenario();
          const near = (site, item) => Math.hypot(item.x-site.x, item.y-site.y) <= 1400;
          const coverage = scn.protectedSites.map(site => ({
            name:site.name,
            sensors:scn.sensors.filter(sensor => near(site, sensor)).length,
            effectors:scn.effectors.filter(effector => near(site, effector)).length,
          }));
          return {id:C.S.scenario, asset:scn.asset.name, terrain:scn.terrain,
            preferred:scn.preferredBasemap, dense:scn.denseLaydown, aoRadius:scn.aoRadius,
            airport:scn.civilAirport.code, trafficCount:scn.civilAirport.trafficCount,
            protectedSites:scn.protectedSites.map(site => site.name), coverage,
            sensors:scn.sensors.length, effectors:scn.effectors.length,
            jba:{s:scn.sensors.filter(s => s.sensorId.includes('-JBA-')).length,
              e:scn.effectors.filter(e => e.effectorId.includes('-JBA-')).length},
            belvoir:{s:scn.sensors.filter(s => s.sensorId.includes('-BEL-')).length,
              e:scn.effectors.filter(e => e.effectorId.includes('-BEL-')).length},
            types:[...new Set(scn.effectors.map(e => e.effectorType))],
            axes:scn.threat.axes.length,
            uniqueAxes:new Set(scn.threat.axes.map(a => Math.round((((a % (2*Math.PI)) + 2*Math.PI) % (2*Math.PI))*1000))).size,
            targets:scn.threatTargets.map(t => t.name), ringFromCenter:scn.threatRingFromCenter,
            noFire:scn.noFire.length, backhaul:scn.networkBackhaul,
            armedF16:[...C.S.tracks.values()].filter(t => t.armed && t.platform === 'F-16C').length,
            civilAir:[...C.S.tracks.values()].filter(t => t.civil && !t.surface).map(t => ({arrival:t.civilArrival, plan:t.flightPlan})),
          };
        }"""
    )
    assert out["id"] == "washington" and "NATIONAL CAPITAL REGION" in out["asset"]
    assert out["terrain"] == "inland" and out["preferred"] == "SAT" and out["dense"] is True
    assert out["aoRadius"] >= 25000 and out["airport"] == "KDCA"
    assert out["trafficCount"] >= 4
    assert out["sensors"] >= 22 and out["effectors"] >= 30
    for name in (
        "WHITE HOUSE", "U.S. CAPITOL", "PENTAGON", "JOINT BASE MYER-HENDERSON HALL",
        "FORT McNAIR", "JOINT BASE ANDREWS", "FORT BELVOIR", "MARK CENTER",
    ):
        assert any(name in site for site in out["protectedSites"])
    assert all(site["sensors"] >= 1 and site["effectors"] >= 1 for site in out["coverage"])
    assert out["jba"]["s"] >= 4 and out["jba"]["e"] >= 6
    assert out["belvoir"]["s"] >= 4 and out["belvoir"]["e"] >= 6
    assert {"EW_JAMMER", "RF_TAKEOVER", "DIRECTED_ENERGY", "KINETIC_GUN", "KINETIC_INTERCEPTOR", "NET_CAPTURE"}.issubset(set(out["types"]))
    assert out["axes"] >= 8 and out["uniqueAxes"] == 8 and out["ringFromCenter"] is True
    assert len(out["targets"]) >= 9 and out["noFire"] >= 3
    assert "ON-LAND" in out["backhaul"]["description"] and "NO OFFSHORE" in out["backhaul"]["description"]
    assert out["armedF16"] >= 3
    assert len(out["civilAir"]) >= 4
    assert any(track["arrival"] is True and "KDCA" in track["plan"] for track in out["civilAir"])
    assert any(track["arrival"] is False and "KDCA" in track["plan"] for track in out["civilAir"])


def test_washington_airports_coast_and_every_effector_share_the_data_mesh(page):
    out = page.evaluate(
        """() => {
          const C = window.__CUAS__; C.applyScenario('washington'); C.setPaused(true);
          const scn = C.currentScenario();
          const airports = scn.regionalAirports.map(airport => {
            const sensors=scn.sensors.filter(s => airport.sensorIds.includes(s.sensorId));
            const effectors=scn.effectors.filter(e => airport.effectorIds.includes(e.effectorId));
            return {code:airport.code, sensors:sensors.length, effectors:effectors.length,
              distances:[...sensors,...effectors].map(item => Math.hypot(item.x-airport.x,item.y-airport.y)),
              sensorDx:sensors.map(item => item.x-airport.x), effectorDy:effectors.map(item => item.y-airport.y),
              perimeter:[...sensors,...effectors].map(item => item.airportPerimeter)};
          });
          const paths = scn.effectors.map(e => C.systemDataPath(e));
          const coastSensor = scn.sensors.find(s => s.coastalInset);
          const airportEffector = scn.effectors.find(e => e.airportInset);
          const coastPoint = C.systemScreenPoints('sensor', coastSensor);
          const airportPoint = C.systemScreenPoints('effector', airportEffector);
          const canvas=document.getElementById('plot'), rect=canvas.getBoundingClientRect();
          const clickPlot=(point) => canvas.dispatchEvent(new MouseEvent('click',{bubbles:true,
            clientX:rect.left+point.x*(rect.width/canvas.clientWidth),
            clientY:rect.top+point.y*(rect.height/canvas.clientHeight)}));
          clickPlot(coastPoint.find(p => p.inset));
          const sensorCard = document.getElementById('trackBody').innerText;
          clickPlot(airportPoint.find(p => p.inset));
          const effectorCard = document.getElementById('trackBody').innerText;
          const version = document.getElementById('appVersion');
          const result = {
            sensors:scn.sensors.length, effectors:scn.effectors.length,
            airports, noFire:scn.noFire.map(z => z.label),
            coastalSensors:scn.sensors.filter(s => s.coastalInset).length,
            coastalEffectors:scn.effectors.filter(e => e.coastalInset).length,
            mesh:scn.dataMesh, allPaths:paths.every(Boolean), paths:[...new Set(paths)],
            civilAir:[...C.S.tracks.values()].filter(t => t.civil && !t.surface).map(t => ({airport:t.civilAirport, arrival:t.civilArrival, plan:t.flightPlan, callsign:t.transponder?.callsign})),
            coastInset:coastPoint.some(p => p.inset), airportInset:airportPoint.some(p => p.inset),
            sensorCard, effectorCard, selected:C.S.selectedAsset,
            version:version.textContent, versionPx:parseFloat(getComputedStyle(version).fontSize),
          };
          C.setPaused(false); return result;
        }"""
    )
    assert out["sensors"] >= 38 and out["effectors"] >= 48
    assert {a["code"] for a in out["airports"]} == {"KIAD", "KBWI", "KHEF", "KCGS", "KGAI"}
    assert all(a["sensors"] >= 2 and a["effectors"] >= 2 for a in out["airports"])
    assert all(min(a["distances"]) >= 650 and max(a["distances"]) <= 1200 for a in out["airports"])
    assert all(min(a["sensorDx"]) < 0 < max(a["sensorDx"]) for a in out["airports"])
    assert all(min(a["effectorDy"]) < 0 < max(a["effectorDy"]) for a in out["airports"])
    assert all(all(a["perimeter"]) for a in out["airports"])
    assert all(any(code in zone for zone in out["noFire"]) for code in ("KIAD", "KBWI", "KHEF", "KCGS", "KGAI"))
    assert out["coastalSensors"] >= 6 and out["coastalEffectors"] >= 8
    assert len(out["mesh"]["gateways"]) >= 5
    assert "5G" in out["mesh"]["localMedium"] and "FIBER" in out["mesh"]["airportMedium"]
    assert "MICROWAVE" in out["mesh"]["coastalMedium"]
    assert out["allPaths"] and len(out["paths"]) >= 3
    for code in ("KDCA", "KIAD"):
        traffic = [track for track in out["civilAir"] if track["airport"] == code]
        assert len(traffic) >= 4
        assert any(track["arrival"] is True and code in track["plan"] for track in traffic)
        assert any(track["arrival"] is False and code in track["plan"] for track in traffic)
        assert all(track["callsign"] for track in traffic)
    assert out["coastInset"] and out["airportInset"]
    assert "DATA-SHARING PATH" in out["sensorCard"] and "SHARED COP" in out["sensorCard"]
    assert "MAGAZINE DEPTH" in out["effectorCard"] and "COMMS LINK" in out["effectorCard"]
    assert out["selected"]["kind"] == "effector"
    assert out["version"].startswith("v1.0.0") and out["versionPx"] <= 8


def test_washington_joint_air_package_joins_a_ground_started_weapons_free_mission(page):
    out = page.evaluate(
        """() => {
          const C=window.__CUAS__; C.applyScenario('washington'); C.setPaused(true);
          for (const t of C.S.tracks.values()) if (t.identity === 'HOSTILE') t.state='NEUTRALIZED';
          const military=[...C.S.tracks.values()].filter(t => !t.civil && !t.surface &&
            (t.identity === 'FRIEND' || t.identity === 'ASSUMED_FRIEND'));
          const armed=military.filter(t => t.armed), support=military.filter(t => !t.armed);
          military.forEach((a,i) => { a.x=7600+(i%5)*80; a.y=11600+Math.floor(i/5)*80; a.cool=0; if (a.armed) a.weaponRange=12000; });
          const target=C.spawnHostile({target:{name:'FEDERAL CORE',x:0,y:0},bearing:1,r:15000,
            tq:15,identity:'HOSTILE',cls:'UAS_GROUP_2'});
          target.x=8000; target.y=12000; target.heading=Math.atan2(-target.y,-target.x);
          target.state='ENGAGING'; target.engEff=C.currentScenario().effectors[0].effectorId; target.engAir=false;
          C.S.wcs='WEAPONS_FREE'; C.S.autoReleaseArmed=true; C.S.role='FIRE_CONTROL_AUTHORITY';
          const safe=!C.noFireZoneAt(target.x,target.y);
          C.airDefense(0.2);
          const result={safe, armed:armed.length, fighters:armed.filter(t => /^F-/.test(t.platform || '')).length,
            mq9:armed.filter(t => /^MQ-9/.test(t.platform || '')).length,
            volley:(target.airVolley || []).length,
            armedActions:armed.filter(t => t.actionRole === 'AIR WEAPONS VOLLEY' && t.actionTarget === target.trackId).length,
            support:support.length,
            supportActions:support.filter(t => t.actionTarget === target.trackId &&
              ['SENSOR / C2 RELAY','PROTECTED-AIRSPACE REPOSITION'].includes(t.actionRole)).length};
          C.setPaused(false); return result;
        }"""
    )
    assert out["safe"]
    assert out["fighters"] >= 8 and out["mq9"] >= 2
    assert out["volley"] == out["armed"] == out["armedActions"]
    assert out["support"] >= 6 and out["supportActions"] == out["support"]


def test_guam_scenario_has_layered_site_protection(page):
    out = page.evaluate(
        """() => {
          const C = window.__CUAS__; C.applyScenario('guam');
          const scn = C.currentScenario();
          const fixed = [
            {name:'SECTOR CENTER', x:0, y:0},
            {name:scn.civilAirport.code, x:scn.civilAirport.x, y:scn.civilAirport.y},
            {name:'5G gNB', ...scn.gnb},
            {name:'5G POP', x:scn.networkBackhaul.x, y:scn.networkBackhaul.y},
            ...scn.protectedSites,
            ...scn.sensors.map(s => ({name:s.sensorId, x:s.x, y:s.y})),
            ...scn.effectors.map(e => ({name:e.effectorId, x:e.x, y:e.y})),
          ];
          const path = scn.networkBackhaul.path;
          const pathSamples = [];
          for (let i=1; i<path.length; i++) {
            for (let step=0; step<=20; step++) {
              const t=step/20;
              pathSamples.push({x:path[i-1].x+(path[i].x-path[i-1].x)*t,
                y:path[i-1].y+(path[i].y-path[i-1].y)*t});
            }
          }
          return {id:C.S.scenario, terrain:scn.terrain, airport:scn.civilAirport.code,
            aoRadius:scn.aoRadius, rings:scn.rings, preferred:scn.preferredBasemap,
            basemap:C.S.basemap,
            polygon:scn.landPolygon.length, source:scn.landPolygonSource,
            serviceBases:scn.protectedSites.filter(s => s.service).map(s => `${s.service}:${s.name}`),
            protectedSites:scn.protectedSites.length, sensors:scn.sensors.length,
            effectors:scn.effectors.length,
            protectedSensors:scn.sensors.filter(s => s.protectedSite).length,
            protectedEffectors:scn.effectors.filter(e => e.protectedSite).length,
            baseLayers:{
              andersen:{s:scn.sensors.filter(s => s.sensorId.includes('-AND-')).length,
                e:scn.effectors.filter(e => e.effectorId.includes('-AND-')).length},
              navy:{s:scn.sensors.filter(s => /-(PORT|NBG)-/.test(s.sensorId)).length,
                e:scn.effectors.filter(e => /-(PORT|NBG)-/.test(e.effectorId)).length},
              blaz:{s:scn.sensors.filter(s => s.sensorId.includes('-BLAZ-')).length,
                e:scn.effectors.filter(e => e.effectorId.includes('-BLAZ-')).length}},
            effectorTypes:[...new Set(scn.effectors.map(e => e.effectorType))],
            axes:scn.threat.axes.length,
            uniqueAxes:new Set(scn.threat.axes.map(a => Math.round((((a % (2*Math.PI)) + 2*Math.PI) % (2*Math.PI))*1000))).size,
            targets:scn.threatTargets.map(t => t.name), ringFromCenter:scn.threatRingFromCenter,
            fighters:[...C.S.tracks.values()].filter(t => t.armed && ((t.platform || '').startsWith('F-') || (t.platform || '').startsWith('F/A-'))).length,
            mq9:[...C.S.tracks.values()].filter(t => t.armed && (t.platform || '').startsWith('MQ-9')).length,
            airportSensors:scn.sensors.filter(s => s.airportDefense && Math.hypot(s.x-scn.civilAirport.x,s.y-scn.civilAirport.y) <= 1800).length,
            airportEffectors:scn.effectors.filter(e => e.airportDefense && Math.hypot(e.x-scn.civilAirport.x,e.y-scn.civilAirport.y) <= 1800).length,
            civilAir:[...C.S.tracks.values()].filter(t => t.civil && !t.surface).map(t => ({arrival:t.civilArrival, plan:t.flightPlan, transponder:t.transponder})),
            backhaul:scn.networkBackhaul,
            offLand:fixed.filter(p => !C.scenarioPointOnLand(p.x,p.y)).map(p => p.name),
            pathOnLand:pathSamples.every(p => C.scenarioPointOnLand(p.x,p.y))};
        }"""
    )
    assert out["id"] == "guam" and out["terrain"] == "island"
    assert out["airport"] == "PGUM"
    assert out["aoRadius"] >= 28000 and out["rings"] == [10000, 20000]
    assert out["preferred"] == "SAT" and out["polygon"] >= 50
    assert out["basemap"] == "TAC", "an explicit Tactical selection must override Guam's satellite preference"
    assert "CENSUS" in out["source"]
    assert out["protectedSites"] >= 4
    assert any("USAF:ANDERSEN AIR FORCE BASE" in base for base in out["serviceBases"])
    assert any("USN:NAVAL BASE GUAM" in base for base in out["serviceBases"])
    assert any("USMC:MARINE CORPS BASE CAMP BLAZ" in base for base in out["serviceBases"])
    assert out["sensors"] >= 23 and out["effectors"] >= 31
    assert out["protectedSensors"] >= 16 and out["protectedEffectors"] >= 22
    assert all(layer["s"] >= 4 and layer["e"] >= 6 for layer in out["baseLayers"].values())
    assert {"EW_JAMMER", "RF_TAKEOVER", "DIRECTED_ENERGY", "KINETIC_GUN", "KINETIC_INTERCEPTOR", "NET_CAPTURE"}.issubset(set(out["effectorTypes"]))
    assert out["axes"] >= 8 and out["uniqueAxes"] == 8 and out["ringFromCenter"] is True
    for target in ("ANDERSEN AIR FORCE BASE", "NAVAL BASE GUAM", "MARINE CORPS BASE CAMP BLAZ", "GUAM INTL", "CENTRAL POWER"):
        assert any(target in name for name in out["targets"])
    assert out["fighters"] >= 6 and out["mq9"] >= 4
    assert out["airportSensors"] >= 4 and out["airportEffectors"] >= 4
    assert len(out["civilAir"]) >= 4
    assert any(track["arrival"] is True and "PGUM" in track["plan"] for track in out["civilAir"])
    assert any(track["arrival"] is False and "PGUM" in track["plan"] for track in out["civilAir"])
    assert all(track["transponder"]["callsign"] and track["transponder"]["icao24"] for track in out["civilAir"])
    assert "ON-ISLAND" in out["backhaul"]["description"]
    assert out["offLand"] == [], f"Guam fixed sites plotted offshore: {out['offLand']}"
    assert out["pathOnLand"], "every sampled Guam 5G backbone point must stay on land"


def test_guam_full_perimeter_targets_all_bases_and_air_shooters_join_weapons_free(page):
    out = page.evaluate(
        """() => {
          const C=window.__CUAS__; C.applyScenario('guam'); C.setPaused(true);
          const scn=C.currentScenario();
          const probes=scn.threatTargets.map((target,i) => {
            const t=C.spawnThreat(0.1,{target,bearing:scn.threat.axes[i % scn.threat.axes.length],r:28000,tq:15});
            return {target:t.targetName, originRange:Math.hypot(t.x,t.y)};
          });
          for (const t of C.S.tracks.values()) if (t.identity === 'HOSTILE') t.state='NEUTRALIZED';
          const air=[...C.S.tracks.values()].filter(t => t.armed && t.classificationType !== 'SURFACE');
          air.forEach((a,i) => { a.x=(i%4)*120-180; a.y=11200+Math.floor(i/4)*100; a.weaponRange=12000; a.cool=0; });
          const hostiles=air.map((a,i) => {
            const t=C.spawnHostile({target:{name:'GUAM JOINT DEFENSE SECTOR',x:0,y:0},bearing:Math.PI/2,r:12000,
              tq:15,identity:'HOSTILE',cls:['MULTIROTOR','UAS_GROUP_1','UAS_GROUP_2','FIXED_WING'][i%4]});
            t.x=(i%4)*120-180; t.y=11800+Math.floor(i/4)*100; t.heading=Math.atan2(-t.y,-t.x); return t;
          });
          C.S.wcs='WEAPONS_FREE'; C.S.autoReleaseArmed=true; C.S.role='FIRE_CONTROL_AUTHORITY';
          C.airDefense(0.2);
          const shooterById=new Map(air.map(a => [a.trackId,a]));
          const engaged=hostiles.filter(t => t.engAir).map(t => shooterById.get(t.engEff)?.platform).filter(Boolean);
          const result={probes,air:air.length,engaged:engaged.length,
            fighters:air.filter(t => (t.platform || '').startsWith('F-') || (t.platform || '').startsWith('F/A-')).length,
            fighterShots:engaged.filter(p => p.startsWith('F-') || p.startsWith('F/A-')).length,
            mq9:air.filter(t => (t.platform || '').startsWith('MQ-9')).length,
            mq9Shots:engaged.filter(p => p.startsWith('MQ-9')).length};
          C.setPaused(false); return result;
        }"""
    )
    assert all(27990 <= probe["originRange"] <= 28010 for probe in out["probes"])
    assert len({probe["target"] for probe in out["probes"]}) >= 5
    assert out["engaged"] == out["air"]
    assert out["fighters"] >= 6 and out["fighterShots"] == out["fighters"]
    assert out["mq9"] >= 4 and out["mq9Shots"] == out["mq9"]


def test_guam_defaults_to_satellite_without_an_explicit_basemap(page):
    satellite_page = page.context.new_page()
    try:
        satellite_page.goto(f"file://{COP}?debug=1&scn=guam&seed=42&wx=CLEAR&tod=DAY")
        satellite_page.wait_for_function("window.__CUAS__ && window.__CUAS__.S.scenario === 'guam'")
        out = satellite_page.evaluate(
            """() => ({basemap:window.__CUAS__.S.basemap,
              pressed:document.querySelector('#baseSeg button[data-v="SAT"]').getAttribute('aria-pressed')})"""
        )
        assert out == {"basemap": "SAT", "pressed": "true"}
    finally:
        satellite_page.close()


def test_guam_regional_view_uses_pacific_installations(page):
    out = page.evaluate(
        """() => {
          const C = window.__CUAS__; C.applyScenario('guam'); C.buildRegional();
          return {title:document.getElementById('regTitle').textContent,
            hq:document.getElementById('regHq').textContent,
            bases:document.getElementById('regBases').textContent};
        }"""
    )
    assert "USINDOPACOM" in out["title"]
    assert "CAMP H.M. SMITH" in out["hq"].upper()
    assert "PEARL HARBOR-HICKAM" in out["bases"].upper()
    assert "KADENA" in out["bases"].upper()
    assert "IWAKUNI" in out["bases"].upper()
    for continental_base in ("MIRAMAR", "JBLM", "NELLIS"):
        assert continental_base not in out["bases"].upper()


def test_west_coast_is_split_into_local_north_island_and_miramar_areas(page):
    out = page.evaluate(
        """() => {
          const C = window.__CUAS__; C.applyScenario('sandiego'); C.setPaused(true);
          const north = C.currentScenario();
          const northResult = {id:north.id, aoRadius:north.aoRadius, name:north.asset.name,
            preferred:north.preferredBasemap, lat:north.asset.lat, lon:north.asset.lon,
            hasMiramar:north.protectedSites.some(s => s.name.includes('MIRAMAR'))};
          C.applyScenario('miramar'); C.setPaused(true);
          const scn = C.currentScenario(), target=scn.threatTargets[0];
          const near = (item) => Math.hypot(item.x-target.x, item.y-target.y) <= 1200;
          const sensors = scn.sensors.filter(s => s.protectedSite && near(s));
          const effectors = scn.effectors.filter(e => e.protectedSite && near(e));
          const threat = C.spawnThreat(0.3, {target, r:6200, bearing:Math.PI});
          const miramarResult = {id:scn.id, aoRadius:scn.aoRadius, name:scn.asset.name,
            preferred:scn.preferredBasemap, lat:scn.asset.lat, lon:scn.asset.lon,
            sensors:sensors.length, effectors:effectors.length,
            types:[...new Set(effectors.map(e => e.effectorType))],
            targets:scn.threatTargets.map(t => t.name),
            uniqueAxes:new Set(scn.threat.axes.map(a => Math.round((((a % (2*Math.PI)) + 2*Math.PI) % (2*Math.PI))*1000))).size,
            westAxes:scn.threat.axes.filter(a => Math.cos(a) < -0.5).length,
            totalAxes:scn.threat.axes.length, offshoreScreen:scn.offshoreScreen,
            offshoreSensors:scn.sensors.filter(s => s.offshore && s.x < -15000).length,
            offshoreEffectors:scn.effectors.filter(e => e.offshore && e.x < -15000).length,
            naval:[...C.S.tracks.values()].filter(t => t.service === 'USN' && ['SURFACE','ROTARY'].includes(t.classificationType) && t.ox < -14000).map(t => t.platform),
            f35b:[...C.S.tracks.values()].some(t => t.platform === 'F-35B' && t.service === 'USMC' && t.armed),
            targetName:threat.targetName, targetX:threat.targetX, targetY:threat.targetY,
            spawnRange:Math.hypot(threat.x-target.x, threat.y-target.y),
            prediction:C.trackPrediction(threat).interceptSec};
          C.setPaused(false); return {north:northResult, miramar:miramarResult,
            buttons:[...document.querySelectorAll('#scnSeg button')].map(b => b.textContent.trim())};
        }"""
    )
    assert out["buttons"][:2] == ["North Island", "Miramar"]
    assert out["north"]["id"] == "sandiego" and out["north"]["aoRadius"] <= 7000
    assert out["north"]["name"] == "NAS NORTH ISLAND"
    assert out["north"]["preferred"] == "SAT" and out["north"]["hasMiramar"] is False
    assert out["miramar"]["id"] == "miramar" and out["miramar"]["aoRadius"] <= 6500
    assert out["miramar"]["name"] == "MCAS MIRAMAR"
    assert out["miramar"]["preferred"] == "SAT"
    assert (out["north"]["lat"], out["north"]["lon"]) != (out["miramar"]["lat"], out["miramar"]["lon"])
    assert out["miramar"]["sensors"] >= 3 and out["miramar"]["effectors"] >= 6
    assert {"EW_JAMMER", "RF_TAKEOVER", "DIRECTED_ENERGY", "KINETIC_GUN", "KINETIC_INTERCEPTOR", "NET_CAPTURE"}.issubset(set(out["miramar"]["types"]))
    assert out["miramar"]["f35b"] is True
    assert len(out["miramar"]["targets"]) >= 3
    assert out["miramar"]["uniqueAxes"] == 8
    assert out["miramar"]["westAxes"] > out["miramar"]["totalAxes"] / 2
    assert "PACIFIC NAVAL SCREEN" in out["miramar"]["offshoreScreen"]["label"]
    assert out["miramar"]["offshoreSensors"] >= 2 and out["miramar"]["offshoreEffectors"] >= 3
    assert {"DDG-51 DESTROYER", "LCS", "MH-60R"}.issubset(set(out["miramar"]["naval"]))
    assert "MIRAMAR" in out["miramar"]["targetName"]
    assert out["miramar"]["targetX"] == 0 and out["miramar"]["targetY"] == 0
    assert 6190 <= out["miramar"]["spawnRange"] <= 6210
    assert out["miramar"]["prediction"] is not None


def test_every_scenario_airport_has_local_defenses_and_traffic(page):
    out = page.evaluate(
        """() => {
          const C = window.__CUAS__;
          return ['sandiego', 'miramar', 'elpaso', 'norfolk', 'washington', 'guam'].map(id => {
            C.applyScenario(id);
            const scn = C.currentScenario(), airport = scn.civilAirport;
            const distance = (item) => Math.hypot(item.x-airport.x, item.y-airport.y);
            const near = (item) => distance(item) <= 1200;
            const sensors=scn.sensors.filter(s => airport.sensorIds.includes(s.sensorId) && near(s));
            const effectors=scn.effectors.filter(e => e.airportDefense && near(e));
            const aircraft = C.spawnCivAir();
            return {id, code:airport.code, plan:aircraft.flightPlan,
              sensors:sensors.length, effectors:effectors.map(e => e.effectorType),
              distances:[...sensors,...effectors].map(distance),
              sensorDx:sensors.map(s => s.x-airport.x), effectorDy:effectors.map(e => e.y-airport.y),
              perimeter:[...sensors,...effectors].map(item => item.airportPerimeter)};
          });
        }"""
    )
    for scenario in out:
        assert scenario["code"] in scenario["plan"]
        assert scenario["sensors"] >= 2
        assert {"EW_JAMMER", "NET_CAPTURE"}.issubset(set(scenario["effectors"]))
        assert min(scenario["distances"]) >= 650
        assert min(scenario["sensorDx"]) < 0 < max(scenario["sensorDx"])
        assert min(scenario["effectorDy"]) < 0 < max(scenario["effectorDy"])
        assert all(scenario["perimeter"])


def test_data_connections_are_contextual_and_dc_routes_are_bounded(page):
    page.evaluate("() => { window.__CUAS__.applyScenario('washington'); window.__CUAS__.setPaused(true); }")
    page.wait_for_timeout(80)
    assert page.evaluate("window.__CUAS__.S.visibleDataRoute") is None

    page.evaluate("window.__CUAS__.selectAsset('sensor','SEN-IAD-RAD-40')")
    page.wait_for_timeout(80)
    selected = page.evaluate("window.__CUAS__.S.visibleDataRoute")
    assert selected["kind"] == "system" and selected["id"] == "SEN-IAD-RAD-40"
    assert selected["points"] <= 4 and selected["bounded"] is True

    page.evaluate("window.__CUAS__.selectTrack(null)")
    page.wait_for_timeout(80)
    assert page.evaluate("window.__CUAS__.S.visibleDataRoute") is None

    point = page.evaluate("window.__CUAS__.transportPoints().gnb")
    page.locator("#plot").click(position={"x": point["x"], "y": point["y"]})
    page.wait_for_timeout(80)
    network = page.evaluate("window.__CUAS__.S.visibleDataRoute")
    assert network["kind"] == "network" and network["points"] <= 4
    assert network["bounded"] is True
    page.evaluate("window.__CUAS__.setPaused(false)")


def test_map_supports_wheel_zoom_drag_pan_and_recenter(page):
    page.evaluate("() => { window.__CUAS__.applyScenario('miramar'); window.__CUAS__.resetMapView(false); }")
    plot = page.locator("#plot")
    box = plot.bounding_box()
    assert box
    center_x, center_y = box["x"] + box["width"] / 2, box["y"] + box["height"] / 2

    page.mouse.move(center_x, center_y)
    page.mouse.wheel(0, -420)
    page.wait_for_timeout(80)
    zoomed = page.evaluate("window.__CUAS__.mapView()")
    assert zoomed["zoom"] > 1.25

    page.mouse.move(center_x, center_y)
    page.mouse.down()
    page.mouse.move(center_x + 120, center_y + 70, steps=5)
    page.mouse.up()
    page.wait_for_timeout(50)
    panned = page.evaluate("window.__CUAS__.mapView()")
    assert abs(panned["centerX"]) > 50 or abs(panned["centerY"]) > 50
    assert page.locator("#btnRecenterMap").get_attribute("class") and "active" in page.locator("#btnRecenterMap").get_attribute("class")

    page.locator("#btnRecenterMap").click()
    reset = page.evaluate("window.__CUAS__.mapView()")
    assert reset == {"centerX": 0, "centerY": 0, "zoom": 1}
    assert page.locator("#mapZoom").text_content() == "100%"


def test_el_paso_border_patrol_network_is_distributed(page):
    out = page.evaluate(
        """() => {
          const C = window.__CUAS__; C.applyScenario('elpaso');
          const scn = C.currentScenario();
          return scn.borderStations.map(station => ({
            name:station.name,
            sensor:scn.sensors.some(s => s.sensorId === station.sensorId && s.borderStation),
            effector:scn.effectors.some(e => e.effectorId === station.effectorId && e.borderStation),
            x:station.x,
          }));
        }"""
    )
    assert len(out) >= 3
    assert all(station["name"].startswith("USBP") for station in out)
    assert all(station["sensor"] and station["effector"] for station in out)
    assert max(station["x"] for station in out) - min(station["x"] for station in out) >= 8000


def test_el_paso_all_sensor_and_effector_sites_are_in_the_united_states(page):
    out = page.evaluate(
        """() => {
          const C = window.__CUAS__; C.applyScenario('elpaso');
          const scn = C.currentScenario();
          return {borderY:scn.borderY,
            sensors:scn.sensors.map(s => ({id:s.sensorId, y:s.y})),
            effectors:scn.effectors.map(e => ({id:e.effectorId, y:e.y})),
            stations:scn.borderStations.map(s => ({name:s.name, y:s.y}))};
        }"""
    )
    positioned = out["sensors"] + out["effectors"] + out["stations"]
    assert all(item["y"] > out["borderY"] for item in positioned), positioned


def test_event_panel_is_taller_and_regional_view_uses_alert_font(page):
    out = page.evaluate(
        """() => {
          const alert = document.querySelector('.reg-alert');
          const title = document.querySelector('.reg-title');
          const note = document.querySelector('.reg-note');
          const button = document.querySelector('.reg-foot .sbtn');
          const ticker = document.querySelector('.event-ticker');
          return {eventHeight:document.querySelector('.event-strip').getBoundingClientRect().height,
            eventColumns:getComputedStyle(document.querySelector('.event-line')).gridTemplateColumns,
            tickerOverflow:getComputedStyle(ticker).overflow,
            alertFont:getComputedStyle(alert).fontFamily,
            titleFont:getComputedStyle(title).fontFamily,
            noteFont:getComputedStyle(note).fontFamily,
            buttonFont:getComputedStyle(button).fontFamily};
        }"""
    )
    assert out["eventHeight"] >= 130
    assert out["tickerOverflow"] == "hidden"
    assert len(out["eventColumns"].split()) == 2
    assert out["titleFont"] == out["alertFont"]
    assert out["noteFont"] == out["alertFont"]
    assert out["buttonFont"] == out["alertFont"]


def test_5g_details_open_only_when_a_transport_node_is_clicked(page):
    expected = {
        "sandiego": "SAN DIEGO",
        "miramar": "MIRAMAR",
        "elpaso": "EL PASO",
        "norfolk": "NORFOLK",
        "washington": "NCR",
        "guam": "GUAM",
    }
    for scenario, label in expected.items():
        page.evaluate("scenario => window.__CUAS__.applyScenario(scenario)", scenario)
        point = page.evaluate("window.__CUAS__.transportPoints().gnb")
        assert page.evaluate("window.__CUAS__.S.transportInfoOpen") is False
        page.locator("#plot").click(position={"x": point["x"], "y": point["y"]})
        page.wait_for_timeout(80)
        rendered = page.evaluate("window.__CUAS__.S.transportInfoRendered")
        assert page.evaluate("window.__CUAS__.S.transportInfoOpen") is True
        assert rendered and label in rendered["label"]
        page.locator("#plot").click(position={"x": point["x"], "y": point["y"]})
        assert page.evaluate("window.__CUAS__.S.transportInfoOpen") is False


def test_moving_vessel_identifier_keeps_a_stable_nearby_anchor(page):
    track_id = page.evaluate(
        """() => {
          const C=window.__CUAS__; C.applyScenario('sandiego');
          const t=C.spawnCivBoat({platform:'FISHING VESSEL', laneX:-5200, y:-1200, north:true});
          return t.trackId;
        }"""
    )
    page.wait_for_timeout(180)
    first = page.evaluate("id => window.__CUAS__.labelOffset(id)", track_id)
    page.wait_for_timeout(420)
    second = page.evaluate("id => window.__CUAS__.labelOffset(id)", track_id)
    assert first is not None and second is not None
    assert first == second, "moving vessel label should retain its collision-avoidance slot"
    assert abs(second) <= 44, "vessel identifier must remain close to its hull"


def test_fire_release_actions_are_red_white_and_use_the_console_font(page):
    track_id = page.evaluate(
        """() => {
          const C=window.__CUAS__; C.applyScenario('sandiego'); C.setWorkspace('FIRES');
          C.S.role='FIRE_CONTROL_AUTHORITY'; C.S.wcs='WEAPONS_TIGHT';
          const t=C.spawnHostile({r:1200,tq:15,identity:'HOSTILE'});
          C.selectTrack(t.trackId); return t.trackId;
        }"""
    )
    page.wait_for_timeout(850)
    queue_button = page.locator(".fire-release:not([disabled])").first
    assert queue_button.count() == 1
    out = page.evaluate(
        """() => {
          const queue=document.querySelector('.fire-release:not([disabled])');
          const engage=document.getElementById('btnEngage');
          const style=(el) => { const s=getComputedStyle(el); return {background:s.backgroundColor,
            color:s.color,font:s.fontFamily}; };
          return {queue:style(queue),engage:style(engage),app:getComputedStyle(document.getElementById('cuas')).fontFamily};
        }"""
    )
    assert out["queue"]["background"] == "rgb(143, 29, 44)"
    assert out["engage"]["background"] == "rgb(143, 29, 44)"
    assert out["queue"]["color"] == out["engage"]["color"] == "rgb(255, 255, 255)"
    assert out["queue"]["font"] == out["engage"]["font"] == out["app"]
    assert track_id


def test_every_text_bearing_element_uses_the_app_monospace_font(page):
    mismatches = page.evaluate(
        """() => {
          const expected=getComputedStyle(document.getElementById('cuas')).fontFamily;
          return [...document.body.querySelectorAll('*')]
            .filter(el => [...el.childNodes].some(n => n.nodeType === Node.TEXT_NODE && n.textContent.trim()))
            .filter(el => getComputedStyle(el).fontFamily !== expected)
            .map(el => ({tag:el.tagName,id:el.id,cls:el.className,font:getComputedStyle(el).fontFamily}));
        }"""
    )
    assert mismatches == []


def test_san_diego_airport_tracks_publish_transponder_data(page):
    out = page.evaluate(
        """() => {
      const C = window.__CUAS__;
      C.applyScenario('sandiego');
      const t = C.spawnCivAir();
      C.selectTrack(t.trackId);
      const scn = C.currentScenario();
      return {civil:t.civil, route:t.civilRoute.length, plan:t.flightPlan,
        callsign:t.transponder.callsign, icao:t.transponder.icao24,
        squawk:t.transponder.squawk, quality:[t.transponder.nacp,t.transponder.sil],
        airportSensors:t.contributingSensors.includes('SEN-AIR-RAD-12'),
        airportEffectors:scn.effectors.filter(e => e.airportDefense).map(e => e.effectorType),
        details:document.getElementById('decisionDetailBody').textContent};
    }"""
    )
    assert out["civil"] and out["route"] == 1
    assert "KSAN" in out["plan"]
    assert len(out["icao"]) == 6 and len(out["squawk"]) == 4
    assert out["quality"][0] >= 9 and out["quality"][1] >= 2
    assert out["airportSensors"]
    assert set(out["airportEffectors"]) == {"EW_JAMMER", "NET_CAPTURE"}
    assert out["callsign"] in out["details"] and out["icao"] in out["details"]


def test_san_diego_5g_overlay_has_island_microwave_backhaul(page):
    out = page.evaluate(
        """() => {
      const C = window.__CUAS__;
      C.applyScenario('sandiego');
      const relay = C.currentScenario().microwaveRelay;
      return {label:relay.label, island:relay.island, inset:relay.inset, note:relay.note,
        legend:document.getElementById('legend').textContent};
    }"""
    )
    assert "SAN CLEMENTE" in out["label"]
    assert out["island"] == "SAN CLEMENTE ISLAND" and out["inset"] is True
    assert "MICROWAVE" in out["note"]
    assert "Microwave backhaul" in out["legend"]


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
      for (const e of C.S.effectors) {
        if (["EW_JAMMER", "RF_TAKEOVER", "NET_CAPTURE"].includes(e.effectorType)) e.mag = 0;
      }
      // Exercise several independent 90%-Pk interceptor outcomes so this test
      // validates the BDA lifecycle rather than depending on one random roll.
      for (let i=0; i<6; i++) {
        const t = C.spawnHostile({r: 1400 + i*80, tq: 15, alt: 300, bearing:-2.8 + i*0.12});
        C.engageTrack(t, false);
      }
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
