"""
E2E smoke tests for the file-detail transcript view.

Regression safety net ahead of the frontend component refactor
(branch: refactor/frontend-overhaul). These are deliberately *tolerant*
(Playwright auto-waiting, data-discovered file, benign console-error
filtering) rather than brittle — they guard the high-value surfaces:

- The file-detail page renders a transcript (at least one segment).
- The transcript Export control opens and lists txt/json/csv/srt/vtt
  (guards the formatter + export refactor).
- The "Edit Speakers" affordance opens the speaker editor when the file
  has diarization.

Requirements:
- Dev environment running: ./opentr.sh start dev
- At least one completed, transcribed file in the dev dataset
- Frontend at localhost:5173, Backend at localhost:5174
  (admin@example.com / password)

Run (headless):
    pytest backend/tests/e2e/test_file_detail_transcript.py -v

Run (visible on XRDP):
    DISPLAY=:11 pytest backend/tests/e2e/test_file_detail_transcript.py -v --headed
"""

from __future__ import annotations

import os
import tempfile
from typing import Any

import pytest
import requests
from playwright.sync_api import Page
from playwright.sync_api import expect

FRONTEND_URL = os.environ.get("E2E_FRONTEND_URL", "http://localhost:5173")
BACKEND_URL = os.environ.get("E2E_BACKEND_URL", "http://localhost:5174")
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
def api_token() -> str:
    """Authenticate once per module via the backend API."""
    resp = requests.post(
        f"{BACKEND_URL}/api/auth/token",
        data={"username": TEST_ADMIN_EMAIL, "password": TEST_ADMIN_PASSWORD},
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        timeout=30,
    )
    if resp.status_code != 200:
        pytest.skip(f"Cannot authenticate against dev stack (HTTP {resp.status_code})")
    return str(resp.json()["access_token"])


@pytest.fixture(scope="module")
def transcribed_file(api_token: str) -> dict[str, Any]:
    """Discover a completed file that actually has transcript segments.

    Prefers a file with diarization (>1 distinct speaker) so the
    speaker-editor assertion has something to exercise; falls back to any
    transcribed file. Skips (rather than fails) if the dev dataset has none.
    """
    listing = requests.get(
        f"{BACKEND_URL}/api/files",
        headers={"Authorization": f"Bearer {api_token}"},
        params={"page": "1", "page_size": "100", "sort_by": "upload_time", "sort_order": "desc"},
        timeout=30,
    )
    items: list[dict[str, Any]] = listing.json().get("items", listing.json().get("files", []))
    completed = [f for f in items if f.get("status") == "completed"]
    if not completed:
        pytest.skip("No completed file in dev dataset — required for transcript E2E tests")

    best: dict[str, Any] | None = None
    fallback: dict[str, Any] | None = None
    for f in completed:
        detail = requests.get(
            f"{BACKEND_URL}/api/files/{f['uuid']}",
            headers={"Authorization": f"Bearer {api_token}"},
            timeout=30,
        ).json()
        segments = detail.get("transcript_segments") or []
        if not segments:
            continue
        fallback = fallback or detail
        speakers = {
            (s.get("speaker") or {}).get("display_name")
            or (s.get("speaker") or {}).get("name")
            or s.get("speaker_label")
            for s in segments
        }
        speakers.discard(None)
        if len(speakers) > 1:
            best = detail
            break

    target = best or fallback
    if not target:
        pytest.skip("No completed file has transcript segments — required for transcript E2E tests")
    assert target is not None  # narrowed for mypy (pytest.skip above raises)
    return target


# ---------------------------------------------------------------------------
# Module-scoped auth: log in ONCE via the form, reuse cookies for every test.
# Per-test form logins trip the backend's per-IP auth rate limiting (the same
# reason test_gallery_actions.py / test_media_download.py log in once). When
# the wider e2e suite has already spent the rate-limit budget, the login can
# briefly bounce; retry through that window rather than flapping.
# ---------------------------------------------------------------------------
def _form_login_with_retry(page, attempts: int = 4) -> None:
    """Submit the login form, retrying through transient auth rate-limiting."""
    last_error: Exception | None = None
    for attempt in range(attempts):
        try:
            page.goto(FRONTEND_URL)
            # Already authenticated (cookie still valid) — no form to fill.
            if page.locator(".user-button").count():
                page.wait_for_selector(".user-button", timeout=10000)
                return
            page.wait_for_selector("#email", timeout=15000)
            page.fill("#email", TEST_ADMIN_EMAIL)
            page.fill("#password", TEST_ADMIN_PASSWORD)
            page.click("button[type=submit]")
            page.wait_for_selector(".user-button", timeout=20000)
            return
        except Exception as exc:  # noqa: BLE001 - retry on any login-flow failure
            last_error = exc
            page.wait_for_timeout(5000 * (attempt + 1))
    raise AssertionError(f"Could not log in via form after {attempts} attempts: {last_error}")


@pytest.fixture(scope="module")
def auth_storage_state(browser):  # type: ignore[no-untyped-def]
    """Login once and persist browser storage state for reuse across tests."""
    context = browser.new_context(
        viewport={"width": 1920, "height": 1080}, ignore_https_errors=True
    )
    page = context.new_page()
    _form_login_with_retry(page)

    fd, state_file = tempfile.mkstemp(suffix=".json")
    os.close(fd)
    context.storage_state(path=state_file)
    page.close()
    context.close()

    yield state_file

    if os.path.exists(state_file):
        os.unlink(state_file)


@pytest.fixture
def detail_page(browser, auth_storage_state: str, transcribed_file: dict[str, Any]):  # type: ignore[no-untyped-def]
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
    page.goto(f"{FRONTEND_URL}/files/{uuid}")
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
        detail_page.wait_for_timeout(1500)
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


class TestSpeakerEditor:
    """The Edit Speakers affordance opens the speaker editor when diarized."""

    def test_edit_speakers_opens_editor(self, detail_page: Page) -> None:
        """If diarization is present, Edit Speakers reveals the speaker editor."""
        edit_btn = detail_page.locator(".edit-speakers-button")
        if edit_btn.count() == 0:
            pytest.skip("File has no diarization (no Edit Speakers affordance)")

        expect(edit_btn).to_be_visible(timeout=10000)
        edit_btn.click()
        editor = detail_page.locator(".speaker-editor-container")
        expect(editor).to_be_visible(timeout=10000)
        expect(detail_page.locator(".speaker-editor-header")).to_be_visible(timeout=5000)
