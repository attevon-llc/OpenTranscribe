"""Redaction of MODEL-GENERATED chat output (the hole offset redaction cannot close).

Every masker in this codebase addresses *stored* text by cached span offsets. A
local model that reads ``"John Smith, SSN 123-45-6789"`` and writes *"the number
he gave was 123-45-6789"* produces a string that matches no stored record at any
offset, so all of them render it clean. These tests pin the module that closes
that: ``services/chat/output_redactor``, and its wiring into the SSE stream.

The controlling properties:

* a **paraphrased** identifier in generated output is masked (the point of the task);
* a span whose detectors could not run is **withheld**, never emitted raw;
* the streaming contract survives — ``[n]`` citations still resolve, ``sources``
  is unchanged, and the first-token watchdog is not tripped by the buffer.
"""

from __future__ import annotations

import json
from contextlib import contextmanager
from typing import Any

import pytest

from app.core import constants as C  # noqa: N812
from app.services.chat import service as chat_service
from app.services.chat.output_redactor import OutputRedactor
from app.services.chat.redactor import MaskedChunk
from app.services.chat.settings import ChatSettings
from app.services.llm_stream import LLMStreamEvent
from app.services.redaction.config import EffectiveRedactionConfig
from app.services.search.chunk_retrieval import ChunkHit

# ---------------------------------------------------------------- fixtures


def _cfg(**over) -> EffectiveRedactionConfig:
    """A config with display redaction ON and PII masked (not the default set)."""
    # Annotated: without it mypy infers dict[str, object] from the mixed value
    # types, and every **base argument then fails its parameter type.
    base: dict[str, Any] = {
        "enabled": True,
        "enabled_categories": {"pii", "profanity", "custom"},
        "pii_entities": set(C.REDACTION_PII_ENTITIES),
        "style": "label",
    }
    base.update(over)
    return EffectiveRedactionConfig(**base)


class _StubDetector:
    """Stands in for Presidio: finds one fixed literal, optionally 'fails'.

    Keeps the streaming tests free of model weights. The real detector is
    exercised by the ``models``-marked test at the bottom of this module.
    """

    def __init__(self, needle: str = "123-45-6789", *, fails: bool = False):
        self.needle = needle
        self.fails = fails
        self.calls: list[str] = []

    def detect_segment_spans(self, text, _words, _cfg, *, run_toxicity=True, failures=None):
        self.calls.append(text)
        if self.fails:
            # EXACTLY what the real one does on a detector error: swallow it,
            # report nothing found, and record the failure in the sink. "No
            # spans" and "could not look" are otherwise the same value.
            if failures is not None:
                failures.append("pii")
            return [], None
        spans = []
        start = text.find(self.needle)
        if start >= 0:
            spans.append(
                {
                    "char_start": start,
                    "char_end": start + len(self.needle),
                    "category": "pii",
                    "entity_type": "SSN",
                    "detector": "presidio",
                    "confidence": 0.85,
                }
            )
        return spans, None


@pytest.fixture
def stub_detector(monkeypatch):
    """Install a deterministic detector for the whole redaction service."""

    def _install(detector: _StubDetector) -> _StubDetector:
        monkeypatch.setattr(
            "app.services.redaction.service.RedactionService.detect_segment_spans",
            staticmethod(detector.detect_segment_spans),
        )
        return detector

    return _install


# ---------------------------------------------------------------- the buffer


def test_a_sentence_is_held_back_until_it_is_complete():
    """Nothing is emitted mid-sentence — that is what makes masking possible."""
    r = OutputRedactor(_cfg())
    assert r.active is True
    assert r.buffer("The number ") == ""
    assert r.buffer("he gave was ") == ""
    assert r.buffer("123-45-6789") == ""
    # The terminator alone is not enough: the next delta could continue it.
    assert r.buffer(".") == ""
    assert r.buffer(" Next") == "The number he gave was 123-45-6789. "
    assert r.drain() == "Next"


def test_a_disabled_policy_costs_the_stream_nothing():
    """Pass-through must not buffer at all, or every deployment pays the latency."""
    r = OutputRedactor(_cfg(enabled=False))
    assert r.active is False
    assert r.buffer("token ") == "token "
    assert r.mask("token ") == "token "


