"""A chat turn must hold NO DB session while it does OpenSearch or LLM work.

A session held open across slow non-DB work is this repo's single most repeated
defect. A plain ``SELECT`` takes ``ACCESS SHARE`` for the life of its
transaction, so such a hold queues every ``ALTER TABLE`` — i.e. it hangs an
Alembic upgrade mid-release — pins the vacuum horizon, and consumes a pool
connection throughout. ``scripts/audit-session-lifetime.py`` is the static gate;
``_prepare_context`` was its last un-fixed finding in this package, carried as a
``BACKLOG`` allowlist entry.

The defect's exact shape: ``stream_reply`` wrapped **one**
``with session_scope()`` around a call that then ran the query rewrite (an LLM
round trip), the counted tier's OpenSearch aggregation, and retrieval
(OpenSearch + cross-encoder + Redis) before it touched Postgres at all.

These tests drive the real ``ChatService.stream_reply`` generator with an
instrumented ``session_scope`` and record, for each slow stage, **how many DB
sessions were live while it ran**. Retrieval, the rewrite and the aggregation
search must each see zero.

The control that stops "zero sessions" being satisfied by deleting the session
is :func:`test_masking_still_runs_inside_a_session`: the DB-bound stage must
still receive a live one, because masking reads cached redaction spans out of
Postgres and fails closed without them.
"""

from __future__ import annotations

import json
from contextlib import contextmanager
from types import SimpleNamespace

import pytest

from app.services.chat import service as chat_service
from app.services.chat.query_rewriter import RewriteResult
from app.services.chat.redactor import MaskedChunk
from app.services.chat.retrieval import RetrievalResult
from app.services.chat.settings import ChatSettings
from app.services.llm_stream import LLMStreamEvent
from app.services.search.chunk_retrieval import ChunkHit

pytestmark = pytest.mark.unit

#: Routes to the aggregate tier (signals ``how-many`` + ``corpus-noun``) with no
#: temporal hint, so the counted tier runs its OpenSearch aggregation and no
#: Postgres date filter — which is the stage the allowlist entry named.
AGGREGATE_QUESTION = "How many meetings discussed the Atlas migration?"


class _FakeSession:
    """Sentinel handed out by the instrumented ``session_scope``."""


class _Ledger:
    """Counts live DB sessions and what ran while they were open."""

    def __init__(self) -> None:
        self.live = 0
        self.opened = 0
        #: stage name -> the live-session count observed each time it ran.
        self.live_during: dict[str, list[int]] = {}
        #: The session object each DB-bound stage was handed.
        self.sessions_seen: dict[str, list[object]] = {}

    @contextmanager
    def session_scope(self):
        self.live += 1
        self.opened += 1
        try:
            yield _FakeSession()
        finally:
            self.live -= 1

    def note(self, stage: str) -> None:
        self.live_during.setdefault(stage, []).append(self.live)


class _CountingClient:
    """OpenSearch stand-in that records the session depth of every search."""

    def __init__(self, ledger: _Ledger) -> None:
        self._ledger = ledger
        self.searches = 0

    def search(self, index: str, body: dict) -> dict:  # noqa: ARG002
        self._ledger.note("aggregation-search")
        self.searches += 1
        return {"aggregations": {"files": {"sum_other_doc_count": 0, "buckets": []}}}


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


def _hit(index: int) -> ChunkHit:
    return ChunkHit(
        file_uuid=f"11111111-1111-1111-1111-00000000000{index}",
        file_id=index,
        chunk_index=index,
        content=f"content {index}",
        title="Recording",
        speaker="Dana",
        start_time=float(index),
        end_time=float(index) + 5.0,
    )


