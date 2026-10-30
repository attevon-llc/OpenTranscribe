"""The WHOLE observable result of ``_prepare_context``, pinned for its decomposition.

``_prepare_context`` was one ~415-line function, split into phase helpers and sibling
modules. The existing suites each pin one facet of it: the masking counters, the map
decoupling, the trace stages, the provider locality kwargs. None pins everything one
turn produces at once, so a split that reorders two phases, drops a ``meta`` key on
one route, or loses a cancellation checkpoint could stay green. These tests pin, for the three shapes a turn takes
(serial summarize, serial counted, planner fan-out):

* the returned six-tuple, including the order of the masked evidence;
* every ``meta`` key and value (timings are checked by key, not value);
* the trace sequence, stage by stage;
* the kwargs every masker call received (the egress decision);
* every cancellation probe, in order.

Only names that stay bound in ``service.py`` are patched through the module alias
(``retrieve_context``, ``mask_chunks``, ``_drop_quarantined_hits``); everything else
is patched where it is defined, which is where the pipeline reads it at call time.
"""

from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
from dataclasses import field
from types import SimpleNamespace
from typing import Any

import pytest

from app.services.chat import service as chat_service
from app.services.chat.language import METADATA_KEY as LANGUAGE_METADATA_KEY
from app.services.chat.language import WARNING_CODE as LANGUAGE_WARNING_CODE
from app.services.chat.legs import FanOutResult
from app.services.chat.legs import Leg
from app.services.chat.legs import LegOutcome
from app.services.chat.mapreduce import DigestScopeHits
from app.services.chat.mapreduce import FileSummary
from app.services.chat.mapreduce import Overview
from app.services.chat.planner import Plan
from app.services.chat.prompt_blocks import format_synthesis_block
from app.services.chat.query_rewriter import RewriteResult
from app.services.chat.redactor import MaskedChunk
from app.services.chat.retrieval import RetrievalResult
from app.services.chat.router import INTENT_AGGREGATE
from app.services.chat.router import INTENT_SUMMARIZE
from app.services.chat.router import TIER_AGGREGATE
from app.services.chat.router import TIER_CHUNK
from app.services.chat.router import TIER_DIGEST
from app.services.chat.router import Route
from app.services.chat.settings import ChatSettings
from app.services.chat.trace import ListTraceRecorder
from app.services.search.chunk_retrieval import ChunkHit

pytestmark = pytest.mark.unit

TURN = "00000000-0000-0000-0000-0000000000c7"
U1 = "11111111-1111-1111-1111-000000000001"
U2 = "11111111-1111-1111-1111-000000000002"
UQ = "11111111-1111-1111-1111-000000000009"  # the quarantined file

_REMOTE_LLM = SimpleNamespace(config=SimpleNamespace(provider="openai", base_url=None))


class _LocalLLM:
    config = SimpleNamespace(provider="vllm", base_url="http://localhost:8012/v1")


class _EnrichingLLM:
    """Remote provider whose one non-streaming call is the enrichment synthesis."""

    config = SimpleNamespace(provider="openai", base_url=None)

    def __init__(self) -> None:
        self.calls: list[list[dict[str, str]]] = []

    def chat_completion(self, messages, **_kwargs):
        self.calls.append(messages)
        return SimpleNamespace(content="The evidence agrees on the budget.")

    def estimate_tokens(self, text: str) -> int:
        return len(text) // 4


def _hit(file_uuid: str, content: str, *, index: int = 0, digest: bool = False) -> ChunkHit:
    return ChunkHit(
        file_uuid=file_uuid,
        file_id=int(file_uuid[-1], 16),
        chunk_index=-1 if digest else index,
        content=content,
        title=f"Recording {file_uuid[-1]}",
        speaker=None if digest else "Dana",
        start_time=float(index * 10),
        end_time=float(index * 10 + 9),
        digest_section=0 if digest else None,
    )


class _LangQuery:
    def filter(self, *_args, **_kwargs):
        return self

    def all(self):
        return [("es",), ("en",)]


