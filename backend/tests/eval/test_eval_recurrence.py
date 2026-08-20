"""Tests for ``harness.recurrence`` — planted-gold action-item groups (#461 W2.E1).

Two things this file exists to pin down: the planted item shape is the DEFAULT
SUMMARY PROMPT's ``action_items`` fields (verified against
``backend/app/core/default_prompts.py`` — see ``recurrence.py``'s module docstring for
the exact line numbers and why the alternative schema shape would have been wrong),
and the planter is fully deterministic so a committed baseline over it reproduces
byte-for-byte.
"""

from __future__ import annotations

import pytest

from tests.eval.harness.recurrence import PLANTED_FIELDS
from tests.eval.harness.recurrence import evaluate_recurrence
from tests.eval.harness.recurrence import plant_recurrence
from tests.eval.harness.recurrence import score_recurrence


class TestPlantedFieldsMatchTheDefaultPrompt:
    def test_planted_fields_are_exactly_the_prompt_shape_not_the_schema_shape(self) -> None:
        """`default_prompts.py`'s action_items block: item/owner/due_date/priority/
        context/mentioned_timestamp. `schemas/summary.py`'s ActionItem is a
        DIFFERENT, unused shape (text/assigned_to/due_date/priority/context/status)
        — planting that one would test a shape nothing in the app emits."""
        assert PLANTED_FIELDS == (
            "item",
            "owner",
            "due_date",
            "priority",
            "context",
            "mentioned_timestamp",
        )
        # Explicitly NOT present: the schema.py-only fields.
        assert "text" not in PLANTED_FIELDS
        assert "assigned_to" not in PLANTED_FIELDS
        assert "status" not in PLANTED_FIELDS

    def test_every_planted_item_carries_exactly_the_planted_fields(self) -> None:
        planted = plant_recurrence(["file-a", "file-b", "file-c"], seed=1)
        assert planted.items, "plant_recurrence produced no items — nothing to check"
        for item in planted.items:
            shape = item.as_prompt_shape()
            assert set(shape) == set(PLANTED_FIELDS)


class TestPlantRecurrenceDeterminism:
    def test_same_inputs_plant_byte_identical_output(self) -> None:
        files = ["file-a", "file-b", "file-c", "file-d"]
        first = plant_recurrence(files, seed=7)
        second = plant_recurrence(files, seed=7)
        assert first.items == second.items
        assert first.gold_groups == second.gold_groups

    def test_a_different_seed_plants_different_owners_or_priorities(self) -> None:
        files = ["file-a", "file-b", "file-c", "file-d", "file-e"]
        a = plant_recurrence(files, seed=1)
        b = plant_recurrence(files, seed=2)
        assert a.items != b.items

    def test_fewer_than_two_files_plants_nothing(self) -> None:
        planted = plant_recurrence(["file-a"], seed=0)
        assert planted.items == ()
        assert planted.gold_groups == {}

    def test_at_least_one_recurring_group_exists_over_enough_files(self) -> None:
        planted = plant_recurrence([f"file-{i}" for i in range(6)], seed=0)
        assert planted.gold_groups, "expected at least one recurring group over 6 files"
        for members in planted.gold_groups.values():
            assert len(members) >= 2, "a 'recurring' group must recur at least twice"

    def test_distractor_items_are_never_members_of_a_gold_group(self) -> None:
        planted = plant_recurrence([f"file-{i}" for i in range(6)], seed=0)
        recurring_ids = planted.recurring_item_ids
        distractor_ids = {item.item_id for item in planted.items if "distractor" in item.item_id}
        assert distractor_ids, "fixture produced no distractors to check"
        assert not (distractor_ids & recurring_ids)


class TestScoreRecurrence:
    def _gold(self) -> dict[str, frozenset[str]]:
        return {"g1": frozenset({"a", "b", "c"}), "g2": frozenset({"d", "e"})}

    def test_perfect_submission_scores_1_and_1(self) -> None:
        scores = score_recurrence(self._gold(), self._gold())
        assert scores == {"group_precision": 1.0, "group_recall": 1.0}

    def test_empty_submission_scores_zero_recall_and_vacuous_precision(self) -> None:
        scores = score_recurrence(self._gold(), {})
        assert scores == {"group_precision": 1.0, "group_recall": 0.0}

    def test_a_submitted_group_merging_two_gold_groups_costs_precision_not_recall(
        self,
    ) -> None:
        """Merging g1+g2 into one submitted group recovers every gold PAIR that was
        already co-grouped (recall unaffected) but also asserts new pairs across the
        two gold groups that are wrong (precision drops)."""
        merged = {"merged": frozenset({"a", "b", "c", "d", "e"})}
        scores = score_recurrence(self._gold(), merged)
        assert scores["group_recall"] == 1.0
        assert scores["group_precision"] < 1.0

    def test_a_submission_that_splits_a_gold_group_costs_recall_not_precision(self) -> None:
        split = {"g1a": frozenset({"a", "b"}), "g1b": frozenset({"c"}), "g2": frozenset({"d", "e"})}
        scores = score_recurrence(self._gold(), split)
        assert scores["group_precision"] == 1.0
        assert scores["group_recall"] < 1.0

    def test_disjoint_submission_scores_zero_on_both(self) -> None:
        scores = score_recurrence(self._gold(), {"x": frozenset({"z1", "z2"})})
        assert scores == {"group_precision": 0.0, "group_recall": 0.0}

    def test_empty_gold_raises(self) -> None:
        with pytest.raises(ValueError, match="empty"):
            score_recurrence({}, {"x": frozenset({"a", "b"})})

    def test_singleton_groups_contribute_no_pairs_either_side(self) -> None:
        """A group of size 1 has no pairs, so it can neither help nor hurt the
        pairwise score — the metric is silent about it rather than penalising it."""
        gold = {"g1": frozenset({"a"})}
        submitted = {"s1": frozenset({"a"})}
        # No pairs anywhere -> "empty submission" convention applies (vacuous 1.0
        # precision, 0.0 recall since gold_pairs is also empty... but recall's
        # denominator is 0, handled explicitly).
        scores = score_recurrence(gold, submitted)
        assert scores["group_recall"] == 0.0


class TestEvaluateRecurrence:
    def test_scopes_missing_from_submitted_are_scored_as_the_honest_floor(self) -> None:
        """The product does not build recurrence groups yet (module docstring) —
        a scope entirely absent from `submitted` must score exactly like an empty
        submission, never be silently dropped from the mean."""
        gold = {
            "scope-a": {"g1": frozenset({"a", "b"})},
            "scope-b": {"g1": frozenset({"c", "d"})},
        }
        result = evaluate_recurrence(gold, submitted={})
        assert result.query_count == 2
        assert result.aggregate["group_recall"] == 0.0

    def test_aggregate_is_the_mean_over_scopes(self) -> None:
        gold = {
            "scope-a": {"g1": frozenset({"a", "b"})},
            "scope-b": {"g1": frozenset({"c", "d"})},
        }
        submitted = {"scope-a": {"g1": frozenset({"a", "b"})}}  # perfect; scope-b absent
        result = evaluate_recurrence(gold, submitted)
        assert result.per_query["scope-a"]["group_recall"] == 1.0
        assert result.per_query["scope-b"]["group_recall"] == 0.0
        assert result.aggregate["group_recall"] == pytest.approx(0.5)

    def test_empty_gold_raises(self) -> None:
        with pytest.raises(ValueError, match="empty"):
            evaluate_recurrence({}, {})
