"""Tests for ``harness.attribution`` — SPEAKER_ATTR / SPEAKER_SUMMARY / ATTRIBUTION_PROBE
scoring (#461 W2.E1). No OpenSearch, Postgres, or LLM needed: pure scoring logic over
:class:`~tests.eval.harness.answers.Answer` and small submitted-answer dataclasses.
"""

from __future__ import annotations

import pytest

from tests.eval.harness.answers import Answer
from tests.eval.harness.attribution import SubmittedAttribution
from tests.eval.harness.attribution import SubmittedCoverage
from tests.eval.harness.attribution import evaluate_attribution
from tests.eval.harness.attribution import evaluate_attribution_probe
from tests.eval.harness.attribution import evaluate_speaker_summary
from tests.eval.harness.attribution import score_attribution_one
from tests.eval.harness.attribution import score_coverage_one
from tests.eval.harness.attribution import score_probe_one
from tests.eval.harness.attribution import subset_attribution


class TestScoreAttributionOne:
    def test_correct_speaker_and_citations_scores_all_ones(self) -> None:
        gold = Answer.speaker("Philip Blaker")
        submitted = SubmittedAttribution(
            speaker="Philip Blaker", citation_speakers=("Philip Blaker", "Philip Blaker")
        )
        scores = score_attribution_one(gold, submitted)
        assert scores == {
            "answer_names_gold_speaker": 1.0,
            "citation_speaker_match": 1.0,
            "answered": 1.0,
        }

    def test_right_name_wrong_citations_are_scored_separately_and_never_merged(self) -> None:
        """The whole point of the two-measure design: a model can name the right
        person while citing the wrong speaker's words as evidence, and that must
        be visible, not averaged away."""
        gold = Answer.speaker("Philip Blaker")
        submitted = SubmittedAttribution(
            speaker="Philip Blaker", citation_speakers=("Gareth Pierce",)
        )
        scores = score_attribution_one(gold, submitted)
        assert scores["answer_names_gold_speaker"] == 1.0
        assert scores["citation_speaker_match"] == 0.0

    def test_wrong_speaker_named(self) -> None:
        gold = Answer.speaker("Philip Blaker")
        submitted = SubmittedAttribution(
            speaker="Gareth Pierce", citation_speakers=("Gareth Pierce",)
        )
        scores = score_attribution_one(gold, submitted)
        assert scores["answer_names_gold_speaker"] == 0.0

    def test_name_comparison_is_case_and_whitespace_insensitive(self) -> None:
        gold = Answer.speaker("Philip Blaker")
        submitted = SubmittedAttribution(speaker="  philip   blaker ")
        scores = score_attribution_one(gold, submitted)
        assert scores["answer_names_gold_speaker"] == 1.0

    def test_partial_citation_match_is_a_fraction(self) -> None:
        gold = Answer.speaker("Philip Blaker")
        submitted = SubmittedAttribution(
            speaker="Philip Blaker",
            citation_speakers=("Philip Blaker", "Gareth Pierce", "Philip Blaker", "Gareth Pierce"),
        )
        scores = score_attribution_one(gold, submitted)
        assert scores["citation_speaker_match"] == pytest.approx(0.5)

    def test_no_citations_scores_zero_citation_match(self) -> None:
        gold = Answer.speaker("Philip Blaker")
        submitted = SubmittedAttribution(speaker="Philip Blaker", citation_speakers=())
        scores = score_attribution_one(gold, submitted)
        assert scores["citation_speaker_match"] == 0.0
        assert scores["answered"] == 1.0

    def test_none_submitted_scores_everything_zero(self) -> None:
        gold = Answer.speaker("Philip Blaker")
        scores = score_attribution_one(gold, None)
        assert scores == {
            "answer_names_gold_speaker": 0.0,
            "citation_speaker_match": 0.0,
            "answered": 0.0,
        }

    def test_rejects_a_gold_answer_of_the_wrong_kind(self) -> None:
        with pytest.raises(ValueError, match="gold.kind"):
            score_attribution_one(Answer.integer(3), None)


class TestScoreCoverageOne:
    def test_gold_speaker_covered(self) -> None:
        gold = Answer.speaker("Philip Blaker")
        submitted = SubmittedCoverage(covered_speakers=("Gareth Pierce", "Philip Blaker"))
        assert score_coverage_one(gold, submitted) == {"speaker_coverage": 1.0, "answered": 1.0}

    def test_gold_speaker_not_covered(self) -> None:
        gold = Answer.speaker("Philip Blaker")
        submitted = SubmittedCoverage(covered_speakers=("Gareth Pierce",))
        assert score_coverage_one(gold, submitted) == {"speaker_coverage": 0.0, "answered": 1.0}

    def test_none_submitted_scores_zero(self) -> None:
        gold = Answer.speaker("Philip Blaker")
        assert score_coverage_one(gold, None) == {"speaker_coverage": 0.0, "answered": 0.0}


