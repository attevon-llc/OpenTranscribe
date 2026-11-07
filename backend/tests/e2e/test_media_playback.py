"""E2E: the app's media players really play in a real browser (#1282, #1327).

Unit tests fake the player, so only a browser can say that play advances the clock, that a
seek lands, that a shortcut reaches the element and that captions show the speaker. These
tests drive the real players against the suite's OWN uploads and assert on the media
element's state, never on a library's markup.

They are **characterization tests for the player replacement (#1327)**: they pin today's
behaviour so the lanes that swap the library must keep every one of them green. That is why
they know nothing about the library: media state comes from the element, controls are found
by role and accessible name through ``media_player.py`` (the one place library labels live),
and app labels come from ``en.json``.

Data: ``owned_transcribed_file`` (the committed 10 s speech WAV through the real pipeline,
read-only) for audio; ``owned_video`` (the committed 5 s speech MP4, looped to 40 s so a 10 s
arrow-key step is distinguishable from a jump to either end, uploaded and deleted by this
module) for video; ``owned_media_factory`` for the one test that writes (a comment).
"""

from __future__ import annotations

import re
import uuid
from collections.abc import Iterator
from typing import Any

import pytest
import requests
from conftest import COMMITTED_SAMPLE_MEDIA
from conftest import OWNED_MEDIA_PREFIX
from conftest import _generate_media
from conftest import delete_media_file
from conftest import wait_for_stable_completion
from media_player import CONTROL_NAMES
from media_player import MediaPlayer
from media_player import app_label
from media_player import speed_name
from playwright.sync_api import Page
from playwright.sync_api import expect
from timeouts import APP_SHELL_READY_MS

pytestmark = pytest.mark.transcription

#: The committed 5 s speech clip (``tests/fixtures/media/README.md``).
COMMITTED_SAMPLE_VIDEO = COMMITTED_SAMPLE_MEDIA.with_name("sample_short.mp4")

#: How many extra times the 5 s clip is looped into the uploaded video (5 s x 8 = 40 s).
VIDEO_LOOPS = 7


@pytest.fixture(scope="module")
def owned_video(backend_url: str, admin_token: str) -> Iterator[dict[str, Any]]:
    """A 40 s speech video put through the REAL pipeline, deleted however the module ends."""
    looped = _generate_media(
        "sample_speech_40s.mp4",
        [
            "-stream_loop",
            str(VIDEO_LOOPS),
            "-i",
            str(COMMITTED_SAMPLE_VIDEO),
            "-c",
            "copy",
            "-movflags",
            "+faststart",
        ],
    )
    headers = {"Authorization": f"Bearer {admin_token}"}
    name = f"{OWNED_MEDIA_PREFIX}{uuid.uuid4().hex[:8]}.mp4"
    with looped.open("rb") as handle:
        response = requests.post(
            f"{backend_url}/api/files",
            headers=headers,
            files={"file": (name, handle, "video/mp4")},
            timeout=300,
        )
    assert response.status_code == 200, f"video upload failed: {response.text[:300]}"
    file_uuid = str(response.json()["uuid"])
    try:
        status = wait_for_stable_completion(backend_url, admin_token, file_uuid, timeout_secs=600)
        assert status == "completed", f"the owned video never completed (status={status})"
        yield {"uuid": file_uuid}
    finally:
        delete_media_file(backend_url, admin_token, file_uuid)


@pytest.fixture
def browser_page(browser: Any, shared_auth_state: str) -> Iterator[Page]:
    """A fresh signed-in page that records console errors (``page._console_errors``)."""
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
    try:
        yield page
    finally:
        page.close()
        context.close()


def _open_file(page: Page, base_url: str, file_uuid: str, query: str = "") -> MediaPlayer:
    page.goto(f"{base_url}/files/{file_uuid}{query}")
    player = MediaPlayer.file_detail(page)
    expect(player.container).to_be_visible(timeout=APP_SHELL_READY_MS)
    # The player is built before the media has metadata; seeking needs a duration.
    player.wait_for_metadata()
    return player


