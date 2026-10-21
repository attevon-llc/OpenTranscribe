"""
E2E: Settings > Transcription > Provider > Local models (issue #1246, WP5).

Every route the library uses is fulfilled in the browser with scripted responses, so nothing
reaches the backend for them: no download starts, no network, no cache or worker change, no dev
data touched. The real downloader (subprocess, Hugging Face protocol, cancel) is covered by the
backend supervisor tests; the live stack by the manual check in the plan's section 9.

Scripted routes (a fixed-sentence UI must work from the error *code* alone):
    GET  /api/admin/local-models                 catalog + the download state of the script
    POST /api/admin/local-models/download        202 queued
    POST /api/admin/local-models/download/cancel 200 downloading (the backend's real reply)
    POST /api/asr-settings/local-model/set       200
    POST /api/asr-settings/local-model/restart   200 with the worker names and the request time
    GET  /api/asr-settings/local-model/active    loaded models, ready after the restart

The library polls every 3 s while a download runs, so a scripted download finishes in ~7 s.

Requirements: dev environment running, super_admin ``admin@example.com`` / ``password``.

Run:
    pytest backend/tests/e2e/test_local_models.py -v
    DISPLAY=:11 pytest backend/tests/e2e/test_local_models.py -v --headed
"""

from __future__ import annotations

import json
import re
from datetime import UTC
from datetime import datetime
from datetime import timedelta
from typing import Any

import pytest
from playwright.sync_api import BrowserContext
from playwright.sync_api import Page
from playwright.sync_api import Route
from playwright.sync_api import expect

pytestmark = pytest.mark.settings

GB = 1_000_000_000


def _model(model_id: str, **over: Any) -> dict[str, Any]:
    base = {
        "id": model_id,
        "display_name": model_id,
        "repo_id": f"Systran/faster-whisper-{model_id}",
        "approx_bytes": 3 * GB,
        "vram_hint_gb": 10,
        "language_support": "multilingual",
        "supports_translation": True,
        "is_default": False,
        "downloaded": False,
        "partial": False,
        "present_bytes": 0,
        "is_active": False,
    }
    base.update(over)
    return base


class ScriptedBackend:
    """The six library routes, with the request order recorded for assertions."""

    def __init__(self) -> None:
        self.large_downloaded = False
        self.active = "tiny"
        self.download: dict[str, Any] | None = None
        self.polls_since_start = 0
        self.cancelled = False
        self.restart_at: datetime | None = None
        self.calls: list[str] = []

    def catalog(self) -> dict[str, Any]:
        if self.download and self.download["status"] == "downloading":
            self.polls_since_start += 1
            if self.cancelled:
                self.download = {**self.download, "status": "cancelled"}
            elif self.polls_since_start >= 2:
                self.large_downloaded = True
                self.download = {**self.download, "status": "completed", "percent": 100.0}
        return {
            "models": [
                _model(
                    "tiny",
                    approx_bytes=75_000_000,
                    vram_hint_gb=1,
                    downloaded=True,
                    is_active=self.active == "tiny",
                ),
                _model(
                    "large-v3",
                    downloaded=self.large_downloaded,
                    is_active=self.active == "large-v3",
                ),
                _model("medium", approx_bytes=int(1.5 * GB), vram_hint_gb=5),
            ],
            "download": self.download,
            "free_bytes": 120 * GB,
            "cache_writable": True,
            "active_model": self.active,
        }

    def start(self) -> dict[str, Any]:
        self.polls_since_start = 0
        self.cancelled = False
        self.download = self._state("queued", 0, None)
        return self.download

    def _state(self, status: str, done: int, percent: float | None) -> dict[str, Any]:
        return {
            "model_id": "large-v3",
            "status": status,
            "bytes_done": done,
            "bytes_total": 3 * GB,
            "percent": percent,
            "error_code": None,
            "updated_at": datetime.now(UTC).isoformat(),
        }


def _json(route: Route, body: dict[str, Any], status: int = 200) -> None:
    route.fulfill(status=status, content_type="application/json", body=json.dumps(body))


