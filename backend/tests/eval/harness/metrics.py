"""Retrieval metrics: the single place a number in this repo comes from.

Everything here exists because a measured implementation difference made it
necessary — see ``.rag-403/oss-leverage-survey.md`` §1 and
``docs-site/docs/developer-guide/rag-evaluation.md``.

Three behaviours are NOT the library default and are implemented here:

1. **Tie normalisation.** ``trec_eval`` breaks equal scores by document id
   *descending*. Our ids are ``{file_uuid}_{chunk_index}`` and (from Stage 3)
   ``{file_uuid}_digest``; ``d`` outsorts every digit, so a digest tied with a
   chunk of the same file wins the tie *because of its name*. RRF ties are
   structural (sums of ``1/(k+rank)`` over integer ranks), so this is not a
   corner case. :func:`normalise_run` re-sorts by
   ``(-score, doc_type, file_uuid, chunk_index)`` and re-emits strictly
   decreasing synthetic scores, making the metric independent of the id scheme.
2. **``trec_eval -c`` semantics.** ``RelevanceEvaluator.evaluate()`` silently
   omits a query the run did not answer, so ``mean(results.values())`` flatters
   exactly the regressions worth catching. :func:`evaluate` iterates the *qrels*
   query set and substitutes 0.0, then asserts the denominator matches.
3. **Linear gain, ``ndcg_cut``.** ``gain = rel`` (Järvelin & Kekäläinen 2002)
   and the ideal DCG truncated at k. Bare ``ndcg`` does not truncate and answers
   differently on the same run. The gain function is fixed for the epic: changing
   it mid-epic would look exactly like a retrieval win.
"""

from __future__ import annotations

from dataclasses import dataclass
from dataclasses import field
from typing import Any

#: Canonical measure name -> the ``pytrec_eval`` measure string that computes it.
#: Keys are the names reported in tables and papers; values are what trec_eval is
#: asked for. Validated against ``ir_measures`` by :func:`measure_provenance`.
MEASURES: dict[str, str] = {
    "nDCG@5": "ndcg_cut.5",
    "nDCG@10": "ndcg_cut.10",
    "nDCG@20": "ndcg_cut.20",
    "R@5": "recall.5",
    "R@10": "recall.10",
    "R@20": "recall.20",
    "MRR": "recip_rank",
}

#: trec_eval's own output key for each canonical name.
_OUTPUT_KEY: dict[str, str] = {
    "nDCG@5": "ndcg_cut_5",
    "nDCG@10": "ndcg_cut_10",
    "nDCG@20": "ndcg_cut_20",
    "R@5": "recall_5",
    "R@10": "recall_10",
    "R@20": "recall_20",
    "MRR": "recip_rank",
}


@dataclass(frozen=True)
class RunDoc:
    """One retrieved document, with the fields the tie-break sorts on.

    ``doc_id`` is what the evaluator sees; the other three fields are what the
    *harness* orders by, so the evaluator's id-based tie-break can never reach a
    result. ``doc_type`` is the Stage 3 discriminator (D1): ``chunk`` today,
    ``digest`` later.
    """

    doc_id: str
    score: float
    doc_type: str = "chunk"
    file_uuid: str = ""
    chunk_index: int = 0

    def tie_key(self) -> tuple[float, str, str, int]:
        return (-self.score, self.doc_type, self.file_uuid, self.chunk_index)


@dataclass
class EvalResult:
    """Per-query and aggregate metric values, plus the denominator used."""

    per_query: dict[str, dict[str, float]] = field(default_factory=dict)
    aggregate: dict[str, float] = field(default_factory=dict)
    query_count: int = 0
    unanswered: list[str] = field(default_factory=list)


def normalise_run(run: dict[str, list[RunDoc]]) -> dict[str, dict[str, float]]:
    """Re-rank each query's documents deterministically, id-scheme-blind.

    Args:
        run: Query id -> retrieved documents, in any order.

    Returns:
        Query id -> ``{doc_id: score}`` with strictly decreasing scores in the
        harness's chosen order. Ties in the original score are resolved by
        ``(doc_type, file_uuid, chunk_index)`` ascending, which is stable and
        carries no information about how a document is *named*.
    """
    normalised: dict[str, dict[str, float]] = {}
    for query_id, docs in run.items():
        ordered = sorted(docs, key=lambda doc: doc.tie_key())
        total = len(ordered)
        normalised[query_id] = {
            doc.doc_id: float(total - position) for position, doc in enumerate(ordered)
        }
    return normalised


