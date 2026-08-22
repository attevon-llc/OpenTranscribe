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
from http import HTTPStatus

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


@pytest.fixture
def api_source(backend_url: str):
    """Create a throwaway watch source over the API and delete it afterwards.

    The modal tests below are about the Files and Notifications screens, not about
    source creation — and driving the create stepper made them depend on the
    local-watch capability they never actually needed (the S3 branch of the form
    wants a bucket and keys the walkthrough does not type). That gate meant they
    skipped on every stack started without ``--with-watch``, so they had never run
    at all. Creating the row directly removes a dependency the subject under test
    does not have.

    Fixture teardown deletes by uuid and runs even when the test fails, which is
    what the E2E hygiene rule requires; the ``SOURCE_PREFIX`` name additionally lets
    ``watch_source_names``' sweep reap it if this process dies outright.
    """
    token = requests.post(
        f"{backend_url}/api/auth/token",
        data={"username": TEST_ADMIN_EMAIL, "password": TEST_ADMIN_PASSWORD},
        timeout=15,
    ).json()["access_token"]
    headers = {"Authorization": f"Bearer {token}"}
    name = _unique_source_name()
    created = requests.post(
        f"{backend_url}/api/watch-sources",
        headers=headers,
        json={
            "name": name,
            "source_type": "s3",
            "s3_endpoint_url": "https://s3.invalid.example.com",
            "s3_bucket_name": "e2e",
            "s3_access_key_id": "AKIAE2ETESTONLY",
            "s3_secret_key": "e2e-not-a-real-secret",
        },
        timeout=20,
    )
    assert created.status_code == 200, created.text
    source_uuid = created.json()["uuid"]
    try:
        yield name
    finally:
        requests.delete(
            f"{backend_url}/api/watch-sources/{source_uuid}", headers=headers, timeout=15
        )


def _open_files_modal(page: Page, source_name: str):
    """Open one source's Files modal and return the dialog locator."""
    _open_watch_sources(page)
    card = page.locator(".source-card", has_text=source_name).first
    expect(card).to_be_visible(timeout=10000)
    card.get_by_role("button", name="Files").click()
    dialog = page.locator(".modal-container")
    expect(dialog.get_by_text("No files tracked yet")).to_be_visible(timeout=8000)
    return dialog


class TestPerFileManagement:
    """The #489 Files modal, driven through the real UI."""

    def test_files_modal_opens_and_reports_an_empty_history(
        self, app_page: Page, api_source: str
    ) -> None:
        """A brand-new source has imported nothing, and must say so.

        The empty state is the case a table most often gets wrong — an unstyled blank
        area reads as a failed request rather than as "nothing here yet".
        """
        dialog = _open_files_modal(app_page, api_source)
        # The controls render even with nothing to filter — a source that has just
        # been created is exactly when an operator goes looking.
        expect(dialog.get_by_placeholder("Search by file name…")).to_be_visible()
        dialog.get_by_role("button", name="Close", exact=True).click()

    def test_status_filter_requeries_the_server(self, app_page: Page, api_source: str) -> None:
        """Choosing a status issues a new request rather than filtering in the browser.

        Asserted on the wire, not on the rendered rows: with an empty history both
        behaviours look identical on screen, so only the request distinguishes them.
        """
        dialog = _open_files_modal(app_page, api_source)
        with app_page.expect_request(
            lambda r: "/files?" in r.url and "status=error" in r.url, timeout=8000
        ) as caught:
            dialog.locator("select").first.select_option("error")

        # Paging must reset with the filter: leaving `page` where it was can land the
        # user on an empty page of a now-shorter result set.
        assert "status=error" in caught.value.url
        assert "page=1" in caught.value.url
        dialog.get_by_role("button", name="Close", exact=True).click()

    def test_search_box_requeries_the_server(self, app_page: Page, api_source: str) -> None:
        """Typing a name issues a debounced ``q=`` request — the filter is server-side."""
        dialog = _open_files_modal(app_page, api_source)
        with app_page.expect_request(
            lambda r: "/files?" in r.url and "q=board" in r.url, timeout=8000
        ) as caught:
            dialog.get_by_placeholder("Search by file name…").fill("board")

        assert "q=board" in caught.value.url
        assert "page=1" in caught.value.url
        dialog.get_by_role("button", name="Close", exact=True).click()


