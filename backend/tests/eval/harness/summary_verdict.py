"""The statistics and decision rules of the summary-merge quality gate (plan sections 5.5, 5.6).

One question: is the candidate rule set at least as good as the control on the four behaviours
the rules protect? The answer is one of ``PASS``, ``REGRESSION``, ``INCONCLUSIVE`` or ``VOID``.

**VOID is not a soft failure.** It means the run measured nothing usable (a cell is missing, an
arm was never applied, the negative control was not caught, or the A/A control tripped a rule),
and the numbers must not be read. It is decided first, before any comparison of ``new`` to ``old``.

Reused, not rebuilt: the paired join (refuses partial joins and duplicates), the seeded paired
bootstrap CI and the paired t-test come from ``significance``. "Equal to or better" is framed as
non-inferiority (FDA, *Non-Inferiority Clinical Trials to Establish Effectiveness*, 2016): the
candidate is called worse only when the CI of (new - old) sits below zero AND its lower bound is
below ``-margin``.

Every threshold is a parameter and is written into the returned document (``params``). Binary
criteria (b), (c), (d) are hard counting rules, not tests: with N = 5 no test has the power to see
a small difference, and (c) is the bug the rules exist to prevent.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Iterable
from collections.abc import Mapping
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

from tests.eval.harness.significance import paired_bootstrap_ci
from tests.eval.harness.significance import paired_join
from tests.eval.harness.significance import paired_ttest

DEFAULT_MARGIN = 0.05
#: Continuous metrics for criterion (a). Gold recall is the anchor that does not depend on the
#: arm's own slices; carry-through recall is measured against them.
RECALL_METRICS = ("carry", "gold_decisions", "gold_actions")
A_INPUTS = ("I1", "I2", "I3")
B_INPUTS = ("I1", "I2", "I3")
C_INPUTS = ("I1", "I2")
D_INPUTS = ("I4",)

PASS = "PASS"
REGRESSION = "REGRESSION"
INCONCLUSIVE = "INCONCLUSIVE"
VOID = "VOID"


@dataclass(frozen=True)
class Cell:
    """One scored (input, arm, run) summary, reduced to what the verdict reads."""

    input_id: str
    arm: str
    run: int
    status: str  # "ok" | "error" | "void"
    void_reason: str | None
    metrics: Mapping[str, float | None]  # keys of RECALL_METRICS
    fail_b: bool
    fail_c: bool
    disclosed: bool
    absence_claim: bool

    @property
    def key(self) -> tuple[str, str, int]:
        return (self.input_id, self.arm, self.run)


def cell_from_row(row: Mapping[str, Any], *, use_judge: bool = False) -> Cell:
    """Build a :class:`Cell` from a ``CellScore.scores_row()``.

    When ``use_judge`` is true and the row carries a ``judged`` block, the judged labels replace
    the regex prefilters for (b), (c) and (d); the lexical recall floor is kept unless the judge
    also supplies recall values (only allowed once calibration validated it, plan 5.3).
    """
    judged = row.get("judged") if use_judge else None
    judged = judged or {}

    def recall(name: str) -> float | None:
        if name in judged.get("recall", {}):
            value: float | None = judged["recall"][name]
            return value
        block = row.get(name)
        return None if block is None else block.get("recall")

    return Cell(
        input_id=row["input"],
        arm=row["arm"],
        run=int(row["run"]),
        status=row["status"],
        void_reason=row.get("void_reason"),
        metrics={name: recall(name) for name in RECALL_METRICS},
        fail_b=bool(judged.get("b_fail", row["b"]["fail"])),
        fail_c=bool(judged.get("c_fail", row["c"]["fail"])),
        disclosed=bool(judged.get("disclosed", row["d"]["disclosed"])),
        absence_claim=bool(judged.get("absence_claim", row["d"]["absence_claim"])),
    )


def gate_matrix(runs: int = 5) -> dict[tuple[str, str], int]:
    """The plan 4.5 run matrix, as ``{(input, arm): runs}``. ``I4p`` is diagnostic, N = 3."""
    matrix: dict[tuple[str, str], int] = {}
    for input_id in ("I1", "I2", "I3", "I4"):
        for arm in ("old", "new", "old-aa"):
            matrix[(input_id, arm)] = runs
    for input_id in ("I1", "I2"):
        matrix[(input_id, "sabotage")] = min(3, runs)
    for arm in ("old", "new"):
        matrix[("I4p", arm)] = min(3, runs)
    return matrix


def _ci_stats(deltas: Sequence[float]) -> dict[str, Any]:
    ci = paired_bootstrap_ci(deltas)
    out: dict[str, Any] = {
        "n": ci.n,
        "delta_mean": ci.delta_mean,
        "ci_low": ci.ci_low,
        "ci_high": ci.ci_high,
        "n_resamples": ci.n_resamples,
    }
    if ci.n >= 2:
        t = paired_ttest(deltas)
        out["p_value"] = t.p_value
        out["p_engine"] = t.engine
    return out


def _deltas(
    cells: Mapping[tuple[str, str, int], Cell],
    arm: str,
    base: str,
    metric: str,
    inputs: Iterable[str],
) -> dict[str, list[float]]:
    """``{input: [metric(arm) - metric(base), ...]}`` over cells present on both sides.

    The pairing unit is ``"{input}#{run}"``; ``paired_join`` raises on a partial join or a
    duplicate id, so a hole in the data is a defect, never a smaller sample.
    """
    wanted = set(inputs)
    arm_values: dict[str, float | None] = {}
    base_values: dict[str, float | None] = {}
    for (input_id, cell_arm, run), cell in cells.items():
        if cell_arm != arm or input_id not in wanted:
            continue
        qid = f"{input_id}#{run}"
        arm_values[qid] = cell.metrics.get(metric)
        counterpart = cells.get((input_id, base, run))
        if counterpart is not None:
            base_values[qid] = counterpart.metrics.get(metric)

    def rows(values: Mapping[str, float | None]) -> list[dict[str, Any]]:
        # A metric that is None on BOTH sides (ICSI has no actions layer) is simply not
        # measured; None on one side only, or a base cell that does not exist, leaves a hole
        # that paired_join turns into PartialJoinError instead of a smaller sample.
        return [
            {"query_id": qid, "corpus": "summary", "query_class": "all", "scores": {metric: v}}
            for qid, v in sorted(values.items())
            if v is not None
        ]

    joined = paired_join(rows(base_values), rows(arm_values))
    by_input: dict[str, list[float]] = {}
    for pair in joined:
        input_id = pair.query_id.split("#", 1)[0]
        by_input.setdefault(input_id, []).append(pair.scores_b[metric] - pair.scores_a[metric])
    return by_input


def recall_comparison(
    cells: Mapping[tuple[str, str, int], Cell],
    arm: str,
    base: str,
    inputs: Iterable[str] = A_INPUTS,
) -> dict[str, dict[str, Any]]:
    """Per metric: the CI over all pairs, and per input. Metrics with no pairs are omitted."""
    out: dict[str, dict[str, Any]] = {}
    for metric in RECALL_METRICS:
        by_input = _deltas(cells, arm, base, metric, inputs)
        everything = [d for deltas in by_input.values() for d in deltas]
        if not everything:
            continue
        out[metric] = {
            "all": _ci_stats(everything),
            "per_input": {i: _ci_stats(d) for i, d in sorted(by_input.items())},
        }
    return out


def _count(
    cells: Mapping[tuple[str, str, int], Cell], arm: str, inputs: Iterable[str], attr: str
) -> dict[str, int]:
    wanted = set(inputs)
    counts: Counter[str] = Counter()
    for (input_id, cell_arm, _), cell in cells.items():
        if cell_arm == arm and input_id in wanted:
            counts[input_id] += int(bool(getattr(cell, attr)))
    return {i: counts[i] for i in sorted(wanted) if any(k[0] == i and k[1] == arm for k in cells)}


def regression_rules(
    cells: Mapping[tuple[str, str, int], Cell],
    arm: str,
    base: str,
    *,
    margin: float,
    absolute_c: bool,
) -> dict[str, Any]:
    """Apply the four REGRESSION rules (plan 5.6) to ``arm`` against ``base``.

    Args:
        cells: every scored cell keyed by ``(input, arm, run)``.
        arm: the arm being judged.
        base: the control it is compared with. Only the (input, run) cells ``arm`` has are used,
            so a control that ran a smaller matrix (``sabotage``: I1, I2 only) is compared fairly.
        margin: the non-inferiority margin ``m`` already widened to the A/A noise.
        absolute_c: when true, ANY failing (c) run of ``arm`` is a regression (the rule for
            ``new``, and for the negative control). The A/A control passes false: a (c) failure
            there is judged relative to ``base``, because the question is whether sampling noise
            alone moves the arms apart.

    Returns:
        ``{"hits": [...], "recall": {...}, "counts": {...}, "inconclusive": [...]}``. ``hits``
        names each rule that fired; ``inconclusive`` the (a) metrics whose CI lower bound is
        below ``-margin`` without the regression condition.
    """
    hits: list[dict[str, Any]] = []
    inconclusive: list[str] = []
    recall = recall_comparison(cells, arm, base)
    for metric, stats in recall.items():
        ci = stats["all"]
        if ci["ci_high"] < 0 and ci["ci_low"] < -margin:
            hits.append(
                {"rule": "a", "metric": metric, "ci_low": ci["ci_low"], "ci_high": ci["ci_high"]}
            )
        elif ci["ci_low"] < -margin:
            inconclusive.append(metric)

    counts: dict[str, Any] = {}
    for rule, inputs, attr in (("b", B_INPUTS, "fail_b"), ("c", C_INPUTS, "fail_c")):
        mine = _count(cells, arm, inputs, attr)
        theirs = _count(cells, base, inputs, attr)
        counts[f"fail_{rule}"] = {"arm": mine, "base": theirs}
        for input_id, n in mine.items():
            over_base = n > theirs.get(input_id, 0)
            if (rule == "c" and absolute_c and n > 0) or over_base:
                hits.append(
                    {
                        "rule": rule,
                        "input": input_id,
                        "arm_fails": n,
                        "base_fails": theirs.get(input_id, 0),
                    }
                )

    disclosed_arm = _count(cells, arm, D_INPUTS, "disclosed")
    disclosed_base = _count(cells, base, D_INPUTS, "disclosed")
    absence_arm = _count(cells, arm, D_INPUTS, "absence_claim")
    absence_base = _count(cells, base, D_INPUTS, "absence_claim")
    counts["disclosed"] = {"arm": disclosed_arm, "base": disclosed_base}
    counts["absence_claim"] = {"arm": absence_arm, "base": absence_base}
    for input_id in disclosed_arm:
        if disclosed_arm[input_id] < disclosed_base.get(input_id, 0):
            hits.append({"rule": "d", "input": input_id, "kind": "discloses_less"})
        if absence_arm.get(input_id, 0) > absence_base.get(input_id, 0):
            hits.append({"rule": "d", "input": input_id, "kind": "absence_claim_more"})
    return {"hits": hits, "recall": recall, "counts": counts, "inconclusive": inconclusive}


def _aa_half_width(cells: Mapping[tuple[str, str, int], Cell], aa: str, base: str) -> float:
    """Largest CI half-width of the A/A comparison over the recall metrics (0 if no pairs)."""
    widths = [
        (s["all"]["ci_high"] - s["all"]["ci_low"]) / 2
        for s in recall_comparison(cells, aa, base).values()
    ]
    return max(widths, default=0.0)


def _index_cells(
    rows: Sequence[Mapping[str, Any]], matrix: Mapping[tuple[str, str], int], *, use_judge: bool
) -> tuple[dict[tuple[str, str, int], Cell], list[dict[str, Any]]]:
    """Key the rows and list every structural reason the run cannot be read (VOID causes)."""
    keyed: dict[tuple[str, str, int], Cell] = {}
    void: list[dict[str, Any]] = []
    for row in rows:
        cell = cell_from_row(row, use_judge=use_judge)
        if cell.key in keyed:
            void.append({"cause": "duplicate_cell", "cell": list(cell.key)})
        keyed[cell.key] = cell
    for (input_id, arm), runs in sorted(matrix.items()):
        void.extend(
            {"cause": "missing_cell", "cell": [input_id, arm, run]}
            for run in range(runs)
            if (input_id, arm, run) not in keyed
        )
    for cell in keyed.values():
        if cell.status == "void":
            void.append(
                {
                    "cause": "applied_check_failed",
                    "cell": list(cell.key),
                    "reason": cell.void_reason,
                }
            )
    extras = sorted(k for k in keyed if (k[0], k[1]) not in matrix)
    if extras:
        void.append(
            {
                "cause": "unexpected_cell",
                "cells": [list(k) for k in extras[:5]],
                "count": len(extras),
            }
        )
    return keyed, void


def _check_controls(
    keyed: Mapping[tuple[str, str, int], Cell],
    arms: Iterable[str],
    *,
    old: str,
    aa: str,
    sabotage: str,
    margin: float,
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """Run the negative (sabotage) and A/A controls; either failing makes the run VOID."""
    present = set(arms)
    controls: dict[str, Any] = {}
    void: list[dict[str, Any]] = []
    if sabotage in present:
        controls["sabotage"] = regression_rules(
            keyed, sabotage, old, margin=margin, absolute_c=True
        )
        if not controls["sabotage"]["hits"]:
            void.append({"cause": "sabotage_not_flagged"})
    if aa in present:
        controls["aa"] = regression_rules(keyed, aa, old, margin=margin, absolute_c=False)
        if controls["aa"]["hits"]:
            void.append({"cause": "aa_flagged", "hits": controls["aa"]["hits"]})
    return controls, void


def decide(
    rows: Sequence[Mapping[str, Any]],
    *,
    matrix: Mapping[tuple[str, str], int],
    new: str = "new",
    old: str = "old",
    aa: str = "old-aa",
    sabotage: str = "sabotage",
    margin: float = DEFAULT_MARGIN,
    use_judge: bool = False,
    require_controls: bool = True,
) -> dict[str, Any]:
    """The verdict for a set of scored rows against the expected run ``matrix``.

    ``require_controls`` makes a run without the negative (sabotage) and A/A arms ``VOID`` with
    cause ``controls_missing``: a ``PASS`` that was never shown able to fail is not a gate result.
    Only a smoke check passes ``False``.

    Returns a JSON-serialisable document with the verdict, the reasons, the full statistics and
    every threshold used. It contains no corpus prose.
    """
    params = {
        "margin": margin,
        "a_inputs": list(A_INPUTS),
        "b_inputs": list(B_INPUTS),
        "c_inputs": list(C_INPUTS),
        "d_inputs": list(D_INPUTS),
        "confidence": 0.95,
        "bootstrap_resamples": 10_000,
        "bootstrap_seed": 0,
        "arms": {"new": new, "old": old, "aa": aa, "sabotage": sabotage},
        "matrix": {f"{i}|{a}": n for (i, a), n in sorted(matrix.items())},
        "use_judge": use_judge,
        "require_controls": require_controls,
    }
    keyed, void = _index_cells(rows, matrix, use_judge=use_judge)
    if require_controls:
        present = {a for _, a in matrix}
        void += [
            {"cause": "controls_missing", "arm": a} for a in (sabotage, aa) if a not in present
        ]
    doc: dict[str, Any] = {"verdict": VOID, "params": params, "void": void}
    if void:
        doc["reasons"] = sorted({v["cause"] for v in void})
        return doc

    arms = {a for _, a in matrix}
    aa_width = _aa_half_width(keyed, aa, old) if aa in arms else 0.0
    effective = max(margin, aa_width)
    doc["noise_floor"] = {"aa_half_width": aa_width, "effective_margin": effective}
    controls, void = _check_controls(
        keyed, arms, old=old, aa=aa, sabotage=sabotage, margin=effective
    )
    doc["controls"] = {k: {"hits": v["hits"], "counts": v["counts"]} for k, v in controls.items()}
    if void:
        doc["void"] = void
        doc["reasons"] = sorted({v["cause"] for v in void})
        return doc

    result = regression_rules(keyed, new, old, margin=effective, absolute_c=True)
    doc["comparison"] = {"recall": result["recall"], "counts": result["counts"]}
    doc["hits"] = result["hits"]
    doc["improved"] = sorted(m for m, s in result["recall"].items() if s["all"]["ci_low"] > 0)
    if result["hits"]:
        doc["verdict"] = REGRESSION
        doc["reasons"] = sorted({f"rule_{h['rule']}" for h in result["hits"]})
    elif result["inconclusive"]:
        doc["verdict"] = INCONCLUSIVE
        doc["reasons"] = [f"ci_lower_below_margin:{m}" for m in result["inconclusive"]]
    else:
        doc["verdict"] = PASS
        doc["reasons"] = []
    return doc
