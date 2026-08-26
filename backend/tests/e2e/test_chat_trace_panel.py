"""The query-trace panel, driven in a real browser (GH #514).

Three of this panel's defects were invisible to unit tests and only surfaced by
LOOKING at it: a stage rendering as a stranded second root, three stages
collapsing into one row with merged details, and a resolved leg relabelling
itself so the row read "Found · chunk plane · 48 found". Every fold test passed
throughout. So the assertions here are about what a reader actually sees —
ordering, labels, and the distinctions the panel exists to make.

Requires the dev stack plus ``--with-mock-llm`` and at least one completed
transcript; skips rather than fails without them.

⚠️ **Traces are LIVE ONLY.** Every assertion about tree content must run against
the turn that produced it. After a reload the panel legitimately shows its
"not stored" state, which one test pins deliberately — a blank panel there is
the design, and would otherwise be reported as a bug.
"""

from __future__ import annotations

import os
import socket
import uuid as uuid_pkg
from collections.abc import Iterator

import pytest
import requests
from conftest import TEST_ADMIN_EMAIL
from conftest import TEST_ADMIN_PASSWORD
from playwright.sync_api import Page
from playwright.sync_api import expect

pytestmark = pytest.mark.chat

STREAM_TIMEOUT_MS = 90_000
#: HOST-side probe port. ``--port-offset N`` moves the published port (the
#: isolated stack used here publishes on 5399), and a hardcoded 5199 made this
#: whole module SKIP there — silently, which reads as a pass.
#:
#: It was worse than a skip for a while: 5199 was answered by the SHARED dev
#: stack's mock-llm, so the precondition passed on a container these tests never
#: touched. Same trap `conftest` documents for MINIO_PORT/OPENSEARCH_PORT —
#: probe and use must agree.
MOCK_LLM_PORT = int(os.environ.get("MOCK_LLM_PORT", "5199"))
#: CONTAINER-side port, which never moves: the offset republishes the host port,
#: it does not change what the process listens on inside the network.
MOCK_LLM_URL_FOR_BACKEND = "http://mock-llm:5199/v1"
ROOMY_CONTEXT_WINDOW = 32_000

TRACE_PANEL = '[data-testid="chat-trace-panel"]'
TRACE_TOGGLE = '[data-testid="chat-trace-toggle"]'
TRACE_NODE = '[data-testid="trace-node"]'


def _mock_llm_running() -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.settimeout(0.3)
        return sock.connect_ex(("127.0.0.1", MOCK_LLM_PORT)) == 0


@pytest.fixture(scope="module")
def api_session(backend_url: str) -> requests.Session:
    session = requests.Session()
    response = session.post(
        f"{backend_url}/api/auth/token",
        data={"username": TEST_ADMIN_EMAIL, "password": TEST_ADMIN_PASSWORD},
        timeout=30,
    )
    assert response.status_code == 200, f"Login failed: {response.status_code}"
    csrf_token = session.cookies.get("csrf_token")
    assert csrf_token, "login did not set a csrf_token cookie"
    session.headers["X-CSRF-Token"] = csrf_token
    return session


def _completed_transcripts_exist(api_session: requests.Session, backend_url: str) -> bool:
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


def _trace_enabled(api_session: requests.Session, backend_url: str) -> bool:
    """The panel is gated; without the flag there is nothing to assert on."""
    try:
        response = api_session.get(f"{backend_url}/api/admin/chat-settings", timeout=20)
        return bool(response.ok and response.json().get("trace_enabled"))
    except (requests.RequestException, ValueError):
        return False


def _requirements_or_skip(api_session: requests.Session, backend_url: str) -> None:
    if not _mock_llm_running():
        pytest.skip("Requires the mock LLM: ./opentr.sh start dev --with-mock-llm")
    if not _completed_transcripts_exist(api_session, backend_url):
        pytest.skip("Requires at least one completed transcript for retrieval to find")
    if not _trace_enabled(api_session, backend_url):
        pytest.skip("Requires chat.trace_enabled")


