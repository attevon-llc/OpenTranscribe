"""Metric-engine tests: the three behaviours that are not the library default.

Each test here corresponds to a measured way a retrieval benchmark lies:
document-id tie-breaks that reward a naming convention, unanswered queries that
vanish from the mean, and a subset mean taken over the wrong denominator.

The metric engine is an eval-only dependency for licence reasons
(``backend/requirements-eval.txt``), so this module skips explicitly when it is
absent rather than passing on a stand-in.
"""

from __future__ import annotations

import pytest

pytest.importorskip(
    "pytrec_eval",
    reason="pytrec_eval_terrier is eval-only (licence); pip install -r requirements-eval.txt",
)

from tests.eval.harness.metrics import MEASURES  # noqa: E402
from tests.eval.harness.metrics import RunDoc  # noqa: E402
from tests.eval.harness.metrics import evaluate  # noqa: E402
from tests.eval.harness.metrics import normalise_run  # noqa: E402
from tests.eval.harness.metrics import subset  # noqa: E402

FILE = "3f2a9c10-0000-0000-0000-000000000000"


def _tied_pool(id_for) -> list[RunDoc]:
    """One digest plus twelve chunks of the same file, ALL at the same score.

    RRF produces exactly this: the fused score is a sum of ``1/(k + rank)`` over
    integer ranks, so documents appearing at the same rank in different legs get
    bit-identical scores.
    """
    docs = [
        RunDoc(
            doc_id=id_for("digest", -1),
            score=0.5,
            doc_type="digest",
            file_uuid=FILE,
            chunk_index=-1,
        )
    ]
    docs += [
        RunDoc(
            doc_id=id_for("chunk", index),
            score=0.5,
            doc_type="chunk",
            file_uuid=FILE,
            chunk_index=index,
        )
        for index in range(12)
    ]
    return docs


def _scheme_production(kind: str, index: int) -> str:
    """The ids this app actually uses, imported rather than restated.

    Stage 3 shipped `{uuid}_digest_{n}` — sectioned, so the section number is part
    of the id. Spelling it by hand here once meant the invariance test guarded a
    convention the app had stopped using; reading it from the module that mints
    the ids is what keeps this honest as the scheme evolves.
    """
    from app.services.ingest_artifacts.index_mapping import digest_document_id

    if kind == "digest":
        # index is the negative chunk_index sentinel; section 0 is -1.
        return digest_document_id(FILE, -1 - index)
    return f"{FILE}_{index}"


def _scheme_swapped(kind: str, index: int) -> str:
    """A different convention with the opposite lexicographic ordering."""
    return f"{FILE}_000-summary" if kind == "digest" else f"{FILE}_chunk~{index:04d}"


def test_tie_break_is_invariant_to_document_id_naming():
    """The single most important correctness property of the harness.

    Stage 3 introduces `{uuid}_digest` documents in the same stage whose gate is
    "nDCG@10 up on the multi-file class". `d` outsorts every digit and trec_eval
    breaks ties by docid DESCENDING, so without normalisation that gate could be
    passed by the naming convention alone.
    """
    results = []
    for scheme in (_scheme_production, _scheme_swapped):
        gold = scheme("chunk", 3)
        qrels = {"q1": {gold: 2}}
        run = {"q1": _tied_pool(scheme)}
        results.append(evaluate(qrels, run).aggregate)

    assert results[0] == results[1], (
        "The metric moved when only the document id convention changed — the "
        f"tie-break normalisation is not doing its job: {results}"
    )


def test_docid_tie_break_would_have_moved_the_metric_without_normalisation():
    """Guard for the test above: prove the hazard is real, not hypothetical.

    Without :func:`normalise_run` the same input scores differently under the two
    id conventions. If this test ever stops failing-to-be-equal, the invariance
    test above has become vacuous.
    """
    import pytrec_eval

    scores = []
    for scheme in (_scheme_production, _scheme_swapped):
        gold = scheme("chunk", 3)
        raw_run = {"q1": {doc.doc_id: doc.score for doc in _tied_pool(scheme)}}
        evaluator = pytrec_eval.RelevanceEvaluator({"q1": {gold: 2}}, {"ndcg_cut.10"})
        scores.append(evaluator.evaluate(raw_run)["q1"]["ndcg_cut_10"])

    assert scores[0] != scores[1], (
        "trec_eval's raw docid tie-break no longer distinguishes these two "
        f"conventions ({scores}); the invariance test above now proves nothing."
    )