def _no_console_errors(page: Page) -> None:
    errors = page._console_errors  # type: ignore[attr-defined]
    assert errors == [], errors


# ---------------------------------------------------------------------------
# Audio: the committed WAV
# ---------------------------------------------------------------------------


class TestAudioPlayback:
    def test_play_pause_seek_and_speed_on_the_real_player(
        self, browser_page: Page, base_url: str, owned_transcribed_file: dict
    ) -> None:
        page = browser_page
        player = _open_file(page, base_url, owned_transcribed_file["uuid"])
        assert player.state()["paused"] is True
        assert player.state()["t"] == 0

        # Play with the real control: the media clock must advance, not just the icon change.
        player.click_play_toggle()
        player.wait_for("!m.paused && m.currentTime > 0.8", timeout=20_000)

        # Pause: the clock must stop (two reads apart agree).
        player.click_play_toggle()
        player.wait_for("m.paused")
        stopped_at = player.state()["t"]
        page.wait_for_timeout(1200)
        assert player.state()["t"] == stopped_at
        assert stopped_at > 0.8

        # Seek with the real progress control to ~70% and confirm the element followed.
        duration = player.state()["d"]
        player.click_seek_bar(0.7)
        player.wait_until_near(duration * 0.7, duration * 0.1)

        # Speed through the settings menu: 1.5x must reach the media element.
        player.choose_speed(1.5)
        player.wait_for("m.playbackRate === 1.5")

        _no_console_errors(page)

    def test_speed_menu_offers_4x_and_it_reaches_the_element(
        self, browser_page: Page, base_url: str, owned_transcribed_file: dict
    ) -> None:
        player = _open_file(browser_page, base_url, owned_transcribed_file["uuid"])
        player.open_settings()
        # One "Speed" row: the speed list's back row (same name) stays out of the
        # accessibility tree until that list opens.
        expect(
            browser_page.get_by_role("menuitem", name=CONTROL_NAMES["speed_menu"])
        ).to_have_count(1)
        player.speed_menu_item().click()
        for rate in (0.5, 0.75, 1, 1.25, 1.5, 1.75, 2, 4):
            expect(browser_page.get_by_role("menuitemradio", name=speed_name(rate))).to_have_count(
                1
            )
        browser_page.get_by_role("menuitemradio", name=speed_name(4)).click()
        player.wait_for("m.playbackRate === 4")

    def test_audio_has_no_caption_pip_or_fullscreen_controls_and_its_waveform_seeks(
        self, browser_page: Page, base_url: str, owned_transcribed_file: dict
    ) -> None:
        player = _open_file(browser_page, base_url, owned_transcribed_file["uuid"])
        assert player.state()["tag"] == "audio"
        # The bar is there (so the absences below are not vacuous) ...
        expect(player.control("play").first).to_be_visible()
        # ... with no video-only control.
        for name in ("captions", "pip", "fullscreen"):
            expect(player.control(name)).to_have_count(0)

        # The waveform scrubber seeks the player (with the page's 0.5 s lead-in).
        duration = player.state()["d"]
        waveform = browser_page.get_by_role("slider", name=app_label("waveform.ariaLabel"))
        expect(waveform).to_be_visible(timeout=15_000)
        box = waveform.bounding_box()
        assert box is not None, "the waveform has no layout box"
        waveform.click(position={"x": box["width"] * 0.5, "y": box["height"] / 2})
        player.wait_until_near(max(0.0, duration * 0.5 - 0.5), 0.75)

    def test_segment_click_seeks_and_the_highlight_follows_playback(
        self, browser_page: Page, base_url: str, owned_transcribed_file: dict
    ) -> None:
        segments = [s for s in owned_transcribed_file["segments"] if s["start_time"] >= 1.0]
        assert segments, "the owned transcript has no segment starting after 1 s to click"
        target = max(segments, key=lambda s: s["start_time"])
        player = _open_file(browser_page, base_url, owned_transcribed_file["uuid"])

        row = browser_page.locator(f'[data-segment-id="{target["uuid"]}"]')
        row.locator("button.segment-content").first.click()
        player.wait_until_near(max(0.0, target["start_time"] - 0.5), 0.3)

        # Play into the segment: the active highlight must move onto it.
        player.click_play_toggle()
        expect(row).to_have_class(re.compile(r"\bactive-segment\b"), timeout=15_000)
        player.pause_media()

    def test_deep_link_t_lands_on_the_target(
        self, browser_page: Page, base_url: str, owned_transcribed_file: dict
    ) -> None:
        player = _open_file(browser_page, base_url, owned_transcribed_file["uuid"], "?t=6")
        player.wait_until_near(6.0, 0.3)
        assert player.state()["paused"] is True

    def test_comment_time_chip_seeks_half_a_second_early(
        self,
        browser_page: Page,
        base_url: str,
        backend_url: str,
        admin_token: str,
        owned_media_factory,
    ) -> None:
        media = owned_media_factory(admin_token)
        file_uuid = str(media["uuid"])
        headers = {"Authorization": f"Bearer {admin_token}"}
        # The file is this test's own throwaway upload, so the text needs no unique suffix.
        text = "e2e playback chip comment"
        created = requests.post(
            f"{backend_url}/api/comments/files/{file_uuid}/comments",
            headers=headers,
            json={"text": text, "timestamp": 6.0},
            timeout=30,
        )
        assert created.status_code == 200, created.text[:300]
        comment = created.json()
        try:
            player = _open_file(browser_page, base_url, file_uuid)
            chip = browser_page.get_by_role(
                "button", name=app_label("comments.jumpToTimestamp").replace("{{time}}", "0:06")
            )
            chip.click()
            player.wait_until_near(5.5, 0.3)
        finally:
            requests.delete(
                f"{backend_url}/api/comments/{comment.get('uuid') or comment['id']}",
                headers=headers,
                timeout=30,
            )


