"""W2.3: wiring the speaker resolver into ``service.py`` — it was built INERT.

A sibling lane (W2.2) built ``speaker_resolver.py``, ``Route.speaker_focus``,
and ``retrieve_context(..., speaker_focus_names=...)``, but ``service.py`` was
off-limits to it, so nothing ever called any of it. These tests pin the wiring:

* Phase 1.5 of ``_prepare_context`` calls ``resolve_speaker_mentions`` — but
  ONLY when ``chat.speaker_resolver_enabled`` is on, and the flag-off shape is
  BYTE-IDENTICAL to before this lane touched the file (no ``meta`` key,
  ``route()``/``retrieve_context()`` called with the exact same defaults they
  always had).
* A resolved focus is threaded into both ``route(speaker_focus=...)`` and
  ``retrieve_context(speaker_focus_names=...)``.
* ``meta["speaker_resolution"]`` carries the resolution, and an ambiguous
  match reaches the client as the ``ambiguous_speaker`` warning frame.
* D6: none of this needs an LLM — ``llm=None`` throughout.
"""

from __future__ import annotations

import json
from contextlib import contextmanager

import pytest

from app.services.chat import service as chat_service
from app.services.chat.redactor import MaskedChunk
from app.services.chat.retrieval import RetrievalResult
from app.services.chat.router import Route
from app.services.chat.settings import ChatSettings
from app.services.chat.speaker_resolver import SpeakerMentionResolution
from app.services.llm_stream import LLMStreamEvent
from app.services.search.chunk_retrieval import ChunkHit

pytestmark = pytest.mark.unit


@contextmanager
def _null_session():
    yield None


# --------------------------------------------------------- _apply_speaker_resolution


def test_flag_off_never_calls_the_resolver_and_returns_no_names(monkeypatch):
    def _explode(*_a, **_k):
        raise AssertionError("resolve_speaker_mentions must not run when the flag is off")

    monkeypatch.setattr("app.services.chat.speaker_resolver.resolve_speaker_mentions", _explode)
    meta: dict = {}

    names = chat_service._apply_speaker_resolution(
        settings=ChatSettings(speaker_resolver_enabled=False),
        question="What did Dana say?",
        user_id=1,
        organization_id=None,
        session_scope=_null_session,
        meta=meta,
    )

    assert names == []
    assert "speaker_resolution" not in meta


def test_flag_on_with_a_resolved_focus_returns_the_matched_names(monkeypatch):
    monkeypatch.setattr(
        "app.services.chat.speaker_resolver.resolve_speaker_mentions",
        lambda *_a, **_k: SpeakerMentionResolution(matched=("Dana Whitfield",), speaker_focus=True),
    )
    meta: dict = {}

    names = chat_service._apply_speaker_resolution(
        settings=ChatSettings(speaker_resolver_enabled=True),
        question="What did Dana say about pricing?",
        user_id=1,
        organization_id=None,
        session_scope=_null_session,
        meta=meta,
    )

    assert names == ["Dana Whitfield"]
    assert meta["speaker_resolution"]["matched"] == ["Dana Whitfield"]


def test_flag_on_but_nothing_resolved_still_never_sets_the_meta_key(monkeypatch):
    monkeypatch.setattr(
        "app.services.chat.speaker_resolver.resolve_speaker_mentions",
        lambda *_a, **_k: SpeakerMentionResolution(),
    )
    meta: dict = {}

    names = chat_service._apply_speaker_resolution(
        settings=ChatSettings(speaker_resolver_enabled=True),
        question="What was decided?",
        user_id=1,
        organization_id=None,
        session_scope=_null_session,
        meta=meta,
    )

    assert names == []
    assert "speaker_resolution" not in meta


def test_a_match_without_a_speaker_verb_frame_is_not_a_focus(monkeypatch):
    """`speaker_focus` is False even though a name matched — no verb frame."""
    monkeypatch.setattr(
        "app.services.chat.speaker_resolver.resolve_speaker_mentions",
        lambda *_a, **_k: SpeakerMentionResolution(
            matched=("Dana Whitfield",), speaker_focus=False
        ),
    )
    meta: dict = {}

    names = chat_service._apply_speaker_resolution(
        settings=ChatSettings(speaker_resolver_enabled=True),
        question="the meeting with Dana",
        user_id=1,
        organization_id=None,
        session_scope=_null_session,
        meta=meta,
    )

    assert names == []
    assert meta["speaker_resolution"]["matched"] == ["Dana Whitfield"]


