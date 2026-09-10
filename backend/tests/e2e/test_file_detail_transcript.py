"""
E2E smoke tests for the file-detail transcript view.

Regression safety net ahead of the frontend component refactor
(branch: refactor/frontend-overhaul). Tolerant where the UI is genuinely
free to vary (Playwright auto-waiting, benign console-error filtering) and
strict about the data, which the suite now owns rather than discovers —
they guard the high-value surfaces:

- The file-detail page renders a transcript (at least one segment).
- The transcript Export control opens and lists txt/json/csv/srt/vtt
  (guards the formatter + export refactor).
- The "Edit Speakers" affordance opens the speaker editor. The owned
  transcript has two speakers, so this is required, not conditional.
- A transcript longer than one 500-segment page scrolls in completely and
  renders no segment twice.

Requirements:
- Dev environment running: ./opentr.sh start dev
- Frontend at localhost:5173, Backend at localhost:5174
  (admin@example.com / password)

The suite supplies its own transcripts (``owned_corpus.py``) and deletes them at
session teardown; it reads nothing out of the dev library and asserts on nothing
a developer put there.

Run (headless):
    pytest backend/tests/e2e/test_file_detail_transcript.py -v

Run (visible on XRDP):
    DISPLAY=:11 pytest backend/tests/e2e/test_file_detail_transcript.py -v --headed
"""

from __future__ import annotations

import os
import re
import tempfile
from typing import Any

import pytest
import requests
from playwright.sync_api import Page
from playwright.sync_api import expect
from timeouts import LOGIN_FORM_READY_MS

pytestmark = pytest.mark.transcription

# This module used to define its own ``FRONTEND_URL``/``BACKEND_URL`` constants here.
# A module constant is evaluated at import time, so it could not see ``--base-url`` /
# ``--backend-url`` and this file always drove whatever was on the default ports — even
# when the run was aimed at an isolated stack (issue #431). Everything below takes
# conftest's ``base_url`` / ``backend_url`` fixtures instead.
TEST_ADMIN_EMAIL = os.environ.get("E2E_ADMIN_EMAIL", "admin@example.com")
TEST_ADMIN_PASSWORD = os.environ.get("E2E_ADMIN_PASSWORD", "password")  # noqa: S105

# Expected transcript export formats (guards formatter/export refactor).
EXPORT_FORMATS = ("txt", "json", "csv", "srt", "vtt")

# Console-error noise that is pre-existing app behavior, NOT a regression:
# - the auth-bootstrap 401 emitted before the stored token rehydrates,
# - generic resource 404s (e.g. the optional /suggestions endpoint, favicon).
# We filter these so the test still catches *new* JS exceptions from the
# refactor without flapping on known noise.
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
    """Drop known-benign console noise; return anything that looks like a real bug."""
    return [e for e in errors if not any(sub in e for sub in BENIGN_CONSOLE_SUBSTRINGS)]


@pytest.fixture(scope="module")
def api_token(backend_url: str) -> str:
    """Authenticate once per module via the backend API."""
    resp = requests.post(
        f"{backend_url}/api/auth/token",
        data={"username": TEST_ADMIN_EMAIL, "password": TEST_ADMIN_PASSWORD},
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        timeout=30,
    )
    if resp.status_code != 200:
        pytest.skip(f"Cannot authenticate against dev stack (HTTP {resp.status_code})")
    return str(resp.json()["access_token"])


