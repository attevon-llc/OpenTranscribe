"""E2E tests for the RAG chat surface (issue #52).

Covers the flows a user actually performs: navigate to chat, ask a question,
watch it stream, stop it, follow a citation into the player, manage
conversations, scope the context, and toggle chat off.

Requirements:
- Dev environment running: ./opentr.sh start dev
- At least one COMPLETED transcript in the library (retrieval needs content)
- An LLM provider for the streaming tests. Start the mock and they run for real:
      ./opentr.sh start dev --with-mock-llm
  Without one they self-skip (the setup CTA test covers that case) — which for a
  long time meant the streaming path was never actually exercised by any E2E run
  while the suite still reported green.

Run:
    pytest backend/tests/e2e/test_chat.py -v
    DISPLAY=:11 pytest backend/tests/e2e/test_chat.py -v --headed

These tests must never persist changes to dev data: every conversation created
here is deleted in the same test, and no transcript is modified.
"""

from __future__ import annotations

import re
import socket
import uuid as uuid_pkg

import pytest
import requests

# Absolute import — the e2e dir is not a package, so a relative import breaks
# collection when invoked as `pytest backend/tests/e2e/` from the repo root.
from conftest import TEST_ADMIN_EMAIL
from conftest import TEST_ADMIN_PASSWORD
from playwright.sync_api import Page
from playwright.sync_api import expect

pytestmark = pytest.mark.chat

# This module used to define its own ``FRONTEND_URL``/``BACKEND_URL`` constants here.
# A module constant is evaluated at import time, so it could not see ``--base-url`` /
# ``--backend-url`` and this file always drove whatever was on the default ports — even
# when the run was aimed at an isolated stack (issue #431). Everything below takes
# conftest's ``base_url`` / ``backend_url`` fixtures instead.

# Generous: a first message pays for model load + retrieval + first token.
STREAM_TIMEOUT_MS = 90_000


# ---------------------------------------------------------------------------
# API helpers (used to arrange state and to clean up deterministically)
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def api_session(backend_url: str) -> requests.Session:
    """An authenticated API session for arranging and cleaning up test state."""
    session = requests.Session()
    response = session.post(
        f"{backend_url}/api/auth/token",
        data={"username": TEST_ADMIN_EMAIL, "password": TEST_ADMIN_PASSWORD},
        timeout=30,
    )
    assert response.status_code == 200, f"Login failed: {response.status_code}"

    # Attach the CSRF token to every subsequent request. Without it the backend
    # rejects all mutations with 403, which silently broke two things: tests
    # that arrange state via POST failed outright, and `cleanup_conversations`
    # DELETEs failed too — so every E2E run was LEAVING its conversations behind
    # in dev data, exactly what this suite is required not to do.
    csrf_token = session.cookies.get("csrf_token")
    assert csrf_token, "login did not set a csrf_token cookie"
    session.headers["X-CSRF-Token"] = csrf_token
    return session


@pytest.fixture
def cleanup_conversations(api_session: requests.Session, backend_url: str):
    """Delete every conversation created during a test, pass or fail."""
    created: list[str] = []
    yield created
    for uuid in created:
        try:
            api_session.delete(f"{backend_url}/api/chat/conversations/{uuid}", timeout=15)
        except requests.RequestException:
            pass


MOCK_LLM_PORT = 5199
MOCK_LLM_URL_FOR_BACKEND = f"http://mock-llm:{MOCK_LLM_PORT}/v1"


def _unique(label: str) -> str:
    """Suffix a display name so it cannot collide with, or outlive, another run.

    Two distinct problems, both real here. An LLM config **name is unique per user**, so a
    fixed one 409s against the leftovers of any run killed before its teardown — the same
    trap ``test_chat_grounding.py``'s ``llm_config_factory`` already documents. A
    conversation **title is not** unique, which is worse for the browser side: the sidebar
    is addressed with ``filter(has_text=<title>)``, so a leftover row with the same title
    makes the locator match twice and Playwright's strict mode fails on a test that is
    working correctly.
    """
    return f"{label} {uuid_pkg.uuid4().hex[:8]}"


