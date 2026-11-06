"""Every prefilter must be able to fire AND be able to stay quiet.

A detector that matches nothing reports 0 findings, which looks exactly like a clean suite, so
each one has a must-fire and a must-stay-clean case (the auditor rule in the root CLAUDE.md).
"""

from __future__ import annotations

import pytest

from tests.eval.harness.summary_detectors import FailureDisclosure
from tests.eval.harness.summary_detectors import empty_result
from tests.eval.harness.summary_detectors import failure_disclosure
from tests.eval.harness.summary_detectors import nothing_decided_claims
from tests.eval.harness.summary_detectors import split_sentences
from tests.eval.harness.summary_detectors import truncation_claims


class TestTruncationClaims:
    @pytest.mark.parametrize(
        "sentence",
        [
            "The transcript appears to be cut off.",
            "This is only a fragment.",
            "The recording ends abruptly.",
            "The meeting seems to be incomplete.",
            "The discussion starts mid-sentence.",
        ],
    )
    def test_fires_on_a_claim_about_the_recording(self, sentence: str) -> None:
        assert truncation_claims(f"The team met. {sentence} Budgets were set.") == [sentence]

    @pytest.mark.parametrize(
        "sentence",
        [
            "The meeting ended with a short wrap-up.",
            "The team cut the budget.",
            "An incomplete prototype was shown.",
            "In the meeting an incomplete prototype was shown.",
        ],
    )
    def test_stays_quiet_about_content_that_merely_uses_the_words(self, sentence: str) -> None:
        assert truncation_claims(sentence) == []

    def test_returns_only_the_offending_sentences_in_order(self) -> None:
        text = "It opened. The recording ends abruptly. Then a fragment follows. This is only a fragment."
        assert truncation_claims(text) == [
            "The recording ends abruptly.",
            "This is only a fragment.",
        ]


class TestNothingDecidedClaims:
    @pytest.mark.parametrize(
        "sentence",
        [
            "No decisions were made.",
            "The team did not reach any decisions.",
            "Nothing was decided.",
            "There were no decisions or action items.",
        ],
    )
    def test_fires_on_a_decision_free_meeting(self, sentence: str) -> None:
        assert nothing_decided_claims(sentence) == [sentence]

    def test_stays_quiet_on_a_decision_free_topic_inside_a_productive_meeting(self) -> None:
        sentence = "No decision on colour yet, but the team agreed to ship in May."
        assert nothing_decided_claims(sentence) == []


class TestEmptyResult:
    def test_fires_when_the_merge_dropped_every_decision_a_slice_reported(self) -> None:
        assert empty_result(
            merged_decisions=[], slice_decisions=["Ship in May"], merged_prose="The team met."
        )

    def test_fires_when_the_prose_claims_nothing_was_decided(self) -> None:
        assert empty_result(
            merged_decisions=["Ship in May"],
            slice_decisions=["Ship in May"],
            merged_prose="No decisions were made.",
        )

    def test_stays_quiet_when_decisions_survived_the_merge(self) -> None:
        assert not empty_result(
            merged_decisions=["Ship in May"],
            slice_decisions=["Ship in May"],
            merged_prose="No decision on colour yet, but the team agreed to ship in May.",
        )

    def test_an_empty_merge_is_fine_when_no_slice_decided_anything(self) -> None:
        assert not empty_result(
            merged_decisions=[], slice_decisions=[], merged_prose="A planning discussion."
        )


class TestFailureDisclosure:
    @pytest.mark.parametrize(
        ("text", "disclosed", "absence"),
        [
            ("The third section could not be processed, so its content is missing.", True, False),
            ("Section 3 failed to process; nothing was discussed in that part.", True, True),
            ("The team agreed on a yellow remote.", False, False),
            ("Nothing was discussed in the third section.", False, True),
        ],
    )
    def test_all_four_combinations(self, text: str, disclosed: bool, absence: bool) -> None:
        assert failure_disclosure(text) == FailureDisclosure(disclosed, absence)

    def test_only_a_disclosure_without_an_absence_claim_passes_criterion_d(self) -> None:
        assert FailureDisclosure(True, False).passes
        assert not FailureDisclosure(True, True).passes
        assert not FailureDisclosure(False, False).passes
        assert not FailureDisclosure(False, True).passes


def test_split_sentences_drops_empties_and_splits_on_newlines() -> None:
    assert split_sentences("One. Two!\n\nThree?  ") == ["One.", "Two!", "Three?"]
    assert split_sentences("") == []
