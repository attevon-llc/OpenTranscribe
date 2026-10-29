"""E2E: a person's own privacy switches and the purge dialog (issue #1335, feature #1280).

Runs as the throwaway ``second_user`` (a plain user), never as the shared admin, so a failed
restore can only ever damage an account that is deleted at the end of the session. Every test
that moves a switch puts it back in a ``finally``.

Two things in the issue text did not match the product and are asserted as they are:

* the voiceprint and gender switches are under Settings > Speaker Identification > Voice
  Attributes (``privacy_ui`` documents the map); only the deployment-wide card is under
  Privacy & Redaction;
* the voiceprint switch saves on click, the gender switch saves on the tab's Save button.
"""

from __future__ import annotations

import pytest
import requests
from playwright.sync_api import Page
from playwright.sync_api import expect
from privacy_ui import GENDER_SWITCH
from privacy_ui import PURGE_DIALOG
from privacy_ui import USER_VOICEPRINT_PURGE
from privacy_ui import USER_VOICEPRINT_SWITCH
from privacy_ui import open_purge_dialog
from privacy_ui import open_voice_privacy
from privacy_ui import purge_modal

pytestmark = pytest.mark.settings


def _server_voiceprints(backend_url: str, headers: dict[str, str]) -> bool:
    response = requests.get(f"{backend_url}/api/privacy/me", headers=headers, timeout=30)
    assert response.status_code == 200, response.text
    return bool(response.json()["voiceprint_matching_enabled"])


def _server_gender(backend_url: str, headers: dict[str, str]) -> bool:
    response = requests.get(
        f"{backend_url}/api/user-settings/speaker-attributes", headers=headers, timeout=30
    )
    assert response.status_code == 200, response.text
    return bool(response.json()["gender_detection_enabled"])


class TestPersonalSwitches:
    """The two personal switches persist across a reload and match the server."""

    def test_voiceprint_switch_persists_across_reload(
        self,
        second_user_page: Page,
        backend_url: str,
        user_headers: dict[str, str],
        voiceprints_restored: None,
    ) -> None:
        page = second_user_page
        open_voice_privacy(page)
        switch = page.locator(USER_VOICEPRINT_SWITCH)
        expect(switch).to_be_checked()
        # The purge is refused while the feature is on, so the button must be disabled.
        expect(page.locator(USER_VOICEPRINT_PURGE)).to_be_disabled()

        switch.click()
        expect(switch).not_to_be_checked(timeout=10000)
        assert _server_voiceprints(backend_url, user_headers) is False
        expect(page.locator(USER_VOICEPRINT_PURGE)).to_be_enabled()

        page.reload()
        open_voice_privacy(page)
        expect(page.locator(USER_VOICEPRINT_SWITCH)).not_to_be_checked()

        # And back on, through the UI, so the restore path is exercised too.
        page.locator(USER_VOICEPRINT_SWITCH).click()
        expect(page.locator(USER_VOICEPRINT_SWITCH)).to_be_checked(timeout=10000)
        assert _server_voiceprints(backend_url, user_headers) is True

    def test_gender_switch_persists_across_reload(
        self,
        second_user_page: Page,
        backend_url: str,
        user_headers: dict[str, str],
        gender_restored: None,
    ) -> None:
        page = second_user_page
        open_voice_privacy(page)
        switch = page.locator(GENDER_SWITCH)
        expect(switch).to_be_checked()

        # The input itself is visually hidden; the visible control is its label.
        page.locator("label.toggle", has=switch).click()
        expect(switch).not_to_be_checked()
        # Unsaved until the tab's Save button is used: nothing may have changed server side.
        assert _server_gender(backend_url, user_headers) is True
        with page.expect_response(
            lambda r: r.request.method == "PUT" and "/speaker-attributes" in r.url
        ) as saved:
            page.locator("#tabpanel-spk-attributes button.btn-primary").click()
        assert saved.value.status == 200
        assert _server_gender(backend_url, user_headers) is False

        page.reload()
        open_voice_privacy(page)
        expect(page.locator(GENDER_SWITCH)).not_to_be_checked()