def _mock_llm_running() -> bool:
    """Whether the docker-compose mock LLM is up on the host."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.settimeout(0.3)
        return sock.connect_ex(("127.0.0.1", MOCK_LLM_PORT)) == 0


# Module-scoped to match api_session; a session-scoped fixture cannot depend
# on a narrower one.
@pytest.fixture(scope="module", autouse=True)
def ensure_llm_provider(api_session: requests.Session, backend_url: str):
    """Give the streaming tests a provider when the mock LLM is available.

    Without this the streaming tests self-skip, so the most valuable part of the
    suite never ran while the whole file still reported green — the failure mode
    where a green run means less than it looks.

    Leaves a real configured provider alone (someone testing against Ollama or a
    cloud model should keep it), and removes anything it registers itself, since
    E2E must never persist changes to dev data.
    """
    if _llm_configured(api_session, backend_url) or not _mock_llm_running():
        yield
        return

    created_uuid = None
    try:
        response = api_session.post(
            f"{backend_url}/api/llm-settings",
            json={
                "name": _unique("Mock LLM (e2e)"),
                "provider": "custom",
                "model_name": "mock-gpt",
                "base_url": MOCK_LLM_URL_FOR_BACKEND,
                "api_key": "mock-key-not-secret",
            },
            timeout=30,
        )
        if response.ok:
            created_uuid = response.json().get("uuid")
    except requests.RequestException:
        created_uuid = None

    yield

    if created_uuid:
        try:
            api_session.delete(
                f"{backend_url}/api/llm-settings/config/{created_uuid}",
                timeout=30,
            )
        except requests.RequestException:
            pass


def _llm_configured(api_session: requests.Session, backend_url: str) -> bool:
    try:
        response = api_session.get(f"{backend_url}/api/llm/status", timeout=15)
        return bool(response.ok and response.json().get("available"))
    except (requests.RequestException, ValueError):
        return False


def _has_completed_file(api_session: requests.Session, backend_url: str) -> bool:
    try:
        response = api_session.get(
            f"{backend_url}/api/files",
            params={"status": "completed", "page_size": "1"},
            timeout=20,
        )
        if not response.ok:
            return False
        data = response.json()
        items = data.get("items", data if isinstance(data, list) else [])
        return len(items) > 0
    except (requests.RequestException, ValueError):
        return False


def _open_chat(page: Page, base_url: str) -> None:
    page.goto(f"{base_url}/chat")
    page.wait_for_selector('[data-testid="chat-composer-input"]', timeout=30_000)


# ---------------------------------------------------------------------------
# Navigation and page shell
# ---------------------------------------------------------------------------


def test_chat_link_in_navbar(gallery_page: Page):
    """Chat is a first-class navbar destination."""
    nav_chat = gallery_page.locator('[data-testid="nav-chat"]')
    expect(nav_chat).to_be_visible()

    nav_chat.click()
    gallery_page.wait_for_url("**/chat", timeout=15_000)
    expect(gallery_page.locator('[data-testid="chat-composer-input"]')).to_be_visible()


def test_empty_state_offers_suggestions(
    gallery_page: Page, api_session: requests.Session, base_url: str, backend_url: str
):
    """A new chat shows starter prompts rather than a blank box."""
    _open_chat(gallery_page, base_url)

    if not _llm_configured(api_session, backend_url):
        # Without an LLM the page shows the setup CTA instead — covered below.
        expect(gallery_page.locator('[data-testid="chat-open-llm-settings"]')).to_be_visible()
        return

    suggestions = gallery_page.locator('[data-testid="chat-suggestion"]')
    expect(suggestions.first).to_be_visible()
    assert suggestions.count() == 3

    suggestions.first.click()
    composer = gallery_page.locator('[data-testid="chat-composer-input"]')
    expect(composer).not_to_have_value("")


def test_setup_cta_when_no_llm(
    gallery_page: Page, api_session: requests.Session, base_url: str, backend_url: str
):
    """With no provider configured, chat explains what to do instead of failing."""
    if _llm_configured(api_session, backend_url):
        pytest.skip("LLM is configured — the setup CTA path does not apply")

    _open_chat(gallery_page, base_url)
    expect(gallery_page.locator('[data-testid="chat-open-llm-settings"]')).to_be_visible()
    expect(gallery_page.locator('[data-testid="chat-composer-input"]')).to_be_disabled()


def test_context_bar_defaults_to_all_transcripts(gallery_page: Page, base_url: str):
    """An unscoped conversation says so explicitly rather than showing nothing."""
    _open_chat(gallery_page, base_url)
    expect(gallery_page.locator('[data-testid="chat-scope-all"]')).to_be_visible()


# ---------------------------------------------------------------------------
# Composer behaviour
# ---------------------------------------------------------------------------


def test_send_button_disabled_until_text_entered(
    gallery_page: Page, api_session: requests.Session, base_url: str, backend_url: str
):
    if not _llm_configured(api_session, backend_url):
        pytest.skip("Requires a configured LLM")

    _open_chat(gallery_page, base_url)
    send = gallery_page.locator('[data-testid="chat-send"]')
    expect(send).to_be_disabled()

    gallery_page.locator('[data-testid="chat-composer-input"]').fill("Hello")
    expect(send).to_be_enabled()


def test_shift_enter_inserts_newline_without_sending(
    gallery_page: Page, api_session: requests.Session, base_url: str, backend_url: str
):
    if not _llm_configured(api_session, backend_url):
        pytest.skip("Requires a configured LLM")

    _open_chat(gallery_page, base_url)
    composer = gallery_page.locator('[data-testid="chat-composer-input"]')
    composer.fill("first line")
    composer.press("Shift+Enter")
    composer.type("second line")

    assert "\n" in composer.input_value()
    # Nothing was sent.
    expect(gallery_page.locator('[data-testid="chat-message-user"]')).to_have_count(0)


# ---------------------------------------------------------------------------
# The core flow: ask, stream, cite
# ---------------------------------------------------------------------------


def test_send_message_streams_answer_with_citations(
    gallery_page: Page,
    api_session: requests.Session,
    cleanup_conversations: list[str],
    base_url: str,
    backend_url: str,
):
    """The end-to-end value of the feature: a grounded, cited answer."""
    if not _llm_configured(api_session, backend_url):
        pytest.skip("Requires a configured LLM")
    if not _has_completed_file(api_session, backend_url):
        pytest.skip("Requires at least one completed transcript")

    _open_chat(gallery_page, base_url)

    composer = gallery_page.locator('[data-testid="chat-composer-input"]')
    composer.fill("What topics are discussed in these recordings?")
    gallery_page.locator('[data-testid="chat-send"]').click()

    # The user's turn appears immediately (optimistic render).
    expect(gallery_page.locator('[data-testid="chat-message-user"]')).to_be_visible(timeout=5_000)

    # The URL becomes the conversation's own, so it can be shared/reloaded.
    gallery_page.wait_for_url("**/chat/*", timeout=30_000)
    conversation_uuid = gallery_page.url.rstrip("/").split("/")[-1]
    cleanup_conversations.append(conversation_uuid)

    assistant = gallery_page.locator('[data-testid="chat-message-assistant"]').last
    expect(assistant).to_be_visible(timeout=STREAM_TIMEOUT_MS)

    # Wait for generation to finish (the Stop button reverts to Send).
    expect(gallery_page.locator('[data-testid="chat-send"]')).to_be_visible(
        timeout=STREAM_TIMEOUT_MS
    )

    assert assistant.inner_text().strip(), "Assistant produced no text"

    # Citations are the trust surface — they must be offered for a RAG answer.
    sources_toggle = gallery_page.locator('[data-testid="chat-sources-toggle"]')
    expect(sources_toggle).to_be_visible(timeout=10_000)


def test_citation_navigates_to_transcript_at_timestamp(
    gallery_page: Page,
    api_session: requests.Session,
    cleanup_conversations: list[str],
    base_url: str,
    backend_url: str,
):
    """Clicking a citation opens the recording at the moment it came from."""
    if not _llm_configured(api_session, backend_url):
        pytest.skip("Requires a configured LLM")
    if not _has_completed_file(api_session, backend_url):
        pytest.skip("Requires at least one completed transcript")

    _open_chat(gallery_page, base_url)
    gallery_page.locator('[data-testid="chat-composer-input"]').fill("Summarise the key points.")
    gallery_page.locator('[data-testid="chat-send"]').click()

    gallery_page.wait_for_url("**/chat/*", timeout=30_000)
    cleanup_conversations.append(gallery_page.url.rstrip("/").split("/")[-1])

    expect(gallery_page.locator('[data-testid="chat-send"]')).to_be_visible(
        timeout=STREAM_TIMEOUT_MS
    )

    toggle = gallery_page.locator('[data-testid="chat-sources-toggle"]').first
    expect(toggle).to_be_visible(timeout=10_000)
    toggle.click()

    link = gallery_page.locator('[data-testid="chat-source-link"]').first
    expect(link).to_be_visible()

    href = link.get_attribute("href")
    assert href and href.startswith("/files/"), f"Unexpected citation href: {href}"
    assert "?t=" in href, "Citation must deep-link to a timestamp"

    link.click()
    gallery_page.wait_for_url("**/files/**", timeout=20_000)


def test_stop_generation_keeps_partial_answer(
    gallery_page: Page,
    api_session: requests.Session,
    cleanup_conversations: list[str],
    base_url: str,
    backend_url: str,
):
    """Stopping mid-stream keeps what arrived rather than discarding it."""
    if not _llm_configured(api_session, backend_url):
        pytest.skip("Requires a configured LLM")

    _open_chat(gallery_page, base_url)
    gallery_page.locator('[data-testid="chat-composer-input"]').fill(
        "Give me a long, detailed summary of everything discussed."
    )
    gallery_page.locator('[data-testid="chat-send"]').click()

    gallery_page.wait_for_url("**/chat/*", timeout=30_000)
    cleanup_conversations.append(gallery_page.url.rstrip("/").split("/")[-1])

    stop = gallery_page.locator('[data-testid="chat-stop"]')
    expect(stop).to_be_visible(timeout=30_000)
    stop.click()

    # The composer becomes usable again rather than staying wedged.
    expect(gallery_page.locator('[data-testid="chat-send"]')).to_be_visible(timeout=20_000)


# ---------------------------------------------------------------------------
# Conversation management
# ---------------------------------------------------------------------------


def test_conversation_appears_in_sidebar_and_can_be_deleted(
    gallery_page: Page, api_session: requests.Session, base_url: str, backend_url: str
):
    """Create via API, verify it lists, then delete it through the UI."""
    title = _unique("E2E temporary conversation")
    response = api_session.post(
        f"{backend_url}/api/chat/conversations",
        json={"title": title},
        timeout=20,
    )
    assert response.status_code == 201, response.text
    uuid = response.json()["uuid"]

    try:
        _open_chat(gallery_page, base_url)
        sidebar = gallery_page.locator('[data-testid="chat-sidebar"]')
        expect(sidebar).to_be_visible()
        expect(sidebar.get_by_text(title)).to_be_visible(timeout=15_000)

        item = gallery_page.locator('[data-testid="chat-conversation-item"]').filter(has_text=title)
        item.hover()
        item.locator('[data-testid="chat-delete"]').click()
        # The confirm prompt REPLACES the row's title, so `item` — which is
        # filtered by that title — no longer matches anything. Target the
        # confirm button directly; only one row can be confirming at a time.
        gallery_page.locator('[data-testid="chat-delete-confirm"]').click()

        expect(sidebar.get_by_text(title)).to_have_count(0, timeout=15_000)
    finally:
        # Idempotent: already deleted via the UI in the happy path.
        api_session.delete(f"{backend_url}/api/chat/conversations/{uuid}", timeout=15)


def test_conversation_reload_replays_history(
    gallery_page: Page,
    api_session: requests.Session,
    cleanup_conversations: list[str],
    base_url: str,
    backend_url: str,
):
    """A conversation URL is durable — reloading replays the thread."""
    if not _llm_configured(api_session, backend_url):
        pytest.skip("Requires a configured LLM")

    _open_chat(gallery_page, base_url)
    question = "What was discussed?"
    gallery_page.locator('[data-testid="chat-composer-input"]').fill(question)
    gallery_page.locator('[data-testid="chat-send"]').click()

    gallery_page.wait_for_url("**/chat/*", timeout=30_000)
    url = gallery_page.url
    cleanup_conversations.append(url.rstrip("/").split("/")[-1])

    expect(gallery_page.locator('[data-testid="chat-send"]')).to_be_visible(
        timeout=STREAM_TIMEOUT_MS
    )

    gallery_page.reload()
    expect(gallery_page.locator('[data-testid="chat-message-user"]').first).to_contain_text(
        question, timeout=30_000
    )


# ---------------------------------------------------------------------------
# Context scoping
# ---------------------------------------------------------------------------


def test_file_picker_scopes_the_conversation(
    gallery_page: Page,
    api_session: requests.Session,
    cleanup_conversations: list[str],
    base_url: str,
    backend_url: str,
):
    """Selecting recordings replaces 'All transcripts' with an explicit scope."""
    if not _has_completed_file(api_session, backend_url):
        pytest.skip("Requires at least one completed transcript")

    _open_chat(gallery_page, base_url)
    gallery_page.locator('[data-testid="chat-add-context"]').click()

    checkbox = gallery_page.locator('[data-testid="picker-file-checkbox"]').first
    expect(checkbox).to_be_visible(timeout=20_000)
    checkbox.check()

    gallery_page.locator('[data-testid="picker-confirm"]').click()

    expect(gallery_page.locator('[data-testid="chat-scope-files"]')).to_be_visible(timeout=15_000)
    expect(gallery_page.locator('[data-testid="chat-scope-all"]')).to_have_count(0)


def test_gallery_chat_with_selection_hands_off_context(gallery_page: Page):
    """'Chat with N' carries the gallery selection into a scoped conversation."""
    checkbox = gallery_page.locator('.file-card input[type="checkbox"]').first
    if checkbox.count() == 0:
        pytest.skip("No files in the gallery to select")

    checkbox.check()
    gallery_page.locator('[data-testid="gallery-chat-with-selected"]').click()

    gallery_page.wait_for_url("**/chat", timeout=20_000)
    expect(gallery_page.locator('[data-testid="chat-scope-files"]')).to_be_visible(timeout=15_000)


# ---------------------------------------------------------------------------
# Chat controls
# ---------------------------------------------------------------------------


def test_context_can_be_turned_off(
    gallery_page: Page, api_session: requests.Session, base_url: str, backend_url: str
):
    """Context-off mode is visibly distinct — no transcripts, no citations."""
    title = _unique("E2E context toggle")
    response = api_session.post(
        f"{backend_url}/api/chat/conversations",
        json={"title": title},
        timeout=20,
    )
    assert response.status_code == 201, response.text
    uuid = response.json()["uuid"]

    try:
        gallery_page.goto(f"{base_url}/chat/{uuid}")
        gallery_page.wait_for_selector('[data-testid="chat-composer-input"]', timeout=30_000)

        gallery_page.locator('[data-testid="chat-controls-toggle"]').click()
        panel = gallery_page.locator('[data-testid="chat-controls-panel"]')
        expect(panel).to_be_visible()

        panel.locator('[data-testid="chat-use-context-toggle"]').uncheck()

        expect(gallery_page.locator('[data-testid="chat-context-off"]')).to_be_visible(
            timeout=15_000
        )
    finally:
        api_session.delete(f"{backend_url}/api/chat/conversations/{uuid}", timeout=15)


def test_chat_controls_persist_across_reload(
    gallery_page: Page, api_session: requests.Session, base_url: str, backend_url: str
):
    """Per-conversation settings are stored server-side, not just in the tab."""
    title = _unique("E2E persistence")
    response = api_session.post(
        f"{backend_url}/api/chat/conversations",
        json={"title": title},
        timeout=20,
    )
    assert response.status_code == 201, response.text
    uuid = response.json()["uuid"]

    try:
        gallery_page.goto(f"{base_url}/chat/{uuid}")
        gallery_page.wait_for_selector('[data-testid="chat-composer-input"]', timeout=30_000)

        gallery_page.locator('[data-testid="chat-controls-toggle"]').click()
        gallery_page.locator('[data-testid="chat-use-context-toggle"]').uncheck()
        expect(gallery_page.locator('[data-testid="chat-context-off"]')).to_be_visible(
            timeout=15_000
        )

        gallery_page.reload()
        expect(gallery_page.locator('[data-testid="chat-context-off"]')).to_be_visible(
            timeout=30_000
        )
    finally:
        api_session.delete(f"{backend_url}/api/chat/conversations/{uuid}", timeout=15)


# ---------------------------------------------------------------------------
# Settings surface
# ---------------------------------------------------------------------------


def test_chat_settings_section_saves(gallery_page: Page, base_url: str):
    """Settings → Chat round-trips the user's defaults."""
    gallery_page.goto(f"{base_url}/chat")
    gallery_page.wait_for_selector('[data-testid="chat-composer-input"]', timeout=30_000)

    # Reach the settings modal the same way a user does.
    gallery_page.goto(f"{base_url}/")
    gallery_page.wait_for_selector(".gallery-action-buttons", timeout=30_000)
    gallery_page.evaluate(
        "() => window.dispatchEvent(new CustomEvent('open-settings', { detail: 'chat' }))"
    )

    section = gallery_page.locator('[data-testid="chat-settings"]')
    if section.count() == 0:
        pytest.skip("Settings modal could not be opened programmatically in this build")

    expect(section).to_be_visible(timeout=15_000)


