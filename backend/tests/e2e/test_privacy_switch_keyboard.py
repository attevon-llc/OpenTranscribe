"""E2E: the privacy switch is operable from the keyboard, in both themes (#1282 browser pass).

Until now "press Space on the privacy switch" was a manual step of every release's browser
pass. ``test_privacy_settings.py`` proves the switch by MOUSE; this module drives the same
``PrivacySwitchCard`` with the keyboard alone and checks what a keyboard user depends on: the
control takes focus and shows a focus ring, Space flips it, ``aria-checked`` semantics hold
(the native ``role="switch"`` checkbox reports the state through ``checked``), the server
agrees, and the state survives a reload.

Runs as the throwaway ``second_user`` and restores in a ``finally`` (``voiceprints_restored``),
so the shared admin account is never toggled.

Enter is asserted as a NON-event: the control is a native checkbox with ``role="switch"``,
and the WAI-ARIA switch pattern requires Space only (Enter is optional). Pinning it keeps a
future change from making Enter submit the surrounding Settings form unnoticed.
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any

import pytest
import requests
from playwright.sync_api import Page
from playwright.sync_api import expect
from privacy_ui import USER_VOICEPRINT_PURGE
from privacy_ui import USER_VOICEPRINT_SWITCH
from privacy_ui import open_voice_privacy

pytestmark = pytest.mark.settings

THEMES = ["light", "dark"]


@pytest.fixture(params=THEMES)
def themed_page(
    request: pytest.FixtureRequest,
    browser: Any,
    second_user_auth_state: str,
    base_url: str,
) -> Iterator[Page]:
    """A throwaway-user page whose theme is forced before first paint (as the visual suite does)."""
    theme = request.param
    context = browser.new_context(
        storage_state=second_user_auth_state,
        viewport={"width": 1920, "height": 1080},
        ignore_https_errors=True,
    )
    context.add_init_script(f"window.localStorage.setItem('theme', '{theme}');")
    page = context.new_page()
    page.goto(base_url)
    page.wait_for_selector(".user-button", timeout=90_000)
    # The test is only about dark if the app actually rendered dark.
    assert page.evaluate("document.documentElement.getAttribute('data-theme')") == theme
    yield page
    page.close()
    context.close()


def _server_voiceprints(backend_url: str, headers: dict[str, str]) -> bool:
    response = requests.get(f"{backend_url}/api/privacy/me", headers=headers, timeout=30)
    assert response.status_code == 200, response.text
    return bool(response.json()["voiceprint_matching_enabled"])


def _focus_ring(page: Page) -> str:
    """Computed outline of the visual slider that follows the focused input."""
    return str(
        page.evaluate(
            """() => {
              const input = document.querySelector('[data-testid="user-voiceprints-switch"]');
              const slider = input.nextElementSibling;
              const s = getComputedStyle(slider);
              return s.outlineStyle + ' ' + s.outlineWidth;
            }"""
        )
    )


def test_space_toggles_the_switch_and_it_persists(
    themed_page: Page,
    backend_url: str,
    user_headers: dict[str, str],
    voiceprints_restored: None,
) -> None:
    page = themed_page
    open_voice_privacy(page)
    switch = page.locator(USER_VOICEPRINT_SWITCH)

    assert switch.get_attribute("role") == "switch"
    expect(switch).to_be_checked()
    assert _server_voiceprints(backend_url, user_headers) is True

    # Reach the control the way a keyboard user does: focus, not a click.
    switch.focus()
    # Script focus after mouse clicks does not match :focus-visible; a Tab round trip is how a
    # keyboard user arrives, and it is what makes the ring appear.
    page.keyboard.press("Shift+Tab")
    page.keyboard.press("Tab")
    expect(switch).to_be_focused()
    # Keyboard focus draws a visible ring (focus-visible), the thing sighted keyboard users see.
    assert _focus_ring(page) == "solid 2px"

    page.keyboard.press("Space")
    expect(switch).not_to_be_checked(timeout=10_000)
    assert _server_voiceprints(backend_url, user_headers) is False
    expect(page.locator(USER_VOICEPRINT_PURGE)).to_be_enabled()

    page.reload()
    open_voice_privacy(page)
    expect(page.locator(USER_VOICEPRINT_SWITCH)).not_to_be_checked()

    # And back on with the keyboard, so the way home is keyboard-operable too.
    page.locator(USER_VOICEPRINT_SWITCH).focus()
    page.keyboard.press("Space")
    expect(page.locator(USER_VOICEPRINT_SWITCH)).to_be_checked(timeout=10_000)
    assert _server_voiceprints(backend_url, user_headers) is True


def test_enter_does_not_toggle_the_switch(
    themed_page: Page,
    backend_url: str,
    user_headers: dict[str, str],
    voiceprints_restored: None,
) -> None:
    page = themed_page
    open_voice_privacy(page)
    switch = page.locator(USER_VOICEPRINT_SWITCH)
    expect(switch).to_be_checked()

    switch.focus()
    page.keyboard.press("Enter")
    # Positive twin below (Space) proves the control would have reacted within this window.
    page.wait_for_timeout(1500)
    expect(switch).to_be_checked()
    assert _server_voiceprints(backend_url, user_headers) is True

    page.keyboard.press("Space")
    expect(switch).not_to_be_checked(timeout=10_000)


@pytest.mark.xfail(
    strict=True,
    reason=(
        "REAL DEFECT found by this browser pass: PrivacySwitchCard sets disabled={locked || busy}, "
        "so the focused input is disabled while the save is in flight and the browser drops "
        "keyboard focus to <body>; a keyboard user has to Tab from the top of Settings again "
        "after every toggle. Fix: aria-disabled (and ignore the change) while busy instead of "
        "the disabled attribute. Remove this xfail with the fix."
    ),
)
def test_keyboard_focus_survives_a_toggle(themed_page: Page, voiceprints_restored: None) -> None:
    page = themed_page
    open_voice_privacy(page)
    switch = page.locator(USER_VOICEPRINT_SWITCH)
    switch.focus()
    page.keyboard.press("Space")
    expect(switch).not_to_be_checked(timeout=10_000)
    expect(switch).to_be_focused(timeout=2000)
