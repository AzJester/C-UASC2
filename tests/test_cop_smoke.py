"""Headless smoke test for the web COP (services/c2-core/app/static/cop.html).

The COP is a large single file we edit often; this asserts it still boots, runs a
raid, and stays error-free in a real browser. It drives the embedded SIM via the
``?debug=1`` hook (window.__CUAS__), so no backend or network is needed.

Skips automatically when Playwright or a browser is unavailable, so ``make test``
stays green on machines without a browser; CI installs Chromium and runs it for
real (see .github/workflows/ci.yml).
"""
from __future__ import annotations

import os
from pathlib import Path

import pytest

sync_api = pytest.importorskip("playwright.sync_api")

COP = Path(__file__).resolve().parents[1] / "services" / "c2-core" / "app" / "static" / "cop.html"


@pytest.fixture(scope="module")
def page():
    from playwright.sync_api import sync_playwright

    # COP_SMOKE_CHROMIUM lets you point at a system/pre-installed Chromium when the
    # bundled browser revision is unavailable; CI leaves it unset and uses the
    # browser fetched by `playwright install chromium`.
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
        pg.goto(f"file://{COP}?debug=1&basemap=tac&seed=42&wx=CLEAR&tod=DAY")
        pg.wait_for_timeout(500)
        yield pg
        browser.close()


def test_cop_boots_clean(page):
    assert COP.exists(), "cop.html missing"
    # debug hook is exposed and the core controls are present
    assert page.eval_on_selector("#cuas", "el => !!el")
    for ctrl in ("#btnAuto", "#btnPause", "#btnHelpTop", "#btnHoldAbort", "#workspaceNav", "#trackList", "#spdSeg", "#injectSel", "#operateSel", "#scoreboard", "#log"):
        assert page.query_selector(ctrl) is not None, f"missing {ctrl}"
    api = page.evaluate("() => Object.keys(window.__CUAS__ || {})")
    assert "S" in api and "resetScenario" in api, f"debug hook incomplete: {api}"


def test_satellite_imagery_is_the_default_basemap(page):
    default_page = page.context.new_page()
    default_page.route("https://server.arcgisonline.com/**", lambda route: route.abort())
    default_page.goto(f"file://{COP}?debug=1&seed=17")
    default_page.wait_for_function("() => !!window.__CUAS__")

    assert default_page.evaluate("window.__CUAS__.S.basemap") == "SAT"
    assert (
        default_page.get_attribute('#baseSeg button[data-v="SAT"]', "aria-pressed")
        == "true"
    )
    assert (
        default_page.get_attribute('#baseSeg button[data-v="TAC"]', "aria-pressed")
        == "false"
    )
    default_page.close()


def test_raid_runs_and_produces_an_engagement_outcome(page):
    # Autonomous air/naval release now defaults OFF. This raid test deliberately
    # arms auto-release through the debug seam; normal operators use the visible
    # two-step WEAPONS FREE confirmation. Reload the seeded laydown so time spent
    # in earlier tests cannot consume inventory or advance the simulation state.
    page.reload()
    page.wait_for_function("() => !!window.__CUAS__")
    page.evaluate("() => { window.__CUAS__.S.wcs='WEAPONS_FREE'; window.__CUAS__.S.autoReleaseArmed=true; }")
    page.click("#btnExercise")
    page.select_option("#injectSel", "raid")
    page.click("#btnExercise")
    page.wait_for_function(
        """() => {
          const s = window.__CUAS__.S.stats;
          const outcomes = s.defeated + s.misses + s.partialEffects + s.unconfirmedEffects;
          return s.engagements > 0 && outcomes > 0;
        }""",
        timeout=15_000,
    )
    stats = page.evaluate(
        "() => ({ tracks: window.__CUAS__.S.tracks.size,"
        " outcomes: window.__CUAS__.S.stats.defeated + window.__CUAS__.S.stats.misses +"
        "   window.__CUAS__.S.stats.partialEffects + window.__CUAS__.S.stats.unconfirmedEffects,"
        " engagements: window.__CUAS__.S.stats.engagements,"
        " integrity: window.__CUAS__.S.assetIntegrity })"
    )
    assert stats["tracks"] > 0, "no tracks after raid"
    assert stats["engagements"] > 0, "no engagements ran"
    assert stats["outcomes"] > 0, "no engagement outcome completed"
    assert 0 <= stats["integrity"] <= 100