def test_an_ambiguous_match_is_reported_but_resolves_no_focus(monkeypatch):
    monkeypatch.setattr(
        "app.services.chat.speaker_resolver.resolve_speaker_mentions",
        lambda *_a, **_k: SpeakerMentionResolution(ambiguous=("Dana",)),
    )
    meta: dict = {}

    names = chat_service._apply_speaker_resolution(
        settings=ChatSettings(speaker_resolver_enabled=True),
        question="what did Dana say",
        user_id=1,
        organization_id=None,
        session_scope=_null_session,
        meta=meta,
    )

    assert names == []
    assert meta["speaker_resolution"]["ambiguous"] == ["Dana"]


# ------------------------------------------------------ _prepare_context wiring


def _prepare(monkeypatch, *, resolution, speaker_resolver_enabled, question="What did Dana say?"):
    """Drive the real `_prepare_context`, capturing what `route()` and
    `retrieve_context()` were called with — the two threading points this
    lane wires up. Retrieval/masking/mapping are stubbed no-ops; only the
    resolver wiring is under test."""
    monkeypatch.setattr("app.db.session_utils.session_scope", _null_session)
    monkeypatch.setattr(chat_service, "_drop_quarantined_hits", lambda _db, hits: hits)
    monkeypatch.setattr(
        "app.services.chat.speaker_resolver.resolve_speaker_mentions",
        lambda *_a, **_k: resolution,
    )

    captured: dict = {}

    def _fake_route(*_a, **kwargs):
        captured["route_kwargs"] = kwargs
        return Route(intent="lookup", tiers=("chunk",))

    monkeypatch.setattr("app.services.chat.router.route", _fake_route)

    def _fake_retrieve_context(**kwargs):
        captured["retrieve_kwargs"] = kwargs
        return RetrievalResult(chunks=[], digests=[], retrieved=0)

    monkeypatch.setattr(chat_service, "retrieve_context", _fake_retrieve_context)
    monkeypatch.setattr(chat_service, "mask_chunks", lambda *_a, **_k: [])

    masked, meta, _counted, _overview, _synthesis, _recurrence = chat_service._prepare_context(
        user_id=1,
        organization_id=None,
        question=question,
        history=[],
        settings=ChatSettings(speaker_resolver_enabled=speaker_resolver_enabled),
        file_uuids=None,
        speakers=None,
        search_mode="hybrid",
        llm=None,
        rewrite_enabled=False,
    )
    return masked, meta, captured


def test_flag_off_is_byte_identical_to_before_this_lane(monkeypatch):
    """THE pin: with the flag off, route() and retrieve_context() are called
    with EXACTLY the arguments they always were — no new key on either call,
    and no meta key appears."""
    _masked, meta, captured = _prepare(
        monkeypatch,
        resolution=SpeakerMentionResolution(matched=("Dana Whitfield",), speaker_focus=True),
        speaker_resolver_enabled=False,
    )

    assert captured["route_kwargs"]["speaker_focus"] is False
    assert captured["retrieve_kwargs"]["speaker_focus_names"] is None
    assert "speaker_resolution" not in meta


def test_flag_on_threads_the_resolved_focus_into_route_and_retrieval(monkeypatch):
    _masked, meta, captured = _prepare(
        monkeypatch,
        resolution=SpeakerMentionResolution(matched=("Dana Whitfield",), speaker_focus=True),
        speaker_resolver_enabled=True,
    )

    assert captured["route_kwargs"]["speaker_focus"] is True
    assert captured["retrieve_kwargs"]["speaker_focus_names"] == ["Dana Whitfield"]
    assert meta["speaker_resolution"]["matched"] == ["Dana Whitfield"]


def test_flag_on_with_no_resolution_reproduces_the_flag_off_shape(monkeypatch):
    """Flag on, but the resolver found nothing — the downstream calls must
    look exactly like the flag-off case."""
    _masked, meta, captured = _prepare(
        monkeypatch,
        resolution=SpeakerMentionResolution(),
        speaker_resolver_enabled=True,
    )

    assert captured["route_kwargs"]["speaker_focus"] is False
    assert captured["retrieve_kwargs"]["speaker_focus_names"] is None
    assert "speaker_resolution" not in meta