def _install(context: BrowserContext, script: ScriptedBackend) -> None:
    def catalog(route: Route) -> None:
        script.calls.append("GET catalog")
        body = script.catalog()
        if script.download and script.download["status"] == "queued":
            script.download = script._state("downloading", int(1.2 * GB), 40.0)
            body["download"] = script.download
        _json(route, body)

    def start(route: Route) -> None:
        script.calls.append("POST download")
        assert json.loads(route.request.post_data or "{}") == {"model_id": "large-v3"}
        _json(route, script.start(), 202)

    def cancel(route: Route) -> None:
        script.calls.append("POST cancel")
        script.cancelled = True
        _json(route, script._state("downloading", int(1.2 * GB), 40.0))

    def set_model(route: Route) -> None:
        script.calls.append("POST set")
        script.active = json.loads(route.request.post_data or "{}")["model_name"]
        _json(route, {"model_name": script.active, "message": "ok"})

    def restart(route: Route) -> None:
        script.calls.append("POST restart")
        script.restart_at = datetime.now(UTC)
        _json(
            route,
            {
                "status": "restart_signaled",
                "model": script.active,
                "workers": ["gpu-transcription@e2e"],
                "active_tasks": 2,
                "restart_requested_at": script.restart_at.isoformat(),
                "message": "scripted",
            },
        )

    def active(route: Route) -> None:
        loaded = []
        if script.restart_at is not None:
            ready = script.restart_at + timedelta(seconds=30)
            loaded = [
                {
                    "worker": "gpu-transcription@e2e",
                    "model": script.active,
                    "ready_at": ready.isoformat(),
                }
            ]
        _json(
            route,
            {
                "active_model": script.active,
                "source": "database",
                "available_models": [],
                "model_info": {
                    "display_name": script.active,
                    "description": "",
                    "supports_translation": True,
                    "supports_diarization": True,
                    "language_support": "multilingual",
                },
                "loaded_models": loaded,
            },
        )

    # Exact-path globs: `**/local-models` does not match `/local-models/download`.
    context.route(re.compile(r".*/api/admin/local-models$"), catalog)
    context.route(re.compile(r".*/api/admin/local-models/download$"), start)
    context.route(re.compile(r".*/api/admin/local-models/download/cancel$"), cancel)
    context.route(re.compile(r".*/api/asr-settings/local-model/set$"), set_model)
    context.route(re.compile(r".*/api/asr-settings/local-model/restart$"), restart)
    context.route(re.compile(r".*/api/asr-settings/local-model/active$"), active)


@pytest.fixture
def library_page(browser, shared_auth_state: str, base_url: str):  # type: ignore[no-untyped-def]
    """A logged-in page with the library routes scripted; yields (page, script)."""
    context = browser.new_context(
        storage_state=shared_auth_state,
        viewport={"width": 1600, "height": 1200},
        ignore_https_errors=True,
    )
    script = ScriptedBackend()
    _install(context, script)
    page = context.new_page()
    page.goto(base_url)
    page.wait_for_selector(".user-button", timeout=30000)
    yield page, script
    context.close()


def _open_library(page: Page) -> None:
    page.locator(".user-button").click()
    page.locator(".dropdown-menu .dropdown-item", has_text="Settings").first.click()
    expect(page.locator(".settings-modal")).to_be_visible(timeout=10000)
    page.locator(".settings-sidebar .nav-item", has_text=re.compile("^Transcription")).first.click()
    page.locator("#tab-tx-provider").click()
    expect(page.get_by_test_id("local-model-library")).to_be_visible(timeout=15000)
    expect(page.get_by_test_id("local-model-large-v3")).to_be_visible(timeout=10000)


def test_download_then_activate_a_model(library_page) -> None:  # type: ignore[no-untyped-def]
    """Download shows progress and completes; activating warns, sets, restarts, then confirms."""
    page, script = library_page
    _open_library(page)
    row = page.get_by_test_id("local-model-large-v3")

    # Not on disk: Download is offered and Set active is disabled; the active model has neither.
    expect(row.get_by_role("button", name=re.compile("^Set active"))).to_be_disabled()
    expect(
        page.get_by_test_id("local-model-tiny").get_by_role(
            "button", name=re.compile("^Set active")
        )
    ).to_have_count(0)

    row.get_by_role("button", name=re.compile("^Download")).click()
    bar = row.get_by_role("progressbar")
    expect(bar).to_be_visible(timeout=10000)
    expect(bar).to_have_attribute("aria-valuenow", "40")
    expect(row.get_by_role("button", name="Cancel download")).to_be_visible()
    # One download at a time: another model cannot start one meanwhile.
    expect(
        page.get_by_test_id("local-model-medium").get_by_role(
            "button", name=re.compile("^Download")
        )
    ).to_be_disabled()

    # The scripted download finishes on the next polls; the row becomes activatable.
    activate = row.get_by_role("button", name=re.compile("^Set active"))
    expect(activate).to_be_enabled(timeout=20000)
    assert "POST set" not in script.calls

    activate.click()
    dialog = page.get_by_role("dialog", name="Switch transcription model")
    expect(dialog).to_contain_text("restarts the transcription worker")
    expect(dialog).to_contain_text("Jobs that are running finish first")
    assert "POST set" not in script.calls  # nothing happens before the explicit confirmation
    dialog.get_by_role("button", name="Restart worker and switch").click()

    expect(page.get_by_text("Jobs running now, which finish first: 2")).to_be_visible(timeout=10000)
    expect(page.get_by_text("Running large-v3")).to_be_visible(timeout=20000)
    assert script.calls.index("POST set") < script.calls.index("POST restart")


def test_cancel_a_running_download(library_page) -> None:  # type: ignore[no-untyped-def]
    """Cancel is sent once and the row goes back to Download when the cancelled state arrives."""
    page, script = library_page
    _open_library(page)
    row = page.get_by_test_id("local-model-large-v3")

    row.get_by_role("button", name=re.compile("^Download")).click()
    cancel = row.get_by_role("button", name="Cancel download")
    expect(cancel).to_be_visible(timeout=10000)
    cancel.click()

    expect(row.get_by_role("button", name=re.compile("^Download"))).to_be_visible(timeout=15000)
    expect(row.get_by_role("progressbar")).to_have_count(0)
    assert script.calls.count("POST cancel") == 1
