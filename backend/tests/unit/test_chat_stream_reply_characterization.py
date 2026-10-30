"""The frame sequence and turn record of ``ChatService.stream_reply``, pinned for its split.

``stream_reply`` (cyclomatic complexity 45) was decomposed into helpers. The existing
suites pin one frame family each (sources, provider errors, keepalives, trace, output
redaction). These pin what none of them do, whole:

* a context turn's complete frame sequence, in order, with every payload;
* the generation kwargs the provider receives and the turn ``_finalize_turn`` sees;
* the first-token timeout (frame, cancel signal, no ``usage`` frame);
* a client disconnect mid-stream: the shielded teardown still flushes the buffered
  redaction tail into the persisted answer, records ``cancelled`` and releases the slot;
* a Stop raised during context preparation (``_TurnCancelled``).

Only names that stay bound in ``service.py`` are patched through the module alias.
"""

from __future__ import annotations

import json
from contextlib import contextmanager
from types import SimpleNamespace
from typing import Any

import pytest

from app.services.chat import service as chat_service
from app.services.chat.mapreduce import Overview
from app.services.chat.redactor import MaskedChunk
from app.services.chat.settings import ChatSettings
from app.services.llm_stream import LLMStreamEvent
from app.services.search.chunk_retrieval import ChunkHit

pytestmark = pytest.mark.unit

ASSISTANT = "00000000-0000-0000-0000-0000000000a1"
USER_MSG = "00000000-0000-0000-0000-0000000000b2"
FILE = "11111111-1111-1111-1111-000000000001"
OVERVIEW_CITATION = {"id": 9, "kind": "digest", "file_uuid": FILE}


class _FakeLLM:
    config = SimpleNamespace(provider=SimpleNamespace(value="custom"), model="test-model")
    user_context_window = 32000
    response_tokens = 4000

    def __init__(self, events: list[LLMStreamEvent]) -> None:
        self.events = events
        self.calls: list[dict[str, Any]] = []

    def chat_completion_stream(self, messages, cancel_event=None, **kwargs):
        self.calls.append({"messages": messages, "cancel_event": cancel_event, "kwargs": kwargs})
        yield from self.events

    def estimate_tokens(self, text: str) -> int:
        return len(text) // 4


@contextmanager
def _null_session():
    yield None


def _masked() -> list[MaskedChunk]:
    hit = ChunkHit(
        file_uuid=FILE,
        file_id=1,
        chunk_index=3,
        content="we agreed on four buttons",
        title="Design review",
        speaker="Dana",
        start_time=60.0,
        end_time=90.0,
    )
    return [MaskedChunk(source=hit, content="we agreed on four buttons")]


def _prepared(meta: dict[str, Any]):
    overview = Overview(block="<overview>one recording</overview>", reducer="code")
    overview.citation_payloads = (dict(OVERVIEW_CITATION),)
    return _masked(), meta, None, overview, "", ""


class _Record:
    def __init__(self) -> None:
        self.prepare_kwargs: dict[str, Any] = {}
        self.finalize: dict[str, Any] = {}
        self.teardowns = 0


def _harness(monkeypatch, *, prepare, policy=None) -> _Record:
    """Patch the five seams every scenario shares; return what they saw."""
    record = _Record()

    def _prepare(**kwargs):
        record.prepare_kwargs = kwargs
        return prepare()

    async def _finalize(**kwargs):
        record.finalize = kwargs
        kwargs["turn"].total_tokens = 42
        kwargs["turn"].title = "Button decision"

    monkeypatch.setattr("app.db.session_utils.session_scope", _null_session)
    monkeypatch.setattr(chat_service, "_prepare_context", _prepare)
    monkeypatch.setattr(chat_service, "_finalize_turn", _finalize)
    monkeypatch.setattr(chat_service.limits, "is_cancelled", lambda _uuid: False)
    monkeypatch.setattr(
        chat_service,
        "_resolve_output_policy",
        lambda _user_id, _organization_id=None: (
            policy or SimpleNamespace(enabled=False, enabled_categories=set())
        ),
    )
    return record


def _reply(llm, record: _Record, *, settings, use_context=True, history=None):
    def _teardown():
        record.teardowns += 1

    return chat_service.ChatService.stream_reply(
        conversation_id=1,
        conversation_uuid="conv-uuid",
        user_id=1,
        organization_id=None,
        question="What did the team decide about the buttons?",
        history=history or [],
        file_uuids=None,
        speakers=None,
        settings=settings,
        use_context=use_context,
        system_prompt="SYS",
        search_mode="hybrid",
        temperature=0.2,
        max_tokens=300,
        top_p=0.9,
        enable_thinking=False,
        llm=llm,
        on_teardown=_teardown,
        assistant_message_uuid=ASSISTANT,
        user_message_uuid=USER_MSG,
        is_first_exchange=True,
    )


