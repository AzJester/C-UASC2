"""Browser checks for the shared fictional evidence-review workspace.

Runs in the existing Chromium CI job. These tests exercise human coordination,
persistence and map presentation, not weapon or sensing performance.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import time
from urllib.request import urlopen

import pytest

pytest.importorskip("playwright.sync_api")
ROOT = Path(__file__).resolve().parents[1]


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
def service(tmp_path):
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    env = os.environ.copy()
    for key in ("EXERCISE_SETUP_TOKEN", "EXERCISE_TAK_CONFIG"):
        env.pop(key, None)
    env["EXERCISE_ALLOWED_HOSTS"] = "127.0.0.1,localhost,testserver"
    output = (tmp_path / "service.log").open("w", encoding="utf-8")
    process = subprocess.Popen(
        [sys.executable, str(ROOT / "scripts/run_exercise.py"), "--port", str(port),
         "--db", str(tmp_path / "exercise.sqlite3")],
        cwd=ROOT, env=env, stdout=output, stderr=subprocess.STDOUT,
    )
    base = f"http://127.0.0.1:{port}"
    try:
        for _ in range(100):
            try:
                with urlopen(base + "/api/exercise/health", timeout=0.3) as response:
                    if response.status == 200:
                        break
            except OSError:
                if process.poll() is not None:
                    pytest.fail("Exercise service failed to start")
                time.sleep(0.1)
        else:
            pytest.fail("Exercise service did not become ready")
        yield base
    finally:
        process.terminate()
        process.wait(timeout=10)
        output.close()


@pytest.fixture
def page(browser, service):
    context = browser.new_context(viewport={"width": 1280, "height": 720})
    context.route("https://server.arcgisonline.com/**", lambda route: route.abort())
    pg = context.new_page()
    errors = []
    pg.on("pageerror", lambda error: errors.append(str(error)))
    pg.goto(service + "/exercise.html")
    yield pg
    context.close()
    assert not errors


def create_room(page):
    page.click("#sessionButton")
    page.fill("#createRoomName", "Shared evidence proof")
    page.fill("#createParticipantName", "Morgan")
    page.click("#dialogSubmitButton")
    page.wait_for_function("document.querySelector('#participantName').textContent.includes('Morgan')")


def invite_participant(page, name="Alex", role="reviewer"):
    page.click("#inviteButton")
    page.fill("#inviteName", name)
    page.select_option("#inviteRole", role)
    page.click("#dialogSubmitButton")
    page.locator("#joinLink").wait_for(state="visible")
    link = page.input_value("#joinLink")
    page.keyboard.press("Escape")
    return link


def add_report(page):
    page.click("#newReportButton")
    page.fill("#reportTitle", "Training observation")
    page.fill("#reportSource", "Exercise observer A")
    page.fill("#reportText", "A fictional observation requiring a second review.")
    page.click("#dialogSubmitButton")
    page.locator(".report-item").filter(has_text="Training observation").click()


def test_shared_report_acknowledgment_resolution_and_refresh(page, service):
    create_room(page)
    link = invite_participant(page)
    reviewer = page.context.new_page()
    reviewer.goto(link)
    reviewer.wait_for_function("document.querySelector('#participantName').textContent.includes('Alex')")
    assert "invite=" not in reviewer.url
    add_report(page)
    reviewer.locator(".report-item").filter(has_text="Training observation").wait_for()
    page.click("#requestReviewButton")
    value = page.locator("#requestAssignee option").filter(has_text="Alex").first.get_attribute("value")
    page.select_option("#requestAssignee", value)
    page.fill("#requestSummary", "Please review the original observation.")
    page.click("#dialogSubmitButton")
    reviewer.locator('[data-request-action="ack"]').first.click()
    reviewer.locator('[data-request-action="resolve"]').first.click()
    reviewer.fill("#resolutionReason", "Reviewed; the evidence is insufficient for a firm identification.")
    reviewer.click("#dialogSubmitButton")
    page.wait_for_function("document.querySelector('#requestList').textContent.toLowerCase().includes('resolved')")
    page.reload()
    page.wait_for_function("document.querySelector('#participantName').textContent.includes('Morgan')")
    assert "Training observation" in page.locator("#reportList").inner_text()
    assert "resolved" in page.locator("#requestList").inner_text().lower()
    reviewer.close()


def test_corrections_preserve_history_and_export_has_no_credentials(page, tmp_path):
    create_room(page)
    add_report(page)
    page.click("#reviseReportButton")
    page.fill("#revisionText", "Corrected report after human clarification.")
    page.fill("#revisionReason", "The source corrected its initial description.")
    page.click("#dialogSubmitButton")
    with page.expect_download() as download:
        page.click("#exportButton")
    path = tmp_path / "record.json"
    download.value.save_as(path)
    record = json.loads(path.read_text())
    text = json.dumps(record)
    assert "Corrected report after human clarification." in text
    assert "A fictional observation requiring a second review." in text
    assert "The source corrected its initial description." in text
    for forbidden in ('"tokenHash"', '"inviteToken"', '"sessionToken"', '"privateKey"'):
        assert forbidden not in text


def test_observer_cannot_mutate_and_dialog_focus_survives_poll(page):
    create_room(page)
    link = invite_participant(page, "Taylor", "observer")
    observer = page.context.new_page()
    observer.goto(link)
    observer.wait_for_function("document.querySelector('#participantName').textContent.includes('Taylor')")
    assert observer.locator("#newReportButton").is_disabled()
    assert observer.locator("#inviteButton").is_disabled()
    assert observer.locator("#clockButton").is_disabled()
    page.click("#newReportButton")
    with page.expect_response("**/api/exercise/state"):
        page.fill("#reportText", "Draft remains while the shared state refreshes.")
    assert page.input_value("#reportText") == "Draft remains while the shared state refreshes."
    assert page.evaluate("document.activeElement.id") == "reportText"
    page.keyboard.press("Escape")
    assert page.evaluate("document.activeElement.id") == "newReportButton"
    observer.close()


@pytest.mark.parametrize("width,height", [(1280, 720), (1920, 1080)])
def test_desktop_layout_and_terrain_default(page, width, height):
    page.set_viewport_size({"width": width, "height": height})
    create_room(page)
    add_report(page)
    assert page.input_value("#basemapSelect") == "imagery"
    assert page.evaluate("document.documentElement.scrollWidth <= window.innerWidth")
    assert page.evaluate("document.documentElement.scrollHeight <= window.innerHeight + 1")
    for selector in ("#sessionButton", "#newReportButton", "#replayButton", "#takDetailsButton"):
        box = page.locator(selector).bounding_box()
        assert box and box["y"] >= 0 and box["y"] + box["height"] <= height


def test_cop_entry_opens_shared_workspace(page, service):
    page.goto(service + "/index.html?basemap=tac")
    page.click("#btnExerciseRoom")
    page.wait_for_url("**/exercise.html")
    assert page.locator("#exerciseApp").is_visible()
