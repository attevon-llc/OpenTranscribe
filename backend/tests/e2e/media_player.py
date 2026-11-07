"""Library-neutral page object for the app's media players (issue #1327).

The player library is being replaced (``docs/design/1327_plyr_replacement_plan.md``), so the
playback tests must not know which one is on the page. They talk to this object instead:

* **Media state** is read from the ``<video>``/``<audio>`` element itself (``#player`` on the
  file page, the element inside ``.sticky-preview`` for the floating preview). Every library
  keeps a real media element, so these reads survive the swap unchanged.
* **Controls** are found by ARIA role and accessible name, never by a library class name.
  The names live in ONE table, :data:`CONTROL_NAMES`. Today they are Plyr's own English
  labels, because Plyr's controls are not translated (plan, PC2). The lane that replaces the
  controls points this table at the app's own ``en.json`` keys; no test changes.
* **App-owned labels** (search preview, speaker preview, waveform) are read from
  ``frontend/src/lib/i18n/locales/en.json`` through :func:`app_label`, so a copy change in the
  app cannot silently break a selector.
* **Captions** are located by the ``.speaker`` element the server's WebVTT carries
  (``<c.speaker>Name</c>``), inside the app's own ``.video-player-container``. Plyr renders
  cues as HTML and keeps that class; so does the replacement's caption overlay.
"""

from __future__ import annotations

import json
import re
from functools import cache
from pathlib import Path
from typing import Any

from playwright.sync_api import Locator
from playwright.sync_api import Page

EN_LOCALE = Path(__file__).resolve().parents[3] / "frontend/src/lib/i18n/locales/en.json"

#: Accessible names of the player's own controls. The ONE place a library's labels appear.
CONTROL_NAMES: dict[str, re.Pattern[str]] = {
    "play": re.compile(r"^play$", re.IGNORECASE),
    "pause": re.compile(r"^pause$", re.IGNORECASE),
    "captions": re.compile(r"captions", re.IGNORECASE),
    "settings": re.compile(r"^settings$", re.IGNORECASE),
    "speed_menu": re.compile(r"^speed", re.IGNORECASE),
    "pip": re.compile(r"\bpip\b|picture.in.picture", re.IGNORECASE),
    "fullscreen": re.compile(r"fullscreen", re.IGNORECASE),
}


@cache
def _english() -> dict[str, str]:
    return dict(json.loads(EN_LOCALE.read_text(encoding="utf-8")))


def app_label(key: str) -> str:
    """The English UI string for an app i18n ``key`` (fails loudly on a missing key)."""
    labels = _english()
    assert key in labels, f"{key!r} is not in {EN_LOCALE}"
    return labels[key]


def speed_name(rate: float) -> re.Pattern[str]:
    """The accessible name of a speed choice: ``1.5x`` / ``1.5×``, or ``Normal`` for 1."""
    if rate == 1:
        return re.compile(r"^(normal|1\s*[x×]?)$", re.IGNORECASE)
    text = f"{rate:g}"
    return re.compile(rf"^{re.escape(text)}\s*[x×]?$", re.IGNORECASE)


class MediaPlayer:
    """One player on the page: its media element plus the controls around it.

    Args:
        page: the Playwright page.
        container: CSS selector of the APP-owned element that wraps the player
            (``.video-player-container`` on the file page, ``.sticky-preview`` for the
            floating preview).
        media: CSS selector of the media element, scoped to the document.
    """

    def __init__(self, page: Page, container: str, media: str) -> None:
        self.page = page
        self.container_selector = container
        self.media_selector = media

    @classmethod
    def file_detail(cls, page: Page) -> MediaPlayer:
        return cls(page, ".video-player-container", "#player")

    @classmethod
    def floating_preview(cls, page: Page) -> MediaPlayer:
        return cls(page, ".sticky-preview", ".sticky-preview video, .sticky-preview audio")

    # --- media element ------------------------------------------------------------------

    @property
    def container(self) -> Locator:
        return self.page.locator(self.container_selector)

    def _js_media(self) -> str:
        return f"document.querySelector({json.dumps(self.media_selector)})"

    def state(self) -> dict[str, Any]:
        """``t``, ``paused``, ``ended``, ``rate``, ``d``, ``muted``, ``volume``, ``loop``, ``tag``."""
        return dict(
            self.page.evaluate(
                f"""() => {{
                  const m = {self._js_media()};
                  return {{t: m.currentTime, paused: m.paused, ended: m.ended,
                           rate: m.playbackRate, d: m.duration, muted: m.muted,
                           volume: m.volume, loop: m.loop, tag: m.tagName.toLowerCase()}};
                }}"""
            )
        )

    def wait_for(self, predicate_js: str, timeout: int = 15_000) -> None:
        """Wait until ``predicate_js`` (an expression over ``m``, the media element) is true."""
        self.page.wait_for_function(
            f"() => {{ const m = {self._js_media()}; return !!m && ({predicate_js}); }}",
            timeout=timeout,
        )

    def wait_for_metadata(self, timeout: int = 60_000) -> None:
        self.wait_for("m.readyState >= 1 && m.duration > 1", timeout=timeout)

    def wait_until_near(self, seconds: float, tolerance: float, timeout: int = 15_000) -> None:
        self.wait_for(f"Math.abs(m.currentTime - {seconds}) <= {tolerance}", timeout=timeout)

    def set_time(self, seconds: float) -> None:
        """Move the playhead directly on the element (test setup, not a control under test)."""
        self.page.evaluate(f"() => {{ {self._js_media()}.currentTime = {seconds}; }}")
        self.wait_until_near(seconds, 0.3)

    def pause_media(self) -> None:
        self.page.evaluate(f"() => {self._js_media()}.pause()")
        self.wait_for("m.paused")

    # --- controls -----------------------------------------------------------------------

    def control(self, name: str) -> Locator:
        """A player button by its accessible name (see :data:`CONTROL_NAMES`)."""
        return self.container.get_by_role("button", name=CONTROL_NAMES[name])

    def click_play_toggle(self) -> None:
        """Press the control bar's play/pause button, whichever it currently is."""
        state = "pause" if not self.state()["paused"] else "play"
        self.control(state).last.click()

    def captions_on(self) -> bool:
        return self.control("captions").first.get_attribute("aria-pressed") == "true"

    def caption_speaker(self) -> Locator:
        """The speaker name rendered in the current caption cue."""
        return self.container.locator(".speaker")

    def captions_loaded(self, timeout: int = 30_000) -> None:
        """Wait until the server-built caption track has cues in the element."""
        self.wait_for(
            "m.textTracks.length > 0 && m.textTracks[0].cues && m.textTracks[0].cues.length > 0",
            timeout=timeout,
        )

    def first_cue_start(self) -> float:
        return float(
            self.page.evaluate(f"() => {self._js_media()}.textTracks[0].cues[0].startTime")
        )

    def choose_speed(self, rate: float) -> None:
        self.open_speed_menu()
        self.page.get_by_role("menuitemradio", name=speed_name(rate)).click()

    def open_speed_menu(self) -> None:
        self.control("settings").first.click()
        self.page.get_by_role("menuitem", name=CONTROL_NAMES["speed_menu"]).click()

    def blur(self) -> None:
        """Move focus off any control so page-wide shortcuts reach the player."""
        self.page.evaluate("() => document.activeElement && document.activeElement.blur()")
