"""``trace`` frames must reach the client WHILE the pipeline runs (GH #514).

The unit tests around ``StreamingTraceRecorder`` prove the bridge in isolation.
These drive the real ``ChatService.stream_reply`` generator, because the two
things most likely to be wrong are properties of the *generator*, not the queue:

- retrieval happens inside one ``run_in_threadpool`` call, so a trace event
  produced there reaches the socket only if something drains the queue **during**
  the await — otherwise the panel animates nothing and then fills in at the end,
  which is a summary wearing a live trace's clothes;
- ``PRESENTED`` and ``BUDGETED`` fire *after* that drain has already returned, so
  without a second flush they are queued and silently never delivered. With a
  live-only trace there is no persistence to cover that gap.

The flag-off control matters as much as the rest: the trace is opt-in, and a turn
with it off must produce byte-identical output to before the feature existed.
"""

from __future__ import annotations

import dataclasses
import json
import threading
import time
from contextlib import contextmanager
from types import SimpleNamespace

import pytest

from app.services.chat import service as chat_service
from app.services.chat.settings import ChatSettings
from app.services.chat.trace import QueryStage
from app.services.chat.trace import emit
from app.services.llm_stream import LLMStreamEvent

pytestmark = pytest.mark.unit

PREP_SECONDS = 0.4


class _FakeProvider:
    value = "custom"


class _FakeConfig:
    provider = _FakeProvider()
    model = "test-model"


class _FakeLLM:
    def __init__(self) -> None:
        self.config = _FakeConfig()
        self.user_context_window = 32000
        self.response_tokens = 1000

    def chat_completion_stream(self, messages, cancel_event=None, **_kwargs):  # noqa: ARG002
        yield LLMStreamEvent(type="delta", text="An answer.")
        yield LLMStreamEvent(type="done", finish_reason="stop")

    def estimate_tokens(self, text: str) -> int:
        return len(text) // 4


class _FakeSession:
    """Sentinel for the persistence path; no query is run against it."""


@contextmanager
def _fake_session_scope():
    yield _FakeSession()


async def _run_turn(monkeypatch, *, trace_enabled: bool) -> dict:
    """Drive one real turn whose retrieval phase emits trace events and blocks."""
    prep_finished_at: list[float] = []

    monkeypatch.setattr("app.db.session_utils.session_scope", _fake_session_scope)

    def _slow_prepare(**kwargs):
        recorder = kwargs.get("recorder")
        # Emitted BEFORE the block, so a live drain must deliver it before
        # `_prepare_context` returns. This is the whole assertion below.
        emit(recorder, QueryStage.FANNED_VECTOR, node_id="main", plane="chunk")
        threading.Event().wait(PREP_SECONDS)
        emit(recorder, QueryStage.FOUND, node_id="main", count=3)
        prep_finished_at.append(time.monotonic())
        return [], {}, None, None, "", ""

    monkeypatch.setattr(chat_service, "_prepare_context", _slow_prepare)
    monkeypatch.setattr(
        chat_service,
        "_resolve_output_policy",
        lambda _user_id: SimpleNamespace(enabled=False, enabled_categories=set()),
    )
    monkeypatch.setattr(chat_service.limits, "is_cancelled", lambda _uuid: False)

    async def _fake_finalize(**_kwargs):
        return None

    monkeypatch.setattr(chat_service, "_finalize_turn", _fake_finalize)

    frames: list[tuple[str, dict, float]] = []
    generator = chat_service.ChatService.stream_reply(
        conversation_id=1,
        conversation_uuid="conv-uuid",
        user_id=1,
        organization_id=None,
        question="what did the board decide about the migration",
        history=[],
        file_uuids=None,
        speakers=[],
        settings=dataclasses.replace(ChatSettings(), trace_enabled=trace_enabled),
        use_context=True,
        system_prompt="SYS",
        search_mode="hybrid",
        temperature=None,
        max_tokens=None,
        top_p=None,
        llm=_FakeLLM(),
        assistant_message_uuid="00000000-0000-0000-0000-0000000000aa",
        user_message_uuid="00000000-0000-0000-0000-0000000000bb",
        is_first_exchange=True,
    )
    async for raw in generator:
        now = time.monotonic()
        if raw.startswith(":"):
            continue
        name = raw.split("event: ", 1)[1].split("\n", 1)[0]
        frames.append((name, json.loads(raw.split("data: ", 1)[1].strip()), now))

    assert prep_finished_at, "the retrieval stub never ran — the harness is wrong"
    return {"frames": frames, "prep_finished_at": prep_finished_at[0]}