class TestScoreProbeOne:
    def test_confirming_the_decoy_is_a_false_attribution(self) -> None:
        gold = Answer.attribution_probe("Philip Blaker", "Gareth Pierce")
        submitted = Answer.speaker("Gareth Pierce")
        scores = score_probe_one(gold, submitted)
        assert scores == {"false_attribution_rate": 1.0, "answered": 1.0}

    def test_naming_the_true_speaker_is_not_a_false_attribution(self) -> None:
        gold = Answer.attribution_probe("Philip Blaker", "Gareth Pierce")
        submitted = Answer.speaker("Philip Blaker")
        scores = score_probe_one(gold, submitted)
        assert scores == {"false_attribution_rate": 0.0, "answered": 1.0}

    def test_a_decline_is_not_a_false_positive(self) -> None:
        """A decline cannot be scored as confirming the decoy — that would punish
        caution as though it were an error."""
        gold = Answer.attribution_probe("Philip Blaker", "Gareth Pierce")
        scores = score_probe_one(gold, None)
        assert scores == {"false_attribution_rate": 0.0, "answered": 0.0}

    def test_rejects_a_gold_answer_of_the_wrong_kind(self) -> None:
        with pytest.raises(ValueError, match="gold.kind"):
            score_probe_one(Answer.speaker("x"), None)

    def test_rejects_a_submitted_answer_of_the_wrong_kind(self) -> None:
        gold = Answer.attribution_probe("Philip Blaker", "Gareth Pierce")
        with pytest.raises(ValueError, match="submitted.kind"):
            score_probe_one(gold, Answer.integer(1))


class TestEvaluateAttribution:
    def test_unanswered_queries_are_scored_zero_and_counted(self) -> None:
        gold = {"q1": Answer.speaker("Alice"), "q2": Answer.speaker("Bob")}
        submitted = {"q1": SubmittedAttribution(speaker="Alice", citation_speakers=("Alice",))}
        result = evaluate_attribution(gold, submitted)
        assert result.query_count == 2
        assert result.unanswered == ["q2"]
        assert result.per_query["q2"]["answer_names_gold_speaker"] == 0.0
        assert result.aggregate["answer_names_gold_speaker"] == pytest.approx(0.5)

    def test_extra_submitted_ids_outside_gold_are_ignored(self) -> None:
        gold = {"q1": Answer.speaker("Alice")}
        submitted = {
            "q1": SubmittedAttribution(speaker="Alice", citation_speakers=("Alice",)),
            "q-not-in-gold": SubmittedAttribution(speaker="Bob"),
        }
        result = evaluate_attribution(gold, submitted)
        assert result.query_count == 1
        assert set(result.per_query) == {"q1"}

    def test_empty_gold_raises(self) -> None:
        with pytest.raises(ValueError, match="empty"):
            evaluate_attribution({}, {})


class TestEvaluateSpeakerSummary:
    def test_basic_evaluation(self) -> None:
        gold = {"q1": Answer.speaker("Alice")}
        submitted = {"q1": SubmittedCoverage(covered_speakers=("Alice",))}
        result = evaluate_speaker_summary(gold, submitted)
        assert result.aggregate["speaker_coverage"] == 1.0


class TestEvaluateAttributionProbe:
    def test_basic_evaluation_mixed_true_and_false_positives(self) -> None:
        gold = {
            "p1": Answer.attribution_probe("Alice", "Bob"),
            "p2": Answer.attribution_probe("Alice", "Carol"),
        }
        submitted = {
            "p1": Answer.speaker("Bob"),  # false attribution
            "p2": Answer.speaker("Alice"),  # correct
        }
        result = evaluate_attribution_probe(gold, submitted)
        assert result.aggregate["false_attribution_rate"] == pytest.approx(0.5)


class TestSubsetAttribution:
    def test_reaggregates_over_a_subset(self) -> None:
        gold = {"q1": Answer.speaker("Alice"), "q2": Answer.speaker("Bob")}
        submitted = {
            "q1": SubmittedAttribution(speaker="Alice", citation_speakers=("Alice",)),
            "q2": SubmittedAttribution(speaker="Wrong", citation_speakers=("Wrong",)),
        }
        result = evaluate_attribution(gold, submitted)
        scoped = subset_attribution(result, {"q1"})
        assert scoped.query_count == 1
        assert scoped.aggregate["answer_names_gold_speaker"] == 1.0

    def test_empty_subset_produces_no_aggregate(self) -> None:
        gold = {"q1": Answer.speaker("Alice")}
        result = evaluate_attribution(gold, {})
        scoped = subset_attribution(result, {"q-not-present"})
        assert scoped.query_count == 0
        assert scoped.aggregate == {}
