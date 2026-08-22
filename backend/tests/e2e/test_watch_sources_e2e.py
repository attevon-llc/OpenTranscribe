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
import uuid

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


#: Every watch source this file types a name into carries this prefix, so the teardown
#: below can sweep by name even when a test aborted before its own UI delete.
SOURCE_PREFIX = "e2e-watch-"


def _unique_source_name() -> str:
    return f"{SOURCE_PREFIX}{uuid.uuid4().hex[:8]}"


@pytest.fixture
def watch_source_names(backend_url: str):
    """Hand out run-unique watch-source names and delete anything left behind.

    Two hygiene problems this closes, both real:

    * The names were fixed (``"E2E Created Source"``), so a run interrupted before its
      UI delete left a row that the next run could not tell from its own — and a watch
      source is not inert, it is a configured ingestion path against a host directory.
    * The delete lived at the END of the test, on the happy path. The assertion that the
      new card appears is the one most likely to fail, and it fires *after* Save has
      already created the row — precisely the case where cleanup was skipped.

    Teardown sweeps by prefix through the API rather than by remembered ids, so it also
    reaps rows from earlier aborted runs.
    """
    names: list[str] = []

    def _make() -> str:
        name = _unique_source_name()
        names.append(name)
        return name

    yield _make

    try:
        token = requests.post(
            f"{backend_url}/api/auth/token",
            data={"username": TEST_ADMIN_EMAIL, "password": TEST_ADMIN_PASSWORD},
            timeout=15,
        ).json()["access_token"]
        headers = {"Authorization": f"Bearer {token}"}
        listing = requests.get(f"{backend_url}/api/watch-sources", headers=headers, timeout=15)
        payload = listing.json() if listing.status_code == 200 else {}
        # The envelope key is ``sources`` (``WatchSourcesList``). This read ``items``,
        # which is never present — so the sweep this fixture exists for iterated an
        # empty list every run and reaped nothing, while its docstring promised it
        # reaped rows from aborted runs. A cleanup that silently cleans nothing is
        # worse than none: it is believed.
        sources = payload if isinstance(payload, list) else payload.get("sources", [])
        for source in sources:
            if str(source.get("name", "")).startswith(SOURCE_PREFIX):
                requests.delete(
                    f"{backend_url}/api/watch-sources/{source['uuid']}", headers=headers, timeout=15
                )
    except (requests.RequestException, KeyError, ValueError) as exc:
        # A teardown must not mask the test result; report and move on.
        print(f"watch-source teardown failed (leaves orphan {SOURCE_PREFIX}* sources): {exc}")


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

    def test_stepper_walks_all_steps(
        self, app_page: Page, require_local_watch: None, watch_source_names
    ) -> None:
        """The Add editor is a guided stepper: Next walks every step to Save.

        Stops at Save without clicking it, so nothing is created — but the name still
        comes from ``watch_source_names``, because "this walkthrough never saves" is a
        property of today's assertions, not of the form.
        """
        _open_watch_sources(app_page)
        app_page.get_by_role("button", name="Add Watch Source").click()
        expect(app_page.get_by_text("Add Watch Source").first).to_be_visible(timeout=8000)

        dialog = app_page.locator(".modal-container")
        # Name is required before the connection step can advance.
        app_page.fill("#ws-name", watch_source_names())

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
        self, app_page: Page, require_local_watch: None, watch_source_names
    ) -> None:
        """Create a local watch source through the stepper, see it listed, delete it.

        The UI delete below is the behaviour under test. ``watch_source_names``'s teardown
        is the safety net for the case the UI path never reaches — a failure between Save
        and the delete click used to leave a live watch source configured on the stack.
        """
        _open_watch_sources(app_page)
        name = watch_source_names()
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


def _create_local_source(page: Page, name: str) -> None:
    """Walk the stepper to Save. The panel then lists a card for ``name``."""
    page.get_by_role("button", name="Add Watch Source").click()
    dialog = page.locator(".modal-container")
    page.fill("#ws-name", name)
    for _ in range(3):
        nxt = dialog.get_by_role("button", name="Next", exact=True)
        expect(nxt).to_be_enabled(timeout=5000)
        nxt.click()
        # See the note in test_create_and_delete_local_source: every step renders its
        # own enabled "Next", so without a settle a step can be clicked twice.
        page.wait_for_timeout(200)
    dialog.get_by_role("button", name="Save", exact=True).click()
    expect(page.locator(".source-card", has_text=name).first).to_be_visible(timeout=8000)


