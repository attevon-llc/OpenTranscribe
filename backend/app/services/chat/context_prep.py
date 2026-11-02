"""Context preparation for one chat turn: rewrite, route, count, retrieve, mask.

:func:`_prepare_context` runs the blocking RAG stages in a worker thread and returns
plain dataclasses; ``service.py`` awaits it and streams the result. Its phases are
wired here and implemented in the siblings (``planning_steps.py``, ``fanout.py``,
``evidence.py``, ``summary_tier.py``). Whether excerpt text is masked before the LLM
is decided once per turn in :func:`_mask_evidence`, from ``masking.py``'s builders.
"""

from __future__ import annotations

import time
from typing import Any

from app.services.chat import planner
from app.services.chat.evidence import _drop_quarantined_hits
from app.services.chat.evidence import _emit_expansion
from app.services.chat.evidence import _merge_evidence
from app.services.chat.evidence import _record_languages
from app.services.chat.evidence import _record_retrieval_diagnostics
from app.services.chat.fanout import _maybe_run_enrichment
from app.services.chat.fanout import _render_recurrence
from app.services.chat.fanout import _run_plan_fanout
from app.services.chat.masking import _build_digest_mask_kwargs
from app.services.chat.masking import _build_mask_kwargs
from app.services.chat.planning_steps import _maybe_rewrite
from app.services.chat.planning_steps import _resolve_plan
from app.services.chat.planning_steps import _resolve_speaker_focus
from app.services.chat.redactor import MaskedChunk
from app.services.chat.redactor import mask_chunks
from app.services.chat.retrieval import RetrievalResult
from app.services.chat.retrieval import retrieve_context
from app.services.chat.settings import ChatSettings
from app.services.chat.summary_tier import _build_summaries
from app.services.chat.summary_tier import _compose_overview
from app.services.chat.trace import Outcome as TraceOutcome
from app.services.chat.trace import QueryStage
from app.services.chat.trace import TraceRecorder
from app.services.chat.trace import emit as emit_trace
from app.services.chat.turn import _check_cancelled

# Tests patch `_drop_quarantined_hits`, `_resolve_speaker_focus`, `mask_chunks` and
# `retrieve_context` on THIS module, so each one's call site lives here and reads it
# from these globals. Moving a call site into a sibling would leave those patches
# silently inert.


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
    # in `frames._grounding_frames` and in `services/chat/CLAUDE.md`): non-zero means
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
