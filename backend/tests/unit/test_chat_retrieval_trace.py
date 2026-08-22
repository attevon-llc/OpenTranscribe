"""What ``retrieve_context`` reports about its own work (GH #514).

This is the DEFAULT retrieval path — ``chat.planner_enabled`` is off, so nearly
every real turn runs here rather than through the fan-out that ``legs.py``
already instruments. Uninstrumented, the panel is empty for almost every user.

Every "it fires" test below is paired with its opposite outcome, because the
distinctions this trace exists to make are all between two things that produce
the same *answer*:

- a cache HIT vs a cache MISS (a miss was invisible entirely),
- a search that ran and found nothing (``EMPTY``) vs one that never ran
  (``SKIPPED``) vs one that broke (``FAILED``) — ``retrieve_chunks`` degrades to
  ``[]`` for all three,
- and how many candidates diversity sampling threw away, which no code path
  reported before.
"""

from __future__ import annotations

import dataclasses

import pytest

from app.services.chat import retrieval as retrieval_mod
from app.services.chat.retrieval import RetrievalResult
from app.services.chat.retrieval import retrieve_context
from app.services.chat.settings import ChatSettings
from app.services.chat.trace import ListTraceRecorder
from app.services.chat.trace import Outcome
from app.services.chat.trace import QueryStage
from app.services.chat.trace import TraceEvent
from app.services.search.chunk_retrieval import ChunkHit

pytestmark = pytest.mark.unit


def _hit(index: int, file_index: int = 0) -> ChunkHit:
    return ChunkHit(
        file_uuid=f"11111111-1111-1111-1111-00000000000{file_index}",
        file_id=file_index,
        chunk_index=index,
        content=f"content {index}",
        title="Q3 Board Review",
        speaker="Alice Chen",
        start_time=float(index),
        end_time=float(index) + 5.0,
    )


def _settings(**overrides) -> ChatSettings:
    """``ChatSettings`` is a FROZEN dataclass — build variants, never mutate."""
    return dataclasses.replace(ChatSettings(), **overrides)


@pytest.fixture
def no_cache(monkeypatch):
    """Neutralise both cache tiers so a test opts INTO a hit rather than out."""
    cache = pytest.importorskip("app.services.chat.retrieval_cache")
    monkeypatch.setattr(cache, "get_cached", lambda _key: None)
    monkeypatch.setattr(cache, "set_cached", lambda *a, **k: None)
    monkeypatch.setattr(cache, "find_semantic_match", lambda **k: None)
    monkeypatch.setattr(cache, "remember_semantic", lambda **k: None)
    return cache


def _run(
    monkeypatch, *, hits, settings=None, **kwargs
) -> tuple[ListTraceRecorder, RetrievalResult]:
    """Drive the real ``retrieve_context`` with a stubbed chunk search."""
    recorder = ListTraceRecorder()
    monkeypatch.setattr(retrieval_mod, "retrieve_chunks", hits)
    result = retrieve_context(
        query="what did the board decide",
        user_id=1,
        organization_id=None,
        file_uuids=None,
        settings=settings or ChatSettings(),
        recorder=recorder,
        **kwargs,
    )
    return recorder, result


def _by_node(recorder: ListTraceRecorder, stage: QueryStage) -> dict[str | None, TraceEvent]:
    return {e.node_id: e for e in recorder.events if e.stage is stage}


def _stages(recorder: ListTraceRecorder) -> list[QueryStage]:
    return [e.stage for e in recorder.events]


# ---------------------------------------------------------------------------
# The cache: a miss is a finding, not an absence
# ---------------------------------------------------------------------------


def test_a_cache_miss_is_recorded_as_a_stage_that_ran_and_found_nothing(monkeypatch, no_cache):
    recorder, _ = _run(monkeypatch, hits=lambda *a, **k: [_hit(0)])

    cache_event = _by_node(recorder, QueryStage.CACHE_LOOKUP)["cache"]
    assert cache_event.outcome is Outcome.EMPTY, "a miss must not look like 'no cache configured'"
    assert cache_event.detail["source"] == "cache"


def test_a_cache_hit_reports_cached_and_marks_the_search_as_never_run(monkeypatch, no_cache):
    """The opposite outcome, and the reason SKIPPED exists as its own value.

    On a hit the search genuinely did not happen. Rendering it as EMPTY would
    claim we looked and found nothing; omitting it entirely would read as "this
    pipeline has no search step".
    """
    monkeypatch.setattr(no_cache, "get_cached", lambda _key: [_hit(0), _hit(1)])

    def _must_not_run(*_a, **_k):
        raise AssertionError("the chunk search must not run on a cache hit")

    recorder, result = _run(monkeypatch, hits=_must_not_run)

    assert result.cache_hit is True
    cache_event = _by_node(recorder, QueryStage.CACHE_LOOKUP)["cache"]
    assert cache_event.outcome is Outcome.CACHED
    assert cache_event.detail["count"] == 2

    search = _by_node(recorder, QueryStage.FANNED_VECTOR)["main"]
    assert search.outcome is Outcome.SKIPPED
    assert search.detail["reason"] == "cached"
    assert _by_node(recorder, QueryStage.RERANKED)["rerank"].outcome is Outcome.SKIPPED
    assert _by_node(recorder, QueryStage.SAMPLED)["sample"].outcome is Outcome.SKIPPED