@pytest.fixture(scope="module")
def transcribed_file(api_token: str, backend_url: str, owned_diarized_transcript_file) -> dict:
    """The suite's OWN multi-speaker transcript — never the dev library's.

    This used to list ``/api/files``, pick a ``completed`` entry, prefer one with
    more than one speaker, and ``pytest.skip`` when the deployment had none. Three
    things were wrong with that, and they compound:

    * On an empty or ``--fresh`` stack every test in this module skipped, and the
      E2E runner reported success. A suite that proves nothing must not read green.
    * On a populated stack the tests asserted against whichever recording happened
      to be newest, so what they exercised changed under the developer, and the
      speaker-editor assertions ran or did not run depending on that file's
      diarization.
    * ``test_edit_segment_cancel_preserves_text`` types into a *real* recording's
      transcript. It cancels, and cancelling is genuinely safe — but the file being
      typed into was someone's actual data.

    ``owned_diarized_transcript_file`` (``owned_corpus.py``) supplies two speakers
    and twelve segments, injected through the production corpus-injection tool and
    deleted at session teardown.

    Returns:
        The full ``GET /api/files/{uuid}`` payload for the owned file.
    """
    detail = requests.get(
        f"{backend_url}/api/files/{owned_diarized_transcript_file['uuid']}",
        headers={"Authorization": f"Bearer {api_token}"},
        timeout=30,
    )
    assert detail.status_code == 200, f"could not read the owned transcript: {detail.text[:300]}"
    payload: dict[str, Any] = detail.json()
    assert payload.get("transcript_segments"), "the owned transcript came back with no segments"
    return payload


# ---------------------------------------------------------------------------
# Module-scoped auth: log in ONCE via the form, reuse cookies for every test.
# Per-test form logins trip the backend's per-IP auth rate limiting (the same
# reason test_gallery_actions.py / test_media_download.py log in once). When
# the wider e2e suite has already spent the rate-limit budget, the login can
# briefly bounce; retry through that window rather than flapping.
# ---------------------------------------------------------------------------
def _form_login_with_retry(page, base_url: str, attempts: int = 4) -> None:
    """Submit the login form, retrying through transient auth rate-limiting."""
    last_error: Exception | None = None
    for attempt in range(attempts):
        try:
            page.goto(base_url)
            # Already authenticated (cookie still valid) — no form to fill.
            if page.locator(".user-button").count():
                page.wait_for_selector(".user-button", timeout=10000)
                return
            page.wait_for_selector("#email", timeout=LOGIN_FORM_READY_MS)
            page.fill("#email", TEST_ADMIN_EMAIL)
            page.fill("#password", TEST_ADMIN_PASSWORD)
            page.click("button[type=submit]")
            page.wait_for_selector(".user-button", timeout=20000)
            return
        except Exception as exc:  # noqa: BLE001 - retry on any login-flow failure
            last_error = exc
            # Kept deliberately: this wait IS the rate-limit backoff, not a settle for
            # something a locator could poll for (issue #431).
            page.wait_for_timeout(5000 * (attempt + 1))
    raise AssertionError(f"Could not log in via form after {attempts} attempts: {last_error}")


@pytest.fixture(scope="module")
def auth_storage_state(browser, base_url: str):  # type: ignore[no-untyped-def]
    """Login once and persist browser storage state for reuse across tests."""
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
def detail_page(browser, auth_storage_state: str, transcribed_file: dict[str, Any], base_url: str):  # type: ignore[no-untyped-def]
    """A pre-authenticated page on a real file-detail view with a transcript.

    Exposes captured console errors on ``page._console_errors``.
    """
    context = browser.new_context(
        storage_state=auth_storage_state,
        viewport={"width": 1920, "height": 1080},
        ignore_https_errors=True,
    )
    page = context.new_page()
    errors: list[str] = []
    page.on("console", lambda m: errors.append(m.text) if m.type == "error" else None)
    page._console_errors = errors  # type: ignore[attr-defined]

    uuid = transcribed_file["uuid"]
    page.goto(f"{base_url}/files/{uuid}")
    page.wait_for_load_state("networkidle")
    # Transcript is the load-bearing surface — wait for at least one segment.
    page.wait_for_selector(".transcript-segment", timeout=25000)
    yield page
    page.close()
    context.close()


