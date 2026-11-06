"""Scorers for the summary quality gate: each must be able to fail."""

from __future__ import annotations

import json
from typing import Any

from tests.eval.harness.summary_scoring import CellScore
from tests.eval.harness.summary_scoring import GoldRef
from tests.eval.harness.summary_scoring import carry_through_recall
from tests.eval.harness.summary_scoring import extract_merged
from tests.eval.harness.summary_scoring import extract_slice
from tests.eval.harness.summary_scoring import gold_recall
from tests.eval.harness.summary_scoring import score_cell
from tests.eval.harness.summary_scoring import substantive_slices

PRICE = "The target selling price will be twenty five euros"
BATTERY = "The remote will use a solar battery cell"
YELLOW = "The remote casing will be yellow rubber"


def _slice(decisions: list[str], actions: list[str] | None = None, **extra: object) -> dict:
    return {
        "key_points": ["a point"],
        "decisions": decisions,
        "action_items": actions or [],
        **extra,
    }


class TestExtraction:
    def test_merged_fields_accept_strings_and_dicts_and_join_into_one_haystack(self) -> None:
        merged = extract_merged(
            {
                "bluf": "Launch agreed.",
                "brief_summary": "A planning meeting.",
                "major_topics": [{"topic": "Pricing", "key_points": ["Margins"]}, "Colour"],
                "key_decisions": ["Ship in May", {"text": "Price at 25"}],
                "action_items": [{"action": "Alice sends the budget"}],
                "follow_up_items": ["Review next week"],
            }
        )
        assert merged.key_decisions == ["Ship in May", "Price at 25"]
        assert merged.action_items == ["Alice sends the budget"]
        assert merged.topics == ["Pricing", "Margins", "Colour"]
        for needle in ("Launch agreed.", "Margins", "Alice sends the budget", "Review next week"):
            assert needle in merged.text

    def test_a_missing_summary_extracts_to_empty_fields_not_an_exception(self) -> None:
        assert extract_merged(None).text == ""
        assert extract_merged({"key_decisions": "not a list"}).key_decisions == ["not a list"]

    def test_slice_error_marker_is_detected_and_not_part_of_the_text(self) -> None:
        failed = extract_slice(2, {"_error": True, "key_points": ["Section 2: Processing failed"]})
        assert failed.errored
        assert "True" not in failed.text
        assert not extract_slice(1, _slice(["x y"])).errored

    def test_substantive_slices_drop_error_slices_and_the_tail(self) -> None:
        outputs = [
            _slice([PRICE]),
            {"_error": True, "key_points": ["Section 2: failed"]},
            _slice([BATTERY]),
            _slice(["Switch off these things please"]),
        ]
        kept = substantive_slices(outputs, tail_index=4)
        assert [s.index for s in kept] == [1, 3]


class TestCarryThroughRecall:
    def test_is_one_when_every_slice_decision_is_in_the_merged_text(self) -> None:
        slices = substantive_slices([_slice([PRICE], [YELLOW]), _slice([BATTERY])])
        merged = f"{PRICE}. {BATTERY}. {YELLOW}."
        result = carry_through_recall(merged, slices)
        assert (result.recalled, result.total, result.recall) == (3, 3, 1.0)
        assert result.missed == []

    def test_drops_below_one_and_names_the_item_when_one_decision_is_removed(self) -> None:
        slices = substantive_slices([_slice([PRICE]), _slice([BATTERY])])
        result = carry_through_recall(f"{PRICE}. Nothing else.", slices)
        assert result.recall == 0.5
        assert [o.text for o in result.missed] == [BATTERY]
        assert [o.key for o in result.missed] == ["s2.d1"]

    def test_excludes_error_slices_and_the_tail_slice_from_the_reference(self) -> None:
        outputs = [_slice([PRICE]), {"_error": True, "decisions": [BATTERY]}, _slice([YELLOW])]
        slices = substantive_slices(outputs, tail_index=3)
        result = carry_through_recall(PRICE, slices)
        assert (result.total, result.recall) == (1, 1.0)

    def test_counts_and_does_not_drop_items_under_the_minimum_token_count(self) -> None:
        slices = substantive_slices([_slice([PRICE, "Yellow"])])
        result = carry_through_recall(PRICE, slices)
        assert result.skipped == 1
        assert result.total == 1
        assert len(result.outcomes) == 2

    def test_no_items_is_none_not_zero(self) -> None:
        assert carry_through_recall("anything", []).recall is None