# ---------------------------------------------------------------------------
# Video: the looped committed MP4
# ---------------------------------------------------------------------------


def _show_first_cue(player: MediaPlayer) -> None:
    """Wait for the server-built track, then park the playhead inside its first cue."""
    player.captions_loaded()
    player.set_time(player.first_cue_start() + 0.2)


class TestVideoPlayback:
    def test_video_offers_captions_pip_and_fullscreen(
        self, browser_page: Page, base_url: str, owned_video: dict
    ) -> None:
        player = _open_file(browser_page, base_url, owned_video["uuid"])
        assert player.state()["tag"] == "video"
        for name in ("play", "captions", "pip", "fullscreen"):
            expect(player.control(name).first).to_be_visible()
        _no_console_errors(browser_page)

    def test_captions_show_the_speaker_and_the_cc_button_hides_and_shows_them(
        self, browser_page: Page, base_url: str, owned_video: dict
    ) -> None:
        player = _open_file(browser_page, base_url, owned_video["uuid"])
        _show_first_cue(player)

        # On by default, with the speaker name styled apart from the speech.
        speaker = player.caption_speaker().first
        expect(speaker).to_be_visible(timeout=15_000)
        assert (speaker.text_content() or "").strip(), "the caption speaker name is empty"
        assert player.captions_on() is True

        player.control("captions").first.click()
        expect(player.caption_speaker().first).to_be_hidden()
        assert player.captions_on() is False

        player.control("captions").first.click()
        expect(player.caption_speaker().first).to_be_visible()
        assert player.captions_on() is True

    def test_captions_stay_off_after_reload_when_switched_off(
        self, browser_page: Page, base_url: str, owned_video: dict
    ) -> None:
        player = _open_file(browser_page, base_url, owned_video["uuid"])
        _show_first_cue(player)
        expect(player.caption_speaker().first).to_be_visible(timeout=15_000)
        player.control("captions").first.click()
        expect(player.caption_speaker().first).to_be_hidden()

        browser_page.reload()
        player = MediaPlayer.file_detail(browser_page)
        player.wait_for_metadata()
        _show_first_cue(player)
        # Captions come on ~0.5 s after the track loads for everyone else; give it longer.
        browser_page.wait_for_timeout(2000)
        assert player.captions_on() is False
        expect(player.caption_speaker().first).to_be_hidden()

    def test_keyboard_shortcuts_reach_the_player(
        self, browser_page: Page, base_url: str, owned_video: dict
    ) -> None:
        page = browser_page
        player = _open_file(page, base_url, owned_video["uuid"])
        player.captions_loaded()
        duration = player.state()["d"]
        assert duration > 25, f"the looped video is too short for the arrow test: {duration}"
        keys = page.keyboard

        player.blur()
        keys.press("5")
        player.wait_until_near(duration * 0.5, 0.5)
        keys.press("0")
        player.wait_until_near(0.0, 0.3)

        player.set_time(20)
        player.blur()
        keys.press("ArrowRight")
        player.wait_until_near(30.0, 0.5)
        keys.press("ArrowLeft")
        player.wait_until_near(20.0, 0.5)

        keys.press("k")
        player.wait_for("!m.paused")
        keys.press("k")
        player.wait_for("m.paused")
        keys.press(" ")
        player.wait_for("!m.paused")
        keys.press(" ")
        player.wait_for("m.paused")

        keys.press("m")
        player.wait_for("m.muted")
        keys.press("m")
        player.wait_for("!m.muted")

        volume = player.state()["volume"]
        keys.press("ArrowDown")
        player.wait_for(f"m.volume < {volume} - 0.05")
        keys.press("ArrowUp")
        player.wait_for(f"Math.abs(m.volume - {volume}) < 0.01")

        captions_before = player.captions_on()
        keys.press("c")
        player.wait_for_captions(not captions_before)
        keys.press("c")
        player.wait_for_captions(captions_before)

        keys.press("l")
        player.wait_for("m.loop")
        keys.press("l")
        player.wait_for("!m.loop")

    def test_typing_in_the_comment_box_does_not_trigger_shortcuts(
        self, browser_page: Page, base_url: str, owned_video: dict
    ) -> None:
        page = browser_page
        player = _open_file(page, base_url, owned_video["uuid"])
        player.captions_loaded()
        player.set_time(10)
        before = player.state()
        captions_before = player.captions_on()

        box = page.locator(".comment-form textarea")
        expect(box).to_be_visible(timeout=15_000)
        box.click()
        typed = "k 5 m c l 0"
        page.keyboard.type(typed)
        page.keyboard.press("ArrowRight")

        expect(box).to_have_value(typed)
        page.wait_for_timeout(800)
        after = player.state()
        assert after["paused"] is True
        assert abs(after["t"] - before["t"]) < 0.05, (before, after)
        assert after["muted"] == before["muted"]
        assert after["loop"] == before["loop"]
        assert player.captions_on() == captions_before
        # Nothing was submitted: the comment exists only in the textarea.
        box.fill("")