class TestTranscriptRenders:
    """The transcript region renders for a real completed file."""

    def test_file_detail_loads_without_unexpected_console_errors(self, detail_page: Page) -> None:
        """Page settles and emits no *new* (non-benign) console errors."""
        # Deterministic settle rather than a guessed 1.5 s: both assertions below are about
        # the ABSENCE of something (a navigation away, a console error), which no locator
        # can auto-wait for (issue #431).
        detail_page.wait_for_load_state("networkidle")
        assert "/files/" in detail_page.url
        unexpected = _unexpected_console_errors(detail_page._console_errors)  # type: ignore[attr-defined]
        assert not unexpected, f"Unexpected console errors on file detail: {unexpected}"

    def test_transcript_region_renders_segments(self, detail_page: Page) -> None:
        """The transcript container shows at least one transcript segment."""
        expect(detail_page.locator(".transcript-display-container")).to_be_visible(timeout=10000)
        segments = detail_page.locator(".transcript-segment")
        expect(segments.first).to_be_visible(timeout=10000)
        assert segments.count() >= 1, "Expected at least one transcript segment to render"

    def test_segment_has_visible_text(self, detail_page: Page) -> None:
        """At least one segment exposes non-empty transcript text."""
        first_text = detail_page.locator(".transcript-segment .segment-text").first
        expect(first_text).to_be_visible(timeout=10000)
        content = first_text.text_content() or ""
        assert content.strip(), "First transcript segment should contain text"


class TestExportControl:
    """The Export dropdown is present and lists all transcript export formats."""

    def test_export_button_present(self, detail_page: Page) -> None:
        """The Export control is visible in the transcript actions bar."""
        btn = detail_page.locator(".export-transcript-button")
        expect(btn).to_be_visible(timeout=10000)
        expect(btn).to_contain_text("Export")

    def test_export_dropdown_lists_all_formats(self, detail_page: Page) -> None:
        """Opening Export lists txt/json/csv/srt/vtt (guards formatter refactor)."""
        detail_page.locator(".export-transcript-button").click()
        menu = detail_page.locator(".export-dropdown.open .export-dropdown-content")
        expect(menu).to_be_visible(timeout=5000)

        options = menu.locator("button")
        expect(options).to_have_count(len(EXPORT_FORMATS), timeout=5000)

        combined = (menu.text_content() or "").lower()
        for fmt in EXPORT_FORMATS:
            assert f".{fmt}" in combined, f"Export dropdown should offer .{fmt}; got: {combined!r}"