class _LangDb:
    """The one real Postgres read left unstubbed: ``describe_context_languages``."""

    def query(self, *_args, **_kwargs):
        return _LangQuery()


@contextmanager
def _lang_session():
    yield _LangDb()


@dataclass
class _Calls:
    """Everything the pipeline handed to its collaborators, in call order."""

    route: list[dict[str, Any]] = field(default_factory=list)
    retrieve: list[dict[str, Any]] = field(default_factory=list)
    quarantine: list[list[str]] = field(default_factory=list)
    mask_chunks: list[tuple[list[str], dict[str, Any]]] = field(default_factory=list)
    mask_digests: list[tuple[list[str], dict[str, Any]]] = field(default_factory=list)
    summaries_masked_text: list[list[str]] = field(default_factory=list)
    overview: list[dict[str, Any]] = field(default_factory=list)
    cancel_probes: list[str] = field(default_factory=list)


def _mask_chunks(calls: _Calls):
    def _fn(_factory, hits, _user_id, **kwargs):
        calls.mask_chunks.append(([h.content for h in hits], kwargs))
        # "beta" fails closed: the masker empties it, exactly as a real policy failure does.
        return [
            MaskedChunk(source=h, content="" if h.content == "beta" else f"M:{h.content}")
            for h in hits
        ]

    return _fn


def _mask_digests(calls: _Calls):
    def _fn(_factory, hits, _user_id, **kwargs):
        calls.mask_digests.append(([h.content for h in hits], kwargs))
        return [MaskedChunk(source=h, content=f"D:{h.content}", was_masked=True) for h in hits]

    return _fn


def _harness(monkeypatch, *, route: Route, retrieval: RetrievalResult) -> _Calls:
    """Patch the collaborators every scenario shares; return the call log."""
    calls = _Calls()

    def _route(*_args, **kwargs):
        calls.route.append(kwargs)
        return route

    def _retrieve(**kwargs):
        calls.retrieve.append(kwargs)
        return retrieval

    def _drop(_db, hits):
        calls.quarantine.append([h.file_uuid for h in hits])
        return [h for h in hits if h.file_uuid != UQ]

    def _probe(message_uuid):
        calls.cancel_probes.append(message_uuid)
        return False

    monkeypatch.setattr("app.db.session_utils.session_scope", _lang_session)
    monkeypatch.setattr(
        "app.services.chat.language.supported_rag_languages", lambda _db: frozenset({"en"})
    )
    monkeypatch.setattr("app.services.chat.router.route", _route)
    monkeypatch.setattr("app.services.chat.limits.is_cancelled", _probe)
    monkeypatch.setattr(chat_service, "retrieve_context", _retrieve)
    monkeypatch.setattr(chat_service, "_drop_quarantined_hits", _drop)
    monkeypatch.setattr(chat_service, "mask_chunks", _mask_chunks(calls))
    monkeypatch.setattr("app.services.chat.redactor.mask_digests", _mask_digests(calls))
    return calls


def _patch_map(monkeypatch, calls: _Calls) -> None:
    """The scope map (bounded summarize): two files mapped, coverage gaps reported."""
    monkeypatch.setattr(
        "app.services.chat.mapreduce.scope_digest_hits",
        lambda *_a, **_k: DigestScopeHits(
            [_hit(U1, "map one", digest=True), _hit(U2, "map two", digest=True)],
            {"files_without_artifacts": 1, "files_no_content": 2, "entries_digest": 2},
        ),
    )

    def _summaries(_db, _hits, *, masked_text, speaker_focus):
        calls.summaries_masked_text.append(sorted(masked_text.values()))
        return [
            FileSummary(file_uuid=U1, digest="d-one", llm_summary="summary"),
            FileSummary(file_uuid=U2, digest="d-two!"),
        ]

    def _overview(question, summaries, **kwargs):
        calls.overview.append({"question": question, "summaries": len(summaries), **kwargs})
        return Overview(
            block="<overview>two recordings</overview>",
            reducer="code",
            files_total=2,
            files_in_scope=2,
            files_listed=2,
            diagnostics={"composer": 1},
        )

    monkeypatch.setattr("app.services.chat.mapreduce.build_file_summaries", _summaries)
    monkeypatch.setattr("app.services.chat.mapreduce.build_overview", _overview)


