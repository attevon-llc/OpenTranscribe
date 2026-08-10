"""E2E: a chat answer must never claim more grounding than it has (issue #384).

`test_chat.py` covers the happy path — a grounded, cited answer. This file covers
the property underneath it: **the citations a user can see and click are exactly
the excerpts the model was given.** That invariant is enforced in three separate
places (`prompting.format_excerpts` returns the ids it emitted,
`citations.build_offered_citations` maps from those ids, and `service.stream_reply`
emits `sources` only after `build_messages`), and unit tests pin each one. What
none of them can prove is that the three still line up once a real browser talks
to a real backend — which is the failure that shipped.

The interesting case is the one the unit tests reach by arithmetic and a user
reaches with a small local model: the prompt budget leaves room for NO excerpt, so
the answer is not grounded in the library at all. Before #384 that rendered as a
normal, fully-cited answer. It must now announce itself.

The trigger is deterministic rather than incidental. A config declaring a
512-token context window gives
``budget_chars = (512 - 256) * 4 - overhead``, and the base system rules alone
exceed 1024 characters, so the budget floors at 0 no matter what retrieval found.
That is a real, reachable configuration — not a mock — and it is why these tests
register their own provider instead of reusing the module-wide one.

Requirements:
- Dev environment running: ./opentr.sh start dev --with-mock-llm
- At least one COMPLETED transcript in the library (retrieval needs content)

Run:
    pytest backend/tests/e2e/test_chat_grounding.py -v
    DISPLAY=:11 pytest backend/tests/e2e/test_chat_grounding.py -v --headed

These tests must never persist changes to dev data: every conversation and every
LLM config created here is deleted in the same test, and no transcript is touched.
"""

from __future__ import annotations

import os
import socket
import uuid as uuid_pkg
from collections.abc import Iterator

import pytest
import requests

# Absolute import — the e2e dir is not a package, so a relative import breaks
# collection when invoked as `pytest backend/tests/e2e/` from the repo root.
from conftest import TEST_ADMIN_EMAIL
from conftest import TEST_ADMIN_PASSWORD
from playwright.sync_api import Page
from playwright.sync_api import expect

pytestmark = pytest.mark.chat

FRONTEND_URL = os.environ.get("E2E_FRONTEND_URL", "http://localhost:5173")
BACKEND_URL = os.environ.get("E2E_BACKEND_URL", "http://localhost:5174")

STREAM_TIMEOUT_MS = 90_000

MOCK_LLM_PORT = 5199
MOCK_LLM_URL_FOR_BACKEND = f"http://mock-llm:{MOCK_LLM_PORT}/v1"

#: Smallest context window the API accepts. Chosen because it is the floor, so
#: the budget cannot accidentally grow past zero if the base rules are ever
#: shortened.
STARVED_CONTEXT_WINDOW = 512
#: Comfortably fits the base rules, history, question and several excerpts.
ROOMY_CONTEXT_WINDOW = 32_000


def _mock_llm_running() -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.settimeout(0.3)
        return sock.connect_ex(("127.0.0.1", MOCK_LLM_PORT)) == 0


@pytest.fixture(scope="module")
def api_session() -> requests.Session:
    """An authenticated API session for arranging and cleaning up test state."""
    session = requests.Session()
    response = session.post(
        f"{BACKEND_URL}/api/auth/token",
        data={"username": TEST_ADMIN_EMAIL, "password": TEST_ADMIN_PASSWORD},
        timeout=30,
    )
    assert response.status_code == 200, f"Login failed: {response.status_code}"

    # Every mutation needs the CSRF header or the backend answers 403 — which
    # would silently turn the cleanup DELETEs below into no-ops and leave this
    # suite's conversations behind in dev data.
    csrf_token = session.cookies.get("csrf_token")
    assert csrf_token, "login did not set a csrf_token cookie"
    session.headers["X-CSRF-Token"] = csrf_token
    return session