class TestSegmentEditing:
    """Inline segment editing (issue #123 Phase 4) — cancel path, and a real save+reload.

    The cancel test uses ``detail_page`` — now the suite's own injected transcript
    (``owned_diarized_transcript_file``), not an ambient dev-dataset file — and never
    saves. The save test below needs the opposite guarantee: a real, persisted write
    through the REAL pipeline, so it uses ``owned_media_factory`` for a per-test
    upload (issue #541) rather than writing into the shared injected fixture that
    every other test in this module is reading.
    """

    def test_edit_segment_cancel_preserves_text(self, detail_page: Page) -> None:
        """Entering edit mode, changing text, and cancelling restores the original."""
        first_segment = detail_page.locator(".transcript-segment").first
        original = (first_segment.locator(".segment-text").first.text_content() or "").strip()
        assert original, "Segment under edit must have text"

        first_segment.hover()
        # Was `if edit_btn.count() == 0: pytest.skip(...)`. The segment being hovered
        # now belongs to a transcript this suite created, so the edit affordance is
        # either there or the feature is broken — there is no third case to skip for.
        edit_btn = first_segment.locator(".edit-button")
        expect(edit_btn.first).to_be_visible(timeout=5000)
        edit_btn.first.click()

        textarea = detail_page.locator(".segment-textarea")
        expect(textarea.first).to_be_visible(timeout=5000)
        textarea.first.fill("E2E EDIT THAT MUST NEVER PERSIST")

        detail_page.locator(".segment-edit-actions .cancel-button").first.click()
        expect(detail_page.locator(".segment-textarea")).to_have_count(0, timeout=5000)

        restored = (first_segment.locator(".segment-text").first.text_content() or "").strip()
        assert restored == original, "Cancel must restore the original segment text"

    def test_edit_segment_save_persists_across_reload(
        self, browser, auth_storage_state: str, admin_token: str, base_url: str, owned_media_factory
    ) -> None:
        """Saving an edited segment survives a full page reload (not just in-memory state).

        Uses its own ephemeral file rather than ``detail_page``'s ambient one, because
        this test — unlike its cancel-path sibling — genuinely persists a write.
        """
        media = owned_media_factory(admin_token)

        context = browser.new_context(
            storage_state=auth_storage_state,
            viewport={"width": 1920, "height": 1080},
            ignore_https_errors=True,
        )
        page = context.new_page()
        try:
            page.goto(f"{base_url}/files/{media['uuid']}")
            page.wait_for_selector(".transcript-segment", timeout=25000)

            first_segment = page.locator(".transcript-segment").first
            original = (first_segment.locator(".segment-text").first.text_content() or "").strip()
            assert original, "Segment under edit must have text"

            unique_text = f"E2E SAVED EDIT {media['uuid'][:8]}"
            assert unique_text != original, "the edit must actually change the text"

            first_segment.hover()
            # Same reasoning as the cancel-path sibling: `media` is this test's own
            # upload, so a missing edit affordance is a failure, not a skip.
            edit_btn = first_segment.locator(".edit-button")
            expect(edit_btn.first).to_be_visible(timeout=5000)
            edit_btn.first.click()

            textarea = page.locator(".segment-textarea")
            expect(textarea.first).to_be_visible(timeout=5000)
            textarea.first.fill(unique_text)

            page.locator(".segment-edit-actions .save-button").first.click()
            expect(page.locator(".segment-textarea")).to_have_count(0, timeout=10000)

            saved = (first_segment.locator(".segment-text").first.text_content() or "").strip()
            assert saved == unique_text, "Save must reflect the edited text immediately"

            page.reload()
            page.wait_for_selector(".transcript-segment", timeout=25000)
            after_reload = page.locator(".transcript-segment").first.locator(".segment-text").first
            expect(after_reload).to_have_text(unique_text, timeout=15000)
        finally:
            page.close()
            context.close()


class TestSpeakerEditor:
    """The Edit Speakers affordance opens the speaker editor when diarized."""

    def test_edit_speakers_opens_editor(self, detail_page: Page) -> None:
        """Edit Speakers reveals the speaker editor.

        The "if diarization is present" hedge — and the skip under it — existed
        because the file came from the dev library and might have had one speaker.
        ``owned_diarized_transcript_file`` carries two by construction, so the
        affordance is required.
        """
        edit_btn = detail_page.locator(".edit-speakers-button")
        expect(edit_btn).to_be_visible(timeout=10000)
        edit_btn.click()
        editor = detail_page.locator(".speaker-editor-container")
        expect(editor).to_be_visible(timeout=10000)
        expect(detail_page.locator(".speaker-editor-header")).to_be_visible(timeout=5000)


