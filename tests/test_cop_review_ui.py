"""Desktop evidence, inventory, export, and accessibility regressions.

These checks exercise review/presentation behavior, not operational performance.
"""
from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

pytest.importorskip("playwright.sync_api")
COP = Path(__file__).resolve().parents[1] / "services/c2-core/app/static/cop.html"


@pytest.fixture(scope="module")
def browser():
    from playwright.sync_api import sync_playwright

    with sync_playwright() as pw:
        try:
            instance = pw.chromium.launch(executable_path=os.environ.get("COP_SMOKE_CHROMIUM") or None)
        except Exception as exc:
            pytest.skip(f"Chromium unavailable: {exc}")
        yield instance
        instance.close()


@pytest.fixture
def page(browser):
    context = browser.new_context(viewport={"width": 1280, "height": 720})
    pg = context.new_page()
    errors = []
    pg.on("pageerror", lambda error: errors.append(str(error)))
    pg.goto(f"file://{COP}?debug=1&basemap=tac&seed=42&wx=CLEAR&tod=DAY")
    pg.wait_for_function("!!window.__CUAS__")
    pg.evaluate("window.__CUAS__.setPaused(true)")
    yield pg
    context.close()
    assert not errors


def select_unknown(page):
    return page.evaluate("""() => {
      const C = window.__CUAS__, t = [...C.S.tracks.values()].find(t => t.identity === 'UNKNOWN');
      C.selectTrack(t.trackId); return t.trackId;
    }""")


def test_operator_evidence_excludes_instructor_intent(page):
    select_unknown(page)
    page.click("#btnDecisionDetails")
    detail = page.locator("#decisionDetailBody").inner_text()
    assert "Not established by these reports" in detail
    assert "FLIGHT-PROFILE ASSESSMENT" in detail
    labels = page.locator("#decisionDetailBody dt").all_text_contents()
    assert "Assigned objective" not in labels
    assert "Scripted intent" not in labels
    assert "Not calibrated or independently validated" in detail
    assert page.locator("#decisionDetailBody dd").first.evaluate("e => getComputedStyle(e).color") != "rgb(0, 0, 0)"
    page.keyboard.press("Escape")
    page.click("#btnExercise")
    page.click('[data-review="instructor"]')
    assert "INSTRUCTOR TRUTH" in page.locator("#reviewBody").inner_text()
    instructor_labels = page.locator("#reviewBody dt").all_text_contents()
    assert "Scripted intent" in instructor_labels
    assert "Assigned objective" in instructor_labels


def test_complete_inventory_search_and_system_details(page):
    total = page.evaluate("window.__CUAS__.currentScenario().sensors.length + window.__CUAS__.S.effectors.length")
    page.click("#btnInventory")
    assert page.locator("#inventoryRows tr").count() == total
    page.fill("#inventorySearch", "SEN-AIR-RAD")
    assert page.locator("#inventoryRows button").count() == 1
    page.locator("#inventoryRows button").click()
    assert page.locator("#reviewBack").is_hidden()
    assert "SEN-AIR-RAD" in page.locator("#trackBody").inner_text()
    assert page.locator("#btnSystemDetails").is_visible()
    page.click("#btnSystemDetails")
    assert "full status" in page.locator("#decisionDialogTitle").inner_text()
    assert "LOCAL MODEL / NO TRANSPORT TEST" in page.locator("#decisionDetailBody").inner_text()


def test_review_modal_escape_restores_focus(page):
    page.click("#btnInventory")
    page.keyboard.press("Escape")
    page.wait_for_function("document.activeElement.id === 'btnInventory'")
    assert page.locator("#reviewBack").is_hidden()


def test_aar_is_a_fixed_snapshot_and_end_accounts_for_unresolved_tracks(page):
    page.click("#wsAar")
    assert page.locator("#aarVerdict").inner_text().startswith("INTERIM REVIEW")
    first = page.locator("#aarRecord").inner_text()
    page.evaluate("window.__CUAS__.S.simTimeMs += 30000")
    assert page.locator("#aarRecord").inner_text() == first
    page.keyboard.press("Escape")
    page.click("#btnExercise")
    page.click("#btnEndRun")
    assert "REVIEW INCOMPLETE" in page.locator("#aarVerdict").inner_text()
    assert page.evaluate("window.__CUAS__.S.paused") is True
    assert "ENDED" in page.locator("#missionSummary").inner_text()