def _traces(seen: dict) -> list[tuple[dict, float]]:
    return [(payload, at) for name, payload, at in seen["frames"] if name == "trace"]


def _names(seen: dict) -> list[str]:
    return [name for name, _payload, _at in seen["frames"]]


@pytest.mark.asyncio
async def test_a_trace_frame_reaches_the_client_before_retrieval_finishes(monkeypatch):
    """Live, not a summary delivered at the end.

    The event is emitted before the retrieval stub blocks, so a working drain
    puts it on the wire while the phase is still running. Buffering it would
    still produce the frame — just uselessly late — so the assertion is on
    ORDERING against the phase, never on the frame merely existing.
    """
    seen = await _run_turn(monkeypatch, trace_enabled=True)

    traces = _traces(seen)
    assert traces, "no trace frames were emitted at all"
    first_at = min(at for _payload, at in traces)
    assert first_at < seen["prep_finished_at"], (
        "every trace frame arrived after retrieval finished, so the panel would "
        "animate nothing and then fill in at the end "
        f"(retrieval ended at {seen['prep_finished_at']:.3f}, first trace at {first_at:.3f})"
    )


@pytest.mark.asyncio
async def test_the_final_stages_are_delivered_and_not_stranded_in_the_queue(monkeypatch):
    """B3. ``BUDGETED``/``PRESENTED`` fire after the drain loop has returned.

    Without a flush at that point they sit in the queue forever — the last and
    most meaningful stages of the turn simply never arrive, and nothing errors.
    """
    seen = await _run_turn(monkeypatch, trace_enabled=True)

    stages = [payload["stage"] for payload, _at in _traces(seen)]
    assert "presented" in stages, f"PRESENTED never reached the client; got {stages}"
    assert "budgeted" in stages, f"BUDGETED never reached the client; got {stages}"

    names = _names(seen)
    last_trace = len(names) - 1 - names[::-1].index("trace")
    assert last_trace < names.index("done"), "a trace frame must not arrive after `done`"


@pytest.mark.asyncio
async def test_the_turn_opens_with_submitted_and_validated(monkeypatch):
    seen = await _run_turn(monkeypatch, trace_enabled=True)

    stages = [payload["stage"] for payload, _at in _traces(seen)]
    assert stages[:2] == ["submitted", "validated"], f"unexpected opening stages: {stages[:4]}"


@pytest.mark.asyncio
async def test_every_frame_carries_a_monotonic_sequence_and_nested_detail(monkeypatch):
    """The wire contract the client's fold depends on."""
    seen = await _run_turn(monkeypatch, trace_enabled=True)

    payloads = [payload for payload, _at in _traces(seen)]
    seqs = [p["seq"] for p in payloads]
    assert seqs == sorted(seqs), f"sequence numbers arrived out of order: {seqs}"
    assert len(set(seqs)) == len(seqs), f"duplicate sequence numbers: {seqs}"
    for payload in payloads:
        assert isinstance(payload["detail"], dict), "detail must stay a nested object"
        assert set(payload) >= {"seq", "stage", "outcome", "parent", "node_id", "detail"}


@pytest.mark.asyncio
async def test_the_flag_off_turn_emits_no_trace_frames_at_all(monkeypatch):
    """The control that makes 'free when off' a fact rather than an intention.

    A turn with the flag off must be indistinguishable from one before this
    feature existed — same frames, same order, no trace machinery on the wire.
    """
    seen = await _run_turn(monkeypatch, trace_enabled=False)

    assert not _traces(seen), "trace frames leaked onto a turn with the flag off"
    names = _names(seen)
    assert names[0] == "start"
    assert names[-1] == "done"
    assert "delta" in names, f"the answer must still stream; frames were {names}"


@pytest.mark.asyncio
async def test_a_recorder_that_explodes_cannot_break_the_turn(monkeypatch):
    """A trace failure must degrade the diagnostic, never the answer.

    ``trace.emit`` guards the calling thread, but the queue hand-off runs later
    on the loop thread where that guard cannot reach — so this drives the real
    generator rather than asserting on ``emit`` alone.
    """
    from app.services.chat import trace_stream

    class _ExplodingRecorder(trace_stream.StreamingTraceRecorder):
        def record(self, event):
            raise RuntimeError("recorder is broken")

    monkeypatch.setattr(trace_stream, "StreamingTraceRecorder", _ExplodingRecorder)
    monkeypatch.setattr(chat_service, "StreamingTraceRecorder", _ExplodingRecorder)

    seen = await _run_turn(monkeypatch, trace_enabled=True)

    names = _names(seen)
    assert names[-1] == "done", f"the turn did not complete; frames were {names}"
    assert "delta" in names, "the answer must still stream through a broken recorder"
