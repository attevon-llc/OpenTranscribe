"""E2E: the upload tray stages and the processing notification card (#1335).

Neither journey may start real processing: the dev workers share a GPU and a real upload
would create a library file this suite then has to hunt down. So:

* the upload tray is driven through the real stepper, with ``POST /api/files/prepare``
  answered by ``page.route`` as a *duplicate* of an existing file. The browser still
  hashes the file, queues it, runs the real ``uploadService`` state machine and renders the
  real tray, but the server never receives a file;
* the notification card is seeded through the same ``notifications`` localStorage record the
  app itself persists, with one processing, one finishing and one failed transcription.
  The card model derives its state from those records exactly as it does for live frames.

Each stage assertion reads the row's ``data-status`` / the card's state text, and the stub
that produces a stage is withdrawn or changed to show the next one, so a tray that never
leaves ``preparing`` (or a card that always says "Processing") fails.
"""

from __future__ import annotations

import contextlib
import json
import uuid
from datetime import UTC
from datetime import datetime
from pathlib import Path
from typing import Any

import pytest
from playwright.sync_api import Error as PlaywrightError
from playwright.sync_api import Page
from playwright.sync_api import Route
from playwright.sync_api import expect
from timeouts import APP_SHELL_READY_MS

pytestmark = pytest.mark.upload

RETENTION_KEY = "opentr:notificationRetention"
ROW = ".upload-item"


@pytest.fixture
def tray_page(browser: Any, shared_auth_state: str, base_url: str) -> Any:
    context = browser.new_context(
        storage_state=shared_auth_state,
        viewport={"width": 1920, "height": 1080},
        ignore_https_errors=True,
    )
    page = context.new_page()
    page._held_routes = []  # type: ignore[attr-defined]
    page.goto(base_url)
    page.wait_for_selector(".gallery-action-buttons", timeout=APP_SHELL_READY_MS)
    yield page
    # A parked request must be released before anything tears routes down:
    # `unroute_all` waits on a route handler that never finished, and never returns.
    for route in page._held_routes:  # type: ignore[attr-defined]
        with contextlib.suppress(PlaywrightError):
            route.abort()
    page.close()
    context.close()


def _submit_upload(page: Page, audio: Path) -> None:
    page.click(".upload-btn")
    page.set_input_files("#drop-zone input[type=file]", str(audio))
    expect(page.locator(".selected-file .file-name")).to_have_text(audio.name, timeout=10000)
    page.click(".nav-btn.nav-next")
    page.wait_for_selector(".nav-btn.nav-review-defaults", timeout=10000)
    page.click(".nav-btn.nav-review-defaults")
    submit = page.locator(".nav-btn.nav-submit")
    expect(submit).to_be_enabled(timeout=10000)
    submit.click()


def _duplicate(route: Route) -> None:
    route.fulfill(json={"file_id": str(uuid.uuid4()), "is_duplicate": True})


def _status(page: Page) -> Any:
    return page.locator(f"{ROW} .upload-icon")


class TestUploadTray:
    def test_a_held_upload_shows_preparing_then_completes(
        self, tray_page: Page, sample_audio: Path
    ) -> None:
        held: list[Route] = tray_page._held_routes  # type: ignore[attr-defined]
        tray_page.route("**/api/files/prepare", lambda route: held.append(route))
        _submit_upload(tray_page, sample_audio)

        expect(_status(tray_page)).to_have_attribute("data-status", "preparing", timeout=15000)
        expect(tray_page.locator(f"{ROW} .upload-cancel-btn")).to_be_visible()
        expect(tray_page.locator(f"{ROW} .upload-name")).to_have_text(sample_audio.name)
        assert len(held) == 1  # the request is genuinely parked, not raced

        _duplicate(held[0])
        expect(_status(tray_page)).to_have_attribute("data-status", "completed", timeout=15000)
        expect(tray_page.locator(f"{ROW} .upload-remove-btn")).to_be_visible()
        expect(tray_page.locator(f"{ROW} .upload-cancel-btn")).to_have_count(0)

    def test_a_refused_upload_shows_failed_with_the_reason(
        self, tray_page: Page, sample_audio: Path
    ) -> None:
        tray_page.route(
            "**/api/files/prepare",
            lambda route: route.fulfill(status=400, json={"detail": "stub refusal e2e"}),
        )
        _submit_upload(tray_page, sample_audio)

        expect(_status(tray_page)).to_have_attribute("data-status", "failed", timeout=20000)
        expect(tray_page.locator(f"{ROW} .error-message")).to_be_visible()
        expect(tray_page.locator(f"{ROW} .upload-retry-btn")).to_be_visible()

        # Withdraw the refusal: the retry (manual or the automatic one) now succeeds.
        tray_page.unroute("**/api/files/prepare")
        tray_page.route("**/api/files/prepare", _duplicate)
        retry = tray_page.locator(f"{ROW} .upload-retry-btn")
        if retry.count():
            retry.click()
        expect(_status(tray_page)).to_have_attribute("data-status", "completed", timeout=30000)

    def test_cancelling_a_held_upload_ends_in_cancelled(
        self, tray_page: Page, sample_audio: Path
    ) -> None:
        held: list[Route] = tray_page._held_routes  # type: ignore[attr-defined]
        tray_page.route("**/api/files/prepare", lambda route: held.append(route))
        _submit_upload(tray_page, sample_audio)
        expect(_status(tray_page)).to_have_attribute("data-status", "preparing", timeout=15000)

        tray_page.locator(f"{ROW} .upload-cancel-btn").click()
        expect(_status(tray_page)).to_have_attribute("data-status", "cancelled", timeout=15000)
        assert held[0].request.method == "POST"


