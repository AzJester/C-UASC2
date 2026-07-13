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


def test_operator_notices_replace_in_place_and_use_screen_type(page):
    page.evaluate(
        """() => {
          window.__CUAS__.toast('ok', 'FIRST EVENT');
          window.__CUAS__.toast('ok', 'SECOND EVENT');
          window.__CUAS__.toast('deny', 'LATEST EVENT');
          window.__CUAS__.showAAR();
        }"""
    )
    notice = page.evaluate(
        """() => {
          const slot = document.querySelector('#toasts');
          const root = document.querySelector('#cuas');
          const aar = document.querySelector('.aar');
          return {
            inRail: !!slot.closest('.rail-toolbar'),
            inMap: !!slot.closest('.plotwrap'),
            count: slot.children.length,
            text: slot.textContent,
            nowrap: getComputedStyle(slot.querySelector('.toast-message')).whiteSpace,
            rootFont: getComputedStyle(root).fontFamily,
            aarFont: getComputedStyle(aar).fontFamily,
          };
        }"""
    )
    assert notice["inRail"] is True
    assert notice["inMap"] is False
    assert notice["count"] == 1
    assert "LATEST EVENT" in notice["text"]
    assert "FIRST EVENT" not in notice["text"]
    assert notice["nowrap"] == "nowrap"
    assert notice["rootFont"] == notice["aarFont"]
    page.keyboard.press("Escape")


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


@pytest.mark.parametrize(
    "width,height",
    [(1280, 720), (1920, 1080), (2560, 1440), (3840, 2160)],
)
def test_fixed_site_desktop_layout_matrix(page, width, height):
    desktop = page.context.new_page()
    errors: list[str] = []
    desktop.on("pageerror", lambda error: errors.append(str(error)))
    desktop.set_viewport_size({"width": width, "height": height})
    desktop.goto(f"file://{COP}?basemap=tac&seed=11")
    desktop.wait_for_timeout(500)
    first_track = desktop.locator("#trackList button[data-track-select]").first
    if first_track.count():
        first_track.click()
    metrics = desktop.evaluate(
        "() => ({sw:document.documentElement.scrollWidth,cw:document.documentElement.clientWidth,"
        "sh:document.documentElement.scrollHeight,ch:document.documentElement.clientHeight,"
        "rootSw:document.querySelector('#cuas').scrollWidth,rootCw:document.querySelector('#cuas').clientWidth,"
        "leftSh:document.querySelector('.rail-left-scroll').scrollHeight,leftCh:document.querySelector('.rail-left-scroll').clientHeight,"
        "rightSh:document.querySelector('.rail-right-scroll').scrollHeight,rightCh:document.querySelector('.rail-right-scroll').clientHeight,"
        "left:document.querySelector('.rail-left').getBoundingClientRect(),plot:document.querySelector('.plotwrap').getBoundingClientRect(),"
        "right:document.querySelector('.rail-right').getBoundingClientRect(),"
        "gate:!!document.querySelector('.workstation-gate'),command:!!document.querySelector('.commandbar'),"
        "trackList:!!document.querySelector('#trackList')})"
    )
    assert metrics["sw"] <= metrics["cw"] + 1
    assert metrics["sh"] <= metrics["ch"] + 1
    assert metrics["rootSw"] <= metrics["rootCw"] + 1
    assert metrics["leftSh"] <= metrics["leftCh"] + 1
    assert metrics["rightSh"] <= metrics["rightCh"] + 1
    assert metrics["left"]["right"] <= metrics["plot"]["left"] + 1
    assert metrics["plot"]["right"] <= metrics["right"]["left"] + 1
    assert metrics["plot"]["width"] >= 600
    assert metrics["gate"] is False
    assert metrics["command"] and metrics["trackList"]
    assert not errors
    desktop.close()