def _completed_transcripts_exist(api_session: requests.Session) -> bool:
    try:
        response = api_session.get(
            f"{BACKEND_URL}/api/files",
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


@pytest.fixture
def llm_config_factory(api_session: requests.Session) -> Iterator:
    """Create mock-LLM configs with a chosen context window; delete them after.

    A per-test provider rather than the shared one, because the whole point is
    to control ``max_tokens`` — the user-declared context window the excerpt
    budget is computed against.
    """
    created: list[str] = []

    def _make(context_window: int, name: str) -> str:
        # UUID-suffixed, like the user fixtures: config names are unique per user,
        # so a fixed name 409s against the leftovers of any run that was killed
        # before its teardown could delete them — turning an unrelated
        # interruption into a permanent failure for everyone afterwards.
        unique_name = f"{name} {uuid_pkg.uuid4().hex[:8]}"
        response = api_session.post(
            f"{BACKEND_URL}/api/llm-settings",
            json={
                "name": unique_name,
                "provider": "custom",
                "model_name": "mock-gpt",
                "base_url": MOCK_LLM_URL_FOR_BACKEND,
                "api_key": "mock-key-not-secret",
                "max_tokens": context_window,
            },
            timeout=30,
        )
        assert response.ok, (
            f"Could not create LLM config {unique_name!r}: {response.status_code} {response.text}"
        )
        uuid = str(response.json()["uuid"])
        created.append(uuid)
        return uuid

    yield _make

    for uuid in created:
        try:
            api_session.delete(f"{BACKEND_URL}/api/llm-settings/config/{uuid}", timeout=30)
        except requests.RequestException:
            pass


@pytest.fixture
def conversation_factory(api_session: requests.Session) -> Iterator:
    """Create conversations pinned to a given model; delete them after, pass or fail."""
    created: list[str] = []

    def _make(llm_config_uuid: str) -> str:
        response = api_session.post(
            f"{BACKEND_URL}/api/chat/conversations",
            json={
                "llm_config_uuid": llm_config_uuid,
                # Empty scope = "all accessible transcripts", so retrieval has
                # the whole library to find chunks in.
                "scope": {
                    "file_uuids": [],
                    "collection_uuids": [],
                    "tag_names": [],
                    "speakers": [],
                },
                "settings": {"use_context": True},
            },
            timeout=30,
        )
        assert response.ok, f"Could not create conversation: {response.status_code} {response.text}"
        uuid = str(response.json()["uuid"])
        created.append(uuid)
        return uuid

    yield _make

    for uuid in created:
        try:
            api_session.delete(f"{BACKEND_URL}/api/chat/conversations/{uuid}", timeout=15)
        except requests.RequestException:
            pass


def _requirements_or_skip(api_session: requests.Session) -> None:
    if not _mock_llm_running():
        pytest.skip("Requires the mock LLM: ./opentr.sh start dev --with-mock-llm")
    if not _completed_transcripts_exist(api_session):
        pytest.skip("Requires at least one completed transcript for retrieval to find")


def _ask(page: Page, conversation_uuid: str, question: str) -> None:
    """Open a conversation, send one question, and wait for the turn to COMPLETE.

    Completion is read from the assistant bubble's ``data-status``, which the
    store sets to ``complete`` on the `done` frame.

    Deliberately NOT "wait for the Send button to come back": Send is already
    visible before the click flips it to Stop, so that assertion can pass
    instantly against the pre-click state and hand the test a half-rendered turn.
    It made this suite flaky in exactly the runs where the retrieval cache was
    warm and the whole turn finished in under a second.
    """
    page.goto(f"{FRONTEND_URL}/chat/{conversation_uuid}")
    composer = page.locator('[data-testid="chat-composer-input"]')
    expect(composer).to_be_visible(timeout=30_000)

    # Wait for the route to finish LOADING the conversation, not just for the
    # composer to paint. `chatStore.sendMessage` falls back to creating a brand
    # new conversation when `activeConversationId` is still null — so sending
    # into that window silently starts a different thread, one with no pinned
    # llm_config and therefore the DEFAULT context window. The test then asks a
    # roomy model why it did not run out of room, and fails about one run in
    # three.
    page.wait_for_load_state("networkidle")

    composer.fill(question)
    page.locator('[data-testid="chat-send"]').click()

    completed = page.locator('[data-testid="chat-message-assistant"][data-status="complete"]')
    expect(completed.last).to_be_visible(timeout=STREAM_TIMEOUT_MS)

    # Belt and braces: if the race above ever recurs the URL moves to the newly
    # created conversation, so this fails loudly instead of quietly measuring the
    # wrong model. It also stops such a thread leaking past the fixture, which
    # only deletes uuids it created itself.
    assert conversation_uuid in page.url, (
        f"The turn was sent to {page.url} instead of conversation {conversation_uuid} — "
        "the store created a new conversation because the route had not loaded yet."
    )


def _reload_thread(page: Page) -> None:
    """Reload the conversation so the SERVER's view of the turn is on screen.

    A message built during streaming carries only what the frames supplied. The
    retrieval diagnostics (`retrieved`, `chunks_used`) live in `msg_metadata` on
    the persisted row and reach the client when the thread is fetched — so the
    Details panel is genuinely empty of them mid-stream, and reading it before a
    reload measures nothing.
    """
    page.reload()
    expect(page.locator('[data-testid="chat-message-assistant"]').last).to_be_visible(
        timeout=30_000
    )


def _excerpts_used(page: Page) -> int:
    """Read `chunks_used` out of the answer's Details panel. Reload first.

    The panel renders `chunks_used / retrieved` straight from the server's
    ``msg_metadata``, so this is the server's own account of how many excerpts
    reached the prompt rather than a client-side guess.
    """
    page.locator('[data-testid="chat-meta-toggle"]').last.click()
    grid = page.locator('[data-testid="chat-meta-grid"]').last
    expect(grid).to_be_visible(timeout=10_000)

    text = grid.inner_text()
    for line in text.splitlines():
        if "/" in line and line.replace("/", "").replace(" ", "").isdigit():
            return int(line.split("/")[0].strip())
    raise AssertionError(
        "Could not find the 'used / retrieved' row in Details. Did you reload the "
        f"thread first? Mid-stream the panel has no retrieval diagnostics.\n{text}"
    )


# ---------------------------------------------------------------------------
# The ungrounded-answer warning
# ---------------------------------------------------------------------------


def test_answer_with_no_room_for_excerpts_is_flagged_as_ungrounded(
    gallery_page: Page,
    api_session: requests.Session,
    llm_config_factory,
    conversation_factory,
):
    """A model whose window fits no excerpt must say the answer is not grounded.

    This is the #384 regression in the shape a user meets it: retrieval succeeds,
    the budget rejects everything it found, and the model answers from nothing.
    Before the fix this rendered as a normal answer with a full set of clickable
    citations attached.
    """
    _requirements_or_skip(api_session)

    config_uuid = llm_config_factory(STARVED_CONTEXT_WINDOW, "Mock LLM starved window (e2e)")
    conversation_uuid = conversation_factory(config_uuid)

    _ask(gallery_page, conversation_uuid, "What topics are discussed in these recordings?")

    notice = gallery_page.locator('[data-testid="chat-context-dropped"]')
    expect(notice).to_be_visible(timeout=15_000)
    assert notice.inner_text().strip(), "The notice rendered empty (missing i18n key?)"

    # Nothing may be offered as a source, because nothing was read. Asserted while
    # the streamed turn is still on screen, since that is when the `sources` frame
    # would have populated it.
    expect(gallery_page.locator('[data-testid="chat-sources-toggle"]')).to_have_count(0)

    # The server's own count must agree with the warning it sent.
    _reload_thread(gallery_page)
    assert _excerpts_used(gallery_page) == 0


def test_ungrounded_notice_survives_a_page_reload(
    gallery_page: Page,
    api_session: requests.Session,
    llm_config_factory,
    conversation_factory,
):
    """The warning is persisted state, not a property of the live stream.

    It arrives as a `warning` SSE frame, but the store folds it into
    `msg_metadata.context_dropped` — the same flag the server writes on the
    message row. If the two ever diverge, the notice vanishes on refresh and the
    answer silently becomes trustworthy-looking again.
    """
    _requirements_or_skip(api_session)

    config_uuid = llm_config_factory(STARVED_CONTEXT_WINDOW, "Mock LLM starved reload (e2e)")
    conversation_uuid = conversation_factory(config_uuid)

    _ask(gallery_page, conversation_uuid, "Summarise the key decisions.")
    expect(gallery_page.locator('[data-testid="chat-context-dropped"]')).to_be_visible(
        timeout=15_000
    )

    gallery_page.reload()
    expect(gallery_page.locator('[data-testid="chat-message-assistant"]').last).to_be_visible(
        timeout=30_000
    )
    expect(gallery_page.locator('[data-testid="chat-context-dropped"]')).to_be_visible(
        timeout=15_000
    )


# ---------------------------------------------------------------------------
# The control: a normal window must NOT warn, and must cite what it read
# ---------------------------------------------------------------------------


def test_roomy_context_window_cites_sources_and_does_not_warn(
    gallery_page: Page,
    api_session: requests.Session,
    llm_config_factory,
    conversation_factory,
):
    """The control for the two tests above.

    Without this they would still pass if the notice were rendered
    unconditionally, and the warning has to stay rare enough to mean something.
    """
    _requirements_or_skip(api_session)

    config_uuid = llm_config_factory(ROOMY_CONTEXT_WINDOW, "Mock LLM roomy window (e2e)")
    conversation_uuid = conversation_factory(config_uuid)

    _ask(gallery_page, conversation_uuid, "What topics are discussed in these recordings?")

    expect(gallery_page.locator('[data-testid="chat-context-dropped"]')).to_have_count(0)
    expect(gallery_page.locator('[data-testid="chat-sources-toggle"]')).to_be_visible(
        timeout=15_000
    )

    _reload_thread(gallery_page)
    assert _excerpts_used(gallery_page) > 0, "A roomy window must place excerpts in the prompt"


def test_offered_sources_match_the_excerpts_the_model_was_given(
    gallery_page: Page,
    api_session: requests.Session,
    llm_config_factory,
    conversation_factory,
):
    """The #384 invariant, asserted through the UI: cited == used.

    `sources` used to be emitted before the excerpt budget existed, so the count
    of citation cards was the count of chunks RETRIEVED. It must now equal the
    count that actually reached the prompt, which the Details panel reports from
    the server's `msg_metadata`.
    """
    _requirements_or_skip(api_session)

    config_uuid = llm_config_factory(ROOMY_CONTEXT_WINDOW, "Mock LLM cited-equals-used (e2e)")
    conversation_uuid = conversation_factory(config_uuid)

    _ask(gallery_page, conversation_uuid, "Summarise the key points across these recordings.")

    # Counted DURING the stream: these are the OFFERED citations, straight from the
    # `sources` frame. After a reload the cards show only the citations the answer
    # actually referenced (`_persist_reply` stores `used_citations`), which is a
    # subset — comparing that to `chunks_used` would prove nothing.
    toggle = gallery_page.locator('[data-testid="chat-sources-toggle"]').last
    expect(toggle).to_be_visible(timeout=15_000)
    toggle.click()

    source_links = gallery_page.locator('[data-testid="chat-source-link"]')
    expect(source_links.first).to_be_visible(timeout=10_000)
    offered = source_links.count()

    # The server's count comes from the persisted row.
    _reload_thread(gallery_page)
    used = _excerpts_used(gallery_page)

    assert offered == used, (
        f"{offered} citations were offered to the user but the server reports "
        f"{used} excerpts reached the prompt — the UI is showing sources the "
        "model never saw (issue #384)."
    )