def test_normalise_run_emits_strictly_decreasing_scores():
    normalised = normalise_run({"q1": _tied_pool(_scheme_production)})["q1"]
    ordered = sorted(normalised.items(), key=lambda item: -item[1])
    values = [score for _, score in ordered]
    assert values == sorted(values, reverse=True)
    assert len(set(values)) == len(values), "ties survived normalisation"
    # doc_type ascending puts 'chunk' before 'digest', so chunk 0 leads.
    assert ordered[0][0] == f"{FILE}_0"
    assert ordered[-1][0] == _scheme_production("digest", -1)


def test_unanswered_query_scores_zero_and_stays_in_the_denominator():
    qrels = {"q1": {"a_1": 1}, "q2": {"a_2": 1}}
    run = {"q1": [RunDoc(doc_id="a_1", score=1.0, file_uuid="a", chunk_index=1)]}

    result = evaluate(qrels, run)

    assert result.query_count == 2
    assert result.unanswered == ["q2"]
    assert result.per_query["q2"] == dict.fromkeys(MEASURES, 0.0)
    assert result.aggregate["nDCG@10"] == pytest.approx(0.5)


def test_naive_mean_over_answered_queries_flatters_the_result():
    """Why the substitution exists: the library's own output omits q2 entirely."""
    import pytrec_eval

    qrels = {"q1": {"a_1": 1}, "q2": {"a_2": 1}}
    raw = pytrec_eval.RelevanceEvaluator(qrels, {"ndcg_cut.10"}).evaluate({"q1": {"a_1": 1.0}})

    assert set(raw) == {"q1"}, "trec_eval started reporting unanswered queries"
    naive = sum(row["ndcg_cut_10"] for row in raw.values()) / len(raw)
    assert naive == pytest.approx(1.0)

    corrected = evaluate(qrels, {"q1": [RunDoc("a_1", 1.0, file_uuid="a", chunk_index=1)]})
    assert corrected.aggregate["nDCG@10"] == pytest.approx(0.5)
    assert corrected.aggregate["nDCG@10"] < naive


def test_evaluate_refuses_empty_qrels():
    with pytest.raises(ValueError, match="empty qrels"):
        evaluate({}, {})


def test_evaluate_ignores_run_queries_absent_from_qrels():
    qrels = {"q1": {"a_1": 1}}
    run = {
        "q1": [RunDoc("a_1", 1.0, file_uuid="a", chunk_index=1)],
        "ghost": [RunDoc("a_9", 1.0, file_uuid="a", chunk_index=9)],
    }
    result = evaluate(qrels, run)
    assert result.query_count == 1
    assert set(result.per_query) == {"q1"}


def test_subset_reaggregates_over_the_class_denominator():
    qrels = {"q1": {"a_1": 1}, "q2": {"a_2": 1}, "q3": {"a_3": 1}}
    run = {
        "q1": [RunDoc("a_1", 1.0, file_uuid="a", chunk_index=1)],
        "q3": [RunDoc("a_3", 1.0, file_uuid="a", chunk_index=3)],
    }
    result = evaluate(qrels, run)

    lookup = subset(result, {"q1", "q2"})
    assert lookup.query_count == 2
    assert lookup.unanswered == ["q2"]
    assert lookup.aggregate["nDCG@10"] == pytest.approx(0.5)

    summarize = subset(result, {"q3"})
    assert summarize.aggregate["nDCG@10"] == pytest.approx(1.0)


def test_graded_relevance_uses_linear_gain():
    """A 2 is worth exactly twice a 1. Pinned so a gain change cannot pass as a win."""
    qrels = {"q1": {"a_1": 2, "a_2": 1}}
    gold_first = evaluate(
        qrels,
        {
            "q1": [
                RunDoc("a_1", 2.0, file_uuid="a", chunk_index=1),
                RunDoc("a_2", 1.0, file_uuid="a", chunk_index=2),
            ]
        },
    ).aggregate["nDCG@5"]
    gold_second = evaluate(
        qrels,
        {
            "q1": [
                RunDoc("a_2", 2.0, file_uuid="a", chunk_index=2),
                RunDoc("a_1", 1.0, file_uuid="a", chunk_index=1),
            ]
        },
    ).aggregate["nDCG@5"]

    assert gold_first == pytest.approx(1.0)
    # DCG 1 + 2/log2(3) vs ideal 2 + 1/log2(3) -- exponential gain would give a
    # different number, which is what makes this an assertion and not a tautology.
    assert gold_second == pytest.approx((1 + 2 / 1.5849625007211562) / (2 + 1 / 1.5849625007211562))