class TestSpeakerRenameRepaint:
    """Renaming a speaker repaints the transcript without a page reload (issue #352).

    The transcript renders from ``file.grouped_segments``, which used to embed its own
    copies of every segment while optimistic updates patched ``file.transcript_segments``
    — a different set of objects. The rename saved to the database and then rendered
    nothing until a full reload. Groups now reference segments by uuid, so there is one
    segment object and a patch cannot miss it.

    The ``PUT`` is stubbed at the network boundary and **nothing is written**: a real
    rename dispatches retroactive matching, which auto-applies the new label to matching
    speakers in OTHER files and is not reversible by renaming back. Everything in front
    of the network — the optimistic write, ``segmentSync``, group resolution, the repaint
    — runs for real.
    """

    def test_rename_repaints_transcript_without_reload(self, detail_page: Page) -> None:
        edit_btn = detail_page.locator(".edit-speakers-button")
        expect(edit_btn).to_be_visible(timeout=10000)

        new_name = "E2E Repaint Check"

        def _stub_put(route):  # type: ignore[no-untyped-def]
            if route.request.method != "PUT":
                return route.continue_()
            return route.fulfill(
                status=200,
                content_type="application/json",
                body=f'{{"uuid":"stub","display_name":"{new_name}","verified":true}}',
            )

        detail_page.route("**/api/speakers/*", _stub_put)

        edit_btn.click()
        expect(detail_page.locator(".speaker-editor-container")).to_be_visible(timeout=10000)

        # Rename the first speaker whose label input is editable.
        # The owned transcript has two speakers, so the editor must expose an input;
        # `expect` also waits for it, which `.count()` never did.
        name_input = detail_page.locator(".speaker-editor-container input").first
        expect(name_input).to_be_visible(timeout=10000)
        name_input.fill(new_name)

        save_btn = detail_page.locator(".save-speakers-button")
        expect(save_btn).to_be_enabled(timeout=5000)
        save_btn.click()

        # A profile-linked speaker asks how to treat its profile before saving.
        confirm = detail_page.locator('.modal-overlay button:has-text("Create New Profile")')
        if confirm.count():
            confirm.click()

        # The assertion that fails against the pre-#352 build: the label repaints in the
        # transcript itself, with no navigation.
        expect(
            detail_page.locator(".transcript-display").get_by_text(new_name).first
        ).to_be_visible(timeout=10000)

        assert _unexpected_console_errors(detail_page._console_errors) == []  # type: ignore[attr-defined]


@pytest.fixture(scope="module")
def paginated_file(api_token: str, backend_url: str, owned_long_transcript_file) -> dict[str, Any]:
    """The suite's OWN transcript, longer than one 500-segment page.

    A transcript that long is not something a developer's library reliably holds —
    ten-minute recordings do not reach 500 segments — so this skipped on essentially
    every stack, including this one (measured 2026-09-07: the only skip in the whole
    module). The pagination invariants it guards are real: the "N of M loaded"
    counter that advanced while rendering nothing, and the duplicate
    ``overlap_group_id`` that made Svelte throw and took down the entire transcript
    list. Neither has been exercised by an E2E run in a long time.

    ``owned_long_transcript_file`` injects 560 segments in about two seconds — no
    ASR, no media — and deletes them at session teardown.

    Returns:
        A mapping carrying at least ``uuid``, matching the shape the test reads.
    """
    total = requests.get(
        f"{backend_url}/api/files/{owned_long_transcript_file['uuid']}/segments",
        headers={"Authorization": f"Bearer {api_token}"},
        params={"segment_limit": "1"},
        timeout=30,
    )
    assert total.status_code == 200, f"could not read owned segments: {total.text[:300]}"
    reported = total.json().get("total_segments", 0)
    assert reported > 500, (
        f"the owned transcript reports {reported} segments through the API; the "
        "pagination invariants need more than one 500-segment page"
    )
    return {"uuid": owned_long_transcript_file["uuid"], "total_segments": reported}