def test_redaction_enabled_with_no_categories_is_also_a_pass_through():
    r = OutputRedactor(_cfg(enabled_categories=set()))
    assert r.active is False
    assert r.buffer("anything") == "anything"


def test_an_unresolvable_config_masks_rather_than_passes_through():
    """Fail closed at the policy level: 'I cannot tell' must not mean 'send it raw'."""
    r = OutputRedactor(None)
    assert r.active is True
    assert r.buffer("hello") == ""


def test_a_newline_is_a_boundary_so_markdown_lists_still_stream():
    """A bulleted answer carries no periods; without this it would never flush."""
    r = OutputRedactor(_cfg())
    assert r.buffer("- Alice raised the budget\n") == "- Alice raised the budget\n"


def test_an_honorific_does_not_end_a_sentence():
    """Splitting 'Mr. Smith' would hand the detector 'Smith' alone."""
    r = OutputRedactor(_cfg())
    assert r.buffer("We spoke to Mr. ") == ""
    assert r.buffer("Smith about it. Then") == "We spoke to Mr. Smith about it. "


def test_an_initial_does_not_end_a_sentence():
    r = OutputRedactor(_cfg())
    assert r.buffer("The author is J. ") == ""
    assert r.buffer("Smith here. x") == "The author is J. Smith here. "


def test_a_decimal_does_not_end_a_sentence():
    r = OutputRedactor(_cfg())
    assert (
        r.buffer("The rate was 3.5 percent overall. done") == "The rate was 3.5 percent overall. "
    )


def test_an_unpunctuated_run_still_flushes_and_holds_a_tail():
    """A model writing one long paragraph must not stall the stream forever."""
    r = OutputRedactor(_cfg())
    long_run = ("word " * 400).strip()  # 1999 chars, no terminator anywhere
    emitted = r.buffer(long_run)
    assert emitted, "an over-long boundary-free buffer must force a flush"
    # The tail is held back so an entity straddling the forced cut is not split.
    held = len(long_run) - len(emitted)
    assert held >= 90, f"expected a held tail, only {held} chars retained"


def test_drain_yields_the_tail_exactly_once():
    """Two flush sites (normal exit and shielded teardown) must not duplicate it."""
    r = OutputRedactor(_cfg())
    r.buffer("an unterminated tail")
    assert r.drain() == "an unterminated tail"
    assert r.drain() == ""


# ---------------------------------------------------------------- the masking


def test_a_paraphrased_identifier_in_generated_output_is_masked(stub_detector):
    """THE case offset redaction cannot catch, and the reason this module exists.

    The model restates an identifier in a sentence it composed itself. There is
    no stored segment with those offsets, so every cached-span masker in the
    codebase renders it verbatim.
    """
    stub_detector(_StubDetector())
    r = OutputRedactor(_cfg())

    span = r.buffer("The number he gave was 123-45-6789. ")
    masked = r.mask(span)

    assert "123-45-6789" not in masked
    assert "[SSN]" in masked
    assert r.masked_spans == 1
    assert r.withheld_spans == 0


def test_an_undetectable_span_is_withheld_rather_than_emitted(stub_detector):
    """Fail CLOSED. The detector reports 'nothing found' when it actually broke."""
    stub_detector(_StubDetector(fails=True))
    r = OutputRedactor(_cfg())

    masked = r.mask("The number he gave was 123-45-6789. ")

    assert "123-45-6789" not in masked
    assert masked == C.REDACTION_LLM_FAILSAFE_TEXT
    assert r.withheld_spans == 1
    assert r.masked_spans == 0


def test_a_detector_failure_outside_the_users_categories_does_not_withhold(stub_detector):
    """PII is not in the default categories; a PII failure must not gag those users.

    Otherwise every deployment that never asked for PII masking loses its
    answers the moment Presidio is absent — which is most CPU-only ones.
    """
    stub_detector(_StubDetector(fails=True))
    r = OutputRedactor(_cfg(enabled_categories={"profanity", "custom"}))

    masked = r.mask("An ordinary sentence. ")

    assert masked == "An ordinary sentence. "
    assert r.withheld_spans == 0


