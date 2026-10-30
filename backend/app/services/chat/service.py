"""Chat turn orchestration: retrieval → prompt → stream → persist.

Emits the SSE frames the frontend consumes (``start``, ``status``, ``sources``,
``warning``, ``delta``, ``reasoning``, ``usage``, ``done``, ``error``). The event
names are a frozen contract shared with the frontend implementation.
``reasoning`` is shaped identically to ``delta`` (``{"text": ...}``) but carries a
model's separately streamed reasoning/thinking content, rendered by the frontend
in its own collapsed-by-default block rather than mixed into the answer.

``sources`` is emitted only once the excerpt budget is known, and lists only the
excerpts that actually reached the prompt — see the comment at its yield site.

``warning`` carries a ``code`` naming exactly one way the answer ended up
ungrounded: ``context_dropped`` (excerpts were retrieved and the budget fit none
of them), ``no_context`` (nothing reached masking at all and the search itself
was not the cause — retrieval genuinely matched nothing, or every chunk failed
closed under masking), or ``retrieval_failed`` (the chunk-plane search itself
raised or had no client, so retrieval degraded to empty rather than reporting an
empty library — issue #438's open half, now closed: see
``search/chunk_retrieval.retrieve_chunks``'s ``diagnostics`` out-param). Adding a
code means teaching ``frontend/src/lib/types/chat.ts``'s ``ChatWarningCode`` about
it too.

Threading model: everything except the SSE plumbing is synchronous (OpenSearch,
SQLAlchemy, ``requests``), so the blocking stages run via Starlette's threadpool
and the provider's token stream is bridged with ``iterate_in_threadpool``.

The generator owns its own DB session rather than borrowing the request's: it
outlives the handler's dependency scope, and a stream that is still writing when
the session closes would fail at exactly the moment we most need to persist the
partial answer.

Module map. This file keeps the SSE generator, ``_prepare_context`` and every call site
that tests patch on this module (see the comment under the imports). The turn's phases
live in its siblings:

* ``turn.py``: ``ChatTurn`` and the ``_TurnCancelled`` cancel signal;
* ``prompt_budget.py``: the reply budget and the prompt built against it;
* ``planning_steps.py``: phase 1, the rewrite, the #403 plan and speaker names;
* ``fanout.py``: the #403 parallel legs, enrichment and recurrence;
* ``masking.py``: SECURITY, whether excerpt text is masked before the LLM;
* ``evidence.py``: the quarantine drop, the digests-first merge, the fail-closed filter
  that drops what masking emptied, and the grounding diagnostics;
* ``summary_tier.py``: which leg feeds the ``<overview>``, and composing it;
* ``streaming.py``: output redaction and bookkeeping on the token stream;
* ``persistence.py``: persist, meter and audit the finished turn.
"""

from __future__ import annotations

import asyncio
import json
import logging
import threading
import time
from collections.abc import AsyncGenerator
from typing import Any

import anyio
from starlette.concurrency import iterate_in_threadpool
from starlette.concurrency import run_in_threadpool

from app.core import constants as C  # noqa: N812
from app.services.chat import citations as citations_mod
from app.services.chat import limits
from app.services.chat import planner
from app.services.chat.evidence import _drop_quarantined_hits
from app.services.chat.evidence import _emit_expansion
from app.services.chat.evidence import _merge_evidence
from app.services.chat.evidence import _record_languages
from app.services.chat.evidence import _record_retrieval_diagnostics
from app.services.chat.fanout import _maybe_run_enrichment
from app.services.chat.fanout import _render_recurrence
from app.services.chat.fanout import _run_plan_fanout
from app.services.chat.language import warning_payload as language_warning_payload
from app.services.chat.masking import _build_digest_mask_kwargs
from app.services.chat.masking import _build_mask_kwargs
from app.services.chat.output_redactor import OutputRedactor
from app.services.chat.persistence import _finalize_turn
from app.services.chat.planning_steps import _initial_stage
from app.services.chat.planning_steps import _maybe_rewrite
from app.services.chat.planning_steps import _resolve_plan
from app.services.chat.planning_steps import _resolve_speaker_focus
from app.services.chat.prompt_budget import _assemble_prompt
from app.services.chat.prompt_budget import _emit_prompt_trace
from app.services.chat.prompting import format_counted_block
from app.services.chat.redactor import MaskedChunk
from app.services.chat.redactor import mask_chunks
from app.services.chat.retrieval import RetrievalResult
from app.services.chat.retrieval import retrieve_context
from app.services.chat.settings import ChatSettings
from app.services.chat.streaming import _flush_redactor
from app.services.chat.streaming import _generation_kwargs
from app.services.chat.streaming import _mark_first_token
from app.services.chat.streaming import _record_output_redaction
from app.services.chat.streaming import _redact_delta
from app.services.chat.streaming import _resolve_output_policy
from app.services.chat.summary_tier import _build_summaries
from app.services.chat.summary_tier import _compose_overview
from app.services.chat.trace import Outcome as TraceOutcome
from app.services.chat.trace import QueryStage
from app.services.chat.trace import TraceRecorder
from app.services.chat.trace import emit as emit_trace
from app.services.chat.trace_stream import DEFAULT_QUEUE_MAXSIZE
from app.services.chat.trace_stream import StreamingTraceRecorder
from app.services.chat.trace_stream import WireEvent
from app.services.chat.trace_stream import drain_available
from app.services.chat.trace_stream import trace_payload
from app.services.chat.turn import ChatTurn
from app.services.chat.turn import _check_cancelled
from app.services.chat.turn import _TurnCancelled

# Tests patch `_drop_quarantined_hits`, `_finalize_turn`, `_resolve_output_policy`,
# `_resolve_speaker_focus`, `mask_chunks`, `retrieve_context` and `_prepare_context` on
# THIS module, so each one's call site lives here and reads it from these globals.
# Moving a call site into a sibling would leave those patches silently inert.

logger = logging.getLogger(__name__)


#: The only failure text a chat turn ever puts on the wire or in the database. The
#: client renders the translated string for the frame's ``code``; these English
#: literals are what an API consumer without i18n sees (issue #1049).
PROVIDER_UNAVAILABLE_MESSAGE = (
    "The AI provider is temporarily unavailable. Please try again in a moment."
)


PROVIDER_ERROR_MESSAGE = "The AI provider could not generate a response. Please try again."


GENERATION_FAILED_MESSAGE = "Generation failed."


def sse(event: str, payload: dict[str, Any]) -> str:
    """Format one SSE frame (same helper shape as the subtitle export stream)."""
    return f"event: {event}\ndata: {json.dumps(payload)}\n\n"