class TestTranscriptPagination:
    """Long transcripts page in fully, and never render a segment twice (issue #352).

    Two failure modes this guards, both of which shipped:

    * ``GET /files/{uuid}/segments`` returned no grouping, so scrolling past segment 500
      advanced the "N of M loaded" counter while rendering nothing new.
    * An overlap run split across the page boundary yields two groups sharing one
      ``overlap_group_id``. Rows keyed by that id collide, and a duplicate key makes
      Svelte throw at render time — taking down the entire transcript list, not one row.

    Read-only: scrolling mutates nothing.
    """

    def test_scrolling_loads_and_renders_every_segment(
        self, browser, auth_storage_state: str, paginated_file: dict[str, Any], base_url: str
    ) -> None:  # type: ignore[no-untyped-def]
        context = browser.new_context(
            storage_state=auth_storage_state,
            viewport={"width": 1920, "height": 1080},
            ignore_https_errors=True,
        )
        page = context.new_page()
        errors: list[str] = []
        page.on("console", lambda m: errors.append(m.text) if m.type == "error" else None)
        try:
            page.goto(f"{base_url}/files/{paginated_file['uuid']}")
            page.wait_for_load_state("networkidle")
            page.wait_for_selector(".transcript-segment", timeout=25000)

            rendered = page.locator("[data-segment-id]").count()
            assert rendered > 0

            # Drive the real infinite-scroll sentinel until it stops yielding rows.
            previous = -1
            for _ in range(40):
                count = page.evaluate(
                    "() => { const el = document.querySelector('.transcript-display');"
                    " if (el) el.scrollTop = el.scrollHeight;"
                    " return document.querySelectorAll('[data-segment-id]').length; }"
                )
                if count == previous:
                    break
                previous = count
                # Kept deliberately: this is the poll interval of the loop itself. The exit
                # condition is "the row count STOPPED growing", i.e. the absence of new
                # rows, which no locator can auto-wait for (issue #431).
                page.wait_for_timeout(900)

            ids = page.eval_on_selector_all(
                "[data-segment-id]", "els => els.map(e => e.dataset.segmentId)"
            )
            indices = page.eval_on_selector_all(
                "[data-seg-index]", "els => els.map(e => Number(e.dataset.segIndex))"
            )

            # Rendered past the first page at all.
            assert len(ids) > 500, f"pagination stalled at {len(ids)} rendered segments"
            # No segment mounted twice — a duplicate key would have thrown before here.
            assert len(ids) == len(set(ids)), "a segment was rendered more than once"
            assert len(indices) == len(set(indices)), "duplicate data-seg-index"
            # Indices stay global across pages; page-local ones invert the progress bar.
            assert indices == sorted(indices), "segment indices are not monotonic"

            assert _unexpected_console_errors(errors) == []
        finally:
            page.close()
            context.close()


def _pick_search_word(page: Page) -> str:
    """Pick a real word (>=4 letters) from the first transcript segment.

    Guarantees the transcript find bar will have at least one literal match to
    highlight and navigate, without mutating any data.

    Stripping non-alpha characters from a token can produce a string that is no
    longer a literal substring of the source text — a contraction like "Code's"
    cleans to "Codes", which never appears in "Code's" itself. The frontend's
    highlighter (`searchHighlight.ts`) does a plain case-insensitive
    `indexOf`, so searching for such a word finds zero matches and the test
    times out waiting for a highlight that can never render. Verify the
    cleaned candidate is actually present in the text before returning it.
    """
    text = (page.locator(".transcript-segment .segment-text").first.text_content() or "").strip()
    lowered = text.lower()
    for token in text.split():
        cleaned = "".join(ch for ch in token if ch.isalpha())
        if len(cleaned) >= 4 and cleaned.lower() in lowered:
            return cleaned
    return "the"


def _open_transcript_search(page: Page):  # type: ignore[no-untyped-def]
    """Open the in-transcript find bar and return its input locator."""
    trigger = page.locator(".search-trigger-button")
    expect(trigger).to_be_visible(timeout=10000)
    trigger.click()
    search_input = page.locator(".transcript-search input")
    expect(search_input).to_be_visible(timeout=5000)
    return search_input