class TestPurgeDialog:
    """The irreversible-delete confirmation."""

    @pytest.fixture
    def off_page(self, second_user_page: Page, voiceprints_off: None) -> Page:
        return second_user_page

    def test_confirm_stays_disabled_until_box_ticked_and_word_typed(self, off_page: Page) -> None:
        page = off_page
        open_purge_dialog(page)
        dialog = purge_modal(page)
        confirm = page.locator('[data-testid="purge-confirm"]')
        box = dialog.get_by_role("checkbox")
        word = dialog.locator("input[type=text]")

        expect(confirm).to_be_disabled()
        box.check()
        expect(confirm).to_be_disabled()  # ticked, but the word is still missing
        word.fill("purge")
        expect(confirm).to_be_disabled()  # the word is case-sensitive
        word.fill("PURG")
        expect(confirm).to_be_disabled()
        word.fill("PURGE")
        expect(confirm).to_be_enabled()
        box.uncheck()
        expect(confirm).to_be_disabled()  # the word alone does not arm it

    def test_cancel_sends_no_purge_request_and_resets_the_dialog(self, off_page: Page) -> None:
        page = off_page
        purge_calls: list[str] = []
        page.on(
            "request",
            lambda r: (
                purge_calls.append(r.url) if "/purge" in r.url and r.method == "POST" else None
            ),
        )
        open_purge_dialog(page)
        dialog = purge_modal(page)
        dialog.get_by_role("checkbox").check()
        dialog.locator("input[type=text]").fill("PURGE")
        expect(page.locator('[data-testid="purge-confirm"]')).to_be_enabled()

        dialog.get_by_role("button", name="Cancel").click()
        expect(page.locator(PURGE_DIALOG)).to_have_count(0)
        # A deterministic settle: reopening proves the dialog finished closing, and
        # requests are recorded synchronously, so nothing can still be in flight.
        page.locator(USER_VOICEPRINT_PURGE).click()
        expect(page.locator(PURGE_DIALOG)).to_be_visible()
        assert purge_calls == []
        # A stale ticked box must never pre-arm a delete on the next opening.
        expect(page.locator('[data-testid="purge-confirm"]')).to_be_disabled()
        expect(purge_modal(page).get_by_role("checkbox")).not_to_be_checked()

        # Positive control: the same listener DOES see a purge POST when one is made, so the
        # empty list above is a measurement and not a listener that cannot fire.
        page.route(
            "**/api/privacy/me/purge",
            lambda route: route.fulfill(status=202, json={"task_id": "stub-control"}),
        )
        page.route(
            "**/api/privacy/purge/stub-control",
            lambda route: route.fulfill(json={"task_id": "stub-control", "status": "skipped"}),
        )
        purge_modal(page).get_by_role("checkbox").check()
        purge_modal(page).locator("input[type=text]").fill("PURGE")
        page.locator('[data-testid="purge-confirm"]').click()
        expect(purge_modal(page)).to_contain_text("Nothing needed to be deleted", timeout=15000)
        assert len(purge_calls) == 1

    def test_confirm_shows_measured_counts_and_the_residual_verdict(self, off_page: Page) -> None:
        """The dialog renders what the SERVER reported, not a fixed success line.

        The status route is stubbed with distinctive numbers, so the assertion cannot be
        satisfied by anything but the numbers the dialog was given.
        """
        page = off_page
        started: list[dict[str, object]] = []

        def start(route):  # type: ignore[no-untyped-def]
            started.append(route.request.post_data_json)
            route.fulfill(status=202, json={"task_id": "stub-task"})

        status = {
            "task_id": "stub-task",
            "status": "completed",
            "counts": {
                "postgres": {"speaker": 4, "profile_embedding": 3},
                "opensearch": {"doc": 5},
            },
            "residual": {"speaker": 0},
            "residual_ok": True,
            "residual_unverified": [],
            "audit": [{"event_type": "privacy.purge.voiceprints", "task_id": "stub-task"}],
        }
        page.route("**/api/privacy/me/purge", start)
        page.route("**/api/privacy/purge/stub-task", lambda route: route.fulfill(json=status))

        open_purge_dialog(page)
        dialog = purge_modal(page)
        dialog.get_by_role("checkbox").check()
        dialog.locator("input[type=text]").fill("PURGE")
        page.locator('[data-testid="purge-confirm"]').click()

        results = page.locator('[data-testid="purge-results"]')
        expect(results).to_contain_text("Database records removed: 7", timeout=15000)
        expect(results).to_contain_text("Search index documents removed: 5")
        expect(page.locator('[data-testid="purge-verification"]')).to_contain_text(
            "nothing from this deletion remains"
        )
        expect(page.locator('[data-testid="purge-audit"]')).to_contain_text(
            "privacy.purge.voiceprints"
        )
        assert started == [{"targets": ["voiceprints"], "confirm": "PURGE"}]

    def test_a_purge_that_left_something_is_reported_as_a_failure(self, off_page: Page) -> None:
        """Negative control for the verdict: residue must read as an error, not as success."""
        page = off_page
        page.route(
            "**/api/privacy/me/purge",
            lambda route: route.fulfill(status=202, json={"task_id": "stub-left"}),
        )
        page.route(
            "**/api/privacy/purge/stub-left",
            lambda route: route.fulfill(
                json={
                    "task_id": "stub-left",
                    "status": "failed",
                    "counts": {"postgres": {"speaker": 1}},
                    "residual": {"speaker": 2},
                    "residual_ok": False,
                    "residual_unverified": [],
                    "audit": [],
                }
            ),
        )
        open_purge_dialog(page)
        dialog = purge_modal(page)
        dialog.get_by_role("checkbox").check()
        dialog.locator("input[type=text]").fill("PURGE")
        page.locator('[data-testid="purge-confirm"]').click()

        verdict = page.locator('[data-testid="purge-verification"]')
        expect(verdict).to_contain_text("still remain", timeout=15000)
        expect(verdict).to_have_attribute("role", "alert")
        expect(
            page.locator('[data-testid="purge-verification"]:has-text("nothing from")')
        ).to_have_count(0)

    def test_a_real_purge_runs_to_a_verified_result(self, off_page: Page) -> None:
        """No stubs: the account has nothing to delete, and the measured result says so."""
        page = off_page
        open_purge_dialog(page)
        dialog = purge_modal(page)
        dialog.get_by_role("checkbox").check()
        dialog.locator("input[type=text]").fill("PURGE")
        page.locator('[data-testid="purge-confirm"]').click()

        expect(page.locator('[data-testid="purge-results"]')).to_be_visible(timeout=120000)
        expect(page.locator('[data-testid="purge-results"]')).to_contain_text(
            "Database records removed: 0"
        )
        expect(page.locator('[data-testid="purge-verification"]')).to_contain_text(
            "nothing from this deletion remains"
        )