#: What the two-row language read above classifies to: one English file, one Spanish.
_LANGUAGES = {
    "supported": ["en"],
    "unsupported": ["es"],
    "files": 2,
    "unsupported_files": 1,
    "unknown_files": 0,
}


def _trace(recorder: ListTraceRecorder) -> list[tuple]:
    """Events as plain tuples; a measured duration is checked for type, not value."""
    out = []
    for event in recorder.events:
        detail = dict(event.detail)
        if "ms" in detail:
            assert isinstance(detail["ms"], int)
            detail["ms"] = "<ms>"
        out.append((event.stage.value, event.outcome.value, event.parent, event.node_id, detail))
    return out


def _split_timings(meta: dict[str, Any]) -> tuple[dict[str, Any], list[str]]:
    timings = meta.pop("timings_ms")
    assert all(isinstance(v, int) for v in timings.values())
    return meta, sorted(timings)


def _run(*, llm, settings, history, file_uuids, speakers, rewrite_enabled, recorder):
    return chat_service._prepare_context(
        user_id=3,
        organization_id=7,
        question="What did the team decide about the budget?",
        history=history,
        settings=settings,
        file_uuids=file_uuids,
        speakers=speakers,
        search_mode="hybrid",
        llm=llm,
        rewrite_enabled=rewrite_enabled,
        assistant_message_uuid=TURN,
        recorder=recorder,
    )


def test_a_scoped_summarize_turn_produces_exactly_this(monkeypatch):
    """Serial pipeline, digest tier, bounded scope, remote provider.

    Exercises the quarantine drop (one chunk), a chunk that fails closed under
    masking, the ranked digest leg AND the scope map, the overview diagnostics,
    the language warning and the retrieval-failed flag in one turn.
    """
    route = Route(intent=INTENT_SUMMARIZE, tiers=(TIER_DIGEST, TIER_CHUNK))
    retrieval = RetrievalResult(
        chunks=[_hit(U1, "alpha"), _hit(UQ, "quarantined"), _hit(U2, "beta", index=1)],
        digests=[_hit(U1, "digest one", digest=True)],
        retrieved=4,
        reranked=3,
        retrieval_failed=True,
        timings_ms={"retrieve": 7},
    )
    calls = _harness(monkeypatch, route=route, retrieval=retrieval)
    _patch_map(monkeypatch, calls)
    recorder = ListTraceRecorder()

    masked, meta, counted, overview, synthesis, recurrence = _run(
        llm=_REMOTE_LLM,
        settings=ChatSettings(),
        history=[],
        file_uuids=[U1, U2],
        speakers=["Dana"],
        rewrite_enabled=False,
        recorder=recorder,
    )

    assert [(m.file_uuid, m.content) for m in masked] == [
        (U1, "D:digest one"),
        (U1, "M:alpha"),
    ]
    assert (counted, synthesis, recurrence) == (None, "", "")
    assert overview is not None and overview.block == "<overview>two recordings</overview>"
    meta, timing_keys = _split_timings(meta)
    assert timing_keys == ["retrieve"]
    assert meta == {
        "route": route.as_metadata(),
        "chunks_dropped_quarantined": 1,
        "map_files_without_artifacts": 1,
        "map_files_no_content": 2,
        "overview": {
            "reducer": "code",
            "files_total": 2,
            "files_in_scope": 2,
            "files_listed": 2,
            "llm_calls": 0,
            "truncated": False,
            "composer": 1,
            "entries_digest": 2,
            "block_chars": 35,
            "summary_chars": 7,
            "digest_chars": 11,
        },
        "map_source": "code",
        "map_leg": "scope_map",
        "digests_retrieved": 1,
        "chunks_dropped_empty_after_masking": 1,
        LANGUAGE_METADATA_KEY: _LANGUAGES,
        LANGUAGE_WARNING_CODE: True,
        "retrieved": 3,
        "reranked": 3,
        "cache_hit": False,
        "retrieval_failed": True,
        "files_searched": 2,
        "speakers_filtered": ["Dana"],
    }
    assert calls.route == [
        {
            "rewritten": None,
            "llm_intent": None,
            "speakers": ["Dana"],
            "speaker_focus": False,
            "recurrence_enabled": False,
        }
    ]
    assert len(calls.retrieve) == 1
    retrieve_kwargs = dict(calls.retrieve[0])
    assert retrieve_kwargs.pop("settings").final_chunks == 40
    assert retrieve_kwargs.pop("recorder") is recorder
    assert retrieve_kwargs == {
        "query": "What did the team decide about the budget?",
        "user_id": 3,
        "organization_id": 7,
        "file_uuids": [U1, U2],
        "speakers": ["Dana"],
        "search_mode": "hybrid",
        "wants_digest": True,
        "speaker_focus_names": None,
    }
    assert calls.quarantine == [[U1, UQ, U2], [U1]]
    # Remote provider: no `unmask_for_local` anywhere; expansion only on the chunk plane.
    assert calls.mask_chunks == [
        (["alpha", "beta"], {"organization_id": 7, "expand_short_chunks": True})
    ]
    assert calls.mask_digests == [
        (["digest one"], {"organization_id": 7}),
        (["map one", "map two"], {"organization_id": 7}),
    ]
    assert calls.summaries_masked_text == [["D:map one", "D:map two"]]
    assert calls.overview == [
        {
            "question": "What did the team decide about the budget?",
            "summaries": 2,
            "files_in_scope": 2,
            "speaker_focus": None,
            "citation_start": None,
        }
    ]
    assert calls.cancel_probes == [TURN]
    assert _trace(recorder) == [
        ("rewritten", "skipped", None, "rewrite", {"reason": "disabled"}),
        ("parsed_names", "skipped", None, "names", {"reason": "disabled"}),
        ("planned", "skipped", None, "plan", {"reason": "rules"}),
        ("filtered", "ok", None, "quarantine", {"source": "postgres", "kept": 2, "dropped": 1}),
        ("expanded", "empty", None, "expansion", {"count": 0, "kept": 2}),
        ("filtered", "ok", None, "masking", {"kept": 2, "dropped": 1}),
        ("reviewed", "skipped", None, "review", {"reason": "disabled"}),
    ]