class TestTranscriptSearch:
    """In-transcript find bar: open, match, highlight, navigate, close.

    Read-only: search never edits the transcript.
    """

    def test_search_bar_opens_from_trigger(self, detail_page: Page) -> None:
        """The find trigger reveals a search input in the transcript header."""
        _open_transcript_search(detail_page)
        expect(detail_page.locator(".transcript-search .search-bar")).to_be_visible(timeout=5000)

    def test_query_highlights_and_counts_matches(self, detail_page: Page) -> None:
        """Typing a real word highlights matches and shows an 'N of M' counter."""
        search_input = _open_transcript_search(detail_page)
        word = _pick_search_word(detail_page)
        search_input.fill(word)

        # Literal matches highlight in the (loaded) transcript.
        expect(detail_page.locator(".transcript-search-highlight").first).to_be_visible(
            timeout=8000
        )
        # The counter resolves to a match count (backend completeness probe may add '+').
        # The loading status reuses .search-bar-counter, so target the match counter only.
        counter = detail_page.locator(
            ".transcript-search .search-bar-counter:not(.search-bar-status-text)"
        )
        expect(counter).to_be_visible(timeout=10000)
        assert "of" in (counter.text_content() or ""), "Counter should read 'N of M'"

    def test_next_navigation_flashes_current_match(self, detail_page: Page) -> None:
        """Advancing to the next match scrolls to and flashes that segment."""
        search_input = _open_transcript_search(detail_page)
        search_input.fill(_pick_search_word(detail_page))
        expect(detail_page.locator(".transcript-search-highlight").first).to_be_visible(
            timeout=8000
        )
        # Second nav button in the shared SearchBar is "next".
        detail_page.locator(".transcript-search .search-bar-btn").nth(1).click()
        # The active match gets the whole-segment pulse class.
        expect(detail_page.locator(".search-current-match").first).to_be_visible(timeout=5000)

    def test_escape_closes_search(self, detail_page: Page) -> None:
        """Escape closes the find bar and restores the trigger button."""
        search_input = _open_transcript_search(detail_page)
        search_input.fill(_pick_search_word(detail_page))
        detail_page.keyboard.press("Escape")
        expect(detail_page.locator(".search-trigger-button")).to_be_visible(timeout=5000)

    def test_transcript_search_no_unexpected_console_errors(self, detail_page: Page) -> None:
        """Searching the transcript produces no new (non-benign) console errors."""
        search_input = _open_transcript_search(detail_page)
        search_input.fill(_pick_search_word(detail_page))
        # Kept deliberately: allow the debounced backend completeness probe to fire AND
        # resolve. networkidle can return before a debounced request has even started, and
        # the assertion is the absence of console errors from it (issue #431).
        detail_page.wait_for_timeout(3500)
        unexpected = _unexpected_console_errors(detail_page._console_errors)  # type: ignore[attr-defined]
        assert not unexpected, f"Unexpected console errors during transcript search: {unexpected}"


def _accept_and_record(page: Page) -> list[str]:
    """Accept every dialog the page raises, returning the list it records them into.

    A ``lambda`` here would return the tuple ``(None, None)`` — harmless at runtime and a
    mypy ``func-returns-value`` error, since ``list.append`` returns nothing.
    """
    messages: list[str] = []

    def _handler(dialog) -> None:  # type: ignore[no-untyped-def]
        messages.append(dialog.message)
        dialog.accept()

    page.on("dialog", _handler)
    return messages