class TestPerSourceEmailLinks:
    """The #490 Notifications panel."""

    def test_notifications_panel_explains_the_per_scan_rule(
        self, app_page: Page, api_source: str
    ) -> None:
        """The panel must state that the flags are per scan, not per file.

        Without it an admin reasonably reads "notify on error" as per-file and then
        concludes notifications are broken when one mail arrives for a scan in which
        three files failed.
        """
        _open_watch_sources(app_page)
        card = app_page.locator(".source-card", has_text=api_source).first
        expect(card).to_be_visible(timeout=10000)
        card.get_by_role("button", name="Notifications").click()
        dialog = app_page.locator(".modal-container")
        expect(dialog.get_by_text("per scan, not per file", exact=False)).to_be_visible(
            timeout=8000
        )
        expect(dialog.get_by_text("No email notifications for this source")).to_be_visible(
            timeout=8000
        )
        dialog.get_by_role("button", name="Close", exact=True).click()

    def test_owner_can_attach_and_detach_a_configuration(
        self, app_page: Page, api_source: str, backend_url: str
    ) -> None:
        """#490 end to end: attach a configuration to one source, then detach it.

        The configuration is created **disabled** on purpose. A completed scan on a
        source carrying a live link dispatches ``send_notification``, which would open
        a real SMTP session against whatever host the config names; a disabled one is
        skipped by design. It doubles as the warning path — the panel must say that a
        disabled configuration delivers nothing, rather than accepting it silently.
        """
        token = requests.post(
            f"{backend_url}/api/auth/token",
            data={"username": TEST_ADMIN_EMAIL, "password": TEST_ADMIN_PASSWORD},
            timeout=15,
        ).json()["access_token"]
        headers = {"Authorization": f"Bearer {token}"}
        cfg_name = f"{SOURCE_PREFIX}mailer-{uuid.uuid4().hex[:8]}"
        cfg = requests.post(
            f"{backend_url}/api/watch-sources/email-configs",
            headers=headers,
            json={
                "name": cfg_name,
                "provider": "smtp",
                "smtp_host": "smtp.invalid.example.com",
                "smtp_port": 587,
                "from_address": "noreply@example.com",
                "is_enabled": False,
            },
            timeout=20,
        )
        if cfg.status_code == HTTPStatus.FORBIDDEN:
            pytest.skip("creating an email config is super_admin; this account cannot")
        assert cfg.status_code == 200, cfg.text
        cfg_uuid = cfg.json()["uuid"]

        try:
            _open_watch_sources(app_page)
            card = app_page.locator(".source-card", has_text=api_source).first
            expect(card).to_be_visible(timeout=10000)
            card.get_by_role("button", name="Notifications").click()
            dialog = app_page.locator(".modal-container")
            expect(dialog.get_by_text("No email notifications for this source")).to_be_visible(
                timeout=8000
            )

            dialog.locator("select").first.select_option(label=cfg_name)
            dialog.get_by_role("button", name="Attach").click()

            expect(dialog.get_by_text(cfg_name)).to_be_visible(timeout=8000)
            expect(
                dialog.get_by_text("This email configuration is disabled", exact=False)
            ).to_be_visible(timeout=8000)

            dialog.get_by_role("button", name="Unlink").click()
            expect(dialog.get_by_text("No email notifications for this source")).to_be_visible(
                timeout=8000
            )
            dialog.get_by_role("button", name="Close", exact=True).click()
        finally:
            requests.delete(
                f"{backend_url}/api/watch-sources/email-configs/{cfg_uuid}",
                headers=headers,
                timeout=15,
            )
