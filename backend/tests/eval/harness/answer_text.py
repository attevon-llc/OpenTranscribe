"""Deterministic answer-quality floor — no LLM, no provider, no GPU required (#463).

Four measures every submitted free-text answer gets, always: ``rougeL_f``, ``rouge1_f``,
``token_f1`` and ``answered``. A fifth, ``bertscore_f1``, is optional and heavier (loads
``microsoft/deberta-large-mnli``) — call :func:`compute_bertscore` explicitly rather than
having it run inside every :func:`score_one`, so a caller who only wants the cheap floor
never pays for a model load.

**This is the floor, not the ceiling.** ROUGE and token-F1 are lexical-overlap measures —
they reward a submitted answer that reuses the gold answer's words, and QMSum's own gold
answers are free-text summaries a system could express correctly in different words and
still score low here. That gap is exactly what the LLM-judged tier (the label judge in
``answer_judge.py``, plus RAGAS ``faithfulness`` via ``faithfulness_judge.py``) exists to
close; this module never claims to measure semantic correctness on its own.

``token_f1`` follows the SQuAD evaluation script's normalisation (casefold, strip
punctuation, collapse whitespace, then bag-of-words F1) — the same normalisation family as
this harness's own ``answers.normalise_name``, just applied to a token multiset instead of
a whole string.

If a caller later wraps this module's per-query scores in
``significance.paired_bootstrap_ci`` for a paired comparison, that function ALREADY seeds
``numpy.random.default_rng`` at a fixed default (``seed=0``) — nothing here needs its own
seeding, because nothing here samples anything; every measure in this module is a pure
deterministic function of ``(gold_text, submitted_text)``.
"""

from __future__ import annotations

import re
import string
from collections import Counter
from collections.abc import Mapping
from collections.abc import Sequence
from dataclasses import dataclass
from dataclasses import field
from typing import Any

#: Always computed. Never requires a model load.
MEASURES = ("rougeL_f", "rouge1_f", "token_f1", "answered")

#: Computed only when a caller opts in via ``include_bertscore=True`` — loads
#: ``microsoft/deberta-large-mnli`` on first use.
OPTIONAL_MEASURES = ("bertscore_f1",)

#: The BERTScore model + settings, pinned at every call site (never the package's own
#: moving default). ``lang="en"``: QMSum is English-only (rag-evaluation.md's
#: multilingual coverage table documents this as a stated scope limit, not a silent one).
BERTSCORE_MODEL = "microsoft/deberta-large-mnli"

_PUNCTUATION_RE = re.compile(f"[{re.escape(string.punctuation)}]")
_WHITESPACE_RE = re.compile(r"\s+")


def _normalise_tokens(text: str) -> list[str]:
    """Casefold, strip punctuation, collapse whitespace, split on whitespace."""
    lowered = _PUNCTUATION_RE.sub(" ", str(text).casefold())
    return _WHITESPACE_RE.sub(" ", lowered).strip().split()


def token_f1(gold: str, submitted: str) -> float:
    """Bag-of-words F1 over normalised tokens (the SQuAD-script convention).

    Both empty scores 1.0 (nothing to recover, nothing wrongly added — the same
    vacuous-match convention ``answers._f1`` uses for an empty gold/submitted file
    set). Exactly one empty scores 0.0.
    """
    gold_tokens = _normalise_tokens(gold)
    submitted_tokens = _normalise_tokens(submitted)
    if not gold_tokens and not submitted_tokens:
        return 1.0
    if not gold_tokens or not submitted_tokens:
        return 0.0
    overlap = sum((Counter(gold_tokens) & Counter(submitted_tokens)).values())
    if overlap == 0:
        return 0.0
    precision = overlap / len(submitted_tokens)
    recall = overlap / len(gold_tokens)
    return 2 * precision * recall / (precision + recall)


_rouge_scorer_singleton: Any = None


def _rouge_scorer() -> Any:
    """A module-level ``RougeScorer``, built once — its stemmer setup is not free."""
    global _rouge_scorer_singleton
    if _rouge_scorer_singleton is None:
        from rouge_score import rouge_scorer

        _rouge_scorer_singleton = rouge_scorer.RougeScorer(["rouge1", "rougeL"], use_stemmer=True)
    return _rouge_scorer_singleton


def rouge_f_scores(gold: str, submitted: str) -> dict[str, float]:
    """``{"rouge1_f": ..., "rougeL_f": ...}`` — F-measure only; precision/recall are
    available from the same call but this harness reports F alone, matching the
    single-number convention every other measure in this repo already uses."""
    scored = _rouge_scorer().score(str(gold), str(submitted))
    return {
        "rouge1_f": float(scored["rouge1"].fmeasure),
        "rougeL_f": float(scored["rougeL"].fmeasure),
    }