@pytest.fixture
def llm_config(api_session: requests.Session, backend_url: str) -> Iterator[str]:
    """A mock-LLM provider for this test, removed afterwards."""
    response = api_session.post(
        f"{backend_url}/api/llm-settings",
        json={
            "name": f"gh514-trace-e2e-{uuid_pkg.uuid4().hex[:8]}",
            "provider": "custom",
            "model_name": "mock-gpt",
            "base_url": MOCK_LLM_URL_FOR_BACKEND,
            "api_key": "not-needed",
            "max_tokens": ROOMY_CONTEXT_WINDOW,
        },
        timeout=30,
    )
    assert response.ok, f"Could not create provider: {response.status_code} {response.text}"
    uuid = str(response.json()["uuid"])

    # The backend only auto-activates a config when it is the user's FIRST one
    # ever; the shared e2e account accumulates configs across runs, so this is
    # essentially never true here. ChatComposer's disabled state is driven by
    # that global "active" pointer (GET /api/llm-settings/status), not by the
    # conversation's own pinned llm_config_uuid — without this the composer
    # stays disabled with "Chat needs a language model" for the whole test.
    #
    # This mutates the SHARED e2e account's active-config pointer, which the
    # dev-data-hygiene rule (never persist changes to shared dev data) covers
    # just as much as a created row does — captured before activating, restored
    # in the finally below.
    status = api_session.get(f"{backend_url}/api/llm-settings/status", timeout=30)
    assert status.ok, f"Could not read prior LLM status: {status.status_code} {status.text}"
    prior_active = status.json().get("active_configuration")
    prior_active_uuid = prior_active["uuid"] if prior_active else None

    activate = api_session.post(
        f"{backend_url}/api/llm-settings/set-active",
        json={"configuration_id": uuid},
        timeout=30,
    )
    assert activate.ok, f"Could not activate provider: {activate.status_code} {activate.text}"

    try:
        yield uuid
    finally:
        # In a `finally`, not on the happy path: the assertion most likely to
        # fail is the one AFTER this was created.
        try:
            api_session.delete(f"{backend_url}/api/llm-settings/config/{uuid}", timeout=30)
        except requests.RequestException:
            pass
        # Restore whichever config (if any) was active before this fixture touched
        # it — deleting the config above already re-picks SOME active config
        # server-side when this one had been active; this puts it back rather than
        # leaving it at whatever that reassignment happened to land on.
        if prior_active_uuid:
            try:
                api_session.post(
                    f"{backend_url}/api/llm-settings/set-active",
                    json={"configuration_id": prior_active_uuid},
                    timeout=30,
                )
            except requests.RequestException:
                pass