def _apply_speaker_resolution(
    *,
    settings: ChatSettings,
    question: str,
    user_id: int,
    organization_id: int | None,
    session_scope,
    meta: dict[str, Any],
    file_uuids: list[str] | None = None,
    recorder: TraceRecorder | None = None,
) -> list[str]:
    """Resolve a mentioned speaker and fold it into ``meta``.

    A second extraction on top of :func:`_resolve_speaker_focus` — this one
    holds the branching (whether the flag is on, whether anything resolved),
    which is what pushed ``_prepare_context`` over ruff's complexity limit.
    Same reasoning :func:`_resolve_summary_tier` documents for its own split.
    Mutates ``meta`` in place, matching how ``_prepare_context`` already
    threads its own ``meta`` dict through several stages.

    Returns:
        The uniquely matched names when the turn has a resolved speaker
        focus, else ``[]`` — always a list, never ``None``, so the caller
        treats "flag off" and "nothing resolved" identically.
    """
    if not settings.speaker_resolver_enabled:
        emit_trace(
            recorder,
            QueryStage.PARSED_NAMES,
            TraceOutcome.SKIPPED,
            node_id="names",
            reason="disabled",
        )
        return []
    resolution = _resolve_speaker_focus(
        question=question,
        user_id=user_id,
        organization_id=organization_id,
        session_scope=session_scope,
        file_uuids=file_uuids,
    )
    resolution_meta = resolution.as_meta()
    if resolution_meta:
        meta["speaker_resolution"] = resolution_meta
    matched = list(resolution.matched) if resolution.speaker_focus else []
    # ⚠️ COUNTS ONLY. `resolution.matched` and `as_meta()["ambiguous"]` hold REAL
    # SPEAKER NAMES, which are permission-scoped. Never `**resolution_meta`.
    #
    # Three outcomes, not two: a mention that matched several roster entries
    # resolves to NO filter on purpose (never a guess), and reporting that as
    # "found nothing" would hide a deliberate refusal behind an empty result.
    ambiguous = (resolution_meta or {}).get("ambiguous") or []
    if ambiguous:
        outcome, count = TraceOutcome.DECLINED, len(ambiguous)
    elif matched:
        outcome, count = TraceOutcome.OK, len(matched)
    else:
        outcome, count = TraceOutcome.EMPTY, 0
    emit_trace(
        recorder,
        QueryStage.PARSED_NAMES,
        outcome,
        node_id="names",
        source="postgres",
        count=count,
        **({"reason": "ambiguous"} if ambiguous else {}),
    )
    return matched