# ---------------------------------------------------------------------------
# What the Video.js player adds over the Plyr it replaced (#1327): each of these failed on Plyr
# ---------------------------------------------------------------------------

#: Any Arabic letter: a control named in Arabic, not in English.
ARABIC = re.compile(r"[\u0600-\u06ff]")


class TestPlayerLanguageAndAccessibility:
    def test_controls_are_named_in_arabic_in_the_arabic_ui(
        self, browser_page: Page, base_url: str, owned_video: dict
    ) -> None:
        """Plyr's controls were English in every locale (plan PC2); these follow the UI."""
        page = browser_page
        page.context.add_init_script("window.localStorage.setItem('locale', 'ar')")
        player = _open_file(page, base_url, owned_video["uuid"])
        expect(page.locator("html")).to_have_attribute("dir", "rtl")
        expect(player.container.get_by_role("button", name=ARABIC).first).to_be_visible(
            timeout=15_000
        )
        expect(player.control("play")).to_have_count(0)
        _no_console_errors(page)

    def test_settings_menu_mirrors_in_rtl(
        self, browser_page: Page, base_url: str, owned_video: dict
    ) -> None:
        """A menu row runs from the inline start: icon then label then value, right to left in RTL.

        Measured on the Speed row's first and last parts (its icon and its current-value hint).
        Plyr's menu never mirrored (plan PC1).
        """
        page = browser_page
        player = _open_file(page, base_url, owned_video["uuid"])
        row_js = """el => {
          const first = el.firstElementChild.getBoundingClientRect();
          const last = el.lastElementChild.getBoundingClientRect();
          return {first: first.x, last: last.x};
        }"""

        player.open_settings()
        speed = player.speed_menu_item()
        ltr = speed.evaluate(row_js)
        assert ltr["first"] < ltr["last"], ltr
        page.keyboard.press("Escape")
        expect(speed).to_be_hidden()

        # What the app's locale store does for Arabic (stores/locale.ts).
        page.evaluate("() => { document.documentElement.dir = 'rtl'; }")
        player.open_settings()
        rtl = speed.evaluate(row_js)
        assert rtl["first"] > rtl["last"], rtl

    def test_play_is_announced_to_screen_readers(
        self, browser_page: Page, base_url: str, owned_video: dict
    ) -> None:
        player = _open_file(browser_page, base_url, owned_video["uuid"])
        player.click_play_toggle()
        expect(player.status_region().first).to_contain_text("Playing", timeout=5_000)
        player.pause_media()

    def test_phone_viewport_plays_on_tap_and_offers_fullscreen(
        self, browser: Any, shared_auth_state: str, base_url: str, owned_video: dict
    ) -> None:
        context = browser.new_context(
            storage_state=shared_auth_state,
            viewport={"width": 390, "height": 844},
            is_mobile=True,
            has_touch=True,
            ignore_https_errors=True,
        )
        page = context.new_page()
        try:
            player = _open_file(page, base_url, owned_video["uuid"])
            expect(player.control("fullscreen").first).to_be_visible()
            player.control("play").last.tap()
            player.wait_for("!m.paused", timeout=20_000)
            player.pause_media()
        finally:
            page.close()
            context.close()