def test_citation_markers_survive_masking(stub_detector):
    """`[n]` markers must still resolve after the answer has been masked."""
    stub_detector(_StubDetector())
    r = OutputRedactor(_cfg())

    masked = r.mask("Per the call [2], the number he gave was 123-45-6789. ")

    assert "[2]" in masked
    assert "[SSN]" in masked


# ---------------------------------------------------------------- the stream


def _chunk(index: int, content: str) -> MaskedChunk:
    return MaskedChunk(
        source=ChunkHit(
            file_uuid=f"11111111-1111-1111-1111-00000000000{index}",
            file_id=index,
            chunk_index=index,
            content=content,
            title=f"Recording {index}",
            speaker="Dana",
            start_time=float(index * 60),
            end_time=float(index * 60 + 30),
        ),
        content=content,
    )


class _FakeProvider:
    value = "vllm"


class _FakeConfig:
    provider = _FakeProvider()
    model = "gemma-local"


class _FakeLLM:
    def __init__(self, deltas: list[str], *, context_window: int = 32_000):
        self.config = _FakeConfig()
        self.user_context_window = context_window
        self.response_tokens = 4000
        self._deltas = deltas
        self.sent_messages = None

    def chat_completion_stream(self, messages, cancel_event=None, **_kwargs):
        self.sent_messages = messages
        for text in self._deltas:
            yield LLMStreamEvent(type="delta", text=text)
        yield LLMStreamEvent(type="done", finish_reason="stop")

    def estimate_tokens(self, text: str) -> int:
        return len(text) // 4


@contextmanager
def _null_session():
    yield None


async def _run_turn(monkeypatch, *, deltas, cfg, chunks=None):
    """Drive the real ``stream_reply``; only retrieval, persistence and the LLM stub out."""
    chunks = chunks if chunks is not None else [_chunk(1, "some excerpt text")]
    monkeypatch.setattr("app.db.session_utils.session_scope", _null_session)
    monkeypatch.setattr(
        chat_service,
        "_prepare_context",
        lambda *_a, **_k: (
            list(chunks),
            {"retrieved": len(chunks), "files_searched": "all"},
            None,
            None,
        ),
    )
    monkeypatch.setattr(chat_service.limits, "is_cancelled", lambda _uuid: False)
    monkeypatch.setattr(chat_service, "_resolve_output_policy", lambda _user_id: cfg)

    captured: dict = {}

    async def _fake_finalize(**kwargs):
        captured["turn"] = kwargs["turn"]

    monkeypatch.setattr(chat_service, "_finalize_turn", _fake_finalize)

    llm = _FakeLLM(deltas)
    frames: list[tuple[str, dict]] = []
    generator = chat_service.ChatService.stream_reply(
        conversation_id=1,
        conversation_uuid="conv-uuid",
        user_id=1,
        organization_id=None,
        question="What number did he give?",
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
        llm=llm,
        assistant_message_uuid="00000000-0000-0000-0000-0000000000aa",
        user_message_uuid="00000000-0000-0000-0000-0000000000bb",
        is_first_exchange=True,
    )
    async for raw in generator:
        if raw.startswith(":"):
            continue
        name = raw.split("event: ", 1)[1].split("\n", 1)[0]
        frames.append((name, json.loads(raw.split("data: ", 1)[1].strip())))
    return frames, captured["turn"], llm


def _deltas_of(frames) -> str:
    return "".join(p["text"] for n, p in frames if n == "delta")


@pytest.mark.asyncio
async def test_the_streamed_answer_never_carries_the_raw_identifier(monkeypatch, stub_detector):
    """End to end: the wire must not contain what the model wrote verbatim."""
    stub_detector(_StubDetector())
    frames, turn, _llm = await _run_turn(
        monkeypatch,
        deltas=["The number ", "he gave ", "was 123-45-6789", ". ", "That is all."],
        cfg=_cfg(),
    )

    on_the_wire = _deltas_of(frames)
    assert "123-45-6789" not in on_the_wire
    assert "[SSN]" in on_the_wire
    # And the PERSISTED answer matches what was displayed — storing the raw
    # generation would create an unmasked PII store no read path masks.
    assert "123-45-6789" not in turn.answer
    assert turn.answer == on_the_wire