def _parse(raw: str) -> tuple[str, Any]:
    name = raw.split("event: ", 1)[1].split("\n", 1)[0]
    return name, json.loads(raw.split("data: ", 1)[1].strip())


async def _frames(generator) -> list[tuple[str, Any]]:
    return [_parse(raw) async for raw in generator if not raw.startswith(":")]


@pytest.mark.asyncio
async def test_a_context_turn_streams_exactly_this(monkeypatch):
    meta = {
        "retrieved": 1,
        "files_searched": "all",
        "speaker_resolution": {"ambiguous": ["Dana Ames", "Dana Bell"]},
        "plan": {"failed": True},
        "extra_llm_prompt_tokens": 5,
        "extra_llm_completion_tokens": 2,
    }
    record = _harness(monkeypatch, prepare=lambda: _prepared(meta))
    monkeypatch.setattr("app.services.chat.planner.needs_plan", lambda **_k: True)
    llm = _FakeLLM(
        [
            LLMStreamEvent(type="reasoning", text="Checking excerpt one."),
            LLMStreamEvent(type="delta", text="Four buttons [1]."),
            LLMStreamEvent(type="usage", prompt_tokens=10, completion_tokens=20),
            LLMStreamEvent(type="done", finish_reason="stop"),
        ]
    )
    settings = ChatSettings(planner_enabled=True, overview_citable=True, trace_enabled=False)

    frames = await _frames(_reply(llm, record, settings=settings))

    names = [name for name, _ in frames]
    assert names == [
        "start",
        "status",
        "warning",
        "warning",
        "sources",
        "status",
        "reasoning",
        "delta",
        "usage",
        "done",
    ]
    payloads = dict(enumerate(payload for _, payload in frames))
    assert payloads[0] == {
        "conversation_uuid": "conv-uuid",
        "user_message_uuid": USER_MSG,
        "assistant_message_uuid": ASSISTANT,
    }
    assert payloads[1] == {"stage": "planning"}
    assert payloads[2] == {"code": "ambiguous_speaker", "candidates": ["Dana Ames", "Dana Bell"]}
    assert payloads[3] == {"code": "plan_failed"}
    citations = payloads[4]["citations"]
    assert len(citations) == 2
    assert citations[0]["file_uuid"] == FILE
    assert citations[1] == OVERVIEW_CITATION
    assert payloads[5] == {"stage": "generating"}
    assert payloads[6] == {"text": "Checking excerpt one."}
    assert payloads[7] == {"text": "Four buttons [1]."}
    assert payloads[8] == {
        "prompt_tokens": 10,
        "completion_tokens": 20,
        "total_tokens": 42,
        "estimated": False,
    }
    assert payloads[9] == {"finish_reason": "stop", "title": "Button decision"}

    assert len(llm.calls) == 1
    assert llm.calls[0]["kwargs"] == {
        "max_tokens": 300,
        "temperature": 0.2,
        "top_p": 0.9,
        "enable_thinking": False,
    }
    assert llm.calls[0]["cancel_event"].is_set()
    assert record.prepare_kwargs["rewrite_enabled"] is True
    assert record.teardowns == 1

    turn = record.finalize.pop("turn")
    messages = record.finalize.pop("messages")
    assert messages[0]["role"] == "system" and messages[-1]["role"] == "user"
    assert isinstance(record.finalize.pop("started"), float)
    assert record.finalize == {
        "llm": llm,
        "masked_count": 1,
        "conversation_id": 1,
        "conversation_uuid": "conv-uuid",
        "assistant_message_uuid": ASSISTANT,
        "user_id": 1,
        "organization_id": None,
        "is_first_exchange": True,
        "question": "What did the team decide about the buttons?",
        "use_context": True,
    }
    assert (turn.answer, turn.reasoning) == ("Four buttons [1].", "Checking excerpt one.")
    assert (turn.prompt_tokens, turn.completion_tokens) == (10, 20)
    assert (turn.extra_prompt_tokens, turn.extra_completion_tokens) == (5, 2)
    assert (turn.finish_reason, turn.error, turn.error_code) == ("stop", None, None)
    assert turn.offered_citations == citations
    assert turn.metadata["chunks_used"] == 1
    assert turn.metadata["context_window"] == 32000
    assert sorted(turn.metadata["timings_ms"]) == ["first_token"]
    for key, value in meta.items():
        assert turn.metadata[key] == value
    assert "output_redaction" not in turn.metadata