def test_hidpi_4k_wall_display_scales_without_gate(page):
    context = page.context.browser.new_context(
        viewport={"width": 1280, "height": 630},
        screen={"width": 1280, "height": 720},
        device_scale_factor=3,
    )
    wall = context.new_page()
    try:
        wall.goto(f"file://{COP}?basemap=tac&seed=23")
        wall.wait_for_timeout(500)
        metrics = wall.evaluate(
            """() => {
              const root = document.querySelector('#cuas');
              const rect = root.getBoundingClientRect();
              return {
                gate: !!document.querySelector('.workstation-gate'),
                mode: root.dataset.displayMode,
                scale: Number(root.dataset.displayScale),
                display: root.dataset.displayEffective,
                viewport: root.dataset.viewportCss,
                rectWidth: rect.width,
                rectHeight: rect.height,
                scrollWidth: document.documentElement.scrollWidth,
                clientWidth: document.documentElement.clientWidth,
                scrollHeight: document.documentElement.scrollHeight,
                clientHeight: document.documentElement.clientHeight,
                bottom: [...document.querySelectorAll('.classbar')].at(-1).getBoundingClientRect().bottom,
                innerWidth,
                innerHeight,
              };
            }"""
        )
        assert metrics["gate"] is False
        assert metrics["mode"] == "scaled"
        assert metrics["display"] == "3840x2160"
        assert metrics["viewport"] == "1280x630"
        assert metrics["scale"] == pytest.approx(0.875, abs=0.001)
        assert metrics["rectWidth"] == pytest.approx(metrics["innerWidth"], abs=1)
        assert metrics["rectHeight"] == pytest.approx(metrics["innerHeight"], abs=1)
        assert metrics["scrollWidth"] <= metrics["clientWidth"] + 1
        assert metrics["scrollHeight"] <= metrics["clientHeight"] + 1
        assert metrics["bottom"] == pytest.approx(metrics["innerHeight"], abs=1)

        wall.set_viewport_size({"width": 600, "height": 340})
        wall.locator("#workstationGate").wait_for(state="visible")
        wall.set_viewport_size({"width": 1280, "height": 630})
        wall.locator("#workstationGate").wait_for(state="detached")
    finally:
        context.close()


def test_high_resolution_mobile_form_factor_remains_blocked(page):
    context = page.context.browser.new_context(
        viewport={"width": 480, "height": 800},
        screen={"width": 480, "height": 900},
        device_scale_factor=3,
        has_touch=True,
        is_mobile=True,
        user_agent=(
            "Mozilla/5.0 (Linux; Android 14; Mobile) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/149.0 Mobile Safari/537.36"
        ),
    )
    mobile = context.new_page()
    try:
        mobile.goto(f"file://{COP}?basemap=tac&seed=29")
        gate = mobile.locator("#workstationGate")
        gate.wait_for(state="visible")
        assert "phone or mobile-tablet" in gate.inner_text()
        effective = mobile.get_attribute("#cuas", "data-display-effective")
        effective_width, effective_height = (int(value) for value in effective.split("x"))
        assert effective_width >= 1280 and effective_height >= 720
    finally:
        context.close()


def test_scaled_canvas_hit_testing_uses_layout_coordinates(page):
    context = page.context.browser.new_context(
        viewport={"width": 1280, "height": 630},
        screen={"width": 1280, "height": 720},
        device_scale_factor=3,
    )
    scaled = context.new_page()
    try:
        scaled.goto(
            f"file://{COP}?debug=1&basemap=tac&seed=31&wx=CLEAR&tod=DAY"
        )
        scaled.wait_for_function("() => !!window.__CUAS__")
        target = scaled.evaluate(
            """() => {
              const C = window.__CUAS__;
              const canvas = document.querySelector('#plot');
              const rect = canvas.getBoundingClientRect();
              const tracks = [...C.S.tracks.values()]
                .filter(track => track.state !== 'NEUTRALIZED')
                .map(track => ({ id: track.trackId, ...C.plotPoint(track) }));
              const chosen = tracks
                .map(track => ({
                  ...track,
                  nearest: Math.min(...tracks.filter(other => other.id !== track.id)
                    .map(other => Math.hypot(track.x - other.x, track.y - other.y))),
                }))
                .sort((a, b) => b.nearest - a.nearest)[0];
              return {
                id: chosen.id,
                clientX: rect.left + chosen.x * (rect.width / canvas.clientWidth),
                clientY: rect.top + chosen.y * (rect.height / canvas.clientHeight),
              };
            }"""
        )
        scaled.mouse.click(target["clientX"], target["clientY"])
        assert scaled.evaluate("window.__CUAS__.S.selected") == target["id"]
    finally:
        context.close()


def test_no_page_scroll_and_no_console_errors(page):
    scroll = page.evaluate(
        "() => ({ sw: document.documentElement.scrollWidth, cw: document.documentElement.clientWidth,"
        " sh: document.documentElement.scrollHeight, ch: document.documentElement.clientHeight })"
    )
    assert scroll["sw"] <= scroll["cw"] + 1, "horizontal page scroll present"
    assert scroll["sh"] <= scroll["ch"] + 1, "vertical page scroll present"
    errors = getattr(page, "console_errors", [])
    assert not errors, f"console errors: {errors}"
