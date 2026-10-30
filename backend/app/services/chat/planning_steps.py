"""Phase 1 of a chat turn: the query rewrite, the query plan and speaker names.

Everything here runs before retrieval and decides WHAT the turn searches for: the
standalone rewrite of a follow-up, the #403 planner's plan, and the roster lookups
that turn a mentioned (or planner-supplied) name into a validated speaker. A plan is
untrusted model output, so its names are re-validated against the roster here and
never used raw.

``_resolve_speaker_focus`` is called through ``service.py``'s module globals (from
``service._apply_speaker_resolution``), which is where tests patch it.
"""

from __future__ import annotations

import logging
import time
from typing import Any

from app.services.chat import planner
from app.services.chat.settings import ChatSettings
from app.services.chat.trace import Outcome as TraceOutcome
from app.services.chat.trace import QueryStage
from app.services.chat.trace import TraceRecorder
from app.services.chat.trace import emit as emit_trace
from app.services.chat.turn import _check_cancelled

logger = logging.getLogger(__name__)


def _resolve_speaker_focus(
    *,
    question: str,
    user_id: int,
    organization_id: int | None,
    session_scope,
    file_uuids: list[str] | None = None,
):
    """Phase 1.5 of ``_prepare_context``: a MENTIONED speaker.

    Split out so ``_prepare_context`` stays under ruff's complexity limit —
    the same reason ``_resolve_summary_tier`` documents for its own
    extraction. The caller (``_prepare_context``) is the ONE place that
    decides whether to run this at all, gated on
    ``settings.speaker_resolver_enabled`` — this function does not re-check
    the flag itself.

    Its own short session: the candidate-roster read is a small number of
    bounded queries (#524 — no longer one unbounded roster read), closing
    well before the LLM round trip / retrieval that follow in the phases
    below. Reads ``question`` as typed, never the rewrite —
    ``speaker_resolver.resolve_speaker_mentions`` is explicit that a rewrite
    can lose or paraphrase a name the original carried. ``file_uuids`` is the
    turn's ALREADY-resolved scope (``_prepare_context`` receives it as a
    parameter) — threading it through scopes the speaker lookup to the same
    files the turn will retrieve from, per #524 design direction #3.

    Returns:
        A ``SpeakerMentionResolution`` — always, even when nothing matched, so
        the caller has one shape to read ``.as_meta()``/``.speaker_focus`` off.
    """
    from app.services.chat.speaker_resolver import resolve_speaker_mentions

    with session_scope() as db:
        return resolve_speaker_mentions(
            db,
            question,
            user_id=user_id,
            organization_id=organization_id,
            file_uuids=file_uuids,
        )


def _validate_plan_speakers(
    names: tuple[str, ...],
    *,
    user_id: int,
    organization_id: int | None,
    session_scope,
    file_uuids: list[str] | None = None,
) -> list[str]:
    """Re-validate planner-supplied names against the real roster (T3, T9).

    A plan is untrusted model output. Its ``speakers`` field is free text —
    never a roster id — so it MUST go through the same matching ladder
    (`speaker_resolver.match_candidate`) that a mention typed in the question
    itself goes through, never trusted as-is. A name with no unique roster
    match, or an ambiguous one, is silently dropped rather than guessed —
    matching this package's standing rule that ambiguity means no filter,
    ever.

    Its own short session, opened and closed here — this is exactly the kind
    of Postgres read a leg must not hold across the parallel retrieval calls
    the caller is about to fan out, so it runs BEFORE the fan-out starts,
    not as one of the legs themselves. ``file_uuids`` is the SAME scope T9
    requires every leg to share (see this function's caller) — the roster a
    plan's speaker names are validated against is scoped to the turn's own
    files, never the whole library (#524 design direction #3). The planner
    already caps ``plan.speakers`` at ``MAX_SPEAKERS`` (5), so this is itself
    a bounded, candidate-targeted lookup — never the old unbounded roster.

    Returns:
        Validated, deduplicated roster names. Empty when ``names`` is empty,
        the roster could not be built, or nothing matched uniquely.
    """
    if not names:
        return []
    from app.services.chat.speaker_name_match import match_candidate
    from app.services.chat.speaker_roster import build_candidate_roster

    try:
        with session_scope() as db:
            roster = build_candidate_roster(
                db,
                user_id,
                list(names),
                organization_id=organization_id,
                file_uuids=file_uuids,
            )
    except Exception as exc:  # noqa: BLE001 — an enhancement, never a dependency
        logger.info(f"Chat plan speaker validation could not build a roster: {exc}")
        return []
    if roster.declined:
        return []

    validated: list[str] = []
    for name in names:
        outcome = match_candidate(name, roster)
        if outcome.matched and outcome.matched not in validated:
            validated.append(outcome.matched)
    return validated