@pytest.mark.asyncio
async def test_a_model_that_never_starts_times_out(monkeypatch):
    record = _harness(monkeypatch, prepare=lambda: pytest.fail("no context turn"))
    monkeypatch.setattr("app.core.chat_defaults.DEFAULT_CHAT_FIRST_TOKEN_TIMEOUT_S", -1)
    llm = _FakeLLM(
        [
            LLMStreamEvent(type="usage", prompt_tokens=3, completion_tokens=0),
            LLMStreamEvent(type="delta", text="Too late."),
        ]
    )

    frames: list[tuple[str, Any]] = []
    stopped_at_timeout_frame = None
    generator = _reply(llm, record, settings=ChatSettings(trace_enabled=False), use_context=False)
    async for raw in generator:
        if raw.startswith(":"):
            continue
        frames.append(_parse(raw))
        if frames[-1][0] == "error":
            # The provider must be told to stop by the time the user is told it timed
            # out, not only later by the turn's own teardown.
            stopped_at_timeout_frame = llm.calls[0]["cancel_event"].is_set()

    assert stopped_at_timeout_frame is True
    assert frames == [
        (
            "start",
            {
                "conversation_uuid": "conv-uuid",
                "user_message_uuid": USER_MSG,
                "assistant_message_uuid": ASSISTANT,
            },
        ),
        ("status", {"stage": "generating"}),
        ("error", {"code": "timeout", "message": "The model did not start responding in time."}),
        ("done", {"finish_reason": "stop", "title": "Button decision"}),
    ]
    turn = record.finalize["turn"]
    assert (turn.error_code, turn.metadata["error_code"]) == ("timeout", "timeout")
    assert turn.answer == ""
    assert record.finalize["use_context"] is False
    assert record.finalize["masked_count"] == 0
    assert llm.calls[0]["cancel_event"].is_set()
    assert record.teardowns == 1


@pytest.mark.asyncio
async def test_a_disconnect_mid_answer_still_persists_the_masked_tail(monkeypatch):
    """The shielded teardown: buffered tail masked and kept, turn recorded as cancelled."""
    record = _harness(
        monkeypatch,
        prepare=lambda: pytest.fail("no context turn"),
        policy=SimpleNamespace(enabled=True, enabled_categories={"pii"}),
    )
    monkeypatch.setattr(
        "app.services.chat.output_redactor.OutputRedactor.mask",
        lambda _self, span: span.upper(),
    )
    llm = _FakeLLM(
        [
            LLMStreamEvent(type="delta", text="First sentence. Second par"),
            LLMStreamEvent(type="delta", text="t never arrives."),
            LLMStreamEvent(type="done", finish_reason="stop"),
        ]
    )
    generator = _reply(llm, record, settings=ChatSettings(trace_enabled=False), use_context=False)

    seen = []
    async for raw in generator:
        if raw.startswith(":"):
            continue
        seen.append(_parse(raw))
        if seen[-1][0] == "delta":
            break
    await generator.aclose()

    assert seen[-1] == ("delta", {"text": "FIRST SENTENCE. "})
    turn = record.finalize["turn"]
    assert turn.answer == "FIRST SENTENCE. SECOND PAR"
    assert (turn.finish_reason, turn.error) == ("cancelled", None)
    assert turn.metadata["output_redaction"] == {"masked_spans": 0, "withheld_spans": 0}
    assert "output_redaction" in turn.metadata["timings_ms"]
    assert llm.calls[0]["cancel_event"].is_set()
    assert record.teardowns == 1


@pytest.mark.asyncio
async def test_a_stop_during_preparation_ends_the_turn_as_cancelled(monkeypatch):
    def _cancelled():
        raise chat_service._TurnCancelled()

    record = _harness(monkeypatch, prepare=_cancelled)
    llm = _FakeLLM([LLMStreamEvent(type="delta", text="never")])

    frames = await _frames(
        _reply(
            llm,
            record,
            settings=ChatSettings(trace_enabled=False),
            history=[{"role": "user", "content": "Earlier question"}],
        )
    )

    assert [name for name, _ in frames] == ["start", "status", "usage", "done"]
    assert frames[1] == ("status", {"stage": "rewriting"})
    assert frames[2] == (
        "usage",
        {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 42, "estimated": False},
    )
    assert frames[3] == ("done", {"finish_reason": "cancelled", "title": "Button decision"})
    assert llm.calls == []
    turn = record.finalize["turn"]
    assert (turn.finish_reason, turn.error) == ("cancelled", None)
    assert record.finalize["messages"] == []
    assert record.teardowns == 1