def evaluate(
    qrels: dict[str, dict[str, int]],
    run: dict[str, list[RunDoc]],
    *,
    measures: dict[str, str] | None = None,
) -> EvalResult:
    """Score ``run`` against ``qrels`` with ``trec_eval -c`` semantics.

    Args:
        qrels: Query id -> ``{doc_id: graded_relevance}``. Defines the query set:
            every query here contributes to the mean, answered or not.
        run: Query id -> retrieved documents. Queries absent from ``qrels`` are
            ignored (they cannot be scored).
        measures: Canonical-name -> trec_eval measure string. Defaults to
            :data:`MEASURES`.

    Returns:
        An :class:`EvalResult` whose ``query_count`` equals ``len(qrels)``.

    Raises:
        ValueError: If ``qrels`` is empty, or the scored denominator does not
            equal the qrels query count — the assertion that makes the zero
            substitution above verifiable rather than merely intended.
    """
    import pytrec_eval

    if not qrels:
        raise ValueError("Refusing to evaluate against empty qrels")

    wanted = measures or MEASURES
    evaluator = pytrec_eval.RelevanceEvaluator(qrels, set(wanted.values()))
    raw: dict[str, dict[str, float]] = evaluator.evaluate(normalise_run(run))

    result = EvalResult(query_count=len(qrels))
    for query_id in sorted(qrels):
        scored = raw.get(query_id)
        if scored is None:
            # trec_eval's -c: a query the run did not answer scores zero. It is
            # NOT dropped -- dropping it turns "returned nothing" into "no data".
            result.unanswered.append(query_id)
            result.per_query[query_id] = dict.fromkeys(wanted, 0.0)
            continue
        result.per_query[query_id] = {
            name: float(scored.get(_OUTPUT_KEY[name], 0.0)) for name in wanted
        }

    if len(result.per_query) != len(qrels):
        raise ValueError(
            f"Scored {len(result.per_query)} queries against {len(qrels)} in qrels — "
            "the zero substitution did not cover the query set"
        )

    for name in wanted:
        values = [row[name] for row in result.per_query.values()]
        result.aggregate[name] = sum(values) / len(values)
    return result


def subset(result: EvalResult, query_ids: set[str]) -> EvalResult:
    """Re-aggregate ``result`` over a subset of its queries (e.g. one class).

    The per-query vectors are already computed with the zero substitution, so a
    class mean is a mean over that class's *whole* query set, not over the ones
    it happened to answer.
    """
    rows = {qid: row for qid, row in result.per_query.items() if qid in query_ids}
    out = EvalResult(
        per_query=rows,
        query_count=len(rows),
        unanswered=[qid for qid in result.unanswered if qid in query_ids],
    )
    if not rows:
        return out
    for name in next(iter(rows.values())):
        out.aggregate[name] = sum(row[name] for row in rows.values()) / len(rows)
    return out


def measure_provenance() -> dict[str, Any]:
    """Name the implementation that produced every number, for the results file.

    ``ir_measures`` is used narrowly and deliberately: it parses the canonical
    measure strings we print, so "nDCG@10" in a table is checkably the same
    measure trec_eval computed, rather than a label we chose. Its optional
    providers are never installed or used.
    """
    import ir_measures
    import pytrec_eval

    parsed = {}
    for name in MEASURES:
        try:
            parsed[name] = str(ir_measures.parse_measure(name))
        except Exception:  # noqa: BLE001 - provenance must not break a measurement
            parsed[name] = "unparsed"

    return {
        "engine": "pytrec_eval_terrier",
        "engine_version": getattr(pytrec_eval, "__version__", "unknown"),
        "measure_strings": dict(MEASURES),
        "canonical_names": parsed,
        "gain": "linear (gain = rel)",
        "ndcg_variant": "ndcg_cut (ideal DCG truncated at k)",
        "unanswered_query_policy": "scored 0 and included in the mean (trec_eval -c)",
        "tie_break": "harness re-sort by (-score, doc_type, file_uuid, chunk_index)",
    }