async def _run_turn(monkeypatch, *, file_uuids=None, history=None) -> tuple[_Ledger, dict]:
    """Drive one real turn. Returns the ledger and what each stage was passed."""
    ledger = _Ledger()
    seen: dict = {}
    hits = [_hit(0), _hit(1)]

    monkeypatch.setattr("app.db.session_utils.session_scope", ledger.session_scope)
    monkeypatch.setattr(
        "app.services.opensearch_service.get_opensearch_client",
        lambda: _CountingClient(ledger),
    )

    def _rewrite(_llm, _history, question, **_kwargs):
        # `**_kwargs` absorbs `want_plan` (#403 W2.6): this harness's default
        # `ChatSettings()` has `planner_enabled=False`, so `_prepare_context`
        # never asks for one, but the real call site always passes the
        # keyword — a stub with the old positional-only signature would
        # break on every turn, planner or not.
        ledger.note("query-rewrite")
        return RewriteResult(query=question, intent=None)

    monkeypatch.setattr("app.services.chat.query_rewriter.rewrite_query", _rewrite)

    def _retrieve(**kwargs):
        ledger.note("retrieval")
        seen["retrieval_file_uuids"] = kwargs["file_uuids"]
        return RetrievalResult(chunks=list(hits), retrieved=len(hits))

    monkeypatch.setattr(chat_service, "retrieve_context", _retrieve)

    # Quarantine dropping (phase 3.5) is a real Postgres query
    # (`app.models.media.MediaFile`), and this harness's session is a bare
    # `_FakeSession()` sentinel with no `.query` — it exists to count live
    # sessions, not to answer real queries. Every stage in this file is
    # already a mocked identity pass-through for exactly that reason; this
    # one is no different. `_drop_quarantined_hits` itself is covered against
    # a real database in test_chat_permissions_quarantine.py.
    monkeypatch.setattr(chat_service, "_drop_quarantined_hits", lambda _db, hits: hits)

    def _mask(session_factory, chunks, _user_id, **_kwargs):
        # The masker is handed the FACTORY, not a session (#83): it gathers its
        # cached spans, closes, and only then runs a detector that may cost a
        # ~10 s Presidio build. So the stub records BOTH — the depth at entry
        # (must be 0) and the depth inside the gather it opens (must be 1).
        # `**_kwargs` absorbs `unmask_for_local` (the provider-locality keying,
        # a separate concern from what this file measures).
        ledger.note("masking")
        with session_factory() as db:
            ledger.note("masking-gather")
            ledger.sessions_seen.setdefault("masking", []).append(db)
        return [MaskedChunk(source=chunk, content=chunk.content) for chunk in chunks]

    monkeypatch.setattr(chat_service, "mask_chunks", _mask)

    # A disabled policy makes OutputRedactor a byte-identical pass-through. The
    # `None` this would otherwise resolve to means "mask everything", which runs
    # real detectors — not what is under test here.
    monkeypatch.setattr(
        chat_service,
        "_resolve_output_policy",
        lambda _user_id: SimpleNamespace(enabled=False, enabled_categories=set()),
    )
    monkeypatch.setattr(chat_service.limits, "is_cancelled", lambda _uuid: False)

    async def _fake_finalize(**kwargs):
        seen["turn"] = kwargs["turn"]

    monkeypatch.setattr(chat_service, "_finalize_turn", _fake_finalize)

    frames: list[tuple[str, dict]] = []
    generator = chat_service.ChatService.stream_reply(
        conversation_id=1,
        conversation_uuid="conv-uuid",
        user_id=1,
        organization_id=None,
        question=AGGREGATE_QUESTION,
        history=history if history is not None else [{"role": "user", "content": "earlier"}],
        file_uuids=file_uuids,
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
        if raw.startswith(":"):
            continue
        name = raw.split("event: ", 1)[1].split("\n", 1)[0]
        frames.append((name, json.loads(raw.split("data: ", 1)[1].strip())))

    seen["frames"] = frames
    return ledger, seen


@pytest.mark.asyncio
async def test_retrieval_runs_with_no_db_session_open(monkeypatch):
    """The stage the allowlist entry named: OpenSearch + rerank + Redis."""
    ledger, _seen = await _run_turn(monkeypatch)

    assert ledger.live_during["retrieval"] == [0], (
        "a DB session was live during OpenSearch retrieval — the session-lifetime "
        f"rule this fix exists for (live counts: {ledger.live_during})"
    )


@pytest.mark.asyncio
async def test_the_query_rewrite_llm_call_runs_with_no_db_session_open(monkeypatch):
    """A provider stall is bounded only by its HTTP timeout; a transaction must not be."""
    ledger, _seen = await _run_turn(monkeypatch)

    assert ledger.live_during["query-rewrite"] == [0], (
        f"a DB session was live during the rewrite LLM call: {ledger.live_during}"
    )


@pytest.mark.asyncio
async def test_the_counted_tier_aggregation_runs_with_no_db_session_open(monkeypatch):
    """``answer_aggregation``'s OpenSearch search, on the caller's session."""
    ledger, _seen = await _run_turn(monkeypatch)

    assert ledger.live_during["aggregation-search"] == [0], (
        f"a DB session was live during the counted tier's aggregation: {ledger.live_during}"
    )


@pytest.mark.asyncio
async def test_masking_still_runs_inside_a_session(monkeypatch):
    """The control: the fix is a phase split, NOT the removal of the session.

    Masking reads each chunk's cached redaction spans out of Postgres. If the
    refactor left it without a session it would fail closed on every chunk and
    every answer would be ungrounded — which the assertions above would happily
    call a pass.

    Since #83 the masker opens that session **itself**, from the factory the turn
    hands it, so that it can close it before the inline Presidio fallback runs.
    Both halves are asserted: the call is entered with none live, and the gather
    inside it gets exactly one, on a real session object.
    """
    ledger, _seen = await _run_turn(monkeypatch)

    assert ledger.live_during["masking"] == [0], (
        f"the turn must hand masking a FACTORY, not an open session: {ledger.live_during}"
    )
    assert ledger.live_during["masking-gather"] == [1], (
        f"masking's gather must run with exactly one live session: {ledger.live_during}"
    )
    handed = ledger.sessions_seen["masking"]
    assert len(handed) == 1
    assert isinstance(handed[0], _FakeSession), "masking was handed something that is not a session"


@pytest.mark.asyncio
async def test_the_turn_still_answers_after_the_split(monkeypatch):
    """Phases must not drop the answer: the whole SSE contract still runs."""
    _ledger, seen = await _run_turn(monkeypatch)

    names = [name for name, _payload in seen["frames"]]
    assert names[0] == "start"
    assert "sources" in names
    assert "done" in names
    assert seen["turn"].answer == "An answer."


@pytest.mark.asyncio
async def test_an_empty_scope_still_means_match_nothing(monkeypatch):
    """``file_uuids=[]`` is "match nothing"; ``None`` is "all accessible".

    Inverting those leaks the entire library, and a phase split that rebuilt the
    scope between phases is exactly how one would get inverted. Both values must
    arrive at retrieval unchanged.
    """
    _ledger, empty_scope = await _run_turn(monkeypatch, file_uuids=[])
    assert empty_scope["retrieval_file_uuids"] == []

    _ledger, all_accessible = await _run_turn(monkeypatch, file_uuids=None)
    assert all_accessible["retrieval_file_uuids"] is None


# --------------------------------------------------------------------------- #403 W2.6: the fan-out
#
# Extends this file's coverage to the planner-driven parallel leg fan-out
# (`chat_service._run_plan_fanout`, backed by `legs.run_legs`'s shared
# executor). The same rule applies PER LEG here, not just per turn — a
# fan-out multiplies any session-held-across-slow-work violation by leg
# count, so this is the test that would catch a leg built with an open
# session instead of a factory.


class _FakeLLMWithPlan:
    """A `_FakeLLM` that also answers the standalone planner call (turn 1,
    no history) with a well-formed two-subquestion plan."""

    def __init__(self) -> None:
        self.config = _FakeConfig()
        self.user_context_window = 32000
        self.response_tokens = 1000

    def chat_completion(self, _messages, **_kwargs):
        return SimpleNamespace(
            content='{"subquestions": ["sub one", "sub two"], "speakers": [], '
            '"time": {}, "wants": []}'
        )

    def chat_completion_stream(self, messages, cancel_event=None, **_kwargs):  # noqa: ARG002
        yield LLMStreamEvent(type="delta", text="An answer.")
        yield LLMStreamEvent(type="done", finish_reason="stop")

    def estimate_tokens(self, text: str) -> int:
        return len(text) // 4


async def _run_fanout_turn(monkeypatch) -> tuple[_Ledger, dict]:
    """Drive one real planner-fan-out turn. Mirrors `_run_turn`'s shape and
    exists for the same reason: keeping every monkeypatch in ONE shared
    helper is what keeps each test function itself under the mock-heavy
    threshold, the same structure the rest of this file already uses.
    """
    ledger = _Ledger()
    seen: dict = {}
    monkeypatch.setattr("app.db.session_utils.session_scope", ledger.session_scope)

    call_count = {"n": 0}

    def _retrieve_chunks(_query, **_kwargs):
        call_count["n"] += 1
        ledger.note("fanout-chunk-leg")
        return [_hit(call_count["n"])]

    monkeypatch.setattr("app.services.search.chunk_retrieval.retrieve_chunks", _retrieve_chunks)
    monkeypatch.setattr(chat_service, "_drop_quarantined_hits", lambda _db, hits: hits)

    def _mask(session_factory, chunks, _user_id, **_kwargs):
        with session_factory() as db:
            ledger.sessions_seen.setdefault("masking", []).append(db)
        return [MaskedChunk(source=chunk, content=chunk.content) for chunk in chunks]

    monkeypatch.setattr(chat_service, "mask_chunks", _mask)
    monkeypatch.setattr(
        chat_service,
        "_resolve_output_policy",
        lambda _user_id: SimpleNamespace(enabled=False, enabled_categories=set()),
    )
    monkeypatch.setattr(chat_service.limits, "is_cancelled", lambda _uuid: False)

    async def _fake_finalize(**_kwargs):
        return None

    monkeypatch.setattr(chat_service, "_finalize_turn", _fake_finalize)

    from app.services.chat.settings import ChatSettings as _ChatSettings

    generator = chat_service.ChatService.stream_reply(
        conversation_id=1,
        conversation_uuid="conv-uuid",
        user_id=1,
        organization_id=None,
        # Multi-part turn 1 (no history): fires `needs_plan` deterministically.
        question="What did Dana say about pricing? What did Ravi say about the budget?",
        history=[],
        file_uuids=None,
        speakers=[],
        settings=_ChatSettings(planner_enabled=True, rerank_enabled=False),
        use_context=True,
        system_prompt="SYS",
        search_mode="hybrid",
        temperature=None,
        max_tokens=None,
        top_p=None,
        llm=_FakeLLMWithPlan(),
        assistant_message_uuid="00000000-0000-0000-0000-0000000000cc",
        user_message_uuid="00000000-0000-0000-0000-0000000000dd",
        is_first_exchange=True,
    )
    frames = []
    async for raw in generator:
        if raw.startswith(":"):
            continue
        frames.append(raw)

    seen["frames"] = frames
    seen["chunk_leg_calls"] = call_count["n"]
    return ledger, seen


@pytest.mark.asyncio
async def test_the_fanouts_chunk_legs_run_with_no_db_session_open(monkeypatch):
    """Each of the fan-out's OpenSearch legs (main + 2 subquestions) must see
    zero live sessions — the same invariant `test_retrieval_runs_with_no_db_
    session_open` pins for the single-leg path, now checked per leg."""
    ledger, seen = await _run_fanout_turn(monkeypatch)

    assert seen["chunk_leg_calls"] >= 3, "expected the main leg plus 2 subquestion legs to run"
    assert ledger.live_during["fanout-chunk-leg"] == [0] * seen["chunk_leg_calls"], (
        f"a DB session was live during a fan-out chunk leg: {ledger.live_during}"
    )
    names = [raw.split("event: ", 1)[1].split("\n", 1)[0] for raw in seen["frames"]]
    assert "start" in names and "done" in names, "the fan-out turn must still answer end to end"