def _run_serial_pipeline(
    *,
    question: str,
    effective_query: str,
    decision,
    user_id: int,
    organization_id: int | None,
    file_uuids: list[str] | None,
    speakers: list[str] | None,
    speaker_focus_names: list[str],
    settings: ChatSettings,
    search_mode: str,
    session_scope,
    settings_config,
    meta: dict[str, Any],
    recorder: TraceRecorder | None = None,
):
    """Phases 2+3 of `_prepare_context`: the pre-#403 counted-then-retrieve
    pipeline, unchanged — this is the path every turn takes with the planner
    off (or when it fired nothing), and it must stay BYTE IDENTICAL to before
    the fan-out existed. Split out purely to keep `_prepare_context` under
    ruff's complexity limit, the same reasoning `_resolve_plan`/`_resolve_
    summary_tier` document for their own extractions — this is not a new
    behavior, only a relocation.

    Returns:
        `(result, counted)` — a `retrieval.RetrievalResult` and the
        aggregation outcome (`None` unless the router asked for it).
    """
    counted = None
    # --- Phase 2: the counted tier. Postgres and OpenSearch, INTERLEAVED but
    # never overlapping: `answer_aggregation` takes the factory below and opens
    # a short session per statement group, so its `size: 0` search never
    # inherits a transaction.
    #
    # It runs BEFORE retrieval and is independent of it: "how many meetings
    # mention X" is answered by an aggregation over the whole library, and the
    # excerpts that follow are examples beside that number, never the thing it
    # was derived from. ROUTE, DON'T FUSE — two queries, combined here.
    if decision.wants_aggregate:
        from app.services.chat.aggregation_service import answer_aggregation
        from app.services.opensearch_service import get_opensearch_client

        counted_started = time.monotonic()
        emit_trace(
            recorder,
            QueryStage.FANNED_RELATIONAL,
            node_id="counted",
            source="postgres",
        )
        counted = answer_aggregation(
            question,
            decision,
            session_factory=session_scope,
            client=get_opensearch_client(),
            index=settings_config.OPENSEARCH_CHUNKS_INDEX,
            user_id=user_id,
            organization_id=organization_id,
            file_uuids=file_uuids,
        )
        meta.setdefault("timings_ms", {})["aggregate"] = int(
            (time.monotonic() - counted_started) * 1000
        )
        # Recorded either way. "The router said aggregate and the mechanism
        # declined" is a different fact from "it was never an aggregation", and
        # only the metadata can tell them apart after the fact.
        meta["aggregation"] = counted.as_metadata() if counted is not None else {"declined": True}
        # Three outcomes, and collapsing any two loses the point. A shape the
        # mechanism REFUSED (an unbounded scope, a truncated bucket list) is not
        # the same as one that counted zero, and neither is the same as a tier
        # that never ran. Every shape here declines rather than guessing,
        # because a number from the wrong mechanism is indistinguishable from a
        # number from the right one.
        #
        # ⚠️ `coverage["declined"]` is free ENGLISH PROSE ("talk-time stats need
        # a bounded set of recordings"). It must never be forwarded as `reason`,
        # which is a closed vocabulary of machine codes.
        _declined = bool((getattr(counted, "coverage", None) or {}).get("declined"))
        _count = int(getattr(counted, "count", 0) or 0) if counted is not None else 0
        if counted is None or _declined:
            _counted_outcome = TraceOutcome.DECLINED
        elif _count:
            _counted_outcome = TraceOutcome.OK
        else:
            _counted_outcome = TraceOutcome.EMPTY
        emit_trace(
            recorder,
            QueryStage.FOUND,
            _counted_outcome,
            node_id="counted",
            source="postgres",
            count=_count,
            ms=meta["timings_ms"]["aggregate"],
            **(
                {"reason": "unsupported_shape"} if _counted_outcome is TraceOutcome.DECLINED else {}
            ),
        )

    # A counted turn still retrieves — a reduced leg, so a misroute always has
    # evidence and every `[n]` marker still resolves to a clickable timestamp.
    # The plan's ratio: a third of the usual excerpts, because the number above
    # them is the answer and the excerpts are illustration.
    retrieval_settings = settings
    if counted is not None:
        from dataclasses import replace as _replace

        retrieval_settings = _replace(settings, final_chunks=max(1, settings.final_chunks // 3))
        meta["chunk_leg_reduced_to"] = retrieval_settings.final_chunks

    # --- Phase 3: retrieval. OpenSearch, the cross-encoder and Redis; the
    # slowest stage of the turn, and NO session. `file_uuids` is passed through
    # exactly as it arrived: `None` means "all accessible", `[]` means "match
    # nothing", and substituting one for the other leaks the whole library.
    result = retrieve_context(
        query=effective_query,
        user_id=user_id,
        organization_id=organization_id,
        file_uuids=file_uuids,
        speakers=speakers,
        settings=retrieval_settings,
        search_mode=search_mode,
        wants_digest=decision.wants_digest,
        speaker_focus_names=speaker_focus_names or None,
        recorder=recorder,
    )
    return result, counted


def _retrieve_or_fanout(
    *,
    plan: planner.Plan | None,
    decision,
    question: str,
    effective_query: str,
    user_id: int,
    organization_id: int | None,
    file_uuids: list[str] | None,
    speakers: list[str] | None,
    speaker_focus_names: list[str],
    settings: ChatSettings,
    search_mode: str,
    session_scope,
    settings_config,
    assistant_message_uuid: str,
    meta: dict[str, Any],
    recorder: TraceRecorder | None = None,
):
    """Phases 2+3 of `_prepare_context`: dispatch to the fan-out or the serial pipeline.

    #403: a validated, non-empty plan replaces the serial
    counted-then-retrieve pipeline with a parallel leg fan-out. Gated so the
    flag-off (or no-plan) path stays BYTE IDENTICAL to before this feature
    existed — `_run_serial_pipeline` is a pure relocation of what
    `_prepare_context` always did here. Split out for the same
    complexity-limit reason as `_resolve_plan`/`_run_serial_pipeline`.

    Returns:
        `(result, counted, recurrence_result)` — `recurrence_result` is
        always `None` on the serial path, since that tier is not wired
        there.
    """
    fanout_active = bool(plan is not None and not plan.failed and not plan.is_empty)
    if not fanout_active:
        # GH #514 seam: "the planner never ran/added nothing" must render
        # differently from "it ran and dispatched legs" — SKIPPED with a
        # machine reason, never silence.
        emit_trace(
            recorder,
            QueryStage.PLANNED,
            TraceOutcome.SKIPPED,
            node_id="plan",
            reason="rules",
        )
        result, counted = _run_serial_pipeline(
            question=question,
            effective_query=effective_query,
            decision=decision,
            user_id=user_id,
            organization_id=organization_id,
            file_uuids=file_uuids,
            speakers=speakers,
            speaker_focus_names=speaker_focus_names,
            settings=settings,
            search_mode=search_mode,
            session_scope=session_scope,
            settings_config=settings_config,
            meta=meta,
            recorder=recorder,
        )
        return result, counted, None

    assert plan is not None  # narrows for mypy; `fanout_active` already proves it
    _check_cancelled(assistant_message_uuid)
    fanout_started = time.monotonic()
    result, counted, recurrence_result = _run_plan_fanout(
        plan=plan,
        decision=decision,
        effective_query=effective_query,
        question=question,
        user_id=user_id,
        organization_id=organization_id,
        file_uuids=file_uuids,
        speakers=speakers,
        settings=settings,
        search_mode=search_mode,
        session_scope=session_scope,
        assistant_message_uuid=assistant_message_uuid,
        meta=meta,
        recorder=recorder,
    )
    meta.setdefault("timings_ms", {})["fanout"] = int((time.monotonic() - fanout_started) * 1000)
    return result, counted, recurrence_result


def _drop_quarantined(
    result: RetrievalResult,
    *,
    session_scope,
    meta: dict[str, Any],
    recorder: TraceRecorder | None,
) -> None:
    """Phase 3.5 of ``_prepare_context``: drop quarantined ranked hits, in place."""
    # --- Phase 3.5: drop quarantined hits before anything downstream sees
    # them (its own short session). See `_drop_quarantined_hits` for what this
    # does and does NOT cover — retrieval itself has no quarantine predicate,
    # so this is the enforcement point for the RANKED tiers of an unscoped turn.
    chunks_before = len(result.chunks)
    with session_scope() as db:
        result.chunks = _drop_quarantined_hits(db, result.chunks)
        result.digests = _drop_quarantined_hits(db, result.digests)
    # `result.retrieved` feeds the `no_context` warning's contract (documented
    # at the yield site below and in `services/chat/CLAUDE.md`): non-zero means
    # masking failed closed on every chunk. A quarantined chunk never reaches
    # masking at all, so leaving it counted in `retrieved` made a quarantine
    # drop impersonate a masking failure — `{"code": "no_context", "retrieved":
    # 3}` beside `chunks_dropped_empty_after_masking == 0`, two diagnostics that
    # contradicted each other and pointed at the wrong subsystem. Decrementing
    # keeps `retrieved` meaning "chunks that could have reached masking", and
    # the drop still has its own named counter so a quarantine cause is never
    # read as a bare "search returned less".
    chunks_dropped_quarantined = chunks_before - len(result.chunks)
    emit_trace(
        recorder,
        QueryStage.FILTERED,
        node_id="quarantine",
        source="postgres",
        kept=len(result.chunks),
        dropped=chunks_dropped_quarantined,
    )
    if chunks_dropped_quarantined:
        result.retrieved = max(0, result.retrieved - chunks_dropped_quarantined)
        meta["chunks_dropped_quarantined"] = chunks_dropped_quarantined


def _mask_evidence(
    result: RetrievalResult,
    *,
    llm,
    settings: ChatSettings,
    session_scope,
    user_id: int,
    organization_id: int | None,
    recorder: TraceRecorder | None,
) -> tuple[list[MaskedChunk], list[MaskedChunk], dict[str, bool]]:
    """Phase 4 of ``_prepare_context``: mask the chunk plane and the ranked digests.

    Returns:
        ``(masked, digest_masked, digest_mask_kwargs)``. The digest kwargs are handed
        on to the summary tier, whose own ``mask_digests`` calls must share them.
    """
    # --- Phase 4: the database phase. Every session here is SHORT and every
    # value that leaves it is a plain dataclass — an ORM instance would re-open a
    # session on the first attribute read afterwards and quietly undo the split.
    #
    # The maskers take the FACTORY, not a session (#83): each one gathers its
    # cached spans, closes, and only then runs the detector. A file whose scan
    # cannot be trusted falls through to inline Presidio, and a cold analyzer
    # build is ~10 s — measured at 13.9 s `idle in transaction` when it ran
    # inside this phase's session.
    #
    # `unmask_for_local` keys egress masking off WHERE THE MODEL RUNS (owner
    # decision, 2026-08-13): a local model never has this text leave the
    # machine, so masking it costs recall for no egress benefit. Resolved ONCE
    # here (pure — reads only `llm.config`, no I/O) and threaded to every
    # masker call below rather than re-derived per call, so the three sites
    # cannot disagree about which provider this turn is actually using.
    #
    # `llm` is None for a legitimate, first-class deployment shape (#403 D6):
    # no LLM_PROVIDER configured at all still has to produce masked context for
    # the deterministic maps/keyphrase/coverage tiers, and that path never had
    # an `llm` to read a provider from. `getattr(llm, "config", None)` reaches
    # `is_local_provider`'s own None-safe duck typing (`getattr(config,
    # "provider", "")` on a None config is `""`, which resolves to "not a known
    # local provider") instead of a SECOND None check here — one fail-closed
    # decision, not two that could disagree.
    #
    # `_mask_kwargs` only includes the keyword when it is True. Every existing
    # caller of `mask_chunks`/`mask_digests` in this codebase — and every test
    # that stubs either with a positional-only signature — predates this
    # parameter; passing it unconditionally would break every one of them for
    # the overwhelmingly common case (remote provider or no provider at all)
    # where it does nothing anyway. Only a genuinely local provider needs the
    # keyword sent at all.
    _mask_kwargs = _build_mask_kwargs(llm, settings)
    # ⚠️ NOT the same dict. The digest plane must never receive
    # `expand_short_chunks` — see `_build_digest_mask_kwargs`.
    _digest_mask_kwargs = _build_digest_mask_kwargs(llm)

    digest_masked: list[MaskedChunk] = []
    masked = mask_chunks(
        session_scope, result.chunks, user_id, organization_id=organization_id, **_mask_kwargs
    )

    _emit_expansion(recorder, masked, enabled="expand_short_chunks" in _mask_kwargs)

    if result.digests:
        # A SEPARATE masking call, not an overload. A digest is
        # non-contiguous selected sentences; `mask_chunks` would rebuild it
        # from every segment overlapping its time range and hand back the
        # whole span verbatim — more text than the digest holds, from a
        # function whose name says it masked it. `mask_digests` goes through
        # the per-sentence provenance.
        from app.services.chat.redactor import mask_digests

        digest_masked = mask_digests(
            session_scope,
            result.digests,
            user_id,
            organization_id=organization_id,
            **_digest_mask_kwargs,
        )
    return masked, digest_masked, _digest_mask_kwargs


def _prepare_context(
    *,
    user_id: int,
    organization_id: int | None,
    question: str,
    history: list[dict[str, str]],
    settings: ChatSettings,
    file_uuids: list[str] | None,
    speakers: list[str] | None,
    search_mode: str,
    llm,
    rewrite_enabled: bool,
    assistant_message_uuid: str = "",
    recorder: TraceRecorder | None = None,
) -> tuple[list[MaskedChunk], dict[str, Any], Any, Any, str, str]:
    """Run the blocking RAG stages: rewrite → route → count → retrieve → mask.

    ``assistant_message_uuid`` is used ONLY for cancellation checks
    (``_check_cancelled`` at each phase boundary and leg submission) — it is
    never used for metering or persistence, both of which happen later in
    ``stream_reply`` against the caller's own copy of the id. It defaults to
    ``""`` so tests exercising this function directly (masking, routing, the
    speaker map, ...) don't have to thread a cancellation id through every
    call. ``""`` is a safe default: ``limits.is_cancelled("")`` can only ever
    read a Redis key no real turn will ever cancel, so an omitted id fails
    open exactly like a Redis outage does elsewhere in this module. The one
    production call site (``stream_reply``'s ``_prep()`` closure) always
    passes a real id explicitly — the default exists for tests, not for it.

    Executed in a worker thread, and **phased**. It opens its own database
    sessions rather than accepting one, because the caller's session would then
    be held across the rewrite (an LLM round trip), the counted tier's
    OpenSearch aggregation and retrieval (OpenSearch + cross-encoder + Redis) —
    the shape that queues every ``ALTER TABLE`` behind a chat turn and hangs an
    Alembic upgrade mid-release. ``scripts/audit-session-lifetime.py`` gates it.

    The phases, in order:

    1. rewrite + route — **no session**;
    2. the counted tier — ``answer_aggregation`` takes a *factory* and opens a
       short session per Postgres statement group, so its search runs clean;
    3. retrieval — **no session**;
    4. masking and the scope map — the database phase. ``mask_chunks`` /
       ``mask_digests`` take the *factory* too (#83) and gather-then-close, so
       the inline Presidio fallback never runs inside a transaction; the scope
       map and the summaries get their own short sessions. Everything returned
       is a plain dataclass (``MaskedChunk``/``FileSummary``, never an ORM
       instance: attribute access on a detached row would silently re-open a
       session and undo the split);
    5. overview composition and diagnostics — pure, except one short session for
       the language read.

    Returns the masked chunks, diagnostics for the message metadata (ids/counts/
    timings only, never text), the counted result when the router sent the turn
    to the aggregation tier, the overview when it asked for a summary, and a
    rendered ``<synthesis>`` block and a rendered ``<recurrence>`` block
    (#403; both ``""`` unless their respective fan-out leg fired).
    """
    from app.core.config import settings as settings_config
    from app.db.session_utils import session_scope

    meta: dict[str, Any] = {}
    plan: planner.Plan | None = None
    llm_calls = 0

    # --- Phase 1: rewrite + route. An LLM round trip; NO session. -------------
    _check_cancelled(assistant_message_uuid)
    effective_query, llm_intent, rewrite, rewrite_calls = _maybe_rewrite(
        llm=llm,
        history=history,
        question=question,
        rewrite_enabled=rewrite_enabled,
        settings=settings,
        meta=meta,
        recorder=recorder,
    )
    llm_calls += rewrite_calls

    # --- Phase 1.5: resolve a speaker MENTIONED in the question text. Gated on
    # `chat.speaker_resolver_enabled`, off by default — a flag-off turn
    # takes none of this branch and stays byte-identical to before it
    # existed: no `meta["speaker_resolution"]` key, `speaker_focus`
    # stays False, `speaker_focus_names` stays empty. Its own short session
    # (see `_resolve_speaker_focus`).
    speaker_focus_names = _apply_speaker_resolution(
        settings=settings,
        question=question,
        user_id=user_id,
        organization_id=organization_id,
        session_scope=session_scope,
        meta=meta,
        file_uuids=file_uuids,
        recorder=recorder,
    )

    from app.services.chat.router import route

    decision = route(
        question,
        rewritten=effective_query if effective_query != question else None,
        llm_intent=llm_intent,
        speakers=speakers,
        speaker_focus=bool(speaker_focus_names),
        recurrence_enabled=settings.recurrence_enabled,
    )
    meta["route"] = decision.as_metadata()

    # --- Phase 1.6: the LLM query planner (#403). See `_resolve_plan`'s
    # own docstring — split out purely to keep this function's branching
    # count under ruff's complexity limit, same reasoning `_resolve_summary_
    # tier`/`_apply_speaker_resolution` already document for their own splits.
    plan, plan_calls = _resolve_plan(
        settings=settings,
        llm=llm,
        history=history,
        rewrite=rewrite,
        question=question,
        decision=decision,
        assistant_message_uuid=assistant_message_uuid,
        meta=meta,
    )
    llm_calls += plan_calls

    result, counted, recurrence_result = _retrieve_or_fanout(
        plan=plan,
        decision=decision,
        question=question,
        effective_query=effective_query,
        user_id=user_id,
        organization_id=organization_id,
        file_uuids=file_uuids,
        speakers=speakers,
        speaker_focus_names=speaker_focus_names,
        settings=settings,
        search_mode=search_mode,
        session_scope=session_scope,
        settings_config=settings_config,
        assistant_message_uuid=assistant_message_uuid,
        meta=meta,
        recorder=recorder,
    )

    _drop_quarantined(result, session_scope=session_scope, meta=meta, recorder=recorder)

    masked, digest_masked, _digest_mask_kwargs = _mask_evidence(
        result,
        llm=llm,
        settings=settings,
        session_scope=session_scope,
        user_id=user_id,
        organization_id=organization_id,
        recorder=recorder,
    )
    summaries, map_leg, map_coverage, speaker_focus_for_summary = _build_summaries(
        decision=decision,
        file_uuids=file_uuids,
        settings=settings,
        session_scope=session_scope,
        result=result,
        digest_masked=digest_masked,
        user_id=user_id,
        organization_id=organization_id,
        digest_mask_kwargs=_digest_mask_kwargs,
        meta=meta,
    )
    overview = _compose_overview(
        question,
        settings=settings,
        summaries=summaries,
        map_leg=map_leg,
        map_coverage=map_coverage,
        speaker_focus_for_summary=speaker_focus_for_summary,
        file_uuids=file_uuids,
        digest_masked=digest_masked,
        masked=masked,
        meta=meta,
    )
    masked = _merge_evidence(masked, digest_masked, result, meta=meta, recorder=recorder)
    _record_languages(session_scope, file_uuids, masked, meta)
    _record_retrieval_diagnostics(result, file_uuids, speakers, meta)

    synthesis_block, enrichment_calls = _maybe_run_enrichment(
        settings=settings,
        llm=llm,
        question=question,
        masked=masked,
        recurrence_result=recurrence_result,
        assistant_message_uuid=assistant_message_uuid,
        meta=meta,
        recorder=recorder,
    )
    llm_calls += enrichment_calls

    recurrence_block = _render_recurrence(recurrence_result, meta)

    if llm_calls:
        meta["llm_calls"] = meta.get("llm_calls", 0) + llm_calls

    return masked, meta, counted, overview, synthesis_block, recurrence_block


# SSE comment lines are ignored by every client but keep the connection warm.
# Without them, retrieval (OpenSearch + cross-encoder + masking) and then the
# wait for a first token can put ZERO bytes on the wire for well over nginx's
# default 60s proxy_read_timeout, and the proxy closes the stream mid-answer.
_KEEPALIVE = ": keepalive\n\n"


_KEEPALIVE_INTERVAL_S = 15


class _Awaited:
    """Carries a result out of :func:`_keepalive_until_done`.

    An async generator cannot ``return`` a value, and the caller needs both the
    frames *and* what the awaited work produced.
    """

    __slots__ = ("value",)

    def __init__(self) -> None:
        self.value: Any = None


async def _keepalive_until_done(awaitable, holder: _Awaited, trace_q=None):
    """Await ``awaitable``, YIELDING frames while it runs.

    ⚠️ **Yielding is the entire point, and buffering is the bug this replaces.**
    The previous shape pushed keepalives into a queue that the caller drained
    *after* awaiting — but ``stream_reply`` is an async generator, and an async
    generator cannot yield from inside an ``await``. So every keepalive produced
    during retrieval reached the client only once retrieval had finished, in one
    burst, at exactly the moment it was no longer needed. Measured on the old
    code: 11 keepalives, all bearing the same timestamp as the end of the phase
    they were supposed to span.

    The result lands on ``holder`` because an async generator cannot return one.

    The awaited task is deliberately **not cancelled** when this generator is torn
    down (a client disconnect or Stop). That matches the pre-existing contract:
    the work runs in a threadpool and stops at its own next cancellation check,
    not because the socket went away.

    GH #514 rides the same loop. Trace events are produced on the SAME worker
    thread this is awaiting (and on ``legs.py``'s fan-out threads), so this is
    the only place that can put them on the wire while the phase is still
    running — which is the entire point of a *live* trace rather than a summary
    delivered once the work is already over.

    ⚠️ **It waits on EVENTS, never on a poll interval, and that is a measured
    decision.** A 50 ms poll cost +206 ms at p95 (592 -> 799 ms) because the loop
    only noticed ``_prep`` finishing on its next tick, and every tick paid a
    wake-up whether or not anything had happened. Racing the work against the
    queue instead wakes exactly when there is something to do: re-measured, p95
    went to within noise of the untraced arm. Do not reintroduce a poll here.

    Args:
        awaitable: The work to await — typically a ``run_in_threadpool`` call.
        holder: Receives the awaited result on completion.
        trace_q: GH #514's event queue, or ``None`` when the flag is off. ``None``
            keeps the original keepalive-only shape exactly.

    Yields:
        ``_KEEPALIVE`` SSE comment frames, plus ``trace`` frames when tracing.
    """
    task = asyncio.ensure_future(awaitable)
    getter: asyncio.Task | None = None
    last_beat = time.monotonic()
    try:
        while True:
            # One pending read of the queue, recreated only after it resolves —
            # a fresh `get()` per iteration would leak a waiter each time round.
            if trace_q is not None and getter is None:
                getter = asyncio.ensure_future(trace_q.get())

            waiting = {task} | ({getter} if getter is not None else set())
            elapsed = time.monotonic() - last_beat
            done, _ = await asyncio.wait(
                waiting,
                timeout=max(0.0, _KEEPALIVE_INTERVAL_S - elapsed),
                return_when=asyncio.FIRST_COMPLETED,
            )

            if getter is not None and getter in done:
                yield sse("trace", trace_payload(getter.result()))
                getter = None
                # Deliver this event before re-checking `task`: the work
                # finishing must not swallow an event already in hand.
                continue

            if task in done:
                holder.value = task.result()
                return

            if time.monotonic() - last_beat >= _KEEPALIVE_INTERVAL_S:
                last_beat = time.monotonic()
                yield _KEEPALIVE
    finally:
        # The pending read holds a reference to the queue and would otherwise
        # be reported as a task destroyed while pending on teardown.
        if getter is not None and not getter.done():
            getter.cancel()


def _open_trace(
    settings: ChatSettings, scope_files_dropped: int
) -> tuple[asyncio.Queue[WireEvent] | None, TraceRecorder | None]:
    """GH #514: the turn's trace queue and recorder, both ``None`` when tracing is off."""
    # GH #514. `trace_q is None` is the flag-off path and stays a hard
    # branch, not a fast path through shared code, so "free when off" is
    # structural rather than a hope.
    trace_q: asyncio.Queue[WireEvent] | None = None
    recorder: TraceRecorder | None = None
    if settings.trace_enabled:
        trace_q = asyncio.Queue(maxsize=DEFAULT_QUEUE_MAXSIZE)
        recorder = StreamingTraceRecorder(asyncio.get_running_loop(), trace_q)
        emit_trace(recorder, QueryStage.SUBMITTED, node_id="submitted")
        emit_trace(
            recorder,
            QueryStage.VALIDATED,
            node_id="validated",
            dropped=scope_files_dropped or 0,
        )
    return trace_q, recorder


def _context_warning_frames(meta: dict[str, Any]) -> list[str]:
    """The ``warning`` frames context preparation can raise: a speaker or a plan."""
    frames: list[str] = []
    # A speaker mention that matched more than one
    # roster entry resolves to NO filter (never a guess) — surface
    # the candidates so the user can disambiguate, exactly like
    # `context_dropped`/language warn elsewhere in this turn.
    ambiguous_speakers = (meta.get("speaker_resolution") or {}).get("ambiguous") or []
    if ambiguous_speakers:
        frames.append(
            sse(
                "warning",
                {"code": "ambiguous_speaker", "candidates": list(ambiguous_speakers)},
            )
        )

    # #403: a plan that failed to parse/execute falls back to
    # the rules-only route rather than failing the turn — surfaced
    # so a user (or `ChatMessageMeta`) can tell "no plan was
    # attempted" from "one was attempted and dropped".
    if (meta.get("plan") or {}).get("failed"):
        frames.append(sse("warning", {"code": "plan_failed"}))
    return frames


def _grounding_frames(
    turn: ChatTurn,
    *,
    settings: ChatSettings,
    masked: list[MaskedChunk],
    excerpt_ids: list[int],
    overview,
    overview_block: str,
    prompt_diagnostics: dict[str, Any],
    llm,
    answer_tokens: int,
    assistant_message_uuid: str,
) -> list[str]:
    """The ``sources`` frame, then any warning that the answer is weakly grounded."""
    frames: list[str] = []
    turn.offered_citations = citations_mod.build_offered_citations(masked, excerpt_ids)
    # #532 arm (a): the overview's own citation ids join the offer —
    # but only when the block actually SURVIVED into the prompt.
    # Offering ids for a budget-dropped block is exactly the
    # sourced-but-not-grounded failure issue #384 closed.
    if (
        settings.overview_citable
        and overview is not None
        and overview.citation_payloads
        and overview_block
        and "overview" not in (prompt_diagnostics.get("evidence_blocks_dropped") or ())
    ):
        turn.offered_citations = turn.offered_citations + list(overview.citation_payloads)
    frames.append(sse("sources", {"citations": turn.offered_citations}))

    if not masked:
        # Nothing reached the prompt at all. The model will answer "I
        # don't have enough information", which is indistinguishable
        # from a grounded negative — and, worse, from a working search
        # that simply found nothing. Retrieval degrades to an empty
        # list on ANY failure (issue #438: an OpenSearch 503 during a
        # reindex produced a confident "I don't know" over a corpus
        # full of matching material), so the counters are the only
        # evidence the user can be shown. `retrieved` separates the
        # remaining two: 0 means the search returned nothing,
        # non-zero means masking failed closed on every chunk.
        #
        # `retrieval_failed` is a THIRD, more specific fact
        # `_prepare_context` now threads out of `retrieve_chunks`'s
        # `diagnostics` param: the search backend itself raised or was
        # unreachable, rather than genuinely returning zero hits. That
        # is worth its own code — "your library has nothing about
        # this" and "search was down" call for different next steps —
        # so the two are mutually exclusive branches here, exactly
        # like `context_dropped`/`no_context` below.
        retrieval_failed = bool(turn.metadata.get("retrieval_failed"))
        code = "retrieval_failed" if retrieval_failed else "no_context"
        turn.metadata[code] = True
        logger.warning(
            "Chat turn %s answered with NO excerpts (%s): retrieved=%s, "
            "files_searched=%s — the reply is ungrounded",
            assistant_message_uuid,
            code,
            turn.metadata.get("retrieved", 0),
            turn.metadata.get("files_searched", 0),
        )
        frames.append(
            sse(
                "warning",
                {
                    "code": code,
                    "retrieved": turn.metadata.get("retrieved", 0),
                    "files_searched": turn.metadata.get("files_searched", 0),
                },
            )
        )
    elif not excerpt_ids:
        # Retrieval found material and the budget left no room for any
        # of it. Say so rather than answering ungrounded behind a
        # normal-looking reply.
        turn.metadata["context_dropped"] = True
        logger.warning(
            "Chat turn %s dropped all %d retrieved excerpts: no room in "
            "the %d-token context window after %d reserved answer tokens",
            assistant_message_uuid,
            len(masked),
            llm.user_context_window,
            answer_tokens,
        )
        frames.append(
            sse(
                "warning",
                {"code": "context_dropped", "retrieved": len(masked)},
            )
        )

    # Answering anyway is deliberate: a library is usually mixed, and
    # refusing every question because one recording is Spanish would
    # be worse than useless. The turn says what it could not serve
    # well and answers from the rest.
    language_warning = language_warning_payload(turn.metadata)
    if language_warning is not None:
        logger.info(
            "Chat turn %s spans unsupported RAG languages %s (%d files, %d of unknown language)",
            assistant_message_uuid,
            language_warning["languages"],
            language_warning["files"],
            language_warning["unknown_files"],
        )
        frames.append(sse("warning", language_warning))
    return frames


async def _text_frame(
    event, turn: ChatTurn, answer_redactor: OutputRedactor, reasoning_redactor: OutputRedactor
) -> str:
    """The ``delta``/``reasoning`` frame for one text event; ``""`` while it is held back."""
    if event.type == "delta":
        safe = await _redact_delta(answer_redactor, event.text)
        if safe:
            turn.answer_parts.append(safe)
            return sse("delta", {"text": safe})
        return ""
    # Reasoning is rendered too (a collapsed block, not a hidden
    # one), so it is a display surface and gets its own buffer.
    safe = await _redact_delta(reasoning_redactor, event.text)
    if safe:
        turn.reasoning_parts.append(safe)
        return sse("reasoning", {"text": safe})
    return ""


def _provider_error_frame(turn: ChatTurn, event, conversation_uuid: str) -> str:
    """Record a provider ``error`` event on the turn and return its ``error`` frame."""
    # Provider prose (quota text, request IDs, SDK retry counts) goes
    # to the log only; the frame, the persisted row and the API all
    # carry a fixed sentence plus a code the client translates
    # (issue #1049, same hygiene as #959).
    logger.warning(
        "LLM provider error for conversation %s (transient=%s): %s",
        conversation_uuid,
        event.transient,
        event.message,
    )
    if event.transient:
        turn.error = PROVIDER_UNAVAILABLE_MESSAGE
        turn.error_code = "provider_unavailable"
        turn.metadata["error_code"] = turn.error_code
        return sse("error", {"code": "provider_unavailable", "message": turn.error})
    turn.error = PROVIDER_ERROR_MESSAGE
    turn.error_code = "provider_error"
    turn.metadata["error_code"] = turn.error_code
    return sse("error", {"code": "provider_error", "message": turn.error})


async def _answer_frames(
    llm,
    messages: list[dict[str, str]],
    kwargs: dict[str, Any],
    *,
    turn: ChatTurn,
    cancel_event: threading.Event,
    answer_redactor: OutputRedactor,
    reasoning_redactor: OutputRedactor,
    started: float,
    assistant_message_uuid: str,
    conversation_uuid: str,
) -> AsyncGenerator[str, None]:
    """Stream the provider's answer as frames, then flush what redaction still holds."""
    first_token_deadline = time.monotonic() + C.DEFAULT_CHAT_FIRST_TOKEN_TIMEOUT_S
    got_first_token = False

    stream = llm.chat_completion_stream(messages, cancel_event=cancel_event, **kwargs)
    last_beat = time.monotonic()
    async for event in iterate_in_threadpool(stream):
        # Long gaps between tokens (or before the first) must not look
        # like a dead connection to an intermediary.
        if time.monotonic() - last_beat >= _KEEPALIVE_INTERVAL_S:
            last_beat = time.monotonic()
            yield _KEEPALIVE
        # A user Stop may arrive over a separate connection.
        if not cancel_event.is_set() and limits.is_cancelled(assistant_message_uuid):
            cancel_event.set()

        if event.type in ("delta", "reasoning"):
            # The watchdog below measures the PROVIDER's first token, so
            # it is satisfied here — before the redactor may hold this
            # delta back. Keying it off emission instead would read a
            # buffered first sentence as a stalled model and kill it.
            # The model IS actively responding once reasoning starts, so a
            # reasoning event satisfies it exactly like an answer delta —
            # otherwise a reasoning-heavy response looks "stalled" and gets
            # killed by the timeout below while tokens are visibly arriving.
            got_first_token = _mark_first_token(turn, got_first_token, started)
            frame = await _text_frame(event, turn, answer_redactor, reasoning_redactor)
            if frame:
                yield frame
        elif event.type == "usage":
            turn.prompt_tokens = event.prompt_tokens
            turn.completion_tokens = event.completion_tokens
            turn.cache_read_tokens = event.cache_read_tokens
            turn.cache_write_tokens = event.cache_write_tokens
        elif event.type == "error":
            yield _provider_error_frame(turn, event, conversation_uuid)
        elif event.type == "done":
            turn.finish_reason = event.finish_reason

        if not got_first_token and time.monotonic() > first_token_deadline:
            cancel_event.set()
            turn.error = "The model did not start responding in time."
            turn.error_code = "timeout"
            turn.metadata["error_code"] = turn.error_code
            yield sse("error", {"code": "timeout", "message": turn.error})
            break

    # The last sentence usually has no trailing whitespace to prove it
    # ended, so it is still in the buffer. Flushing here (rather than
    # only in the shielded teardown) is what puts it on the wire; the
    # teardown flush is idempotent and only covers a torn-down turn.
    tail = await _flush_redactor(answer_redactor)
    if tail:
        turn.answer_parts.append(tail)
        yield sse("delta", {"text": tail})
    reasoning_tail = await _flush_redactor(reasoning_redactor)
    if reasoning_tail:
        turn.reasoning_parts.append(reasoning_tail)
        yield sse("reasoning", {"text": reasoning_tail})


async def _teardown_turn(
    turn: ChatTurn,
    *,
    answer_redactor: OutputRedactor | None,
    reasoning_redactor: OutputRedactor | None,
    on_teardown,
    llm,
    messages: list[dict[str, str]],
    masked_count: int,
    conversation_id: int,
    conversation_uuid: str,
    assistant_message_uuid: str,
    user_id: int,
    organization_id: int | None,
    is_first_exchange: bool,
    question: str,
    started: float,
    use_context: bool,
) -> None:
    """Flush, record, persist and release one turn. Awaited inside a shielded scope."""
    # A turn torn down mid-sentence still holds a buffered tail. It
    # cannot be yielded (there is no client left, and this scope
    # contains no `yield` by design) but it IS part of the answer,
    # so it is masked and persisted — the user sees it on reload.
    # `drain()` is idempotent, so a normal exit does not duplicate it.
    try:
        tail = await _flush_redactor(answer_redactor)
        if tail:
            turn.answer_parts.append(tail)
        reasoning_tail = await _flush_redactor(reasoning_redactor)
        if reasoning_tail:
            turn.reasoning_parts.append(reasoning_tail)
    except Exception:  # noqa: BLE001 — never mask the real outcome
        logger.exception("Chat output redaction teardown flush failed")
    _record_output_redaction(turn, answer_redactor, reasoning_redactor)

    try:
        await _finalize_turn(
            turn=turn,
            llm=llm,
            messages=messages,
            masked_count=masked_count,
            conversation_id=conversation_id,
            conversation_uuid=conversation_uuid,
            assistant_message_uuid=assistant_message_uuid,
            user_id=user_id,
            organization_id=organization_id,
            is_first_exchange=is_first_exchange,
            question=question,
            started=started,
            use_context=use_context,
        )
    finally:
        # Its OWN finally: finalisation can raise (the conversation may
        # have been deleted underneath a disconnected stream), and a
        # release placed after it would then never run — leaking the
        # slot exactly in the case this hook exists to cover.
        if on_teardown is not None:
            try:
                on_teardown()
            except Exception:  # noqa: BLE001 — never mask the real outcome
                logger.exception("Chat stream teardown hook failed")


def _closing_frames(turn: ChatTurn) -> list[str]:
    """``usage`` (only when the turn did not fail) and ``done``."""
    frames: list[str] = []
    # Only reachable on a normal (non-cancelled) exit — there is no client
    # left to receive these frames otherwise.
    if not turn.error:
        frames.append(
            sse(
                "usage",
                {
                    "prompt_tokens": turn.prompt_tokens or 0,
                    "completion_tokens": turn.completion_tokens or 0,
                    "total_tokens": turn.total_tokens,
                    "estimated": turn.tokens_estimated,
                },
            )
        )

    done_payload: dict[str, Any] = {"finish_reason": turn.finish_reason or "stop"}
    if turn.title:
        done_payload["title"] = turn.title
    frames.append(sse("done", done_payload))
    return frames


class ChatService:
    """Streams one assistant reply and persists both sides of the exchange."""

    @staticmethod
    async def stream_reply(
        *,
        conversation_id: int,
        conversation_uuid: str,
        user_id: int,
        organization_id: int | None,
        question: str,
        history: list[dict[str, str]],
        file_uuids: list[str] | None,
        speakers: list[str] | None,
        scope_files_dropped: int = 0,
        settings: ChatSettings,
        use_context: bool,
        system_prompt: str,
        search_mode: str,
        temperature: float | None,
        max_tokens: int | None,
        top_p: float | None,
        enable_thinking: bool | None = None,
        llm,
        on_teardown=None,
        assistant_message_uuid: str,
        user_message_uuid: str,
        is_first_exchange: bool,
    ) -> AsyncGenerator[str, None]:
        """Yield SSE frames for one turn.

        Args:
            conversation_id: Owning conversation's primary key.
            conversation_uuid: Public id echoed in the ``start`` frame.
            user_id: Message author (also the redaction-policy subject).
            organization_id: Tenant stamp for metering and audit.
            question: The user's message (already persisted by the endpoint).
            history: Prior turns, oldest first.
            file_uuids: Resolved scope; None = all accessible transcripts.
            speakers: Restrict retrieval to these speakers' turns.
            scope_files_dropped: How many of the caller's EXPLICIT file picks
                (never collections/tags) `resolve_scope_file_uuids` could not
                resolve into `file_uuids` — inaccessible, deleted, or
                quarantined. Stamped onto `msg_metadata` so a picker offering
                files the scope then silently discards (most visible for an
                admin, whose picker is tenant-wide while scope resolution has
                no admin bypass) is visible in the reply rather than only in
                a smaller `files_searched`.
            settings: Admin-tuned RAG knobs.
            use_context: False skips retrieval entirely (pure-LLM chat).
            system_prompt: Fully layered system prompt.
            search_mode: Retrieval mode for this turn.
            temperature: Per-conversation override, or None for the config default.
            max_tokens: Per-conversation answer-length override, clamped below.
            top_p: Per-conversation nucleus-sampling override, omitted when None.
            enable_thinking: ``False`` sends the measured "reasoning off" arm;
                ``None`` (the default) builds the payload exactly as it is built
                today, keeping vLLM's issue-#439 activation intact. Already
                gated on the model's measured capability by the endpoint --
                never derive it from a raw user preference here.
            on_teardown: Called exactly once inside the shielded finally, however
                the turn ends. Used to release the concurrency slot: a wrapping
                generator's own ``finally`` does NOT reliably run when Starlette
                tears this one down on client disconnect, so a slot released
                there leaks on every Stop.
            llm: The caller's ``LLMService``.
            assistant_message_uuid: Pre-allocated id for the reply.
            user_message_uuid: Id of the already-persisted user message.
            is_first_exchange: Whether to derive a title from this question.

        Yields:
            SSE frame strings.
        """
        turn = ChatTurn()
        if scope_files_dropped:
            turn.metadata["scope_files_dropped"] = scope_files_dropped
        cancel_event = threading.Event()
        started = time.monotonic()
        trace_q, recorder = _open_trace(settings, scope_files_dropped)

        yield sse(
            "start",
            {
                "conversation_uuid": conversation_uuid,
                "user_message_uuid": user_message_uuid,
                "assistant_message_uuid": assistant_message_uuid,
            },
        )

        masked: list[MaskedChunk] = []
        messages: list[dict[str, str]] = []
        # A counted answer is not "context" in the excerpt sense: it survives an
        # empty retrieval, so it is initialised here and not inside the branch.
        counted_block = ""
        overview_block = ""
        # #403: same reasoning as `counted_block`/`overview_block` above
        # — initialised here so a `use_context=False` turn (which never runs
        # `_prep()`) still has a defined, empty value to pass to
        # `build_messages`.
        synthesis_block = ""
        recurrence_block = ""
        reached_end = False
        # Declared out here because the shielded `finally` flushes them: a turn
        # torn down mid-sentence still has to persist the buffered tail.
        answer_redactor: OutputRedactor | None = None
        reasoning_redactor: OutputRedactor | None = None
        try:
            if use_context:
                stage = _initial_stage(
                    settings=settings,
                    llm=llm,
                    history=history,
                    question=question,
                    speakers=speakers,
                )
                yield sse("status", {"stage": stage})

                # No `session_scope` here on purpose: `_prepare_context` is
                # phased and opens its own short sessions. Wrapping it would put
                # the turn back to holding one transaction across the rewrite,
                # the counted tier's aggregation and retrieval.
                def _prep():
                    return _prepare_context(
                        user_id=user_id,
                        organization_id=organization_id,
                        question=question,
                        history=history,
                        settings=settings,
                        file_uuids=file_uuids,
                        speakers=speakers,
                        search_mode=search_mode,
                        llm=llm,
                        rewrite_enabled=settings.query_rewrite_enabled,
                        assistant_message_uuid=assistant_message_uuid,
                        recorder=recorder,
                    )

                prepared = _Awaited()
                async for frame in _keepalive_until_done(
                    run_in_threadpool(_prep), prepared, trace_q=trace_q
                ):
                    yield frame
                (
                    masked,
                    meta,
                    counted,
                    overview,
                    synthesis_block,
                    recurrence_block,
                ) = prepared.value
                turn.metadata.update(meta)
                counted_block = format_counted_block(counted)
                overview_block = overview.block if overview is not None else ""

                # #403: extra tokens spent on the planner/enrichment/
                # rewrite-extension calls this turn made, folded into the
                # turn's own counters so `_finalize_turn` meters them through
                # the same `record_chat_usage` call the answer stream uses.
                turn.extra_prompt_tokens += int(meta.get("extra_llm_prompt_tokens", 0))
                turn.extra_completion_tokens += int(meta.get("extra_llm_completion_tokens", 0))

                for frame in _context_warning_frames(meta):
                    yield frame

            messages, excerpt_ids, prompt_diagnostics, answer_tokens = _assemble_prompt(
                turn,
                llm=llm,
                settings=settings,
                system_prompt=system_prompt,
                masked=masked,
                history=history,
                question=question,
                max_tokens=max_tokens,
                counted_block=counted_block,
                overview_block=overview_block,
                recurrence_block=recurrence_block,
                synthesis_block=synthesis_block,
            )
            _emit_prompt_trace(
                recorder,
                chunks_used=len(excerpt_ids),
                prompt_diagnostics=prompt_diagnostics,
                started=started,
            )
            frames = [sse("trace", _payload) for _payload in await drain_available(trace_q)]
            if use_context:
                frames.extend(
                    _grounding_frames(
                        turn,
                        settings=settings,
                        masked=masked,
                        excerpt_ids=excerpt_ids,
                        overview=overview,
                        overview_block=overview_block,
                        prompt_diagnostics=prompt_diagnostics,
                        llm=llm,
                        answer_tokens=answer_tokens,
                        assistant_message_uuid=assistant_message_uuid,
                    )
                )
            for frame in frames:
                yield frame

            # Redaction of the model's OWN words. Offset masking covers the text
            # we gave it; nothing covered the text it writes about that material
            # until this. Resolved here rather than inside `_prepare_context`
            # because a `use_context=False` turn never runs that stage and its
            # answer is just as visible — which is also why it sits OUTSIDE the
            # use_context block above, unlike the language warning.
            output_policy = await run_in_threadpool(
                _resolve_output_policy, user_id, organization_id
            )
            answer_redactor = OutputRedactor(output_policy)
            reasoning_redactor = OutputRedactor(output_policy)

            yield sse("status", {"stage": "generating"})

            kwargs = _generation_kwargs(
                answer_tokens,
                temperature=temperature,
                top_p=top_p,
                enable_thinking=enable_thinking,
            )
            async for frame in _answer_frames(
                llm,
                messages,
                kwargs,
                turn=turn,
                cancel_event=cancel_event,
                answer_redactor=answer_redactor,
                reasoning_redactor=reasoning_redactor,
                started=started,
                assistant_message_uuid=assistant_message_uuid,
                conversation_uuid=conversation_uuid,
            ):
                yield frame

        except _TurnCancelled:
            # #403: a Stop landed at one of `_prepare_context`'s phase
            # boundaries, between leg submissions, or before the planner/
            # enrichment call it was guarding. No `turn.error` is set —
            # `reached_end` stays False, so the `finally` below records this
            # exactly like a client disconnect: `finish_reason = "cancelled"`,
            # no extra LLM call was ever made.
            logger.info("Chat turn %s cancelled during context preparation", assistant_message_uuid)
        except Exception:  # noqa: BLE001 — surface as a frame, never a 500
            logger.exception("Chat stream failed for conversation %s", conversation_uuid)
            turn.error = GENERATION_FAILED_MESSAGE
            turn.error_code = "provider_error"
            turn.metadata["error_code"] = turn.error_code
            yield sse("error", {"code": "provider_error", "message": turn.error})
        else:
            reached_end = True
        finally:
            cancel_event.set()

            # Persistence, metering and audit MUST run here, not after the try.
            #
            # A client disconnect (closed tab, navigation, or the Stop button —
            # which aborts the fetch before its cancel POST lands) makes Starlette
            # cancel this task, delivering GeneratorExit/CancelledError at a
            # `yield`. Both are BaseException, so `except Exception` above does not
            # catch them and any code placed AFTER the try block would simply never
            # run — silently losing the partial answer, skipping the cloud metering
            # hook (letting a user consume provider tokens unbilled by aborting
            # just before completion), and dropping the audit record.
            #
            # The cancel scope is shielded so the DB write survives the very
            # cancellation that triggered it. It contains no `yield`, which is what
            # makes awaiting here legal during generator teardown.
            if not reached_end and not turn.error:
                # Torn down mid-stream: record it as the cancellation it is.
                turn.finish_reason = "cancelled"

            with anyio.CancelScope(shield=True):
                await _teardown_turn(
                    turn,
                    answer_redactor=answer_redactor,
                    reasoning_redactor=reasoning_redactor,
                    on_teardown=on_teardown,
                    llm=llm,
                    messages=messages,
                    masked_count=len(masked),
                    conversation_id=conversation_id,
                    conversation_uuid=conversation_uuid,
                    assistant_message_uuid=assistant_message_uuid,
                    user_id=user_id,
                    organization_id=organization_id,
                    is_first_exchange=is_first_exchange,
                    question=question,
                    started=started,
                    use_context=use_context,
                )

        for frame in _closing_frames(turn):
            yield frame
