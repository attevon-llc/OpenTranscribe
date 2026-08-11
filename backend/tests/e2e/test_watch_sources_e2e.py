"""E2E smoke test for the Watch Sources settings panel (issue #26).

Opens Settings → Watch Sources, asserts the panel renders, opens the Add Watch
Source editor, switches its tabs, and closes it — all without new console errors.

Requirements:
- Dev environment running with the watch overlay: ./opentr.sh start dev --with-watch
- Frontend at localhost:5173 (admin@example.com / password)

Run (headless):
    pytest backend/tests/e2e/test_watch_sources_e2e.py -v
Run (visible on XRDP):
    DISPLAY=:11 pytest backend/tests/e2e/test_watch_sources_e2e.py -v --headed
"""

from __future__ import annotations

import os
import tempfile

import pytest
import requests
from playwright.sync_api import Page
from playwright.sync_api import expect

# This module used to define its own ``FRONTEND_URL``/``BACKEND_URL`` constants here.
# A module constant is evaluated at import time, so it could not see ``--base-url`` /
# ``--backend-url`` and this file always drove whatever was on the default ports — even
# when the run was aimed at an isolated stack (issue #431). Everything below takes
# conftest's ``base_url`` / ``backend_url`` fixtures instead.
TEST_ADMIN_EMAIL = os.environ.get("E2E_ADMIN_EMAIL", "admin@example.com")
TEST_ADMIN_PASSWORD = os.environ.get("E2E_ADMIN_PASSWORD", "password")  # noqa: S105


@pytest.fixture(scope="module")
def local_watch_capability(backend_url: str) -> bool:
    """Whether the stack exposes the local-folder watch capability.

    The stepper defaults to a *local* source when the watch overlay is up
    (./opentr.sh start dev --with-watch mounts WATCH_HOST_PATH). Without it
    the form defaults to S3 (bucket+keys required), so the name-only stepper
    walkthroughs cannot advance — that's environment, not a bug.
    """
    try:
        resp = requests.post(
            f"{backend_url}/api/auth/token",
            data={"username": TEST_ADMIN_EMAIL, "password": TEST_ADMIN_PASSWORD},
            timeout=15,
        )
        token = resp.json().get("access_token")
        caps = requests.get(
            f"{backend_url}/api/watch-sources/capabilities",
            headers={"Authorization": f"Bearer {token}"},
            timeout=15,
        ).json()
        return bool(caps.get("local_enabled"))
    except Exception:
        return False


@pytest.fixture
def require_local_watch(local_watch_capability: bool) -> None:
    if not local_watch_capability:
        pytest.skip(
            "local watch capability not enabled — start the stack with "
            "./opentr.sh start dev --with-watch"
        )


BENIGN_CONSOLE_SUBSTRINGS = (
    "Failed to load resource",
    "status code 401",
    "Failed to fetch user info",
    "/suggestions",
    "favicon",
    "401 (Unauthorized)",
    "404 (Not Found)",
)


def _unexpected_console_errors(errors: list[str]) -> list[str]:
    return [e for e in errors if not any(sub in e for sub in BENIGN_CONSOLE_SUBSTRINGS)]


def _form_login_with_retry(page, base_url: str, attempts: int = 4) -> None:
    last_error: Exception | None = None
    for attempt in range(attempts):
        try:
            page.goto(base_url)
            if page.locator(".user-button").count():
                page.wait_for_selector(".user-button", timeout=10000)
                return
            page.wait_for_selector("#email", timeout=15000)
            page.fill("#email", TEST_ADMIN_EMAIL)
            page.fill("#password", TEST_ADMIN_PASSWORD)
            page.click("button[type=submit]")
            page.wait_for_selector(".user-button", timeout=20000)
            return
        except Exception as exc:  # noqa: BLE001
            last_error = exc
            # Kept deliberately: this wait IS the rate-limit backoff, not a settle for
            # something a locator could poll for (issue #431).
            page.wait_for_timeout(5000 * (attempt + 1))
    raise AssertionError(f"Could not log in via form after {attempts} attempts: {last_error}")


@pytest.fixture(scope="module")
def auth_storage_state(browser, base_url: str):  # type: ignore[no-untyped-def]
    context = browser.new_context(
        viewport={"width": 1920, "height": 1080}, ignore_https_errors=True
    )
    page = context.new_page()
    _form_login_with_retry(page, base_url)
    fd, state_file = tempfile.mkstemp(suffix=".json")
    os.close(fd)
    context.storage_state(path=state_file)
    page.close()
    context.close()
    yield state_file
    if os.path.exists(state_file):
        os.unlink(state_file)