# ---------------------------------------------------------------------------
# Empty vs failed: `retrieve_chunks` returns [] for both
# ---------------------------------------------------------------------------


def test_a_search_that_found_nothing_is_empty_not_failed(monkeypatch, no_cache):
    recorder, _ = _run(monkeypatch, hits=lambda *a, **k: [])

    found = _by_node(recorder, QueryStage.FOUND)["main"]
    assert found.outcome is Outcome.EMPTY
    assert found.detail["count"] == 0


def test_a_search_that_broke_is_failed_not_empty(monkeypatch, no_cache):
    """The #438 distinction, carried into the trace.

    ``retrieve_chunks`` swallows an OpenSearch outage and returns ``[]``, which
    is byte-identical to a genuine no-match. Only the ``diagnostics`` out-param
    separates them, and a panel that showed both as "found nothing" would
    reproduce the exact confident-wrong-answer failure #438 was opened for.
    """

    def _broken(*_a, **kwargs):
        kwargs["diagnostics"]["retrieval_failed"] = True
        return []

    recorder, _ = _run(monkeypatch, hits=_broken)

    found = _by_node(recorder, QueryStage.FOUND)["main"]
    assert found.outcome is Outcome.FAILED
    assert found.detail["reason"] == "search_failed"


# ---------------------------------------------------------------------------
# Sampling: where a turn's evidence actually goes
# ---------------------------------------------------------------------------


def test_diversity_sampling_reports_what_it_kept_and_what_it_dropped(monkeypatch, no_cache):
    """The '48 -> 12' node — the most useful number the trace adds.

    Twenty candidates from one file, capped at 4 per file, must report the
    discard rather than quietly presenting 4 as the whole evidence base.
    """
    settings = _settings(rerank_enabled=False, max_chunks_per_file=4, final_chunks=12)
    pool = [_hit(i, file_index=0) for i in range(20)]

    recorder, result = _run(monkeypatch, hits=lambda *a, **k: pool, settings=settings)

    sampled = _by_node(recorder, QueryStage.SAMPLED)["sample"]
    assert sampled.detail["kept"] == len(result.chunks)
    assert sampled.detail["kept"] == 4, "one file, capped at 4 per file"
    assert sampled.detail["dropped"] == 16, "20 candidates in, 4 kept — 16 discarded"
    assert sampled.detail["limit"] == 4


def test_reranking_off_is_skipped_with_a_reason_not_silently_absent(monkeypatch, no_cache):
    settings = _settings(rerank_enabled=False)

    recorder, _ = _run(monkeypatch, hits=lambda *a, **k: [_hit(0)], settings=settings)

    rerank = _by_node(recorder, QueryStage.RERANKED)["rerank"]
    assert rerank.outcome is Outcome.SKIPPED
    assert rerank.detail["reason"] == "disabled"


# ---------------------------------------------------------------------------
# Leak review: this stage sees titles, speakers and the query itself
# ---------------------------------------------------------------------------


def test_no_recorded_detail_carries_content_titles_or_speaker_names(monkeypatch, no_cache):
    """Every value in this module's fixtures that a node must never hold.

    ``retrieve_context`` has the query string, chunk content, file titles and
    speaker names all in scope at the moment it emits. ``_scrub`` is the
    backstop; this asserts the call sites never construct the leak in the first
    place, which is the property that survives a rendering mistake downstream.
    """
    settings = _settings(rerank_enabled=False)
    pool = [_hit(i) for i in range(5)]

    recorder, _ = _run(
        monkeypatch,
        hits=lambda *a, **k: pool,
        settings=settings,
        speaker_focus_names=["Alice Chen"],
    )

    assert recorder.events, "precondition: the run recorded something to inspect"
    blob = " ".join(str(e.detail) for e in recorder.events)
    for secret in (
        "Q3 Board Review",  # file title
        "Alice Chen",  # speaker name, incl. the focus leg's own argument
        "what did the board decide",  # the user's question
        "content 0",  # chunk text
        "11111111-1111-1111-1111",  # file uuid
    ):
        assert secret not in blob, f"{secret!r} reached a trace node"


def test_the_recorder_is_optional_and_costs_nothing_when_absent(monkeypatch, no_cache):
    """Control for every test above: instrumentation must not be load-bearing.

    Passing no recorder is the production default until the flag is on, so the
    retrieval result must be identical without one.
    """
    monkeypatch.setattr(retrieval_mod, "retrieve_chunks", lambda *a, **k: [_hit(0), _hit(1)])
    settings = _settings(rerank_enabled=False)

    result = retrieve_context(
        query="what did the board decide",
        user_id=1,
        organization_id=None,
        file_uuids=None,
        settings=settings,
    )

    assert len(result.chunks) == 2
    assert result.retrieved == 2