def _patch_counted_turn(monkeypatch) -> None:
    """Rewrite, a resolved speaker mention and the counted tier, all answering."""
    monkeypatch.setattr(
        "app.services.chat.query_rewriter.rewrite_query",
        lambda _llm, _history, _question, want_plan: RewriteResult(
            query="How many budget meetings?", intent="aggregate"
        ),
    )
    monkeypatch.setattr(
        "app.services.chat.speaker_resolver.resolve_speaker_mentions",
        lambda *_a, **_k: SimpleNamespace(
            as_meta=lambda: {"matched": ["Dana"]}, speaker_focus=True, matched=("Dana",)
        ),
    )
    monkeypatch.setattr(
        "app.services.chat.aggregation_service.answer_aggregation",
        lambda *_a, **_k: SimpleNamespace(
            as_metadata=lambda: {"shape": "count", "count": 3}, coverage={}, count=3
        ),
    )
    monkeypatch.setattr("app.services.opensearch_service.get_opensearch_client", lambda: object())


def test_a_counted_follow_up_turn_produces_exactly_this(monkeypatch):
    """Serial pipeline, counted tier, rewrite + speaker focus, LOCAL provider."""
    route = Route(intent=INTENT_AGGREGATE, tiers=(TIER_AGGREGATE, TIER_CHUNK))
    retrieval = RetrievalResult(chunks=[_hit(U1, "alpha")], retrieved=1, timings_ms={"rr": 2})
    calls = _harness(monkeypatch, route=route, retrieval=retrieval)
    _patch_counted_turn(monkeypatch)
    recorder = ListTraceRecorder()

    masked, meta, counted, overview, synthesis, recurrence = _run(
        llm=_LocalLLM(),
        settings=ChatSettings(speaker_resolver_enabled=True),
        history=[{"role": "user", "content": "Earlier question"}],
        file_uuids=None,
        speakers=None,
        rewrite_enabled=True,
        recorder=recorder,
    )

    assert [(m.file_uuid, m.content) for m in masked] == [(U1, "M:alpha")]
    assert counted is not None and counted.count == 3
    assert (overview, synthesis, recurrence) == (None, "", "")
    meta, timing_keys = _split_timings(meta)
    assert timing_keys == ["aggregate", "rewrite", "rr"]
    assert meta == {
        "rewritten_query": "How many budget meetings?",
        "speaker_resolution": {"matched": ["Dana"]},
        "route": route.as_metadata(),
        "aggregation": {"shape": "count", "count": 3},
        "chunk_leg_reduced_to": 13,
        "chunks_dropped_empty_after_masking": 0,
        LANGUAGE_METADATA_KEY: _LANGUAGES,
        LANGUAGE_WARNING_CODE: True,
        "retrieved": 1,
        "reranked": 0,
        "cache_hit": False,
        "files_searched": "all",
        "llm_calls": 1,
    }
    assert calls.route == [
        {
            "rewritten": "How many budget meetings?",
            "llm_intent": "aggregate",
            "speakers": None,
            "speaker_focus": True,
            "recurrence_enabled": False,
        }
    ]
    retrieve_kwargs = dict(calls.retrieve[0])
    assert retrieve_kwargs.pop("settings").final_chunks == 13
    assert retrieve_kwargs["query"] == "How many budget meetings?"
    assert retrieve_kwargs["speaker_focus_names"] == ["Dana"]
    assert retrieve_kwargs["wants_digest"] is False
    # Local provider: the exemption reaches the chunk masker; there is no digest call.
    assert calls.mask_chunks == [
        (
            ["alpha"],
            {"organization_id": 7, "unmask_for_local": True, "expand_short_chunks": True},
        )
    ]
    assert calls.mask_digests == []
    assert calls.quarantine == [[U1], []]
    assert calls.cancel_probes == [TURN]
    assert _trace(recorder) == [
        ("rewritten", "ok", None, "rewrite", {"source": "llm", "ms": "<ms>"}),
        ("parsed_names", "ok", None, "names", {"source": "postgres", "count": 1}),
        ("planned", "skipped", None, "plan", {"reason": "rules"}),
        ("fanned_relational", "ok", None, "counted", {"source": "postgres"}),
        ("found", "ok", None, "counted", {"source": "postgres", "count": 3, "ms": "<ms>"}),
        ("filtered", "ok", None, "quarantine", {"source": "postgres", "kept": 1, "dropped": 0}),
        ("expanded", "empty", None, "expansion", {"count": 0, "kept": 1}),
        ("filtered", "ok", None, "masking", {"kept": 1, "dropped": 0}),
        ("reviewed", "skipped", None, "review", {"reason": "disabled"}),
    ]


