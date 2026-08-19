"""Reasoning must arrive on ``reasoning:`` frames, never on ``delta:`` (issue #439).

With a real vLLM-served Gemma the model's internal deliberation and a raw
``<channel|>`` control token were rendered **as the answer**: the user saw "The
user is asking… I need to search the excerpts…" instead of a conclusion, and the
reasoning was billed as completion tokens.

The fix is request-side — ``llm_service`` activates the template's thought channel
so vLLM's streaming parser engages and emits ``delta.reasoning_content`` — and
``tests/unit/test_llm_reasoning_not_rendered_as_answer.py`` pins that against the
real mock server, at the PROVIDER layer.

**This module closes the layer above it**, which nothing covered: whether
``stream_reply`` routes a ``reasoning`` event to its own SSE frame after the
``OutputRedactor``'s sentence buffering, or lets it fall into the answer. The two
buffers are separate objects and the reasoning one is easy to lose in a refactor —
that would put deliberation back on the wire as the answer with every provider
test still green.

Driven through the REAL ``stream_reply``: only retrieval, persistence and the LLM
itself are stubbed, so the frame names come from the shipped code path.
"""

from __future__ import annotations

import json
from contextlib import contextmanager
from typing import Any

import pytest

from app.services.chat import service as chat_service
from app.services.chat.redactor import MaskedChunk
from app.services.chat.settings import ChatSettings
from app.services.llm_stream import LLMStreamEvent
from app.services.redaction.config import EffectiveRedactionConfig
from app.services.search.chunk_retrieval import ChunkHit

#: The verbatim leak from the issue: a control token the gemma4 parser does not
#: strip when the thought channel was never activated.
CONTROL_TOKEN = "<channel|>"

#: Deliberation, phrased as the issue reports it.
REASONING = "The user is asking what the team decided. I need to scan the excerpts."


def _cfg_disabled() -> EffectiveRedactionConfig:
    """Redaction OFF: this module is about frame ROUTING, not masking.

    With masking on, an assertion that reasoning text is absent from ``delta``
    could pass because the text was masked rather than because it was routed —
    two different reasons for the same observation.
    """
    # Every field defaulted except `enabled`: inventing keyword names here is how
    # this first failed — a TypeError inside the stream surfaces only as a
    # `provider_error` frame, so the assertions read as "the answer vanished".
    return EffectiveRedactionConfig(enabled=False)


def _chunk(index: int, content: str) -> MaskedChunk:
    """A MASKED chunk, which is what `_prepare_context` really returns.

    `build_offered_citations` reads `.source`, so handing it a bare `ChunkHit`
    fails inside the stream and surfaces only as a `provider_error` frame —
    the assertions then read as "the answer vanished" rather than "the fixture
    was the wrong shape".
    """
    return MaskedChunk(
        source=ChunkHit(
            file_uuid=f"11111111-1111-1111-1111-00000000000{index}",
            file_id=index,
            chunk_index=index,
            content=content,
            title="Remote control review",
            speaker="Dana",
            start_time=float(index * 60),
            end_time=float(index * 60 + 30),
        ),
        content=content,
    )


class _FakeConfig:
    provider = "vllm"
    model = "gemma-4-e4b"


class _ReasoningLLM:
    """Streams a reasoning phase, then the answer — the ``mock-reasoning`` shape."""

    def __init__(self, reasoning: list[str], deltas: list[str]):
        self.config = _FakeConfig()
        self.user_context_window = 32_000
        self.response_tokens = 4000
        self._reasoning = reasoning
        self._deltas = deltas

    def chat_completion_stream(self, messages, cancel_event=None, **_kwargs):
        for text in self._reasoning:
            yield LLMStreamEvent(type="reasoning", text=text)
        for text in self._deltas:
            yield LLMStreamEvent(type="delta", text=text)
        yield LLMStreamEvent(type="done", finish_reason="stop")

    def estimate_tokens(self, text: str) -> int:
        return len(text) // 4


@contextmanager
def _null_session():
    yield None