def _record(file_id: str, filename: str, status: str, progress: int, stage: str) -> dict[str, Any]:
    return {
        "id": uuid.uuid4().hex,
        "type": "transcription_status",
        "title": "Transcription",
        "message": f"{status} {filename}",
        "timestamp": datetime.now(UTC).isoformat(),
        "read": False,
        "data": {
            "file_id": file_id,
            "filename": filename,
            "status": status,
            "progress": progress,
            "stage": stage,
        },
    }


@pytest.fixture
def card_page(browser: Any, shared_auth_state: str, base_url: str) -> Any:
    names = {
        "processing": f"e2e-card-processing-{uuid.uuid4().hex[:6]}.wav",
        "finishing": f"e2e-card-finishing-{uuid.uuid4().hex[:6]}.wav",
        "failed": f"e2e-card-failed-{uuid.uuid4().hex[:6]}.wav",
    }
    records = [
        _record("910001", names["processing"], "processing", 40, "transcribe"),
        _record("910002", names["finishing"], "completed", 100, "finalize"),
        _record("910003", names["failed"], "failed", 20, "transcribe"),
    ]
    context = browser.new_context(
        storage_state=shared_auth_state,
        viewport={"width": 1920, "height": 1080},
        ignore_https_errors=True,
    )
    # Seeded on every navigation, so the panel has rows after a reload too (the retention
    # control is part of the populated panel).
    context.add_init_script(
        f"localStorage.setItem('notifications', {json.dumps(json.dumps(records))});"
    )
    page = context.new_page()
    page._card_names = names  # type: ignore[attr-defined]
    page.goto(base_url)
    page.wait_for_selector(".gallery-action-buttons", timeout=APP_SHELL_READY_MS)
    yield page
    page.close()
    context.close()


class TestNotificationCard:
    def test_card_states_follow_the_records(self, card_page: Page) -> None:
        names = card_page._card_names  # type: ignore[attr-defined]
        card_page.locator(".notifications-btn").click()
        panel = card_page.locator(".notifications-panel")
        expect(panel).to_be_visible(timeout=15000)

        def state_of(key: str) -> Any:
            card = panel.locator(".processing-job", has_text=names[key])
            expect(card).to_have_count(1, timeout=15000)
            return card.locator(".job-state")

        expect(state_of("processing")).to_have_text("Processing")
        expect(state_of("finishing")).to_have_text("Finishing up")
        expect(state_of("failed")).to_have_text("Needs attention")
        # One card per file, however many records fold into it.
        expect(panel.locator(".processing-job")).to_have_count(3)

    def test_retention_choice_persists_across_reload(self, card_page: Page) -> None:
        card_page.locator(".notifications-btn").click()
        select = card_page.locator(".retention-setting select")
        expect(select).to_be_visible(timeout=15000)
        assert select.input_value() == "read24h"  # the default, so the change below is real

        select.select_option("never")
        assert card_page.evaluate(f"localStorage.getItem('{RETENTION_KEY}')") == "never"
        try:
            card_page.reload()
            card_page.wait_for_selector(".gallery-action-buttons", timeout=APP_SHELL_READY_MS)
            card_page.locator(".notifications-btn").click()
            expect(card_page.locator(".retention-setting select")).to_have_value("never")
        finally:
            card_page.evaluate(f"localStorage.removeItem('{RETENTION_KEY}')")