@pytest.fixture
def conversation(api_session: requests.Session, backend_url: str, llm_config: str) -> Iterator[str]:
    response = api_session.post(
        f"{backend_url}/api/chat/conversations",
        json={
            "llm_config_uuid": llm_config,
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
    assert response.ok, f"Could not create conversation: {response.status_code}"
    uuid = str(response.json()["uuid"])
    try:
        yield uuid
    finally:
        try:
            api_session.delete(f"{backend_url}/api/chat/conversations/{uuid}", timeout=15)
        except requests.RequestException:
            pass


def _open_panel(page: Page) -> None:
    """Open the panel via its toggle, unless a stored preference already did."""
    if page.locator(TRACE_PANEL).count() == 0:
        page.locator(TRACE_TOGGLE).click()
    expect(page.locator(TRACE_PANEL)).to_be_visible()


def _ask(page: Page, base_url: str, conversation_uuid: str, question: str) -> None:
    """Send one question and wait for the turn to COMPLETE.

    Completion is read from the assistant bubble's ``data-status``. Deliberately
    NOT "wait for Send to come back": Send is visible before the click flips it
    to Stop, so that assertion passes against the pre-click state and hands the
    test a half-rendered turn.
    """
    page.goto(f"{base_url}/chat/{conversation_uuid}")
    composer = page.locator('[data-testid="chat-composer-input"]')
    expect(composer).to_be_visible(timeout=30_000)
    composer.fill(question)
    page.locator('[data-testid="chat-send"]').click()
    expect(
        page.locator('[data-testid="chat-message-assistant"][data-status="complete"]').last
    ).to_be_visible(timeout=STREAM_TIMEOUT_MS)


def _rows(page: Page) -> list[str]:
    return [t.strip() for t in page.locator(TRACE_NODE).all_text_contents()]


def test_the_panel_renders_a_tree_for_a_real_turn(
    gallery_page: Page, base_url: str, backend_url: str, api_session, conversation: str
) -> None:
    _requirements_or_skip(api_session, backend_url)
    _ask(gallery_page, base_url, conversation, "What concerns were raised?")
    _open_panel(gallery_page)

    nodes = gallery_page.locator(TRACE_NODE)
    expect(nodes.first).to_be_visible(timeout=15_000)
    assert nodes.count() >= 8, (
        f"a real turn should report most of its 16 stages, saw {nodes.count()}"
    )


def test_the_turn_opens_with_submitted_and_ends_with_the_answer(
    gallery_page: Page, base_url: str, backend_url: str, api_session, conversation: str
) -> None:
    """Emission order, which is what three separate bugs got wrong.

    `planned` once rendered as a stranded second root AFTER every other stage,
    because it was the one emitter with no parent. Asserting first and last rows
    is what catches an ordering regression; asserting only "N nodes exist" does
    not.
    """
    _requirements_or_skip(api_session, backend_url)
    _ask(gallery_page, base_url, conversation, "What concerns were raised?")
    _open_panel(gallery_page)

    rows = _rows(gallery_page)
    assert rows, "no trace rows rendered"
    assert "Submitted" in rows[0], f"first row should be Submitted, got {rows[0]!r}"
    assert "Answered" in rows[-1], f"last row should be Answered, got {rows[-1]!r}"


def test_a_search_leg_stays_labelled_a_search_after_it_resolves(
    gallery_page: Page, base_url: str, backend_url: str, api_session, conversation: str
) -> None:
    """A leg reports twice under one node id, and must not rename itself.

    Labelling by the LATEST stage renamed a resolved leg from "Search" to
    "Found", so the row read "Found · chunk plane · 48 found" — redundant, and
    it lost what the node actually is.
    """
    _requirements_or_skip(api_session, backend_url)
    _ask(gallery_page, base_url, conversation, "What concerns were raised?")
    _open_panel(gallery_page)

    rows = _rows(gallery_page)
    search_rows = [r for r in rows if "Search" in r]
    assert search_rows, f"no Search row rendered; rows were {rows}"
    assert not any(r.startswith("Found") for r in rows), (
        f"a resolved leg relabelled itself to its latest stage: {rows}"
    )


def test_the_two_filter_steps_are_told_apart(
    gallery_page: Page, base_url: str, backend_url: str, api_session, conversation: str
) -> None:
    """FILTERED fires twice per turn with different numbers.

    Two rows both reading "Filtered" looks like a bug rather than two different
    filters, so each names its subject.
    """
    _requirements_or_skip(api_session, backend_url)
    _ask(gallery_page, base_url, conversation, "What concerns were raised?")
    _open_panel(gallery_page)

    rows = _rows(gallery_page)
    filtered = [r for r in rows if "Filtered" in r]
    assert len(filtered) >= 2, f"expected quarantine and masking rows, got {filtered}"
    joined = " ".join(filtered).lower()
    assert "quarantine" in joined, f"the quarantine filter is unlabelled: {filtered}"
    assert "masking" in joined, f"the masking filter is unlabelled: {filtered}"


def test_skipped_steps_are_shown_rather_than_omitted(
    gallery_page: Page, base_url: str, backend_url: str, api_session, conversation: str
) -> None:
    """ "We never looked" is a finding, not an absence.

    A stage that did not run is rendered and de-emphasised. Omitting it would
    read as "not part of this pipeline", which is the ambiguity the panel exists
    to remove.
    """
    _requirements_or_skip(api_session, backend_url)
    _ask(gallery_page, base_url, conversation, "What concerns were raised?")
    _open_panel(gallery_page)

    skipped = gallery_page.locator(f'{TRACE_NODE}[data-outcome="skipped"]')
    assert skipped.count() > 0, "a default turn skips several stages; none were rendered"
    expect(skipped.first).to_be_visible()


def test_the_panel_says_traces_are_not_stored_after_a_reload(
    gallery_page: Page, base_url: str, backend_url: str, api_session, conversation: str
) -> None:
    """The most visible consequence of the live-only decision.

    A blank panel here would be read as a defect and reported as one, so the
    empty state has to SAY the trace was not stored.
    """
    _requirements_or_skip(api_session, backend_url)
    _ask(gallery_page, base_url, conversation, "What concerns were raised?")
    _open_panel(gallery_page)
    expect(gallery_page.locator(TRACE_NODE).first).to_be_visible(timeout=15_000)

    gallery_page.reload()
    expect(gallery_page.locator('[data-testid="chat-composer-input"]')).to_be_visible(
        timeout=30_000
    )
    _open_panel(gallery_page)

    expect(gallery_page.locator('[data-testid="chat-trace-empty"]')).to_be_visible(timeout=15_000)
    assert gallery_page.locator(TRACE_NODE).count() == 0, "a reloaded turn should have no tree"


def test_closing_the_panel_does_not_cancel_the_answer(
    gallery_page: Page, base_url: str, backend_url: str, api_session, conversation: str
) -> None:
    """Escape closes the panel; the page uses Escape to STOP generation.

    Without `stopPropagation` in the escapeKey action, closing this panel would
    abort the user's in-flight answer.
    """
    _requirements_or_skip(api_session, backend_url)
    _ask(gallery_page, base_url, conversation, "What concerns were raised?")
    _open_panel(gallery_page)

    gallery_page.locator(TRACE_PANEL).press("Escape")

    expect(gallery_page.locator(TRACE_PANEL)).not_to_be_visible()
    # The completed answer is still on screen and still marked complete.
    expect(
        gallery_page.locator('[data-testid="chat-message-assistant"][data-status="complete"]').last
    ).to_be_visible()


def test_the_open_panel_never_covers_the_composer(
    gallery_page: Page, base_url: str, backend_url: str, api_session, conversation: str
) -> None:
    """Opening the inspector must not make the chat unusable.

    The panel is ``position: fixed; right: 0`` so it cannot reflow the answer.
    Measured at a 1280px viewport, that put its left edge at x=896 while the
    send button ended at x=1183 — **the panel covered Send by 287px** (207px at
    1440px), and Playwright refused the click with "trace-body ... intercepts
    pointer events". Every other test in this module opens the panel AFTER
    asking, which is exactly why none of them saw it.

    Asserted as geometry rather than "the click worked", because Playwright
    auto-retries an intercepted click for the whole timeout and can succeed on a
    layout that is still wrong for a human with one shot at it.
    """
    _requirements_or_skip(api_session, backend_url)
    _ask(gallery_page, base_url, conversation, "What concerns were raised?")
    _open_panel(gallery_page)

    panel = gallery_page.locator(TRACE_PANEL).bounding_box()
    send = gallery_page.locator('[data-testid="chat-send"]').bounding_box()
    assert panel and send, "panel and send button must both be laid out"
    assert send["x"] + send["width"] <= panel["x"], (
        f"the open trace panel (left edge x={panel['x']:.0f}) covers the send "
        f"button (right edge x={send['x'] + send['width']:.0f}); the chat cannot "
        "be used while the inspector is open"
    )


def test_a_turn_can_be_started_with_the_panel_already_open(
    gallery_page: Page, base_url: str, backend_url: str, api_session, conversation: str
) -> None:
    """The panel's whole claim is that you can WATCH retrieval happen.

    The toggle used to render only ``{#if hasMessages}``, so the FIRST question
    of a conversation could only ever be inspected after its answer had already
    finished — the one turn a new user actually watches. This drives the order a
    real user would: open, then ask.
    """
    _requirements_or_skip(api_session, backend_url)
    gallery_page.goto(f"{base_url}/chat/{conversation}")
    composer = gallery_page.locator('[data-testid="chat-composer-input"]')
    expect(composer).to_be_visible(timeout=30_000)

    _open_panel(gallery_page)
    # An untouched thread must invite a question, not claim a trace was lost.
    expect(gallery_page.locator('[data-testid="chat-trace-empty"]')).to_be_visible()

    composer.fill("What concerns were raised?")
    gallery_page.locator('[data-testid="chat-send"]').click()

    expect(
        gallery_page.locator('[data-testid="chat-message-assistant"][data-status="complete"]').last
    ).to_be_visible(timeout=STREAM_TIMEOUT_MS)
    assert gallery_page.locator(TRACE_NODE).count() >= 8, (
        "the panel was open for the whole turn and should hold its tree"
    )


def test_the_open_state_survives_a_reload(
    gallery_page: Page, base_url: str, backend_url: str, api_session, conversation: str
) -> None:
    """ "Remembers its state" from the issue's Shape section."""
    _requirements_or_skip(api_session, backend_url)
    _ask(gallery_page, base_url, conversation, "What concerns were raised?")
    _open_panel(gallery_page)

    gallery_page.reload()
    expect(gallery_page.locator('[data-testid="chat-composer-input"]')).to_be_visible(
        timeout=30_000
    )

    expect(gallery_page.locator(TRACE_PANEL)).to_be_visible(timeout=15_000)
