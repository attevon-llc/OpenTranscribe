"""E2E: the Plyr media player really plays in a real browser (#1282 browser pass).

"Plyr playback in a real browser" used to be a by-hand step: unit tests mock Plyr, so only
Chromium can say that play advances the clock, pause stops it, seeking lands, and the speed
menu changes the media element's rate. This drives the real controls of the file-detail
player against the suite's OWN real upload (``owned_transcribed_file``: a real clip in real
storage), read-only, and fails on any console error from the page.
"""

from __future__ import annotations

from typing import Any

import pytest
from playwright.sync_api import Page
from playwright.sync_api import expect
from timeouts import APP_SHELL_READY_MS

pytestmark = pytest.mark.transcription

MEDIA = "#player"


@pytest.fixture
def player_page(
    browser: Any, shared_auth_state: str, base_url: str, owned_transcribed_file: dict
) -> Any:
    context = browser.new_context(
        storage_state=shared_auth_state,
        viewport={"width": 1920, "height": 1080},
        ignore_https_errors=True,
    )
    page = context.new_page()
    errors: list[str] = []
    page.on("console", lambda m: errors.append(m.text) if m.type == "error" else None)
    page.on("pageerror", lambda e: errors.append(f"pageerror: {e}"))
    page._console_errors = errors  # type: ignore[attr-defined]
    page.goto(f"{base_url}/files/{owned_transcribed_file['uuid']}")
    expect(page.locator(".plyr")).to_be_visible(timeout=APP_SHELL_READY_MS)
    # Plyr is constructed before the media has metadata; seeking needs a duration.
    page.wait_for_function(
        f"() => {{ const m = document.querySelector('{MEDIA}'); return m && m.readyState >= 1 && m.duration > 1; }}",
        timeout=60_000,
    )
    yield page
    page.close()
    context.close()


def _state(page: Page) -> dict[str, Any]:
    return dict(
        page.evaluate(
            f"""() => {{
              const m = document.querySelector('{MEDIA}');
              return {{t: m.currentTime, paused: m.paused, rate: m.playbackRate, d: m.duration}};
            }}"""
        )
    )


def test_play_pause_seek_and_speed_on_the_real_player(player_page: Page) -> None:
    page = player_page
    assert _state(page)["paused"] is True
    assert _state(page)["t"] == 0

    # Play with the real control: the media clock must advance, not just the icon change.
    page.locator('.plyr__controls button[data-plyr="play"]').first.click()
    page.wait_for_function(
        f"() => {{ const m = document.querySelector('{MEDIA}'); return !m.paused && m.currentTime > 0.8; }}",
        timeout=20_000,
    )
    assert _state(page)["paused"] is False

    # Pause: the clock must stop (two reads apart agree).
    page.locator('.plyr__controls button[data-plyr="play"]').first.click()
    page.wait_for_function(f"() => document.querySelector('{MEDIA}').paused", timeout=10_000)
    stopped_at = _state(page)["t"]
    page.wait_for_timeout(1200)
    assert _state(page)["t"] == stopped_at
    assert stopped_at > 0.8

    # Seek with the real progress control to ~70% and confirm the media element followed.
    duration = _state(page)["d"]
    page.evaluate(
        """() => {
          const s = document.querySelector('.plyr input[data-plyr="seek"]');
          s.value = '70';
          s.dispatchEvent(new Event('input', {bubbles: true}));
        }"""
    )
    page.wait_for_function(
        f"() => Math.abs(document.querySelector('{MEDIA}').currentTime - {duration * 0.7}) < {duration * 0.1}",
        timeout=10_000,
    )

    # Speed through the settings menu: 1.5x must reach the media element.
    page.get_by_role("button", name="Settings", exact=True).click()
    page.locator('.plyr__menu__container [role="menuitem"]', has_text="Speed").click()
    page.locator('.plyr__menu__container [role="menuitemradio"]', has_text="1.5").click()
    page.wait_for_function(
        f"() => document.querySelector('{MEDIA}').playbackRate === 1.5", timeout=10_000
    )

    assert page._console_errors == [], page._console_errors  # type: ignore[attr-defined]
