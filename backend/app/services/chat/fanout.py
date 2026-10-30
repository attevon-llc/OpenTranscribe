"""The #403 planned fan-out: parallel retrieval legs, enrichment and recurrence.

Taken only when the planner produced a usable plan (``service._retrieve_or_fanout``).
Every leg shares the turn's ALREADY-resolved scope (T9), and the enrichment call reads
only evidence that has already been through ``mask_chunks``/``mask_digests``.
"""

from __future__ import annotations

import logging
from typing import Any

from app.services.chat import legs
from app.services.chat import limits
from app.services.chat import planner
from app.services.chat.planning_steps import _validate_plan_speakers
from app.services.chat.redactor import MaskedChunk
from app.services.chat.settings import ChatSettings
from app.services.chat.trace import Outcome as TraceOutcome
from app.services.chat.trace import QueryStage
from app.services.chat.trace import TraceRecorder
from app.services.chat.trace import emit as emit_trace
from app.services.chat.turn import _check_cancelled

logger = logging.getLogger(__name__)


def _run_plan_fanout(
    *,
    plan: planner.Plan,
    decision,
    effective_query: str,
    question: str,
    user_id: int,
    organization_id: int | None,
    file_uuids: list[str] | None,
    speakers: list[str] | None,
    settings: ChatSettings,
    search_mode: str,
    session_scope,
    assistant_message_uuid: str,
    meta: dict[str, Any],
    recorder: TraceRecorder | None = None,
):
    """Build and run one turn's parallel leg fan-out (#403).

    Returns a :class:`~app.services.chat.retrieval.RetrievalResult`-shaped
    object plus the counted/recurrence outcomes, so the caller can drop them
    into the exact slots the single-leg pipeline already fills — everything
    downstream of this call (quarantine drop, masking, the summary tier,
    diagnostics) does not need to know a fan-out happened.

    **T9: every leg reuses the SAME resolved ``file_uuids``/scope the turn
    already established.** A plan may only ADD legs of KINDS the rules could
    already produce; it never gets a wider file scope, never touches SQL
    directly, and never overrides ``router._apply_structure``. The one
    per-leg narrowing that IS legitimate is the speaker leg's ``speakers``
    list — narrower evidence within the SAME files, never a different file
    set — and even that is built from roster-VALIDATED names
    (:func:`_validate_plan_speakers`), never the plan's raw strings.

    Args:
        plan: The validated, non-empty, non-failed plan.
        decision: The router's `Route` for this turn.
        effective_query: The (possibly rewritten) question — what the main
            and speaker legs search with.
        question: The ORIGINAL question — what sub-question legs are relative
            to is the plan's own wording, and what the counted/recurrence
            tiers and the final merge-rerank score against.
        session_scope: Factory for a short-lived Postgres session; threaded
            straight into `answer_aggregation`/`answer_recurrence` exactly as
            the single-leg pipeline already does, and into
            `_validate_plan_speakers` for its own short read.
        assistant_message_uuid: For the "between leg submissions" cancel
            check inside `legs.run_legs`.
        meta: Mutated in place with `legs.FanOutResult.as_metadata()` plus
            `subquestion_legs_with_evidence` (enrichment's own trigger).

    Returns:
        ``(result, counted, recurrence_result)``.
    """
    import functools

    from app.services.chat.retrieval import RetrievalResult
    from app.services.search.chunk_retrieval import diversity_sample
    from app.services.search.chunk_retrieval import retrieve_chunks

    built: list[legs.Leg] = []

    built.append(
        legs.Leg(
            kind=legs.LEG_MAIN,
            name="main",
            run=lambda: retrieve_chunks(
                effective_query,
                user_id=user_id,
                organization_id=organization_id,
                file_uuids=file_uuids,
                speakers=speakers,
                size=settings.candidate_pool,
                search_mode=search_mode,
            ),
        )
    )

    for index, subq in enumerate(plan.subquestions[:3]):
        # `functools.partial`, not a `lambda q=subq: ...` default-arg — the
        # classic closure-over-loop-variable trap (every lambda in the loop
        # would otherwise share the same `subq` name and all fire with the
        # LAST value) needs to be avoided WITHOUT relying on a default
        # argument mypy cannot infer the type of against `Leg.run`'s
        # `Callable[[], Any]` field type. `partial` binds `subq` by VALUE at
        # construction time, sidestepping both problems at once.
        built.append(
            legs.Leg(
                kind=legs.LEG_SUBQUESTION,
                name=f"subquestion-{index}",
                run=functools.partial(
                    retrieve_chunks,
                    subq,
                    user_id=user_id,
                    organization_id=organization_id,
                    file_uuids=file_uuids,  # SAME scope as the main leg — T9
                    speakers=speakers,
                    size=max(1, settings.candidate_pool // 2),
                    search_mode=search_mode,
                ),
            )
        )

    validated_speakers = _validate_plan_speakers(
        plan.speakers,
        user_id=user_id,
        organization_id=organization_id,
        session_scope=session_scope,
        file_uuids=file_uuids,  # SAME scope as every other leg — T9
    )
    if validated_speakers:
        built.append(
            legs.Leg(
                kind=legs.LEG_SPEAKER,
                name="speaker",
                run=lambda: retrieve_chunks(
                    effective_query,
                    user_id=user_id,
                    organization_id=organization_id,
                    file_uuids=file_uuids,  # SAME scope as the main leg — T9
                    speakers=validated_speakers,
                    size=settings.candidate_pool,
                    search_mode=search_mode,
                ),
            )
        )

    wants_counted = decision.wants_aggregate or "counted" in plan.wants
    if wants_counted:
        from app.core.config import settings as settings_config
        from app.services.chat.aggregation_service import answer_aggregation
        from app.services.opensearch_service import get_opensearch_client

        built.append(
            legs.Leg(
                kind=legs.LEG_COUNTED,
                name="counted",
                run=lambda: answer_aggregation(
                    question,
                    decision,
                    session_factory=session_scope,
                    client=get_opensearch_client(),
                    index=settings_config.OPENSEARCH_CHUNKS_INDEX,
                    user_id=user_id,
                    organization_id=organization_id,
                    file_uuids=file_uuids,
                ),
            )
        )

    wants_recurrence = settings.recurrence_enabled and (
        decision.wants_recurrence or "recurrence" in plan.wants
    )
    if wants_recurrence:
        from app.services.chat.recurrence_service import answer_recurrence

        built.append(
            legs.Leg(
                kind=legs.LEG_RECURRENCE,
                name="recurrence",
                run=lambda: answer_recurrence(
                    decision,
                    session_factory=session_scope,
                    user_id=user_id,
                    organization_id=organization_id,
                    file_uuids=file_uuids,
                    recurrence_enabled=settings.recurrence_enabled,
                ),
            )
        )

    # GH #514 seam: PLANNED is recorded here, not in `_resolve_plan`, because
    # only this function knows the final dispatched leg count — the earlier
    # SKIPPED/"rules" case (no plan at all) is recorded by the caller,
    # `_retrieve_or_fanout`.
    emit_trace(recorder, QueryStage.PLANNED, node_id="plan", legs=len(built))

    outcome = legs.run_legs(
        built,
        max_workers=settings.planner_max_parallel_legs,
        cancel_check=lambda: limits.is_cancelled(assistant_message_uuid),
        recorder=recorder,
        parent="plan",
    )

    result = RetrievalResult()
    result.retrieved = len(outcome.chunk_hits)
    hits = outcome.chunk_hits

    # ONE rerank over the union, against the ORIGINAL question, normal
    # budget — never a per-leg rerank. A per-leg rerank would score each
    # leg's hits against whatever sub-query produced them, which is not the
    # ranking problem the merged pool poses.
    if settings.rerank_enabled and hits:
        from app.services.chat.reranker import rerank as rerank_fn

        hits = rerank_fn(question, hits, max_pairs=settings.rerank_max_pairs)
        result.reranked = min(len(hits), settings.rerank_max_pairs)
        emit_trace(recorder, QueryStage.RERANKED, parent="plan", count=len(hits))
    else:
        emit_trace(recorder, QueryStage.RERANKED, TraceOutcome.SKIPPED, parent="plan")

    counted_outcome = outcome.other.get("counted")
    counted = counted_outcome.result if counted_outcome is not None else None
    if wants_counted:
        # Recorded either way, matching the single-leg pipeline: "aggregation
        # was attempted and declined" is a different fact from "it was never
        # attempted", and only present on `meta` when a counted leg actually
        # ran (never on a fan-out that had no reason to run one).
        meta["aggregation"] = counted.as_metadata() if counted is not None else {"declined": True}
    recurrence_outcome = outcome.other.get("recurrence")
    recurrence_result = recurrence_outcome.result if recurrence_outcome is not None else None

    # Same ratio the single-leg pipeline applies when a counted answer is the
    # thing being asked for — the excerpts are illustration, not the answer.
    final_cap = settings.final_chunks
    if counted is not None:
        final_cap = max(1, settings.final_chunks // 3)

    result.chunks = diversity_sample(hits, max_per_file=settings.max_chunks_per_file, cap=final_cap)

    if decision.wants_digest:
        from app.services.search.chunk_retrieval import retrieve_digests

        result.digests = retrieve_digests(
            effective_query,
            user_id=user_id,
            organization_id=organization_id,
            file_uuids=file_uuids,
            size=6,
            search_mode=search_mode,
        )

    meta.update(outcome.as_metadata())
    subq_names = {f"subquestion-{i}" for i in range(len(plan.subquestions[:3]))}
    meta["subquestion_legs_with_evidence"] = sum(
        1 for name, count in outcome.chunk_counts_by_leg.items() if name in subq_names and count > 0
    )
    return result, counted, recurrence_result


def _run_enrichment(
    llm, question: str, masked_chunks: list[MaskedChunk]
) -> tuple[str, int, int, int]:
    """One bounded non-streaming call reconciling merged evidence (#403).

    Reads ``masked_chunks`` — evidence that has ALREADY passed through
    ``mask_chunks``/``mask_digests`` — never a raw retrieval hit, so the same
    redaction posture protecting the main generation call protects this one.
    Temperature 0, non-streaming, one call, bounded to ~500 reply tokens.

    Returns:
        ``(block, llm_calls, prompt_tokens, completion_tokens)``. ``block``
        is ``""`` on any failure or when there is no evidence to reconcile —
        an enhancement in the hot path, never a dependency.
    """
    evidence = "\n\n".join(c.content.strip() for c in masked_chunks[:12] if c.content.strip())
    if not evidence.strip():
        return "", 0, 0, 0
    evidence = evidence[:6000]
    system = (
        "You reconcile several pieces of retrieved evidence about ONE question "
        "into a short, neutral synthesis. State only what the evidence "
        "supports; do not invent facts or resolve disagreements the evidence "
        "itself leaves open — note a contradiction between excerpts rather "
        "than picking a side. Write at most 6 sentences, no headings, no "
        "citation markers."
    )
    user_content = f"Question: {question}\n\nEvidence:\n{evidence}"
    messages = [
        {"role": "system", "content": system},
        {"role": "user", "content": user_content},
    ]
    try:
        response = llm.chat_completion(messages, max_tokens=500, temperature=0)
    except Exception as exc:  # noqa: BLE001 — an enhancement, never a dependency
        logger.info(f"Chat enrichment call failed, continuing without a synthesis block: {exc}")
        return "", 1, 0, 0

    text = (getattr(response, "content", "") or "").strip()[:3000]
    if not text:
        return "", 1, 0, 0

    from app.services.chat.prompt_blocks import format_synthesis_block

    try:
        prompt_tokens = llm.estimate_tokens(system + user_content)
        completion_tokens = llm.estimate_tokens(text)
    except Exception:  # noqa: BLE001 — metering must not break the feature
        prompt_tokens = 0
        completion_tokens = 0
    return format_synthesis_block(text), 1, prompt_tokens, completion_tokens


def _maybe_run_enrichment(
    *,
    settings: ChatSettings,
    llm,
    question: str,
    masked: list[MaskedChunk],
    recurrence_result,
    assistant_message_uuid: str,
    meta: dict[str, Any],
    recorder: TraceRecorder | None = None,
) -> tuple[str, int]:
    """Phase 5 of `_prepare_context` (#403): decide and run enrichment.

    Split out for the same reason `_resolve_plan` was — keeps
    `_prepare_context` under ruff's complexity limit. Fires only when the
    merged evidence looks like it actually came from more than one angle —
    either the recurrence leg found a real cross-meeting pattern, or more
    than one sub-question leg contributed evidence of its own. A plan with
    one subquestion (or none) and no recurrence result has nothing worth
    reconciling, and running the call anyway would just paraphrase what a
    single leg already found.

    Mutates `meta` in place with the extra token counters `_finalize_turn`
    folds into the turn's metered totals.

    Returns:
        `(synthesis_block, llm_calls)` — `synthesis_block` is `""` unless
        enrichment actually fired and produced text.
    """
    if not (settings.enrichment_enabled and llm is not None):
        emit_trace(
            recorder,
            QueryStage.REVIEWED,
            TraceOutcome.SKIPPED,
            node_id="review",
            reason="disabled",
        )
        return "", 0

    recurrence_groups = len(getattr(recurrence_result, "groups", ()) or ())
    subq_with_evidence = int(meta.get("subquestion_legs_with_evidence", 0))
    if not (recurrence_groups >= 3 or subq_with_evidence >= 2):
        emit_trace(
            recorder,
            QueryStage.REVIEWED,
            TraceOutcome.SKIPPED,
            node_id="review",
            reason="not_applicable",
        )
        return "", 0

    _check_cancelled(assistant_message_uuid)
    block, calls, extra_prompt, extra_completion = _run_enrichment(llm, question, masked)
    meta["extra_llm_prompt_tokens"] = meta.get("extra_llm_prompt_tokens", 0) + extra_prompt
    meta["extra_llm_completion_tokens"] = (
        meta.get("extra_llm_completion_tokens", 0) + extra_completion
    )
    emit_trace(
        recorder,
        QueryStage.REVIEWED,
        TraceOutcome.OK if block else TraceOutcome.EMPTY,
        node_id="review",
        count=1 if block else 0,
    )
    return block, calls


def _render_recurrence(recurrence_result, meta: dict[str, Any]) -> str:
    """Phase 5 of ``_prepare_context``: the ``<recurrence>`` block, ``""`` when none."""
    recurrence_block = ""
    if recurrence_result is not None:
        from app.services.chat.prompt_blocks import format_recurrence_block

        recurrence_block = format_recurrence_block(recurrence_result)
        if recurrence_block:
            meta["recurrence"] = {
                "groups": len(recurrence_result.groups),
                "truncated": bool(recurrence_result.truncated),
            }
    return recurrence_block