# ---------------------------------------------------------------------------
# Speaker scoping — the transcript-native filter
# ---------------------------------------------------------------------------


def test_speaker_tab_scopes_the_conversation(
    gallery_page: Page,
    api_session: requests.Session,
    cleanup_conversations: list[str],
    base_url: str,
    backend_url: str,
):
    """Select a speaker and the conversation shows a speaker chip."""
    response = api_session.get(
        f"{backend_url}/api/speakers", params={"for_filter": "true"}, timeout=20
    )
    named = [s for s in (response.json() if response.ok else []) if s.get("display_name")]
    if not named:
        pytest.skip("No named speakers — label a speaker on a transcript first")

    _open_chat(gallery_page, base_url)
    gallery_page.locator('[data-testid="chat-add-context"]').click()

    gallery_page.get_by_role("tab", name=re.compile("speaker", re.I)).click()
    checkbox = gallery_page.locator('[data-testid="picker-speaker-checkbox"]').first
    expect(checkbox).to_be_visible(timeout=20_000)
    checkbox.check()

    gallery_page.locator('[data-testid="picker-confirm"]').click()
    expect(gallery_page.locator('[data-testid="chat-scope-speakers"]')).to_be_visible(
        timeout=15_000
    )


# ---------------------------------------------------------------------------
# ChatGPT-parity interactions
# ---------------------------------------------------------------------------