class TestUnsavedChangesGuard:
    """Leaving the page mid-edit must ask first (issue #787).

    Before this, ``SpeakerEditorPanel``'s "unsaved" dot was the entire protection: a nav
    click or a tab close discarded the work silently. Speaker naming is the most manual,
    least re-doable work in the product — and the one the app deliberately does NOT
    auto-apply — so dropping it without a word is the worst place to do it.

    Driven through the SEGMENT editor rather than the speaker panel because it dirties in
    one keystroke and the cancel path restores the original, which keeps this test inside
    the suite's "never persist changes to dev data" rule. The guard itself is one
    predicate over both surfaces (``routes/files/[id]/+page.svelte``: ``hasUnsavedEdits``),
    and its branch logic is unit-tested in
    ``frontend/src/lib/navigation/unsavedChangesGuard.test.ts``.
    """

    UNSAVED_TEXT = "E2E UNSAVED EDIT THAT MUST NEVER PERSIST"

    def _start_an_unsaved_edit(self, page: Page) -> None:
        first_segment = page.locator(".transcript-segment").first
        first_segment.hover()
        edit_btn = first_segment.locator(".edit-button")
        expect(edit_btn.first).to_be_visible(timeout=5000)
        edit_btn.first.click()
        textarea = page.locator(".segment-textarea")
        expect(textarea.first).to_be_visible(timeout=5000)
        textarea.first.fill(self.UNSAVED_TEXT)

    def test_declining_the_prompt_keeps_the_page_and_the_edit(self, detail_page: Page) -> None:
        """The whole point: cancelling must leave the user where they were, edits intact."""
        messages: list[str] = []

        def _decline(dialog) -> None:  # type: ignore[no-untyped-def]
            messages.append(dialog.message)
            dialog.dismiss()

        detail_page.on("dialog", _decline)
        before = detail_page.url

        try:
            self._start_an_unsaved_edit(detail_page)
            detail_page.locator('[data-testid="nav-gallery"]').click()

            expect(detail_page.locator(".segment-textarea").first).to_be_visible(timeout=5000)
            assert messages, "navigating away mid-edit asked nothing"
            assert detail_page.url == before, (
                f"declining the prompt still navigated: {before} -> {detail_page.url}"
            )
            assert detail_page.locator(".segment-textarea").first.input_value() == self.UNSAVED_TEXT
        finally:
            # Cancel path only — this suite never persists a transcript change.
            detail_page.locator(".segment-edit-actions .cancel-button").first.click()

    def test_accepting_the_prompt_leaves_the_page(self, detail_page: Page) -> None:
        """The must-not-fire half: the guard asks, it does not simply trap the user.

        Without this, an implementation that cancelled every navigation unconditionally
        would pass the test above.
        """
        _accept_and_record(detail_page)

        self._start_an_unsaved_edit(detail_page)
        detail_page.locator('[data-testid="nav-gallery"]').click()

        # Discarding is what the user asked for; the edit was never saved, so nothing
        # persisted either way.
        expect(detail_page).not_to_have_url(re.compile(r"/files/[0-9a-f-]{36}"), timeout=10000)

    def test_a_clean_page_navigates_with_no_prompt(self, detail_page: Page) -> None:
        """The control. A guard that prompts on a clean page is a guard nobody reads."""
        messages = _accept_and_record(detail_page)

        detail_page.locator('[data-testid="nav-gallery"]').click()

        expect(detail_page).not_to_have_url(re.compile(r"/files/[0-9a-f-]{36}"), timeout=10000)
        assert not messages, f"a clean transcript page prompted on the way out: {messages}"

    def test_opening_the_editor_without_typing_is_not_unsaved(self, detail_page: Page) -> None:
        """Opening the editor is not an edit.

        A dirty flag of "is an editor open" would prompt on every accidental click of the
        pencil, which is the fastest way to train people to click through the prompt.
        """
        messages = _accept_and_record(detail_page)

        first_segment = detail_page.locator(".transcript-segment").first
        first_segment.hover()
        edit_btn = first_segment.locator(".edit-button")
        expect(edit_btn.first).to_be_visible(timeout=5000)
        edit_btn.first.click()
        expect(detail_page.locator(".segment-textarea").first).to_be_visible(timeout=5000)

        detail_page.locator('[data-testid="nav-gallery"]').click()

        expect(detail_page).not_to_have_url(re.compile(r"/files/[0-9a-f-]{36}"), timeout=10000)
        assert not messages, f"an untouched editor counted as an unsaved edit: {messages}"
