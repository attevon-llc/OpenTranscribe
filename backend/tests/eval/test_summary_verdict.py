"""The verdict rules over synthetic scored records: every outcome and every VOID cause."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

import pytest

from tests.eval.harness.significance import PartialJoinError
from tests.eval.harness.summary_verdict import INCONCLUSIVE
from tests.eval.harness.summary_verdict import PASS
from tests.eval.harness.summary_verdict import REGRESSION
from tests.eval.harness.summary_verdict import VOID
from tests.eval.harness.summary_verdict import Cell
from tests.eval.harness.summary_verdict import cell_from_row
from tests.eval.harness.summary_verdict import decide
from tests.eval.harness.summary_verdict import gate_matrix
from tests.eval.harness.summary_verdict import recall_comparison

RUNS = 5
MATRIX = gate_matrix(RUNS)


def _wobble(input_id: str, run: int) -> float:
    """Deterministic run-to-run noise that cancels exactly between arms that share it."""
    return 0.01 * ((len(input_id) + run) % 3 - 1)


def _row(input_id: str, arm: str, run: int, **over: Any) -> dict[str, Any]:
    """A healthy scored row: recall near 0.8, no (b)/(c) failure, I4 discloses its failure."""
    carry = 0.80 + _wobble(input_id, run)
    row: dict[str, Any] = {
        "input": input_id,
        "arm": arm,
        "run": run,
        "status": "ok",
        "void_reason": None,
        "carry": {"recall": carry},
        "gold_decisions": {"recall": 0.6 + _wobble(input_id, run)},
        "gold_actions": None if input_id in ("I1", "I2") else {"recall": 0.5},
        "b": {"fail": False},
        "c": {"fail": False},
        "d": {"disclosed": input_id.startswith("I4"), "absence_claim": False},
    }
    row.update(over)
    return row


def _world(tweak: Callable[[dict[str, Any]], None] | None = None) -> list[dict[str, Any]]:
    """A full healthy matrix; ``sabotage`` fails (b) and (c) on its inputs and recalls little."""
    rows = []
    for (input_id, arm), runs in sorted(MATRIX.items()):
        for run in range(runs):
            row = _row(input_id, arm, run)
            if arm == "sabotage":
                row["carry"] = {"recall": 0.1}
                row["b"] = {"fail": True}
                row["c"] = {"fail": True}
            if tweak:
                tweak(row)
            rows.append(row)
    return rows


def _verdict(rows: list[dict[str, Any]], **kw: Any) -> dict[str, Any]:
    return decide(rows, matrix=MATRIX, **kw)


def _is_cell(arm: str, input_id: str, run: int) -> Callable[[dict[str, Any]], bool]:
    def match(row: dict[str, Any]) -> bool:
        return (row["arm"], row["input"], row["run"]) == (arm, input_id, run)

    return match


def _set(rows: list[dict[str, Any]], match: Callable[[dict[str, Any]], bool], **over: Any) -> None:
    for row in rows:
        if match(row):
            row.update(over)


class TestOutcomes:
    def test_identical_arms_pass_and_report_nothing_improved(self) -> None:
        verdict = _verdict(_world())
        assert verdict["verdict"] == PASS
        assert verdict["hits"] == []
        assert verdict["improved"] == []

    def test_a_clearly_better_new_arm_passes_and_is_reported_as_improved(self) -> None:
        rows = _world()
        _set(rows, lambda r: r["arm"] == "new", carry={"recall": 0.95})
        verdict = _verdict(rows)
        assert verdict["verdict"] == PASS
        assert verdict["improved"] == ["carry"]

    def test_rule_a_a_clearly_lower_carry_through_recall_is_a_regression(self) -> None:
        rows = _world()
        _set(rows, lambda r: r["arm"] == "new", carry={"recall": 0.5})
        verdict = _verdict(rows)
        assert verdict["verdict"] == REGRESSION
        assert [h["metric"] for h in verdict["hits"] if h["rule"] == "a"] == ["carry"]

    def test_rule_a_gold_recall_is_gated_separately_from_carry_through(self) -> None:
        rows = _world()
        _set(rows, lambda r: r["arm"] == "new" and r["gold_actions"], gold_actions={"recall": 0.1})
        verdict = _verdict(rows)
        assert verdict["verdict"] == REGRESSION
        assert [h["metric"] for h in verdict["hits"]] == ["gold_actions"]

    def test_rule_b_one_more_truncation_failure_than_old_on_any_input_regresses(self) -> None:
        rows = _world()
        _set(rows, lambda r: (r["arm"], r["input"], r["run"]) == ("new", "I3", 2), b={"fail": True})
        verdict = _verdict(rows)
        assert verdict["verdict"] == REGRESSION
        assert [(h["rule"], h["input"]) for h in verdict["hits"]] == [("b", "I3")]

    def test_rule_b_equal_failures_in_both_arms_are_not_a_regression(self) -> None:
        rows = _world()
        _set(
            rows,
            lambda r: r["arm"] in ("old", "new", "old-aa") and r["input"] == "I3" and r["run"] == 2,
            b={"fail": True},
        )
        assert _verdict(rows)["verdict"] == PASS

    def test_rule_c_is_absolute_one_empty_result_for_new_regresses_even_if_old_has_one_too(
        self,
    ) -> None:
        rows = _world()
        for arm in ("old", "new", "old-aa"):
            _set(rows, _is_cell(arm, "I1", 0), c={"fail": True})
        verdict = _verdict(rows)
        assert verdict["verdict"] == REGRESSION
        assert [h["rule"] for h in verdict["hits"]] == ["c"]

    def test_rule_d_disclosing_the_failed_slice_in_fewer_runs_regresses(self) -> None:
        rows = _world()
        _set(
            rows,
            lambda r: r["arm"] == "new" and r["input"] == "I4" and r["run"] < 2,
            d={"disclosed": False, "absence_claim": False},
        )
        verdict = _verdict(rows)
        assert verdict["verdict"] == REGRESSION
        assert ("d", "discloses_less") in {(h["rule"], h["kind"]) for h in verdict["hits"]}

    def test_rule_d_claiming_nothing_happened_in_more_runs_regresses(self) -> None:
        rows = _world()
        _set(
            rows,
            lambda r: r["arm"] == "new" and r["input"] == "I4" and r["run"] == 0,
            d={"disclosed": True, "absence_claim": True},
        )
        verdict = _verdict(rows)
        assert verdict["verdict"] == REGRESSION
        assert ("d", "absence_claim_more") in {(h["rule"], h["kind"]) for h in verdict["hits"]}

    def test_a_noisy_interval_that_straddles_zero_but_reaches_below_the_margin_is_inconclusive(
        self,
    ) -> None:
        rows = _world()
        new_rows = [r for r in rows if r["arm"] == "new" and r["input"] in ("I1", "I2", "I3")]
        for i, row in enumerate(new_rows):
            row["carry"] = {"recall": row["carry"]["recall"] + (0.15 if i % 2 else -0.16)}
        verdict = _verdict(rows)
        assert verdict["verdict"] == INCONCLUSIVE
        assert verdict["reasons"] == ["ci_lower_below_margin:carry"]

    def test_the_margin_is_widened_to_the_aa_noise_and_recorded(self) -> None:
        rows = _world()
        aa_rows = [r for r in rows if r["arm"] == "old-aa" and r["input"] in ("I1", "I2", "I3")]
        for i, row in enumerate(aa_rows):
            row["carry"] = {"recall": row["carry"]["recall"] + (0.2 if i % 2 else -0.2)}
        verdict = _verdict(rows)
        floor = verdict["noise_floor"]
        assert floor["aa_half_width"] > 0.05
        assert floor["effective_margin"] == floor["aa_half_width"]
        assert verdict["params"]["margin"] == 0.05


class TestVoid:
    def test_a_missing_cell_voids_the_run_and_no_comparison_is_made(self) -> None:
        rows = [r for r in _world() if (r["input"], r["arm"], r["run"]) != ("I2", "new", 3)]
        verdict = _verdict(rows)
        assert verdict["verdict"] == VOID
        assert verdict["reasons"] == ["missing_cell"]
        assert verdict["void"][0]["cell"] == ["I2", "new", 3]
        assert "comparison" not in verdict

    def test_a_failed_applied_check_voids_the_run(self) -> None:
        rows = _world()
        _set(
            rows,
            lambda r: (r["arm"], r["input"], r["run"]) == ("new", "I1", 0),
            status="void",
            void_reason="arm_not_applied",
        )
        verdict = _verdict(rows)
        assert verdict["verdict"] == VOID
        assert verdict["void"][0]["reason"] == "arm_not_applied"

    def test_a_duplicate_cell_voids_the_run(self) -> None:
        rows = _world()
        rows.append(dict(rows[0]))
        assert _verdict(rows)["reasons"] == ["duplicate_cell"]

    def test_a_cell_outside_the_matrix_voids_the_run(self) -> None:
        rows = _world() + [_row("I9", "new", 0)]
        assert _verdict(rows)["reasons"] == ["unexpected_cell"]

    def test_a_negative_control_that_is_not_flagged_voids_the_run(self) -> None:
        rows = _world()
        _set(
            rows,
            lambda r: r["arm"] == "sabotage",
            carry={"recall": 0.8},
            b={"fail": False},
            c={"fail": False},
        )
        verdict = _verdict(rows)
        assert verdict["verdict"] == VOID
        assert verdict["reasons"] == ["sabotage_not_flagged"]

    def test_an_aa_control_that_trips_a_rule_voids_the_run(self) -> None:
        rows = _world()
        _set(rows, lambda r: r["arm"] == "old-aa" and r["input"] == "I3", b={"fail": True})
        verdict = _verdict(rows)
        assert verdict["verdict"] == VOID
        assert verdict["reasons"] == ["aa_flagged"]

    def test_a_run_without_the_control_arms_is_not_a_gate_result(self) -> None:
        small = {k: v for k, v in MATRIX.items() if k[1] in ("old", "new")}
        rows = [r for r in _world() if (r["input"], r["arm"]) in small]
        verdict = decide(rows, matrix=small)
        assert verdict["verdict"] == VOID
        assert verdict["reasons"] == ["controls_missing"]
        assert {v["arm"] for v in verdict["void"]} == {"sabotage", "old-aa"}

    def test_a_smoke_run_may_waive_the_controls_and_still_compare(self) -> None:
        small = {k: v for k, v in MATRIX.items() if k[1] in ("old", "new")}
        rows = [r for r in _world() if (r["input"], r["arm"]) in small]
        verdict = decide(rows, matrix=small, require_controls=False)
        assert verdict["verdict"] == PASS
        assert verdict["params"]["require_controls"] is False

    def test_void_takes_precedence_over_a_regression(self) -> None:
        rows = _world()
        _set(rows, lambda r: r["arm"] == "new", carry={"recall": 0.2})
        rows = [r for r in rows if (r["input"], r["arm"], r["run"]) != ("I1", "old", 0)]
        assert _verdict(rows)["verdict"] == VOID


class TestPairing:
    def test_a_cell_with_no_counterpart_in_the_control_raises_instead_of_shrinking_the_sample(
        self,
    ) -> None:
        cells = {
            c.key: c
            for c in (cell_from_row(r) for r in _world())
            if (c.input_id, c.arm, c.run) != ("I1", "old", 1)
        }
        with pytest.raises(PartialJoinError):
            recall_comparison(cells, "new", "old")

    def test_a_metric_missing_on_one_side_only_raises(self) -> None:
        cells: dict[tuple[str, str, int], Cell] = {c.key: c for c in map(cell_from_row, _world())}
        broken = cells[("I3", "new", 0)]
        cells[broken.key] = Cell(
            broken.input_id,
            broken.arm,
            broken.run,
            broken.status,
            None,
            {**broken.metrics, "gold_actions": None},
            False,
            False,
            True,
            False,
        )
        with pytest.raises(PartialJoinError):
            recall_comparison(cells, "new", "old")

    def test_a_metric_missing_on_both_sides_is_skipped_not_scored_as_zero(self) -> None:
        cells = {c.key: c for c in map(cell_from_row, _world())}
        comparison = recall_comparison(cells, "new", "old", inputs=("I1", "I2"))
        assert set(comparison) == {"carry", "gold_decisions"}

    def test_the_judged_labels_replace_the_prefilter_when_asked(self) -> None:
        row = _row("I1", "new", 0)
        row["judged"] = {"b_fail": True, "c_fail": True, "disclosed": True, "absence_claim": True}
        plain, judged = cell_from_row(row), cell_from_row(row, use_judge=True)
        assert (plain.fail_b, plain.fail_c) == (False, False)
        assert (judged.fail_b, judged.fail_c, judged.absence_claim) == (True, True, True)