def test_d6_llm_is_none_throughout_speaker_resolution(monkeypatch):
    """The resolver is Postgres-only and never touches an LLM — pin it works
    with no provider configured at all (#403 D6)."""
    masked, meta, _captured = _prepare(
        monkeypatch,
        resolution=SpeakerMentionResolution(matched=("Dana Whitfield",), speaker_focus=True),
        speaker_resolver_enabled=True,
    )
    assert masked == []
    assert meta["speaker_resolution"]["matched"] == ["Dana Whitfield"]


# ------------------------------------------------------- the ambiguous_speaker frame


class _FakeProvider:
    value = "custom"


class _FakeConfig:
    provider = _FakeProvider()
    model = "test-model"


class _FakeLLM:
    def __init__(self) -> None:
        self.config = _FakeConfig()
        self.user_context_window = 8192
        self.response_tokens = 512

    def chat_completion_stream(self, messages, cancel_event=None, **_kwargs):
        yield LLMStreamEvent(type="delta", text="An answer.")
        yield LLMStreamEvent(type="done", finish_reason="stop")

    def estimate_tokens(self, text: str) -> int:
        return len(text) // 4


async def _stream_with_meta(monkeypatch, *, meta_extra: dict):
    monkeypatch.setattr("app.db.session_utils.session_scope", _null_session)
    chunk = MaskedChunk(
        source=ChunkHit(
            file_uuid="11111111-1111-1111-1111-111111111111",
            file_id=1,
            chunk_index=0,
            content="excerpt text",
            title="Recording",
        ),
        content="excerpt text",
    )
    diagnostics = {"retrieved": 1, "files_searched": "all", **meta_extra}
    monkeypatch.setattr(
        chat_service,
        "_prepare_context",
        # Six values since #403 W2.6 added the fan-out's `<synthesis>`/
        # `<recurrence>` blocks to `_prepare_context`'s return; both empty for a
        # turn the router left on the chunk plane. A stale arity here does NOT
        # surface as an error: the unpack raises inside `stream_reply`, whose
        # broad `except` turns it into a `provider_error` frame, so the warning
        # frame under test simply never appears.
        lambda *_a, **_k: ([chunk], dict(diagnostics), None, None, "", ""),
    )
    monkeypatch.setattr(chat_service.limits, "is_cancelled", lambda _uuid: False)

    async def _fake_finalize(**_kwargs):
        return None

    monkeypatch.setattr(chat_service, "_finalize_turn", _fake_finalize)

    llm = _FakeLLM()
    frames: list[tuple[str, dict]] = []
    generator = chat_service.ChatService.stream_reply(
        conversation_id=1,
        conversation_uuid="conv-uuid",
        user_id=1,
        organization_id=None,
        question="what did Dana say?",
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
        payload = json.loads(raw.split("data: ", 1)[1].strip())
        frames.append((name, payload))
    return frames


def _frame(frames, name, code=None):
    for event, payload in frames:
        if event == name and (code is None or payload.get("code") == code):
            return payload
    return None


@pytest.mark.asyncio
async def test_an_ambiguous_speaker_resolution_emits_the_warning_frame(monkeypatch):
    frames = await _stream_with_meta(
        monkeypatch,
        meta_extra={"speaker_resolution": {"ambiguous": ["Dana", "Dan"]}},
    )

    warning = _frame(frames, "warning", code="ambiguous_speaker")
    assert warning is not None
    assert warning["candidates"] == ["Dana", "Dan"]


@pytest.mark.asyncio
async def test_no_speaker_resolution_meta_emits_no_ambiguous_warning(monkeypatch):
    """Must-stay-clean twin: an ordinary turn with no resolution data emits
    no ambiguous_speaker frame at all."""
    frames = await _stream_with_meta(monkeypatch, meta_extra={})

    assert _frame(frames, "warning", code="ambiguous_speaker") is None


@pytest.mark.asyncio
async def test_a_uniquely_matched_resolution_emits_no_ambiguous_warning(monkeypatch):
    frames = await _stream_with_meta(
        monkeypatch,
        meta_extra={"speaker_resolution": {"matched": ["Dana Whitfield"]}},
    )

    assert _frame(frames, "warning", code="ambiguous_speaker") is None