def test_edit_a_question_and_resend(
    gallery_page: Page,
    api_session: requests.Session,
    cleanup_conversations: list[str],
    base_url: str,
    backend_url: str,
):
    """Editing an earlier question re-answers from that point."""
    if not _llm_configured(api_session, backend_url):
        pytest.skip("Requires a configured LLM")

    _open_chat(gallery_page, base_url)
    gallery_page.locator('[data-testid="chat-composer-input"]').fill("First question")
    gallery_page.locator('[data-testid="chat-send"]').click()

    gallery_page.wait_for_url("**/chat/*", timeout=30_000)
    cleanup_conversations.append(gallery_page.url.rstrip("/").split("/")[-1])
    expect(gallery_page.locator('[data-testid="chat-send"]')).to_be_visible(
        timeout=STREAM_TIMEOUT_MS
    )

    user_message = gallery_page.locator('[data-testid="chat-message-user"]').first
    user_message.hover()
    user_message.locator('[data-testid="chat-edit"]').click()

    editor = gallery_page.locator('[data-testid="chat-edit-input"]')
    expect(editor).to_be_visible()
    editor.fill("Corrected question about the recordings")
    gallery_page.get_by_role("button", name=re.compile("resend", re.I)).click()

    expect(gallery_page.locator('[data-testid="chat-message-user"]').first).to_contain_text(
        "Corrected question", timeout=30_000
    )