# ---------------------------------------------------------------------------
# The floating preview player (search and speakers pages)
# ---------------------------------------------------------------------------


class TestPreviewPlayers:
    def test_search_preview_starts_at_the_hit_and_the_card_pauses_and_resumes_it(
        self,
        browser_page: Page,
        base_url: str,
        owned_transcribed_file: dict,
        owned_search_term: str,
    ) -> None:
        page = browser_page
        file_uuid = owned_transcribed_file["uuid"]
        page.goto(f"{base_url}/search")
        page.wait_for_selector(".search-page", timeout=APP_SHELL_READY_MS)
        page.fill(".search-input", owned_search_term)
        with page.expect_response(
            lambda r: re.search(r"/api/search\?", r.url) is not None and r.request.method == "GET"
        ) as searched:
            page.click(".search-btn")
        hits = [h for h in searched.value.json()["results"] if h["file_uuid"] == file_uuid]
        assert hits and hits[0]["occurrences"], (
            f"the owned file is not on page 1 for {owned_search_term!r}"
        )
        start = float(hits[0]["occurrences"][0]["start_time"])

        card = page.locator(".result-card", has=page.locator(f'a[href^="/files/{file_uuid}"]'))
        card.get_by_role("button", name=app_label("search.previewClip")).first.click()

        preview = MediaPlayer.floating_preview(page)
        expect(preview.container).to_be_visible(timeout=15_000)
        preview.wait_for(f"!m.paused && m.currentTime >= {start} - 0.3", timeout=20_000)

        card.get_by_role("button", name=app_label("search.pausePreview")).first.click()
        preview.wait_for("m.paused")
        card.get_by_role("button", name=app_label("search.resumePreview")).first.click()
        preview.wait_for("!m.paused")

        page.get_by_role("button", name=app_label("search.closePreviewAriaLabel")).click()
        expect(preview.container).to_have_count(0)

    def test_speaker_preview_pauses_at_the_clip_end(
        self,
        browser_page: Page,
        base_url: str,
        backend_url: str,
        admin_token: str,
        owned_transcribed_file: dict,
    ) -> None:
        """The speaker preview stops at ``end_time`` instead of playing on (row 14).

        The inbox listing is stubbed to one item for the owned speaker: which speakers sit
        in the inbox depends on the library's clustering state, which this suite does not
        own. Everything after it is real: the media-preview request, its presigned URL and
        the player. The preview's ``end_time`` is pulled in to 1.5 s after its start so the
        clip end is well clear of the end of the 10 s file, where a natural ``ended`` would
        look the same.
        """
        page = browser_page
        listed = requests.get(
            f"{backend_url}/api/speakers",
            params={"file_uuid": owned_transcribed_file["uuid"]},
            headers={"Authorization": f"Bearer {admin_token}"},
            timeout=30,
        )
        assert listed.status_code == 200, listed.text[:300]
        payload = listed.json()
        speakers = payload if isinstance(payload, list) else payload.get("items", [])
        assert speakers, "the owned file has no speakers"
        speaker = speakers[0]
        clip: dict[str, float] = {}

        def inbox(route: Any) -> None:
            route.fulfill(
                json={
                    "items": [
                        {
                            "speaker_uuid": speaker["uuid"],
                            "speaker_name": speaker.get("name") or "SPEAKER_00",
                            "display_name": speaker.get("display_name"),
                            "suggested_name": None,
                            "suggestion_source": None,
                            "confidence": None,
                            "media_file_uuid": owned_transcribed_file["uuid"],
                            "media_file_title": None,
                            "media_file_duration": None,
                            "cluster_uuid": None,
                            "cluster_label": None,
                            "cluster_member_count": 0,
                            "verified": False,
                            "predicted_gender": None,
                            "predicted_age_range": None,
                            "created_at": None,
                        }
                    ],
                    "total": 1,
                    "page": 1,
                    "per_page": 20,
                    "pages": 1,
                }
            )

        def media_preview(route: Any) -> None:
            body = route.fetch().json()
            start = min(float(body["start_time"]), 6.0)
            body["start_time"] = start
            body["end_time"] = start + 1.5
            clip.update(start=start, end=start + 1.5)
            route.fulfill(json=body)

        page.route("**/api/speaker-clusters/unverified/inbox**", inbox)
        page.route(
            f"**/api/speaker-clusters/speakers/{speaker['uuid']}/media-preview", media_preview
        )

        page.goto(f"{base_url}/speakers?tab=inbox")
        listen = page.get_by_role("button", name=app_label("speakers.inbox.previewTitle"))
        expect(listen.first).to_be_visible(timeout=APP_SHELL_READY_MS)
        listen.first.click()

        preview = MediaPlayer.floating_preview(page)
        expect(preview.container).to_be_visible(timeout=15_000)
        preview.wait_for("!m.paused", timeout=20_000)
        preview.wait_for("m.paused", timeout=15_000)
        state = preview.state()
        assert state["ended"] is False, state
        assert clip["end"] - 0.05 <= state["t"] <= clip["end"] + 0.6, (clip, state)
