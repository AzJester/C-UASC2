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
        pg.goto(f"file://{COP}?debug=1")
        pg.wait_for_timeout(500)
        yield pg
        browser.close()


def test_cop_boots_clean(page):
    assert COP.exists(), "cop.html missing"
    # debug hook is exposed and the core controls are present
    assert page.eval_on_selector("#cuas", "el => !!el")
    for ctrl in ("#btnAuto", "#btnPause", "#btnHelp", "#spdSeg", "#btnRaid", "#scoreboard", "#log"):
        assert page.query_selector(ctrl) is not None, f"missing {ctrl}"
    api = page.evaluate("() => Object.keys(window.__CUAS__ || {})")
    assert "S" in api and "resetScenario" in api, f"debug hook incomplete: {api}"


def test_raid_runs_and_defeats_threats(page):
    page.click("#btnRaid")
    page.wait_for_timeout(5000)
    stats = page.evaluate(
        "() => ({ tracks: window.__CUAS__.S.tracks.size,"
        " defeated: window.__CUAS__.S.stats.defeated,"
        " engagements: window.__CUAS__.S.stats.engagements,"
        " integrity: window.__CUAS__.S.assetIntegrity })"
    )
    assert stats["tracks"] > 0, "no tracks after raid"
    assert stats["engagements"] > 0, "no engagements ran"
    assert stats["defeated"] > 0, "nothing defeated"
    assert 0 <= stats["integrity"] <= 100


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


def test_no_page_scroll_and_no_console_errors(page):
    scroll = page.evaluate(
        "() => ({ sw: document.documentElement.scrollWidth, cw: document.documentElement.clientWidth,"
        " sh: document.documentElement.scrollHeight, ch: document.documentElement.clientHeight })"
    )
    assert scroll["sw"] <= scroll["cw"] + 1, "horizontal page scroll present"
    assert scroll["sh"] <= scroll["ch"] + 1, "vertical page scroll present"
    errors = getattr(page, "console_errors", [])
    assert not errors, f"console errors: {errors}"