def _patch_fanout(monkeypatch, legs_seen: list[dict[str, Any]]) -> None:
    """A turn-1 plan with two sub-questions and a recurrence leg that finds 3 groups."""
    recurrence = SimpleNamespace(groups=[1, 2, 3], truncated=False)
    monkeypatch.setattr("app.services.chat.planner.needs_plan", lambda **_k: True)
    monkeypatch.setattr(
        "app.services.chat.planner.build_plan",
        lambda _llm, _q: (Plan(subquestions=("first?", "second?"), wants=("recurrence",)), 1),
    )

    def _run_legs(built, *, max_workers, cancel_check, recorder, parent):
        legs_seen.append(
            {"legs": [leg.name for leg in built], "max_workers": max_workers, "parent": parent}
        )
        cancel_check()
        return FanOutResult(
            chunk_hits=[_hit(U1, "alpha"), _hit(U2, "gamma", index=2)],
            other={
                "recurrence": LegOutcome(
                    leg=Leg(kind="recurrence", name="recurrence", run=lambda: None),
                    result=recurrence,
                )
            },
            timings_ms={"main": 5, "subquestion-0": 3, "subquestion-1": 4, "recurrence": 2},
            chunk_counts_by_leg={"main": 1, "subquestion-0": 1, "subquestion-1": 1},
        )

    monkeypatch.setattr("app.services.chat.legs.run_legs", _run_legs)
    monkeypatch.setattr(
        "app.services.chat.prompt_blocks.format_recurrence_block",
        lambda result: f"<recurrence>{len(result.groups)}</recurrence>",
    )