class TestGoldRecall:
    GOLD = [
        GoldRef("ES2008b", "decisions", "b.s.1", PRICE, 1),
        GoldRef("ES2008c", "decisions", "c.s.1", BATTERY, 2),
        GoldRef("ES2008c", "actions", "c.s.2", "Alice will post the minutes online", None),
    ]

    def test_reports_each_layer_each_meeting_and_each_slice_and_names_misses(self) -> None:
        result = gold_recall(f"{PRICE}. Alice will post the minutes online.", self.GOLD)
        assert result["layers"]["decisions"].recall == 0.5
        assert result["layers"]["actions"].recall == 1.0
        per_meeting = result["per_meeting"].by_group()
        assert per_meeting["ES2008b"]["recall"] == 1.0
        assert per_meeting["ES2008c"]["recall"] == 0.5
        assert per_meeting["ES2008c"]["missed"] == ["c.s.1"]
        per_slice = result["per_slice"].by_group()
        assert set(per_slice) == {"slice 1", "slice 2", "unlocated"}

    def test_a_layer_with_no_gold_items_is_absent_not_zero(self) -> None:
        only_decisions = [g for g in self.GOLD if g.layer == "decisions"]
        assert "actions" not in gold_recall("x", only_decisions)["layers"]


class TestScoreCell:
    SLICES = [_slice([PRICE]), _slice([BATTERY]), _slice(["Please switch off these things"])]
    GOLD = [GoldRef("Bed003", "decisions", "g1", PRICE, 1)]

    def _cell(self, merged: dict | None, status: str = "ok", **overrides: Any) -> CellScore:
        kwargs: dict[str, Any] = {
            "cell_id": "I1|old|0",
            "input_id": "I1",
            "arm": "old",
            "run": 0,
            "status": status,
            "void_reason": None,
            "merged": merged,
            "slice_outputs": self.SLICES,
            "gold": self.GOLD,
            "tail_index": 3,
        } | overrides
        return score_cell(**kwargs)

    def test_a_good_merge_scores_clean_on_every_criterion(self) -> None:
        cell = self._cell({"bluf": "Agreed.", "key_decisions": [PRICE, BATTERY]})
        assert cell.carry.recall == 1.0
        assert cell.gold_decisions is not None
        assert cell.gold_decisions.recall == 1.0
        assert not cell.fail_b_prefilter
        assert not cell.empty_result

    def test_a_merge_that_claims_truncation_fails_b_unless_a_slice_said_so(self) -> None:
        claim = {"bluf": "The transcript appears to be cut off.", "key_decisions": [PRICE, BATTERY]}
        assert self._cell(claim).fail_b_prefilter
        said = [_slice([PRICE], note="This is only a fragment."), _slice([BATTERY])]
        assert not self._cell(claim, slice_outputs=said).fail_b_prefilter

    def test_a_merge_that_keeps_only_the_tail_is_an_empty_result(self) -> None:
        cell = self._cell({"bluf": "Switch things off.", "key_decisions": []})
        assert cell.empty_result
        assert cell.empty_decisions
        assert cell.carry.recall == 0.0

    def test_an_error_cell_scores_zero_everywhere_and_counts_as_empty(self) -> None:
        cell = self._cell(None, status="error")
        assert cell.status == "error"
        assert cell.carry.recall == 0.0
        assert cell.gold_decisions is not None
        assert cell.gold_decisions.recall == 0.0
        assert cell.empty_result
        assert not cell.disclosed

    def test_a_void_cell_keeps_its_status_and_reason(self) -> None:
        cell = self._cell({"key_decisions": [PRICE]}, status="void", void_reason="arm_not_applied")
        assert (cell.status, cell.void_reason) == ("void", "arm_not_applied")

    def test_the_scores_row_carries_ids_and_counts_but_no_corpus_prose(self) -> None:
        cell = self._cell({"bluf": "Agreed.", "key_decisions": [PRICE]})
        row = json.dumps(cell.scores_row())
        assert "s2.d1" in row  # the missed BATTERY decision, by id
        assert BATTERY not in row
        assert PRICE not in row