class TestPerFileManagement:
    """The #489 Files modal, driven through the real UI.

    Everything here runs against a throwaway source created and deleted by the test, so
    no pre-existing watch source is opened, filtered or mutated.
    """

    def test_files_modal_opens_and_reports_an_empty_history(
        self, app_page: Page, require_local_watch: None, watch_source_names
    ) -> None:
        """A brand-new source has imported nothing, and must say so.

        The empty state is the case a table most often gets wrong — an unstyled blank
        area reads as a failed request rather than as "nothing here yet".
        """
        _open_watch_sources(app_page)
        name = watch_source_names()
        _create_local_source(app_page, name)
        card = app_page.locator(".source-card", has_text=name).first

        try:
            card.get_by_role("button", name="Files").click()
            dialog = app_page.locator(".modal-container")
            expect(dialog.get_by_text("No files tracked yet")).to_be_visible(timeout=8000)
            # The controls render even with nothing to filter — a source that has just
            # been created is exactly when an operator goes looking.
            expect(dialog.get_by_placeholder("Search by file name…")).to_be_visible()
            dialog.get_by_role("button", name="Close").click()
        finally:
            card.get_by_role("button", name="Delete").click()
            app_page.locator(".modal-container").get_by_role("button", name="Delete").click()
            expect(app_page.locator(".source-card", has_text=name)).to_have_count(0, timeout=8000)

    def test_status_filter_requeries_the_server(
        self, app_page: Page, require_local_watch: None, watch_source_names
    ) -> None:
        """Choosing a status issues a new request rather than filtering in the browser.

        Asserted on the wire, not on the rendered rows: with an empty history both
        behaviours look identical on screen, so only the request distinguishes them.
        """
        _open_watch_sources(app_page)
        name = watch_source_names()
        _create_local_source(app_page, name)
        card = app_page.locator(".source-card", has_text=name).first

        try:
            card.get_by_role("button", name="Files").click()
            dialog = app_page.locator(".modal-container")
            expect(dialog.get_by_text("No files tracked yet")).to_be_visible(timeout=8000)

            with app_page.expect_request(
                lambda r: "/files?" in r.url and "status=error" in r.url, timeout=8000
            ):
                dialog.locator("select").first.select_option("error")

            dialog.get_by_role("button", name="Close").click()
        finally:
            card.get_by_role("button", name="Delete").click()
            app_page.locator(".modal-container").get_by_role("button", name="Delete").click()
            expect(app_page.locator(".source-card", has_text=name)).to_have_count(0, timeout=8000)

    def test_search_box_requeries_the_server(
        self, app_page: Page, require_local_watch: None, watch_source_names
    ) -> None:
        """Typing a name issues a debounced `q=` request — the filter is server-side."""
        _open_watch_sources(app_page)
        name = watch_source_names()
        _create_local_source(app_page, name)
        card = app_page.locator(".source-card", has_text=name).first

        try:
            card.get_by_role("button", name="Files").click()
            dialog = app_page.locator(".modal-container")
            expect(dialog.get_by_text("No files tracked yet")).to_be_visible(timeout=8000)

            with app_page.expect_request(
                lambda r: "/files?" in r.url and "q=board" in r.url, timeout=8000
            ):
                dialog.get_by_placeholder("Search by file name…").fill("board")

            dialog.get_by_role("button", name="Close").click()
        finally:
            card.get_by_role("button", name="Delete").click()
            app_page.locator(".modal-container").get_by_role("button", name="Delete").click()
            expect(app_page.locator(".source-card", has_text=name)).to_have_count(0, timeout=8000)


class TestPerSourceEmailLinks:
    """The #490 Notifications panel.

    Deliberately does NOT attach a deliverable configuration: a completed scan on a
    source with a live link dispatches ``send_notification`` and would open a real
    SMTP/Graph session against whatever host the config names.
    """

    def test_notifications_panel_opens_and_explains_the_per_scan_rule(
        self, app_page: Page, require_local_watch: None, watch_source_names
    ) -> None:
        """The panel must state that the flags are per scan, not per file.

        Without it an admin reasonably reads "notify on error" as per-file and concludes
        notifications are broken when one mail arrives for a scan with three failures.
        """
        _open_watch_sources(app_page)
        name = watch_source_names()
        _create_local_source(app_page, name)
        card = app_page.locator(".source-card", has_text=name).first

        try:
            card.get_by_role("button", name="Notifications").click()
            dialog = app_page.locator(".modal-container")
            expect(dialog.get_by_text("per scan", exact=False)).to_be_visible(timeout=8000)
            expect(dialog.get_by_text("No email notifications for this source")).to_be_visible(
                timeout=8000
            )
            dialog.get_by_role("button", name="Close").click()
        finally:
            card.get_by_role("button", name="Delete").click()
            app_page.locator(".modal-container").get_by_role("button", name="Delete").click()
            expect(app_page.locator(".source-card", has_text=name)).to_have_count(0, timeout=8000)

    def test_panel_says_who_can_create_a_config_when_none_exist(
        self, app_page: Page, require_local_watch: None, watch_source_names
    ) -> None:
        """With no configurations at all, the picker must not just be empty.

        A source owner cannot create one, so a bare empty dropdown reads as a broken
        page rather than as "an administrator has to do this first". Skips when the
        deployment already has configurations, since then the state under test does not
        exist.
        """
        _open_watch_sources(app_page)
        name = watch_source_names()
        _create_local_source(app_page, name)
        card = app_page.locator(".source-card", has_text=name).first

        try:
            card.get_by_role("button", name="Notifications").click()
            dialog = app_page.locator(".modal-container")
            expect(dialog.get_by_text("No email notifications for this source")).to_be_visible(
                timeout=8000
            )

            picker = dialog.locator("select")
            if picker.count():
                pytest.skip("this deployment already has email configurations")
            expect(dialog.get_by_text("Ask an administrator", exact=False)).to_be_visible(
                timeout=5000
            )
            dialog.get_by_role("button", name="Close").click()
        finally:
            card.get_by_role("button", name="Delete").click()
            app_page.locator(".modal-container").get_by_role("button", name="Delete").click()
            expect(app_page.locator(".source-card", has_text=name)).to_have_count(0, timeout=8000)
