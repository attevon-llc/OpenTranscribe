"""Score answered queries and summarise per-query retrieval latency."""

from __future__ import annotations

from typing import Any

from rag_bench.config import logger


def latency_summary(samples: list[float], workers: int) -> dict[str, Any]:
    """Quantiles of the per-query retrieval cost, for the Phase 7 p95 gate.

    Lives in ``runinfo.json``, never in ``metrics.json``: a duration cannot be
    byte-identical across runs, and the results document's determinism is what
    makes an arm-to-arm difference attributable.

    ``concurrency`` is recorded beside the numbers because these are measured
    under the harness's own worker pool, so they are a **comparable cost signal
    between arms**, not a user-facing latency figure. Reading them as the
    latter is the mistake the field is named against.

    Args:
        samples: Per-call durations in milliseconds.
        workers: Concurrent retrieval requests in flight during the run.

    Returns:
        Quantiles plus the concurrency they were taken at.
    """
    if not samples:
        return {'samples': 0}
    ordered = sorted(samples)

    def _q(fraction: float) -> float:
        # Nearest-rank: no interpolation, so the value reported is one that was
        # actually observed.
        index = min(len(ordered) - 1, max(0, int(round(fraction * len(ordered))) - 1))
        return round(ordered[index], 1)

    return {
        'samples': len(ordered),
        'concurrency': workers,
        'p50': _q(0.50),
        'p95': _q(0.95),
        'p99': _q(0.99),
        'max': round(ordered[-1], 1),
        'mean': round(sum(ordered) / len(ordered), 1),
    }


def score_answers(args, queries: list, user_id: int, client, settings):
    """Score the answer-scored queries, and record what produced each answer.

    Returns:
        ``(answers_block, answer_rows)``. The block is self-describing: it names
        the answerer, its mechanism per intent, and the scoring policy, so an EM
        value in it cannot be read without its provenance. Answering is serial
        and in query-id order — the aggregations are cheap, and a thread pool
        would buy wall clock at the cost of the one property that matters here.
    """
    from tests.eval.harness import answerers as answerers_mod
    from tests.eval.harness import report as report_mod
    from tests.eval.harness.answers import AnswerPolicy, evaluate_answers, scoring_provenance

    policy = AnswerPolicy(
        count_tolerance=args.answer_count_tolerance, set_credit=args.answer_set_credit
    )
    if not queries:
        return {
            'scored': 0,
            'note': 'no answer-scored queries resolved onto this stack',
            'scoring': scoring_provenance(policy),
        }, []

    from app.db.base import engine

    answerer = answerers_mod.build_answerer(
        args.answerer,
        client=client,
        index=settings.OPENSEARCH_CHUNKS_INDEX,
        user_id=user_id,
        engine=engine,
    )
    gold = {q.query_id: q.gold_answer for q in queries if q.gold_answer is not None}
    submitted = {
        query.query_id: answerer.answer(query)
        for query in sorted(queries, key=lambda q: q.query_id)
    }
    result = evaluate_answers(gold, submitted, policy=policy)
    rows = report_mod.build_answer_rows(queries, result)
    logger.info(
        'Answers (%s): %d scored, %d unanswered, EM %.4f',
        answerer.name,
        result.query_count,
        len(result.unanswered),
        result.aggregate['EM'],
    )
    return {
        'scored': result.query_count,
        'unanswered': len(result.unanswered),
        'answerer': answerer.describe(),
        'scoring': scoring_provenance(policy),
        'rows': rows,
        'details': report_mod.build_answer_details(queries, result),
    }, rows