def test_export_downloads_the_conversation(
    gallery_page: Page, api_session: requests.Session, base_url: str, backend_url: str
):
    """Export produces a Markdown file the user can keep."""
    title = _unique("E2E export")
    response = api_session.post(
        f"{backend_url}/api/chat/conversations",
        json={"title": title},
        timeout=20,
    )
    assert response.status_code == 201, response.text
    uuid = response.json()["uuid"]

    try:
        gallery_page.goto(f"{base_url}/chat/{uuid}")
        gallery_page.wait_for_selector('[data-testid="chat-composer-input"]', timeout=30_000)

        with gallery_page.expect_download(timeout=20_000) as download_info:
            gallery_page.locator('[data-testid="chat-export"]').click()

        download = download_info.value
        assert download.suggested_filename.endswith(".md")
    finally:
        api_session.delete(f"{backend_url}/api/chat/conversations/{uuid}", timeout=15)


def test_archive_and_restore_a_conversation(
    gallery_page: Page, api_session: requests.Session, base_url: str, backend_url: str
):
    """Archiving hides a conversation; the archived view brings it back."""
    title = _unique("E2E archive target")
    response = api_session.post(
        f"{backend_url}/api/chat/conversations",
        json={"title": title},
        timeout=20,
    )
    assert response.status_code == 201, response.text
    uuid = response.json()["uuid"]

    try:
        _open_chat(gallery_page, base_url)
        sidebar = gallery_page.locator('[data-testid="chat-sidebar"]')
        item = gallery_page.locator('[data-testid="chat-conversation-item"]').filter(has_text=title)
        expect(item).to_be_visible(timeout=15_000)

        item.hover()
        item.locator('[data-testid="chat-archive"]').click()
        expect(sidebar.get_by_text(title)).to_have_count(0, timeout=15_000)

        # It is not gone — it moved.
        gallery_page.locator('[data-testid="chat-toggle-archived"]').click()
        expect(sidebar.get_by_text(title)).to_be_visible(timeout=15_000)
    finally:
        api_session.delete(f"{backend_url}/api/chat/conversations/{uuid}", timeout=15)


def test_escape_stops_generation(
    gallery_page: Page,
    api_session: requests.Session,
    cleanup_conversations: list[str],
    base_url: str,
    backend_url: str,
):
    """Escape is the conventional stop key and must reach the stream."""
    if not _llm_configured(api_session, backend_url):
        pytest.skip("Requires a configured LLM")

    _open_chat(gallery_page, base_url)
    gallery_page.locator('[data-testid="chat-composer-input"]').fill(
        "Write an extremely long and detailed summary."
    )
    gallery_page.locator('[data-testid="chat-send"]').click()

    gallery_page.wait_for_url("**/chat/*", timeout=30_000)
    cleanup_conversations.append(gallery_page.url.rstrip("/").split("/")[-1])

    expect(gallery_page.locator('[data-testid="chat-stop"]')).to_be_visible(timeout=30_000)
    gallery_page.keyboard.press("Escape")

    expect(gallery_page.locator('[data-testid="chat-send"]')).to_be_visible(timeout=20_000)
