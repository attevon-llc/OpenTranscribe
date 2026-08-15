"""Reasoning must never reach the rendered answer (issue #439).

Drives the **real** :class:`LLMService` streaming path — payload construction,
HTTP, SSE parsing — against the **real** mock LLM server, so the only canned part
is token generation. A monkeypatched client would prove the patch behaves; this
proves the app does.

No GPU and no container: ``mock_llm_url`` falls back to a subprocess, so these run
in CI. The scenario the assertions depend on is faithful to a measured vLLM 0.19 +
Gemma 4 E4B run — ``mock-reasoning`` separates reasoning **only when the request
activated thinking**, and otherwise streams the chain-of-thought inline, closed by
a raw ``<channel|>`` control token, exactly as the real server did.
"""

from __future__ import annotations

import pytest

from app.core.config import settings
from app.services.llm_service import LLMConfig
from app.services.llm_service import LLMProvider
from app.services.llm_service import LLMService
from app.services.llm_stream import LLMStreamEvent

CONTROL_TOKENS = ("<channel|>", "<|channel>", "<|think|>", "<turn|>", "<|turn>")

# A phrase only the mock's REASONING_TEMPLATE contains, so "did deliberation leak
# into the answer" is checked against reasoning text rather than a token.
REASONING_PHRASE = "Let me look at what was actually retrieved"

QUESTION = "what did the team decide about the remote control buttons?"


@pytest.fixture(autouse=True)
def _allow_the_loopback_mock(monkeypatch: pytest.MonkeyPatch) -> None:
    """Let the SSRF guard reach the mock LLM, which is always on loopback.

    ``LLM_ALLOW_PRIVATE_ENDPOINTS`` defaults to False and `llm_service` refuses a
    private address, so without this every request here is blocked and the stream
    comes back EMPTY — the assertions then fail as ``assert '<channel|>' in ''``,
    which reads like a parser bug rather than a refused connection. That cost this
    repo several rounds of "known pre-existing failure".

    Set on the TEST, deliberately, not exported by ``run-backend-tests.sh``:
    flipping it for the whole suite would weaken the guard's coverage for every
    other test, and the file would still fail under bare ``pytest`` and in CI. A
    test that needs a relaxed setting should declare it, the same way the
    ``RUN_*`` suites carry their own module-level gates.
    """
    monkeypatch.setattr(settings, "LLM_ALLOW_PRIVATE_ENDPOINTS", True)


def _stream(url: str, model: str, **kwargs) -> list[LLMStreamEvent]:
    service = LLMService(
        LLMConfig(
            provider=LLMProvider.VLLM,
            model=model,
            base_url=url,
            api_key="mock-key-not-secret",
        )
    )
    return list(service.chat_completion_stream([{"role": "user", "content": QUESTION}], **kwargs))


def _joined(events: list[LLMStreamEvent], type_: str) -> str:
    return "".join(event.text for event in events if event.type == type_)


@pytest.fixture
def reasoning_stream(mock_llm_url: str) -> list[LLMStreamEvent]:
    return _stream(mock_llm_url, "mock-reasoning")


def test_no_control_token_reaches_the_rendered_answer(reasoning_stream):
    answer = _joined(reasoning_stream, "delta")

    assert answer, "the scenario must produce an answer at all"
    leaked = [token for token in CONTROL_TOKENS if token in answer]
    assert leaked == [], f"control token(s) rendered as answer text: {leaked}"


def test_no_reasoning_text_reaches_the_rendered_answer(reasoning_stream):
    """The defect users saw: an answer that opens with the model thinking aloud."""
    answer = _joined(reasoning_stream, "delta")

    assert REASONING_PHRASE not in answer


def test_the_reasoning_is_not_dropped_but_routed_to_its_own_event_type(reasoning_stream):
    """Separation, not suppression — the collapsible display needs the text."""
    reasoning = _joined(reasoning_stream, "reasoning")

    assert REASONING_PHRASE in reasoning
    assert all(token not in reasoning for token in CONTROL_TOKENS)


def test_the_answer_still_carries_its_citations(reasoning_stream):
    """Guard against "fixing" the leak by discarding content."""
    answer = _joined(reasoning_stream, "delta")

    assert "[1]" in answer
    assert "[2]" in answer


def test_a_non_reasoning_model_is_unaffected(mock_llm_url):
    """Control: same code path, a scenario that never reasons."""
    events = _stream(mock_llm_url, "mock-gpt")

    assert _joined(events, "delta")
    assert _joined(events, "reasoning") == ""


def test_the_hazard_is_real_when_thinking_is_not_activated(mock_llm_url):
    """The measured pre-fix behaviour, pinned so the fix cannot be silently reverted.

    ``enable_thinking=False`` reproduces what every request looked like before
    this was fixed. If this ever stops leaking, the mock has stopped modelling the
    server and the tests above are no longer evidence of anything.
    """
    events = _stream(mock_llm_url, "mock-reasoning", enable_thinking=False)
    answer = _joined(events, "delta")

    assert "<channel|>" in answer
    assert REASONING_PHRASE in answer
    assert _joined(events, "reasoning") == ""
