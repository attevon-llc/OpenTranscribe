"""The SSE keepalive must reach the socket WHILE retrieval runs, not after it.

``stream_reply`` puts zero bytes on the wire during ``_prepare_context`` — the
slowest phase of a turn, routinely several seconds — so nginx's 60 s
``proxy_read_timeout`` can close the stream mid-answer. ``_KEEPALIVE`` comments
exist to stop that.

**They did not work.** ``_with_keepalive`` pushed a comment into ``keepalive_q``
every 15 s, but the drain that yielded them ran *after* the ``await`` returned,
and **an async generator cannot yield from inside an ``await``**. So every
keepalive produced during retrieval was buffered and flushed only once retrieval
had already finished — precisely when it was no longer needed. The mechanism
looked present in the code, produced frames in a transcript, and protected
nothing.

That is why the assertion here is about **ordering against the phase it is
supposed to cover**, not about a keepalive existing. "A keepalive frame was
emitted" is true on the broken code too, which is exactly how this survived.
"""

from __future__ import annotations

import threading
import time
from contextlib import contextmanager
from types import SimpleNamespace

import pytest

from app.services.chat import service as chat_service
from app.services.chat.settings import ChatSettings
from app.services.llm_stream import LLMStreamEvent

pytestmark = pytest.mark.unit

#: Long enough that several keepalive ticks must fall inside it, short enough to
#: keep the suite fast. The real phase is seconds; this is the same shape.
PREP_SECONDS = 0.6

#: Well under PREP_SECONDS, so a working keepalive fires many times during the
#: phase. The production value is 15 s and is patched out here — testing the real
#: interval would mean a 15 s test.
FAST_INTERVAL = 0.05


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
    """Sentinel for the persistence path; no query is ever run against it."""


@contextmanager
def _fake_session_scope():
    yield _FakeSession()


async def _run_turn_with_slow_retrieval(monkeypatch) -> dict:
    """Drive a real turn whose retrieval phase blocks for ``PREP_SECONDS``.

    Returns the arrival time of every frame plus the moment retrieval finished,
    all on one ``time.monotonic()`` clock so they are directly comparable.
    """
    prep_finished_at: list[float] = []

    monkeypatch.setattr(chat_service, "_KEEPALIVE_INTERVAL_S", FAST_INTERVAL)
    monkeypatch.setattr("app.db.session_utils.session_scope", _fake_session_scope)

    def _slow_prepare(**_kwargs):
        # Blocking on purpose: the real `_prepare_context` is synchronous and
        # runs via `run_in_threadpool`, which is the whole reason the generator
        # cannot yield during it.
        #
        # An Event rather than `time.sleep`, so the wait is releasable: this runs
        # on a shared threadpool worker, and a bare sleep would keep occupying it
        # past the end of the test if anything above went wrong.
        threading.Event().wait(PREP_SECONDS)
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

    keepalive_times: list[float] = []
    event_times: list[tuple[str, float]] = []

    generator = chat_service.ChatService.stream_reply(
        conversation_id=1,
        conversation_uuid="conv-uuid",
        user_id=1,
        organization_id=None,
        question="What did the team decide about the migration?",
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
        llm=_FakeLLM(),
        assistant_message_uuid="00000000-0000-0000-0000-0000000000aa",
        user_message_uuid="00000000-0000-0000-0000-0000000000bb",
        is_first_exchange=True,
    )
    async for raw in generator:
        now = time.monotonic()
        if raw.startswith(":"):
            keepalive_times.append(now)
        else:
            event_times.append((raw.split("event: ", 1)[1].split("\n", 1)[0], now))

    assert prep_finished_at, "the retrieval stub never ran — the harness is wrong, not the code"
    return {
        "keepalives": keepalive_times,
        "events": event_times,
        "prep_finished_at": prep_finished_at[0],
    }


@pytest.mark.asyncio
async def test_a_keepalive_reaches_the_client_during_retrieval(monkeypatch):
    """The bug, stated as an ordering fact.

    At least one keepalive must be *yielded* before retrieval finishes. On the
    broken code every keepalive is yielded afterwards, so this fails while
    ``len(keepalives) > 0`` still passes.
    """
    seen = await _run_turn_with_slow_retrieval(monkeypatch)
    expected_ticks = int(PREP_SECONDS / FAST_INTERVAL) // 2

    assert len(seen["keepalives"]) >= expected_ticks, (
        f"expected at least {expected_ticks} keepalives across a {PREP_SECONDS}s "
        f"phase at a {FAST_INTERVAL}s interval, got {len(seen['keepalives'])}"
    )
    # The whole claim, as one comparison: the FIRST keepalive must land before
    # retrieval ends. On the broken code every keepalive carried the same
    # timestamp as the end of the phase, so this is `>=` there, never `<`.
    first_keepalive = min(seen["keepalives"])
    assert first_keepalive < seen["prep_finished_at"], (
        "every keepalive arrived AFTER retrieval finished, so the connection was "
        "silent for the whole phase the keepalive exists to cover "
        f"(retrieval ended at {seen['prep_finished_at']:.3f}, first keepalive at "
        f"{first_keepalive:.3f})"
    )


@pytest.mark.asyncio
async def test_the_turn_still_completes_and_answers(monkeypatch):
    """Control: the keepalive path must not change what the turn produces.

    Without this, "yield keepalives during retrieval" is also satisfied by a
    generator that emits comments and then breaks the answer.
    """
    seen = await _run_turn_with_slow_retrieval(monkeypatch)

    names = [name for name, _ in seen["events"]]
    assert names[0] == "start", f"first frame should be `start`, got {names[:3]}"
    assert names[-1] == "done", f"last frame should be `done`, got {names[-3:]}"
    assert "delta" in names, f"the answer never streamed; frames were {names}"


@pytest.mark.asyncio
async def test_keepalives_are_paced_not_dumped_in_one_burst(monkeypatch):
    """A buffered flush arrives as a burst; a live one arrives spread out.

    This is the same defect seen from the other side, and it is what stops the
    fix being "drain the queue one frame earlier".
    """
    seen = await _run_turn_with_slow_retrieval(monkeypatch)

    during = [t for t in seen["keepalives"] if t < seen["prep_finished_at"]]
    assert len(during) >= 2, (
        f"expected several keepalives across {PREP_SECONDS}s at a {FAST_INTERVAL}s "
        f"interval, got {len(during)} during the phase"
    )
    spread = during[-1] - during[0]
    assert spread >= FAST_INTERVAL, (
        f"keepalives spanned only {spread:.4f}s, which is a buffered burst rather "
        f"than live pacing at a {FAST_INTERVAL}s interval"
    )