@pytest.fixture
def app_page(browser, auth_storage_state: str, base_url: str):  # type: ignore[no-untyped-def]
    context = browser.new_context(
        storage_state=auth_storage_state,
        viewport={"width": 1920, "height": 1080},
        ignore_https_errors=True,
    )
    page = context.new_page()
    errors: list[str] = []
    page.on("console", lambda m: errors.append(m.text) if m.type == "error" else None)
    page._console_errors = errors  # type: ignore[attr-defined]
    page.goto(base_url)
    page.wait_for_selector(".user-button", timeout=30000)
    yield page
    page.close()
    context.close()


def _open_watch_sources(page: Page) -> None:
    page.locator(".user-button").click()
    settings_item = page.locator(".dropdown-menu .dropdown-item", has_text="Settings")
    expect(settings_item.first).to_be_visible(timeout=5000)
    settings_item.first.click()
    expect(page.locator(".settings-modal")).to_be_visible(timeout=10000)
    nav = page.locator(".settings-sidebar .nav-item", has_text="Watch Sources")
    expect(nav.first).to_be_visible(timeout=8000)
    nav.first.click()


class TestWatchSourcesPanel:
    def test_panel_renders(self, app_page: Page) -> None:
        _open_watch_sources(app_page)
        title = app_page.locator(".settings-content .section-title").first
        expect(title).to_contain_text("Watch Sources", timeout=8000)
        # The "Add Watch Source" button should be present.
        expect(app_page.get_by_role("button", name="Add Watch Source")).to_be_visible(timeout=8000)

    def test_stepper_walks_all_steps(self, app_page: Page, require_local_watch: None) -> None:
        """The Add editor is a guided stepper: Next walks every step to Save."""
        _open_watch_sources(app_page)
        app_page.get_by_role("button", name="Add Watch Source").click()
        expect(app_page.get_by_text("Add Watch Source").first).to_be_visible(timeout=8000)

        dialog = app_page.locator(".modal-container")
        # Name is required before the connection step can advance.
        app_page.fill("#ws-name", "E2E Stepper Source")

        # Walk Connection → Processing → Advanced → Organize via Next.
        for _ in range(3):
            nxt = dialog.get_by_role("button", name="Next", exact=True)
            expect(nxt).to_be_enabled(timeout=5000)
            nxt.click()
            # Kept deliberately: every step renders its OWN enabled "Next", so the
            # expect() at the top of the next iteration cannot tell "this step advanced"
            # from "the step hasn't re-rendered yet" — dropping the settle risks clicking
            # one step twice and never reaching Save (issue #431).
            app_page.wait_for_timeout(200)

        # On the last step the primary action is Save.
        expect(dialog.get_by_role("button", name="Save", exact=True)).to_be_visible(timeout=5000)
        # Tag + collection multiselects render on the organize step.
        expect(app_page.locator("#ws-tags")).to_be_visible(timeout=5000)
        expect(app_page.locator("#ws-cols")).to_be_visible(timeout=5000)

        unexpected = _unexpected_console_errors(app_page._console_errors)  # type: ignore[attr-defined]
        assert not unexpected, f"Unexpected console errors: {unexpected}"

    def test_create_and_delete_local_source(
        self, app_page: Page, require_local_watch: None
    ) -> None:
        """Create a local watch source through the stepper, see it listed, delete it."""
        _open_watch_sources(app_page)
        name = "E2E Created Source"
        app_page.get_by_role("button", name="Add Watch Source").click()
        dialog = app_page.locator(".modal-container")
        app_page.fill("#ws-name", name)
        for _ in range(3):
            nxt = dialog.get_by_role("button", name="Next", exact=True)
            expect(nxt).to_be_enabled(timeout=5000)
            nxt.click()
            # Kept deliberately: every step renders its OWN enabled "Next", so the
            # expect() at the top of the next iteration cannot tell "this step advanced"
            # from "the step hasn't re-rendered yet" — dropping the settle risks clicking
            # one step twice and never reaching Save (issue #431).
            app_page.wait_for_timeout(200)
        dialog.get_by_role("button", name="Save", exact=True).click()

        # The new source card appears in the panel.
        card = app_page.locator(".source-card", has_text=name)
        expect(card.first).to_be_visible(timeout=8000)

        # Clean up: delete it and confirm in the confirmation dialog.
        card.first.get_by_role("button", name="Delete").click()
        app_page.locator(".modal-container").get_by_role("button", name="Delete").click()
        expect(app_page.locator(".source-card", has_text=name)).to_have_count(0, timeout=8000)