async def _run(monkeypatch, *, reasoning: list[str], deltas: list[str]):
    """Drive the real ``stream_reply`` and return its SSE frames."""
    chunks = [_chunk(1, "we agreed on four buttons")]
    monkeypatch.setattr("app.db.session_utils.session_scope", _null_session)
    monkeypatch.setattr(
        chat_service,
        "_prepare_context",
        lambda *_a, **_k: (list(chunks), {"retrieved": 1, "files_searched": "all"}, None, None),
    )
    monkeypatch.setattr(chat_service.limits, "is_cancelled", lambda _uuid: False)
    monkeypatch.setattr(chat_service, "_resolve_output_policy", lambda _user_id: _cfg_disabled())

    captured: dict[str, Any] = {}

    async def _fake_finalize(**kwargs):
        captured["turn"] = kwargs["turn"]

    monkeypatch.setattr(chat_service, "_finalize_turn", _fake_finalize)

    frames: list[tuple[str, dict]] = []
    generator = chat_service.ChatService.stream_reply(
        conversation_id=1,
        conversation_uuid="conv-uuid",
        user_id=1,
        organization_id=None,
        question="what did the team decide about the buttons?",
        history=[],
        file_uuids=None,
        speakers=[],
        settings=ChatSettings(),
        use_context=True,
        system_prompt="SYS",
        search_mode="hybrid",
        temperature=None,
        max_tokens=None,
        top_p=None,
        llm=_ReasoningLLM(reasoning, deltas),
        assistant_message_uuid="00000000-0000-0000-0000-0000000000aa",
        user_message_uuid="00000000-0000-0000-0000-0000000000bb",
        is_first_exchange=True,
    )
    async for raw in generator:
        if raw.startswith(":"):
            continue
        name = raw.split("event: ", 1)[1].split("\n", 1)[0]
        frames.append((name, json.loads(raw.split("data: ", 1)[1].strip())))
    return frames, captured.get("turn")


def _text_of(frames, name: str) -> str:
    return "".join(payload.get("text", "") for frame, payload in frames if frame == name)


@pytest.mark.asyncio
async def test_reasoning_never_reaches_the_answer_frames(monkeypatch):
    """The #439 defect, at the frame layer: deliberation rendered as the answer."""
    frames, _turn = await _run(
        monkeypatch,
        reasoning=[REASONING],
        deltas=["They chose four buttons.", " Nothing else changed."],
    )

    answer = _text_of(frames, "delta")
    assert "The user is asking" not in answer, (
        f"deliberation was streamed as the answer: {answer!r}"
    )
    assert "They chose four buttons." in answer, "the real answer did not survive"


@pytest.mark.asyncio
async def test_reasoning_is_not_dropped_but_gets_its_own_frame(monkeypatch):
    """The complement, and it matters: "not in the answer" is also satisfied by
    throwing the reasoning away, which would silently break the collapsible
    reasoning display the SPA renders."""
    frames, _turn = await _run(
        monkeypatch, reasoning=[REASONING], deltas=["They chose four buttons."]
    )

    assert any(name == "reasoning" for name, _ in frames), "no reasoning frame was emitted at all"
    assert "The user is asking" in _text_of(frames, "reasoning")


@pytest.mark.asyncio
async def test_a_control_token_in_the_reasoning_stays_out_of_the_answer(monkeypatch):
    """``<channel|>`` reached the rendered answer verbatim in the report.

    The shipped fix prevents the token being GENERATED into content rather than
    scrubbing it afterwards, so here it can only arrive on the reasoning channel —
    and must not cross over.
    """
    frames, _turn = await _run(
        monkeypatch,
        reasoning=[f"{REASONING}{CONTROL_TOKEN}"],
        deltas=["They chose four buttons."],
    )

    assert CONTROL_TOKEN not in _text_of(frames, "delta"), (
        "a raw control token reached the rendered answer"
    )


@pytest.mark.asyncio
async def test_a_reply_with_no_reasoning_emits_no_reasoning_frame(monkeypatch):
    """The control. Without it, "there is always a reasoning frame" would pass."""
    frames, _turn = await _run(monkeypatch, reasoning=[], deltas=["They chose four buttons."])

    assert not any(name == "reasoning" for name, _ in frames), (
        "a reasoning frame was emitted for a model that produced none"
    )
    assert "They chose four buttons." in _text_of(frames, "delta")


@pytest.mark.asyncio
async def test_the_reasoning_is_persisted_separately_from_the_answer(monkeypatch):
    """It survives a reload as its own field, not folded into the message body."""
    _frames, turn = await _run(
        monkeypatch, reasoning=[REASONING], deltas=["They chose four buttons."]
    )

    assert turn is not None, "the turn was never finalized"
    reasoning = "".join(turn.reasoning_parts)
    answer = "".join(turn.answer_parts) if hasattr(turn, "answer_parts") else ""
    assert "The user is asking" in reasoning
    assert "The user is asking" not in answer