def score_one(gold_text: str, submitted: str | None) -> dict[str, float]:
    """Score one submitted answer. Returns a value for every :data:`MEASURES` name.

    ``submitted`` is ``None`` OR blank-after-strip: both count as declined, scored
    zero on every measure and ``answered=0.0`` — the same "declined answers zero,
    not dropped" convention as ``answers.score_one``.
    """
    if submitted is None or not submitted.strip():
        return {"rougeL_f": 0.0, "rouge1_f": 0.0, "token_f1": 0.0, "answered": 0.0}
    rouge = rouge_f_scores(gold_text, submitted)
    return {
        "rougeL_f": rouge["rougeL_f"],
        "rouge1_f": rouge["rouge1_f"],
        "token_f1": token_f1(gold_text, submitted),
        "answered": 1.0,
    }


def compute_bertscore(golds: Sequence[str], submissions: Sequence[str]) -> list[float]:
    """Batch BERTScore F1 (MIT), :data:`BERTSCORE_MODEL` pinned, ``rescale_with_baseline=True``.

    Batched deliberately — ``bert_score.score`` amortises one model forward pass over
    the whole list, so this is called once per corpus/class subset rather than once
    per query. Positions in ``golds``/``submissions`` must correspond 1:1; an empty
    ``submissions[i]`` (a decline) is scored 0.0 without being sent to the model, since
    BERTScore over an empty string is undefined, not a real zero.

    Args:
        golds: gold answer text, one per query.
        submissions: submitted answer text, one per query, same order.

    Returns:
        One F1 per query, same order and length as the inputs.

    Raises:
        ValueError: ``golds`` and ``submissions`` differ in length.
    """
    if len(golds) != len(submissions):
        raise ValueError(f"compute_bertscore: {len(golds)} golds vs {len(submissions)} submissions")
    if not golds:
        return []

    scored_indices = [i for i, text in enumerate(submissions) if text and text.strip()]
    scores = [0.0] * len(golds)
    if not scored_indices:
        return scores

    import bert_score

    _, _, f1 = bert_score.score(
        [submissions[i] for i in scored_indices],
        [golds[i] for i in scored_indices],
        model_type=BERTSCORE_MODEL,
        rescale_with_baseline=True,
        lang="en",
        verbose=False,
    )
    for position, index in enumerate(scored_indices):
        scores[index] = float(f1[position])
    return scores


@dataclass
class AnswerTextResult:
    """Per-query and aggregate scores, plus the denominator used."""

    per_query: dict[str, dict[str, float]] = field(default_factory=dict)
    aggregate: dict[str, float] = field(default_factory=dict)
    query_count: int = 0
    unanswered: list[str] = field(default_factory=list)


def evaluate_answer_text(
    gold: Mapping[str, str],
    submitted: Mapping[str, str | None],
    *,
    include_bertscore: bool = False,
) -> AnswerTextResult:
    """Score submitted free-text answers against gold, over the **gold** query set.

    Follows the same ``trec_eval -c`` convention as every other engine in this
    harness: ``gold``'s keys are the denominator, a query absent from ``submitted``
    (or present with ``None``) scores zero and is counted in ``unanswered``, and the
    per-query row count is asserted against the gold count before any mean is taken.

    Args:
        gold: query id -> gold answer text.
        submitted: query id -> submitted answer text, or ``None``/absent for a
            decline.
        include_bertscore: also compute ``bertscore_f1`` (batched, one model pass).

    Returns:
        An :class:`AnswerTextResult` whose ``query_count`` equals ``len(gold)``.

    Raises:
        ValueError: ``gold`` is empty.
    """
    if not gold:
        raise ValueError("Refusing to evaluate against an empty gold answer-text set")

    result = AnswerTextResult(query_count=len(gold))
    ordered_ids = sorted(gold)
    for query_id in ordered_ids:
        answer = submitted.get(query_id)
        if answer is None or not str(answer).strip():
            result.unanswered.append(query_id)
        result.per_query[query_id] = score_one(gold[query_id], answer)

    if len(result.per_query) != len(gold):
        raise ValueError(
            f"Scored {len(result.per_query)} answers against {len(gold)} gold — "
            "the zero substitution did not cover the query set"
        )

    measures = list(MEASURES)
    if include_bertscore:
        bert_scores = compute_bertscore(
            [gold[qid] for qid in ordered_ids], [submitted.get(qid) or "" for qid in ordered_ids]
        )
        for query_id, score in zip(ordered_ids, bert_scores, strict=True):
            result.per_query[query_id]["bertscore_f1"] = score
        measures = [*measures, "bertscore_f1"]

    for name in measures:
        values = [row[name] for row in result.per_query.values()]
        result.aggregate[name] = sum(values) / len(values)
    return result


def subset_answer_text(result: AnswerTextResult, query_ids: set[str]) -> AnswerTextResult:
    """Re-aggregate over a subset (one corpus, one class), keeping the denominator."""
    rows = {qid: row for qid, row in result.per_query.items() if qid in query_ids}
    out = AnswerTextResult(
        per_query=rows,
        query_count=len(rows),
        unanswered=[qid for qid in result.unanswered if qid in query_ids],
    )
    if not rows:
        return out
    for name in next(iter(rows.values())):
        out.aggregate[name] = sum(row[name] for row in rows.values()) / len(rows)
    return out