def test_a_summarize_turn_records_the_digest_leg_and_others_mark_it_skipped(monkeypatch, no_cache):
    """The digest plane is a real leg, and its absence is a decision.

    A turn the router did not send to the summarize tier never queries that
    plane — which must read as "not applicable here", not as a plane that
    returned nothing.
    """
    monkeypatch.setattr(retrieval_mod, "retrieve_chunks", lambda *a, **k: [_hit(0)])
    off = ListTraceRecorder()
    retrieve_context(
        query="q",
        user_id=1,
        organization_id=None,
        file_uuids=None,
        settings=ChatSettings(),
        recorder=off,
        wants_digest=False,
    )
    digest_off = _by_node(off, QueryStage.FANNED_VECTOR)["digest"]
    assert digest_off.outcome is Outcome.SKIPPED
    assert digest_off.detail["reason"] == "not_applicable"
    assert QueryStage.FANNED_VECTOR in _stages(off), "precondition: the stage was recorded at all"


# ---------------------------------------------------------------------------
# Speaker resolution: three outcomes, and the sharpest leak surface in the trace
# ---------------------------------------------------------------------------


def _resolution(*, matched, ambiguous=(), speaker_focus=None):
    """Stand in for a `_resolve_speaker_focus` result."""

    class _R:
        def __init__(self):
            self.matched = list(matched)
            self.speaker_focus = speaker_focus if speaker_focus is not None else bool(matched)

        def as_meta(self):
            return {"ambiguous": list(ambiguous)} if ambiguous else {}

    return _R()


def _resolve_names(monkeypatch, resolution, *, enabled=True):
    from app.services.chat import service as chat_service

    recorder = ListTraceRecorder()
    monkeypatch.setattr(chat_service, "_resolve_speaker_focus", lambda **_k: resolution)
    settings = _settings(speaker_resolver_enabled=enabled)
    names = chat_service._apply_speaker_resolution(
        settings=settings,
        question="what did Alice Chen say about layoffs",
        user_id=1,
        organization_id=None,
        session_scope=None,
        meta={},
        recorder=recorder,
    )
    return recorder, names


def test_speaker_resolution_reports_three_distinct_outcomes(monkeypatch):
    """Off, resolved, and ambiguous are three different facts.

    An ambiguous mention resolves to NO filter deliberately — never a guess —
    and reporting that as EMPTY would present a refusal as an absence.
    """
    off, _ = _resolve_names(monkeypatch, _resolution(matched=[]), enabled=False)
    assert off.events[0].outcome is Outcome.SKIPPED
    assert off.events[0].detail["reason"] == "disabled"

    hit, names = _resolve_names(monkeypatch, _resolution(matched=["Alice Chen"]))
    assert names == ["Alice Chen"], "control: resolution still returns the real names"
    assert hit.events[0].outcome is Outcome.OK
    assert hit.events[0].detail["count"] == 1

    amb, _ = _resolve_names(
        monkeypatch,
        _resolution(matched=[], ambiguous=["Alice Chen", "Alice Chu"]),
    )
    assert amb.events[0].outcome is Outcome.DECLINED
    assert amb.events[0].detail["count"] == 2
    assert amb.events[0].detail["reason"] == "ambiguous"


def test_speaker_resolution_never_records_a_speaker_name(monkeypatch):
    """The sharpest leak surface in the whole trace.

    This stage holds real roster names in locals at the moment it emits, and
    `**resolution.as_meta()` is one keystroke from putting them on the wire.
    `_scrub` is the backstop; the call site must not build the leak at all.
    """
    for resolution in (
        _resolution(matched=["Alice Chen"]),
        _resolution(matched=[], ambiguous=["Alice Chen", "Alice Chu"]),
    ):
        recorder, _ = _resolve_names(monkeypatch, resolution)
        assert recorder.events, "precondition: something was recorded to inspect"
        blob = " ".join(str(e.detail) for e in recorder.events)
        assert "Alice Chen" not in blob, "a speaker name reached a trace node"
        assert "Alice Chu" not in blob, "a candidate name reached a trace node"


# ---------------------------------------------------------------------------
# Every node must be addressable
# ---------------------------------------------------------------------------


def test_every_emitted_node_names_itself(monkeypatch, no_cache):
    """A node without an id is not addressable, and the client keys on it.

    `traceTree` identifies a node by `(parent, node_id ?? stage)`. The stage
    fallback works, but it is only safe while at most one such emitter fires per
    parent — so an anonymous node is a latent collision, not a cosmetic gap.
    `REVIEWED` shipped anonymous and was caught by a live probe rather than by a
    test; this is that test.
    """
    settings = _settings(rerank_enabled=False)
    recorder, _ = _run(
        monkeypatch,
        hits=lambda *a, **k: [_hit(0)],
        settings=settings,
        wants_digest=True,
    )

    assert recorder.events, "precondition: the run recorded something to inspect"
    anonymous = [e.stage.value for e in recorder.events if e.node_id is None]
    assert not anonymous, f"stages emitted with no node_id: {sorted(set(anonymous))}"