def test_role_workspaces_and_exercise_controls_are_separated(page):
    page.evaluate("window.__CUAS__.setWorkspace('FIRES')")
    assert page.locator("#queueCard").is_visible()
    assert page.locator("#authorityCard").is_visible()
    assert not page.locator("#scoreboard").is_visible()
    assert page.locator("#tray").is_hidden()
    page.click("#btnExercise")
    assert page.locator("#tray").is_visible()
    assert page.get_attribute("#btnExercise", "aria-expanded") == "true"
    page.click("#btnExercise")


def test_modal_focus_moves_and_returns(page):
    page.focus("#btnHelpTop")
    page.click("#btnHelpTop")
    page.wait_for_timeout(50)
    assert page.evaluate("document.activeElement.id") == "helpClose"
    page.keyboard.press("Escape")
    page.wait_for_timeout(50)
    assert page.evaluate("document.activeElement.id") == "btnHelpTop"


def test_space_on_button_does_not_toggle_pause(page):
    page.evaluate("window.__CUAS__.setPaused(false)")
    page.focus("#btnHelpTop")
    page.keyboard.press("Space")
    assert page.evaluate("window.__CUAS__.S.paused") is False
    if page.locator("#helpBack").is_visible():
        page.keyboard.press("Escape")


def test_pause_freezes_motion(page):
    page.evaluate("() => window.__CUAS__.setPaused(true)")
    p1 = page.evaluate(
        "() => { const t=[...window.__CUAS__.S.tracks.values()].find(x=>x.identity==='HOSTILE');"
        " return t ? {x:t.x,y:t.y} : null; }"
    )
    page.wait_for_timeout(600)
    p2 = page.evaluate(
        "() => { const t=[...window.__CUAS__.S.tracks.values()].find(x=>x.identity==='HOSTILE');"
        " return t ? {x:t.x,y:t.y} : null; }"
    )
    page.evaluate("() => window.__CUAS__.setPaused(false)")
    if p1 and p2:
        assert abs(p1["x"] - p2["x"]) < 1 and abs(p1["y"] - p2["y"]) < 1, "motion not frozen while paused"


@pytest.mark.parametrize("width,height", [(1280, 720), (1920, 1080), (2560, 1440)])
def test_fixed_site_desktop_layout_matrix(page, width, height):
    desktop = page.context.new_page()
    errors: list[str] = []
    desktop.on("pageerror", lambda error: errors.append(str(error)))
    desktop.set_viewport_size({"width": width, "height": height})
    desktop.goto(f"file://{COP}?debug=1&basemap=tac&seed=11")
    desktop.wait_for_timeout(500)
    metrics = desktop.evaluate(
        "() => ({sw:document.documentElement.scrollWidth,cw:document.documentElement.clientWidth,"
        "sh:document.documentElement.scrollHeight,ch:document.documentElement.clientHeight,"
        "rootSw:document.querySelector('#cuas').scrollWidth,rootCw:document.querySelector('#cuas').clientWidth,"
        "gate:!!document.querySelector('.workstation-gate'),command:!!document.querySelector('.commandbar'),"
        "trackList:!!document.querySelector('#trackList')})"
    )
    assert metrics["sw"] <= metrics["cw"] + 1
    assert metrics["sh"] <= metrics["ch"] + 1
    assert metrics["rootSw"] <= metrics["rootCw"] + 1
    assert metrics["gate"] is False
    assert metrics["command"] and metrics["trackList"]
    assert not errors
    desktop.close()


def test_no_page_scroll_and_no_console_errors(page):
    scroll = page.evaluate(
        "() => ({ sw: document.documentElement.scrollWidth, cw: document.documentElement.clientWidth,"
        " sh: document.documentElement.scrollHeight, ch: document.documentElement.clientHeight })"
    )
    assert scroll["sw"] <= scroll["cw"] + 1, "horizontal page scroll present"
    assert scroll["sh"] <= scroll["ch"] + 1, "vertical page scroll present"
    errors = getattr(page, "console_errors", [])
    assert not errors, f"console errors: {errors}"
