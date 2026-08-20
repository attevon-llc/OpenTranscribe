"""Tests for the deterministic answer-quality floor (#463) — no LLM, no provider.

``rouge-score``/``bert-score`` are eval-only optional deps (``backend/requirements-eval.txt``),
not in CI — every test here ``importorskip``s them, matching the repo's existing
``pytrec_eval`` convention.
"""

from __future__ import annotations

import pytest

pytest.importorskip("rouge_score", reason="rouge-score is eval-only; see requirements-eval.txt")

from tests.eval.harness.answer_text import MEASURES  # noqa: E402
from tests.eval.harness.answer_text import compute_bertscore  # noqa: E402
from tests.eval.harness.answer_text import evaluate_answer_text  # noqa: E402
from tests.eval.harness.answer_text import rouge_f_scores  # noqa: E402
from tests.eval.harness.answer_text import score_one  # noqa: E402
from tests.eval.harness.answer_text import subset_answer_text  # noqa: E402
from tests.eval.harness.answer_text import token_f1  # noqa: E402


class TestTokenF1:
    def test_identical_text_scores_1(self) -> None:
        assert token_f1("the cat sat on the mat", "the cat sat on the mat") == 1.0

    def test_disjoint_text_scores_0(self) -> None:
        assert token_f1("apples oranges", "zebra giraffe") == 0.0

    def test_partial_overlap_is_a_real_fraction(self) -> None:
        score = token_f1("the cat sat", "the cat sat on the mat")
        assert 0.0 < score < 1.0

    def test_case_and_punctuation_insensitive(self) -> None:
        assert token_f1("The Cat, Sat!", "the cat sat") == 1.0

    def test_both_empty_scores_1(self) -> None:
        assert token_f1("", "") == 1.0

    def test_one_empty_scores_0(self) -> None:
        assert token_f1("something", "") == 0.0
        assert token_f1("", "something") == 0.0


class TestRougeFScores:
    def test_identical_text_scores_1_on_both_measures(self) -> None:
        scores = rouge_f_scores(
            "the committee approved the budget", "the committee approved the budget"
        )
        assert scores["rouge1_f"] == pytest.approx(1.0)
        assert scores["rougeL_f"] == pytest.approx(1.0)

    def test_disjoint_text_scores_0(self) -> None:
        scores = rouge_f_scores("apples oranges bananas", "zebras giraffes elephants")
        assert scores["rouge1_f"] == 0.0
        assert scores["rougeL_f"] == 0.0

    def test_word_order_affects_rouge_l_more_than_rouge_1(self) -> None:
        """rougeL is a longest-common-SUBSEQUENCE measure — sensitive to order —
        while rouge1 is a bag-of-unigrams measure and is not."""
        gold = "the committee approved the new budget plan"
        reordered = "plan budget new the approved committee the"
        scores = rouge_f_scores(gold, reordered)
        assert scores["rouge1_f"] == pytest.approx(1.0)  # same multiset of words
        assert scores["rougeL_f"] < 1.0  # but the order is scrambled


class TestScoreOne:
    def test_declined_none_scores_everything_zero(self) -> None:
        assert score_one("gold text", None) == {
            "rougeL_f": 0.0,
            "rouge1_f": 0.0,
            "token_f1": 0.0,
            "answered": 0.0,
        }

    def test_declined_blank_string_scores_everything_zero(self) -> None:
        assert score_one("gold text", "   ")["answered"] == 0.0

    def test_answered_query_carries_every_measure(self) -> None:
        scores = score_one("the cat sat on the mat", "the cat sat on the mat")
        assert set(scores) == set(MEASURES)
        assert scores["answered"] == 1.0
        assert scores["rougeL_f"] == pytest.approx(1.0)


class TestEvaluateAnswerText:
    def test_unanswered_queries_are_scored_zero_and_counted(self) -> None:
        gold = {"q1": "the answer", "q2": "another answer"}
        submitted = {"q1": "the answer"}
        result = evaluate_answer_text(gold, submitted)
        assert result.query_count == 2
        assert result.unanswered == ["q2"]
        assert result.per_query["q2"]["answered"] == 0.0
        assert result.aggregate["answered"] == pytest.approx(0.5)

    def test_empty_gold_raises(self) -> None:
        with pytest.raises(ValueError, match="empty"):
            evaluate_answer_text({}, {})

    def test_extra_submitted_ids_outside_gold_are_ignored(self) -> None:
        gold = {"q1": "the answer"}
        submitted = {"q1": "the answer", "q-not-in-gold": "irrelevant"}
        result = evaluate_answer_text(gold, submitted)
        assert result.query_count == 1
        assert set(result.per_query) == {"q1"}

    def test_include_bertscore_adds_the_measure_when_requested(self) -> None:
        pytest.importorskip("bert_score", reason="bert-score is eval-only")
        gold = {"q1": "the committee approved the budget"}
        submitted = {"q1": "the committee approved the budget"}
        result = evaluate_answer_text(gold, submitted, include_bertscore=True)
        assert "bertscore_f1" in result.per_query["q1"]
        assert "bertscore_f1" in result.aggregate
        assert result.per_query["q1"]["bertscore_f1"] > 0.9  # near-identical text

    def test_bertscore_not_computed_unless_requested(self) -> None:
        gold = {"q1": "the committee approved the budget"}
        submitted = {"q1": "the committee approved the budget"}
        result = evaluate_answer_text(gold, submitted, include_bertscore=False)
        assert "bertscore_f1" not in result.per_query["q1"]
        assert "bertscore_f1" not in result.aggregate


class TestComputeBertscore:
    def test_length_mismatch_raises(self) -> None:
        pytest.importorskip("bert_score", reason="bert-score is eval-only")
        with pytest.raises(ValueError, match=r"1 golds vs 2 submissions"):
            compute_bertscore(["one gold"], ["one submission", "an extra one"])

    def test_empty_input_returns_empty_list(self) -> None:
        pytest.importorskip("bert_score", reason="bert-score is eval-only")
        assert compute_bertscore([], []) == []

    def test_a_decline_scores_0_without_reaching_the_model(self) -> None:
        pytest.importorskip("bert_score", reason="bert-score is eval-only")
        scores = compute_bertscore(["the gold text"], [""])
        assert scores == [0.0]

    def test_scores_are_in_order_and_match_length(self) -> None:
        pytest.importorskip("bert_score", reason="bert-score is eval-only")
        golds = ["the committee approved the budget", "completely unrelated text"]
        submissions = ["the committee approved the budget", "the committee approved the budget"]
        scores = compute_bertscore(golds, submissions)
        assert len(scores) == 2
        # Near-identical pair scores higher than the mismatched pair.
        assert scores[0] > scores[1]


class TestSubsetAnswerText:
    def test_reaggregates_over_a_subset(self) -> None:
        gold = {"q1": "same text here", "q2": "different text entirely"}
        submitted = {"q1": "same text here", "q2": "nothing matching at all"}
        result = evaluate_answer_text(gold, submitted)
        scoped = subset_answer_text(result, {"q1"})
        assert scoped.query_count == 1
        assert scoped.aggregate["token_f1"] == pytest.approx(1.0)

    def test_empty_subset_produces_no_aggregate(self) -> None:
        gold = {"q1": "text"}
        result = evaluate_answer_text(gold, {"q1": "text"})
        scoped = subset_answer_text(result, {"q-not-present"})
        assert scoped.query_count == 0
        assert scoped.aggregate == {}