def test_a_planned_fan_out_turn_produces_exactly_this(monkeypatch):
    """Planner fan-out, recurrence leg, enrichment synthesis, remote provider."""
    route = Route(intent="lookup", tiers=(TIER_CHUNK,))
    calls = _harness(monkeypatch, route=route, retrieval=RetrievalResult())
    legs_seen: list[dict[str, Any]] = []
    _patch_fanout(monkeypatch, legs_seen)
    llm = _EnrichingLLM()
    recorder = ListTraceRecorder()

    masked, meta, counted, overview, synthesis, recurrence = _run(
        llm=llm,
        settings=ChatSettings(
            planner_enabled=True,
            enrichment_enabled=True,
            recurrence_enabled=True,
            rerank_enabled=False,
        ),
        history=[],
        file_uuids=None,
        speakers=None,
        rewrite_enabled=True,
        recorder=recorder,
    )

    assert [(m.file_uuid, m.content) for m in masked] == [(U1, "M:alpha"), (U2, "M:gamma")]
    assert (counted, overview) == (None, None)
    assert synthesis == format_synthesis_block("The evidence agrees on the budget.")
    assert recurrence == "<recurrence>3</recurrence>"
    assert len(llm.calls) == 1
    meta, timing_keys = _split_timings(meta)
    assert timing_keys == ["fanout"]
    assert meta == {
        "route": route.as_metadata(),
        "plan": Plan(subquestions=("first?", "second?"), wants=("recurrence",)).as_metadata(),
        "leg_timings_ms": {"main": 5, "subquestion-0": 3, "subquestion-1": 4, "recurrence": 2},
        "leg_count": 4,
        "subquestion_legs_with_evidence": 2,
        "chunks_dropped_empty_after_masking": 0,
        LANGUAGE_METADATA_KEY: _LANGUAGES,
        LANGUAGE_WARNING_CODE: True,
        "retrieved": 2,
        "reranked": 0,
        "cache_hit": False,
        "files_searched": "all",
        "extra_llm_prompt_tokens": meta["extra_llm_prompt_tokens"],
        "extra_llm_completion_tokens": len("The evidence agrees on the budget.") // 4,
        "recurrence": {"groups": 3, "truncated": False},
        "llm_calls": 2,
    }
    assert meta["extra_llm_prompt_tokens"] > 0
    assert legs_seen == [
        {
            "legs": ["main", "subquestion-0", "subquestion-1", "recurrence"],
            "max_workers": 4,
            "parent": "plan",
        }
    ]
    assert calls.retrieve == []
    assert calls.mask_chunks == [
        (["alpha", "gamma"], {"organization_id": 7, "expand_short_chunks": True})
    ]
    # Phase 1, the turn-1 planner call, the fan-out start, the leg runner's own check,
    # and the enrichment call: five checkpoints, every one on THIS turn's id.
    assert calls.cancel_probes == [TURN, TURN, TURN, TURN, TURN]
    assert _trace(recorder) == [
        ("rewritten", "skipped", None, "rewrite", {"reason": "not_applicable"}),
        ("parsed_names", "skipped", None, "names", {"reason": "disabled"}),
        ("planned", "ok", None, "plan", {"legs": 4}),
        ("reranked", "skipped", "plan", None, {}),
        ("filtered", "ok", None, "quarantine", {"source": "postgres", "kept": 2, "dropped": 0}),
        ("expanded", "empty", None, "expansion", {"count": 0, "kept": 2}),
        ("filtered", "ok", None, "masking", {"kept": 2, "dropped": 0}),
        ("reviewed", "ok", None, "review", {"count": 1}),
    ]