def _maybe_rewrite(
    *,
    llm,
    history: list[dict[str, str]],
    question: str,
    rewrite_enabled: bool,
    settings: ChatSettings,
    meta: dict[str, Any],
    recorder: TraceRecorder | None = None,
):
    """Phase 1 of `_prepare_context`: the query-rewrite LLM round trip.

    Split out for the same complexity-limit reason as `_resolve_plan` — a
    pure relocation, no behavior change. Mutates `meta` in place with
    `rewritten_query`/`timings_ms.rewrite`, matching how the rest of
    `_prepare_context` already threads `meta` through its phases.

    Returns:
        `(effective_query, llm_intent, rewrite, llm_calls)` — `rewrite` is
        the raw `RewriteResult` (or `None`), needed downstream by
        `_resolve_plan` for a follow-up turn's piggybacked `PLAN:` line.
    """
    if not (rewrite_enabled and history):
        emit_trace(
            recorder,
            QueryStage.REWRITTEN,
            TraceOutcome.SKIPPED,
            node_id="rewrite",
            reason="disabled" if not rewrite_enabled else "not_applicable",
        )
        return question, None, None, 0

    from app.services.chat.query_rewriter import rewrite_query

    rewrite_started = time.monotonic()
    # #403: a follow-up turn's plan rides this SAME call (a third
    # `PLAN:` line) rather than a second, standalone round trip — see
    # `query_rewriter.rewrite_query`'s `want_plan` docstring. Only requested
    # when the flag is on, so a deployment with the planner off pays nothing
    # extra for this call.
    rewrite = rewrite_query(llm, history, question, want_plan=settings.planner_enabled)
    effective_query = rewrite.query
    if effective_query != question:
        meta["rewritten_query"] = effective_query
    meta.setdefault("timings_ms", {})["rewrite"] = int((time.monotonic() - rewrite_started) * 1000)
    # ⚠️ `ms` ONLY. The obvious thing to show on this node is the rewritten
    # query, which is the USER'S OWN TEXT. `_scrub` would drop a `query` key,
    # but the call site must never construct one.
    emit_trace(
        recorder,
        QueryStage.REWRITTEN,
        TraceOutcome.OK if effective_query != question else TraceOutcome.EMPTY,
        node_id="rewrite",
        source="llm",
        ms=meta["timings_ms"]["rewrite"],
    )
    return effective_query, rewrite.intent, rewrite, 1


def _resolve_plan(
    *,
    settings: ChatSettings,
    llm,
    history: list[dict[str, str]],
    rewrite,
    question: str,
    decision,
    assistant_message_uuid: str,
    meta: dict[str, Any],
) -> tuple[planner.Plan | None, int]:
    """Phase 1.6 of `_prepare_context` (#403): resolve this turn's plan.

    Split out so `_prepare_context` stays under ruff's complexity limit — the
    same reason `_resolve_summary_tier`/`_apply_speaker_resolution` document
    for their own extractions.

    **Never a routing-only call.** Turn 1 (no history) makes a STANDALONE
    planner call, and only when `needs_plan` fires — the trigger is kept
    deliberately cheap and rare (<=15% on ordinary lookups). A follow-up
    turn NEVER calls `needs_plan` at all: its plan, if any, already rode the
    rewrite call this function was handed (`rewrite.plan`) as a third
    `PLAN:` line — see `query_rewriter.rewrite_query`'s `want_plan`.

    Mutates `meta` in place with `meta["plan"]`, matching how the rest of
    `_prepare_context` already threads its own `meta` dict through several
    stages.

    Returns:
        `(plan, llm_calls)` — `plan` is `None` when the flag is off, no LLM
        is configured, or (for turn 1) `needs_plan` never fired. `llm_calls`
        is `1` exactly when a STANDALONE call was made here (never for the
        follow-up path, which piggybacks on a call `_prepare_context` already
        counted for the rewrite itself).
    """
    if not (settings.planner_enabled and llm is not None):
        return None, 0

    plan: planner.Plan | None = None
    llm_calls = 0
    if history:
        plan = rewrite.plan if rewrite is not None else None
    else:
        _check_cancelled(assistant_message_uuid)
        ambiguous_speaker = bool((meta.get("speaker_resolution") or {}).get("ambiguous"))
        if planner.needs_plan(
            question=question, route=decision, ambiguous_speaker=ambiguous_speaker
        ):
            plan, calls = planner.build_plan(llm, question)
            llm_calls += calls

    if plan is not None:
        if plan.failed:
            meta["plan"] = {"failed": True}
        elif not plan.is_empty:
            meta["plan"] = plan.as_metadata()
    return plan, llm_calls


def _initial_stage(
    *,
    settings: ChatSettings,
    llm,
    history: list[dict[str, str]],
    question: str,
    speakers: list[str] | None,
) -> str:
    """The first ``status`` stage a context turn announces: planning, rewriting or retrieving."""
    # Query rewriting is a separate LLM round trip, so it is worth
    # its own stage. Retrieval and reranking happen inside one
    # threadpool call and are reported together as "retrieving" —
    # the frontend renders every pre-generation stage with the same
    # label anyway, so splitting them further would add machinery
    # for no visible difference.
    will_rewrite = settings.query_rewrite_enabled and bool(history)
    # #403: a turn-1 (no-history) question that would trigger
    # a STANDALONE planner call gets its own stage — the model has
    # not started retrieving anything yet, and lumping it under
    # "retrieving" would read as a stalled search. Cheap to
    # compute here (both `route()` and `needs_plan()` are pure,
    # microsecond-scale, no I/O) — the leg COUNT is not known
    # until the fan-out finishes well after this frame, so it
    # rides `msg_metadata.leg_count`/`leg_timings_ms` instead (see
    # `legs.FanOutResult.as_metadata`), not this frame.
    will_plan = False
    if settings.planner_enabled and llm is not None and not history:
        from app.services.chat.router import route as _preview_route

        will_plan = planner.needs_plan(
            question=question,
            route=_preview_route(
                question,
                speakers=speakers,
                recurrence_enabled=settings.recurrence_enabled,
            ),
        )
    return "planning" if will_plan else ("rewriting" if will_rewrite else "retrieving")
