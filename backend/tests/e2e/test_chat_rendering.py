"""E2E: chat answer rendering and the answer-length setting, against the mock LLM (#1282 item C).

``mock-gpt`` streams a fixed markdown answer (bold, a list, a quote, a fenced code block),
so what the page renders is checkable. The answer-length control is checked at the wire: the
mock records every request it receives (``GET /_mock/requests``), so the value the app sent
as ``max_tokens`` is read back rather than assumed. Every conversation and LLM config the
tests create is deleted.
"""

from __future__ import annotations

import uuid as uuid_pkg
from collections.abc import Iterator
from typing import Any

import pytest
import requests
from conftest import unique_conversation_title
from playwright.sync_api import Page
from playwright.sync_api import expect
from timeouts import APP_SHELL_READY_MS

from tests.fixtures.mock_llm import HOST_PORT as MOCK_LLM_HOST_PORT

pytestmark = pytest.mark.chat

STREAM_TIMEOUT_MS = 90_000
MOCK_BACKEND_URL = "http://mock-llm:5199/v1"
ANSWER_LENGTH = 1024


@pytest.fixture
def mock_chat_config(
    api_session: requests.Session, backend_url: str, request: pytest.FixtureRequest
) -> Iterator[dict[str, Any]]:
    """A ``mock-gpt`` config of this test's own, with an ``mock-echo`` variant on request."""
    model = getattr(request, "param", "mock-gpt")
    created = api_session.post(
        f"{backend_url}/api/llm-settings",
        json={
            "name": f"Mock chat render {model} {uuid_pkg.uuid4().hex[:8]}",
            "provider": "custom",
            "model_name": model,
            "base_url": MOCK_BACKEND_URL,
            "api_key": "mock-key-not-secret",
        },
        timeout=30,
    )
    assert created.status_code == 200, created.text[:300]
    config = created.json()
    try:
        yield config
    finally:
        api_session.delete(f"{backend_url}/api/llm-settings/config/{config['uuid']}", timeout=30)


@pytest.fixture
def conversation(
    api_session: requests.Session, backend_url: str, mock_chat_config: dict[str, Any]
) -> Iterator[str]:
    """A conversation pinned to this test's mock config (never the account's active one)."""
    made = api_session.post(
        f"{backend_url}/api/chat/conversations",
        json={
            "title": unique_conversation_title("rendering"),
            "llm_config_uuid": mock_chat_config["uuid"],
        },
        timeout=30,
    )
    assert made.status_code == 201, made.text[:300]
    conversation_uuid = str(made.json()["uuid"])
    try:
        yield conversation_uuid
    finally:
        api_session.delete(f"{backend_url}/api/chat/conversations/{conversation_uuid}", timeout=15)


def _ask(page: Page, base_url: str, conversation_uuid: str, text: str) -> Any:
    page.goto(f"{base_url}/chat/{conversation_uuid}")
    page.wait_for_selector('[data-testid="chat-composer-input"]', timeout=APP_SHELL_READY_MS)
    page.locator('[data-testid="chat-composer-input"]').fill(text)
    page.locator('[data-testid="chat-send"]').click()
    answer = page.locator('[data-testid="chat-message-assistant"]').last
    expect(answer).to_be_visible(timeout=STREAM_TIMEOUT_MS)
    # Generation is over when the Stop button reverts to Send.
    expect(page.locator('[data-testid="chat-send"]')).to_be_visible(timeout=STREAM_TIMEOUT_MS)
    return answer


def _mock_requests() -> list[dict[str, Any]]:
    resp = requests.get(f"http://127.0.0.1:{MOCK_LLM_HOST_PORT}/_mock/requests", timeout=10)
    resp.raise_for_status()
    return list(resp.json()["requests"])


def test_answer_renders_markdown_and_a_code_block_with_a_copy_button(
    gallery_page: Page, base_url: str, conversation: str
) -> None:
    answer = _ask(gallery_page, base_url, conversation, "What is discussed?")
    prose = answer.locator(".chat-markdown")
    expect(prose.locator("strong").first).to_be_visible()
    expect(prose.locator("ul > li")).to_have_count(3)
    expect(prose.locator("blockquote")).to_have_count(1)
    code = prose.locator("pre")
    expect(code).to_have_count(1)
    expect(code.locator("code")).to_contain_text("hello from the mock LLM")
    expect(code.locator(".code-header .code-lang")).to_have_text("python")
    expect(code.locator(".code-header button.code-copy")).to_be_visible()
    # Raw markdown must not leak through as text.
    assert "```" not in prose.inner_text(), "fence markers leaked into the rendered answer"
    assert "**" not in prose.inner_text(), "bold markers leaked into the rendered answer"


@pytest.mark.parametrize("mock_chat_config", ["mock-echo"], indirect=True)
def test_answer_renders_math_as_mathml(
    gallery_page: Page, base_url: str, conversation: str
) -> None:
    """``mock-echo`` returns the prompt it was sent, which carries the question's TeX.

    KaTeX runs with ``output: 'mathml'`` (chatMarkdownExtensions.ts), so the formula is a native
    ``<math>`` inside ``.chat-math`` / ``.chat-math-display``; there is no ``.katex`` wrapper.
    """
    answer = _ask(gallery_page, base_url, conversation, "Show $$x^2 + y^2 = z^2$$ please")
    formula = answer.locator(".chat-markdown :is(.chat-math, .chat-math-display) math")
    expect(formula.first).to_be_visible(timeout=30_000)


def test_the_answer_length_setting_is_what_the_app_sends_as_max_tokens(
    gallery_page: Page, base_url: str, conversation: str
) -> None:
    page = gallery_page
    page.goto(f"{base_url}/chat/{conversation}")
    page.wait_for_selector('[data-testid="chat-composer-input"]', timeout=APP_SHELL_READY_MS)
    page.locator('[data-testid="chat-controls-toggle"]').click()
    page.locator('[data-testid="chat-advanced-toggle"]').click()
    field = page.locator('[data-testid="chat-max-tokens"]')
    with page.expect_response(
        lambda r: r.request.method == "PATCH" and conversation in r.url, timeout=30_000
    ) as saved:
        field.fill(str(ANSWER_LENGTH))
        field.press("Tab")
    assert saved.value.status == 200, saved.value.text()
    expect(page.locator('[data-testid="chat-max-tokens-value"]')).to_contain_text(
        f"{ANSWER_LENGTH:,}"
    )

    requests.post(f"http://127.0.0.1:{MOCK_LLM_HOST_PORT}/_mock/requests/clear", timeout=10)
    _ask(page, base_url, conversation, "Answer briefly.")
    sent = [r.get("max_tokens") for r in _mock_requests() if r.get("model") == "mock-gpt"]
    assert ANSWER_LENGTH in sent, f"the mock never received max_tokens={ANSWER_LENGTH}: {sent}"