def test_focused_control_does_not_freeze_freshness(page):
    track_id = select_unknown(page)
    page.evaluate("""id => {
      const C = window.__CUAS__, t = C.S.tracks.get(id);
      t.lastUpdate = t.syncT = t.displayObservedAt = C.S.simTimeMs;
    }""", track_id)
    page.focus("#btnTask")
    page.evaluate("window.__CUAS__.S.simTimeMs += 10000")
    page.wait_for_function("document.querySelector('#trackBody').textContent.includes('10 s')")
    assert page.evaluate("document.activeElement.id") == "btnTask"


def test_case_records_reports_reason_and_no_command_in_export(page, tmp_path):
    page.click("#btnExercise")
    page.click('[data-review="case"]')
    page.click('[data-case-action="start"]')
    assert "No human review decisions recorded" in page.locator("#reviewBody").inner_text()
    page.click('[data-case-action="next"]')
    page.click('[data-case-action="next"]')
    assert "Delivery interruption" in page.locator("#reviewBody").inner_text()
    assert "60 s" in page.locator("#caseReports").inner_text()
    page.fill("#caseReason", "Reports conflict; wait for clarified evidence and coordination.")
    page.click('[data-case-action="next"]')
    assert page.input_value("#caseReason").startswith("Reports conflict")
    assert "likely biological" in page.locator("#caseReports").inner_text()
    page.click('[data-case-decision="Record no action"]')
    page.click('[data-case-action="next"]')
    page.click('[data-case-action="next"]')
    assert "ENDED" in page.locator("#caseStatus").inner_text()
    assert "5:00" in page.locator("#caseStatus").inner_text()
    with page.expect_download() as result:
        page.click('[data-case-export="1"]')
    destination = tmp_path / "review.json"
    result.value.save_as(destination)
    record = json.loads(destination.read_text())
    assert record["metadata"]["build"]
    assert record["metadata"]["initial"]["rngState"] is not None
    assert record["caseStudy"]["decisions"][0]["reason"].startswith("Reports conflict")
    assert len(record["caseStudy"]["reports"]) == 4
    assert len(record["caseStudy"]["checkpoints"]) == 6
    assert record["metrics"]["engagements"] == 0
    assert record["metadata"]["coordination"]["airspace"] == "Not requested"


@pytest.mark.parametrize("workspace", ["COP", "SENSORS", "FIRES", "SUPERVISOR"])
@pytest.mark.parametrize("width,height", [(1280, 720), (1920, 1080)])
def test_desktop_glance_cards_do_not_clip_controls(page, workspace, width, height):
    page.set_viewport_size({"width": width, "height": height})
    # Chromium exposes the new innerHeight before the app's resize event has
    # refitted its explicitly sized root. Wait for that fit, otherwise the
    # tall-screen inventory can be rendered into the previous 720px layout.
    page.wait_for_function(
        "size => document.querySelector('#cuas').dataset.viewportCss === size",
        arg=f"{width}x{height}",
    )
    select_unknown(page)
    page.evaluate("value => window.__CUAS__.setWorkspace(value)", workspace)
    clipped = page.evaluate("""() => [...document.querySelectorAll('.rail-scroll > .card')]
      .filter(e => e.offsetParent && e.scrollHeight > e.clientHeight + 2)
      .map(e => ({id:e.id, height:e.clientHeight, content:e.scrollHeight}))""")
    assert not clipped


def test_tour_uses_readable_text_and_keyboard_focus(page):
    page.evaluate("window.__CUAS__.startTour()")
    assert page.locator("#tourTitle").evaluate("e => getComputedStyle(e).color") == "rgb(231, 238, 247)"
    page.keyboard.press("Escape")
    assert page.locator("#tourTitle").count() == 0