@pytest.mark.asyncio
async def test_the_unterminated_tail_is_still_emitted(monkeypatch, stub_detector):
    """A model that stops mid-sentence must not lose its last words to the buffer."""
    stub_detector(_StubDetector())
    frames, turn, _llm = await _run_turn(
        monkeypatch,
        deltas=["A complete sentence. ", "and a trailing fragment"],
        cfg=_cfg(),
    )

    assert _deltas_of(frames) == "A complete sentence. and a trailing fragment"
    assert turn.answer.endswith("and a trailing fragment")


@pytest.mark.asyncio
async def test_the_sources_frame_and_citations_are_unchanged(monkeypatch, stub_detector):
    """The streaming contract still holds around the new buffering."""
    stub_detector(_StubDetector())
    frames, turn, llm = await _run_turn(
        monkeypatch,
        deltas=["Per the excerpt [1], the number he gave was 123-45-6789. "],
        cfg=_cfg(),
    )

    sources = next(p for n, p in frames if n == "sources")
    assert len(sources["citations"]) == turn.metadata["chunks_used"] == 1
    prompt = "".join(m["content"] for m in llm.sent_messages)
    assert f'<excerpt id="{sources["citations"][0]["id"]}"' in prompt
    # The marker survived masking, so citation extraction still resolves it.
    assert "[1]" in _deltas_of(frames)
    order = [n for n, _ in frames]
    assert order.index("sources") < order.index("delta")


@pytest.mark.asyncio
async def test_buffering_does_not_trip_the_first_token_watchdog(monkeypatch, stub_detector):
    """The watchdog measures the PROVIDER's first token, not the first emission.

    A whole answer that never completes a sentence emits nothing until the tail
    flush. If the watchdog were keyed off emission that would read as a stalled
    model and be killed as a timeout.
    """
    stub_detector(_StubDetector())
    monkeypatch.setattr(C, "DEFAULT_CHAT_FIRST_TOKEN_TIMEOUT_S", 0.25)
    frames, turn, _llm = await _run_turn(
        monkeypatch,
        deltas=["a fragment ", "with no ", "terminator at all"],
        cfg=_cfg(),
    )

    assert not [p for n, p in frames if n == "error"], "the watchdog fired on a buffered stream"
    assert turn.metadata.get("timings_ms", {}).get("first_token") is not None
    assert _deltas_of(frames) == "a fragment with no terminator at all"


@pytest.mark.asyncio
async def test_a_pass_through_policy_leaves_the_stream_byte_identical(monkeypatch):
    """Redaction off (the default deployment) must change nothing at all."""
    frames, turn, _llm = await _run_turn(
        monkeypatch,
        deltas=["The number ", "he gave was 123-45-6789. "],
        cfg=_cfg(enabled=False),
    )

    assert _deltas_of(frames) == "The number he gave was 123-45-6789. "
    assert turn.metadata.get("output_redaction") is None


@pytest.mark.asyncio
async def test_the_turn_records_what_output_redaction_did(monkeypatch, stub_detector):
    """Diagnostics: a withheld span is otherwise indistinguishable from a short answer."""
    stub_detector(_StubDetector(fails=True))
    _frames, turn, _llm = await _run_turn(
        monkeypatch,
        deltas=["The number he gave was 123-45-6789. "],
        cfg=_cfg(),
    )

    diagnostics = turn.metadata["output_redaction"]
    assert diagnostics["withheld_spans"] == 1
    assert diagnostics["masked_spans"] == 0


# ---------------------------------------------------------------- real detector


@pytest.mark.models
def test_the_real_detector_masks_a_paraphrase_the_offset_path_would_miss():
    """No stub: real Presidio over text that exists in no transcript row.

    Marked ``models`` because it needs the PII detector's weights. It is the
    only test here that proves the *detector* — not the plumbing — catches what
    a model composes in its own words.
    """
    r = OutputRedactor(_cfg())
    masked = r.mask("The number he gave was 123-45-6789 and he lives in Boston. ")

    assert "123-45-6789" not in masked
    assert "[SSN]" in masked
    assert r.withheld_spans == 0
