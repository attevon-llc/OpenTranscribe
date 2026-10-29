"""Navigation helpers for the privacy surfaces (issue #1335).

Shared by the journey tests, the visual baselines and the a11y scans so that "open the
purge dialog" means one thing everywhere. Deliberately navigation only: assertions live in
the tests, so a helper can never be the reason a test passes.

Where each control really lives (the issue text says "Privacy & Redaction" for all of it,
which is only true of the deployment-wide card):

* the person's voiceprint switch and the two purge buttons: Settings > Speaker
  Identification > Voice Attributes (``UserPrivacyCard``);
* the person's gender switch: the same tab, above that card (``#gender-detection``);
* the deployment-wide switches (``InstancePrivacyCard``): Settings > Privacy & Redaction >
  the super-admin policy tab.
"""

from __future__ import annotations

import requests
from playwright.sync_api import Page
from playwright.sync_api import expect

#: Page-load budget for these journeys. Two full runs on 2026-10-28, with the host at a load
#: average of ~20 (other lanes building images), each lost one test to a 30 s wait that the
#: same step met in 3-4 s when the host was quiet. The app shell is gated on an auth probe
#: with a 60 s ceiling (backend/tests/CLAUDE.md, "Before you debug a flaky E2E test"), so
#: 90 s is the budget that cannot fire before the product itself gives up.
PAGE_READY_MS = 90_000

USER_VOICEPRINT_SWITCH = '[data-testid="user-voiceprints-switch"]'
USER_VOICEPRINT_PURGE = '[data-testid="user-voiceprints-purge"]'
USER_PRIVACY_CARD = '[data-testid="user-privacy-card"]'
PURGE_DIALOG = '[data-testid="purge-dialog"]'
GENDER_SWITCH = "#gender-detection"


def open_settings(page: Page) -> None:
    """Open the Settings modal through the navbar user menu."""
    user_button = page.locator(".user-button")
    expect(user_button).to_be_visible(timeout=PAGE_READY_MS)
    user_button.click()
    item = page.locator(".dropdown-menu .dropdown-item", has_text="Settings")
    expect(item.first).to_be_visible(timeout=5000)
    item.first.click()
    expect(page.locator(".settings-modal")).to_be_visible(timeout=10000)


def open_settings_section(page: Page, nav_label: str) -> None:
    """Open Settings and switch to the sidebar entry whose text contains *nav_label*."""
    open_settings(page)
    nav = page.locator(".settings-sidebar .nav-item", has_text=nav_label)
    expect(nav.first).to_be_visible(timeout=10000)
    nav.first.click()
    expect(page.locator(".settings-content .section-title").first).to_be_visible(timeout=10000)


def open_voice_privacy(page: Page) -> None:
    """Settings > Speaker Identification > Voice Attributes, with the privacy card loaded."""
    open_settings_section(page, "Speaker Identification")
    # The section is a lazy chunk; a loaded host has taken well over 30 s to deliver it.
    tab = page.locator("#tab-spk-attributes")
    expect(tab).to_be_visible(timeout=PAGE_READY_MS)
    tab.click()
    expect(page.locator(USER_VOICEPRINT_SWITCH)).to_be_visible(timeout=30000)


def open_purge_dialog(page: Page) -> None:
    """Open the user's voiceprint purge dialog (the voiceprint switch must already be off)."""
    open_voice_privacy(page)
    button = page.locator(USER_VOICEPRINT_PURGE)
    expect(button).to_be_enabled(timeout=10000)
    button.click()
    expect(page.locator(PURGE_DIALOG)).to_be_visible(timeout=10000)


def purge_modal(page: Page):  # type: ignore[no-untyped-def]
    """The modal element holding the purge dialog (footer buttons live outside the body).

    ``.last``: the Settings modal that opened it also contains the dialog, and is earlier in
    the document, so the innermost match is the purge dialog itself.
    """
    return page.locator('[role="dialog"]:has([data-testid="purge-dialog"])').last


def open_about(page: Page) -> None:
    """Open the About dialog from the navbar logo."""
    page.locator(".navbar-brand .logo-link").click()
    expect(page.locator("h2.modal-title", has_text="About OpenTranscribe")).to_be_visible(
        timeout=15000
    )


def token_for(backend_url: str, email: str, password: str) -> str:
    """A bearer token for an account the test owns (the caller logs nothing out: it expires)."""
    response = requests.post(
        f"{backend_url}/api/auth/token",
        data={"username": email, "password": password},
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        timeout=30,
    )
    assert response.status_code == 200, f"login for {email} failed: {response.status_code}"
    return str(response.json()["access_token"])


#: Visual/a11y surface names for the privacy controls (``test_visual_regression.SURFACES``).
PRIVACY_SURFACES = frozenset({"privacy_settings", "purge_dialog", "about"})


def open_privacy_surface(page: Page, base_url: str, surface: str):  # type: ignore[no-untyped-def]
    """Put *surface* on screen and return the locator that is its capture/scan root.

    Mutates nothing: the purge dialog needs voiceprints OFF to be reachable, so the single
    read that says so is answered by ``page.route`` instead of changing the account.
    """
    root = _open_privacy_surface(page, base_url, surface)
    root.evaluate(f"(el) => el.setAttribute('{SCAN_ROOT_ATTR}', '')")
    return root


def _open_privacy_surface(page: Page, base_url: str, surface: str):  # type: ignore[no-untyped-def]
    page.goto(base_url)
    page.wait_for_selector(".user-button", timeout=PAGE_READY_MS)
    if surface == "about":
        open_about(page)
        return page.locator('[role="dialog"]:has(h2.modal-title)').last
    if surface == "privacy_settings":
        open_voice_privacy(page)
        return page.locator(USER_PRIVACY_CARD)
    if surface == "purge_dialog":
        page.route(
            "**/api/privacy/me",
            lambda route: (
                route.fulfill(json={"voiceprint_matching_enabled": False, "locked_by": None})
                if route.request.method == "GET"
                else route.continue_()
            ),
        )
        open_purge_dialog(page)
        return purge_modal(page)
    raise AssertionError(f"unknown privacy surface {surface!r}")


#: CSS scope for an axe scan of any privacy surface: `open_privacy_surface` stamps the
#: surface's root element with this attribute. The page behind a modal carries its own debt
#: (the gallery's range-slider handles) that must not be charged to the privacy controls, and
#: the purge dialog sits INSIDE the Settings modal's DOM, which CSS alone cannot tell apart
#: from the dialog itself (`:has()` does not nest).
SCAN_ROOT_ATTR = "data-e2e-scan-root"
SURFACE_SCAN_SCOPE = {name: f"[{SCAN_ROOT_ATTR}]" for name in PRIVACY_SURFACES}
